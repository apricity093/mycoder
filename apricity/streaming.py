"""流式响应解析与增量脱敏。

本模块在 provider adapter 与 AgentLoop 之间建立两级事件模型：

- provider adapter 只负责传输层：按序产出 ``content_delta``（原始模型文本）、
  ``response_completed`` 和 ``response_failed``，不判断协议标记。
- ``StreamingResponseParser`` 消费原始文本增量，输出两类高层事件：
  ``visible_delta``（允许展示的 final 文本）和 ``tool_candidate_ready``（完整
  工具 envelope）。可见文本由 ``IncrementalRedactor`` 做跨 chunk 脱敏后再交给
  CLI sink。

解析采用 PRD v1.1 的“首个协议标记 commit”规则，与 ``Apricity.parse`` 同步
维护，保证流式与非流式语义一致。
"""

from dataclasses import dataclass, field
import re

from .security import REDACTED_VALUE

CHANNEL_PROTOCOL = "protocol"
CHANNEL_FINAL = "final"

EVENT_RESPONSE_STARTED = "response_started"
EVENT_CONTENT_DELTA = "content_delta"
EVENT_RESPONSE_COMPLETED = "response_completed"
EVENT_RESPONSE_FAILED = "response_failed"
EVENT_VISIBLE_DELTA = "visible_delta"
EVENT_TOOL_CANDIDATE_READY = "tool_candidate_ready"

PARSER_STATUS_FINAL_COMPLETE = "final_complete"
PARSER_STATUS_FALLBACK_COMPLETE = "fallback_complete"
PARSER_STATUS_TOOL_COMPLETE = "tool_complete"
PARSER_STATUS_EMPTY = "empty"
PARSER_STATUS_UNCLOSED_FINAL = "unclosed_final"
PARSER_STATUS_UNCLOSED_TOOL = "unclosed_tool"
PARSER_STATUS_TOOL_TOO_LARGE = "tool_too_large"

FINAL_OPEN = "<final>"
FINAL_CLOSE = "</final>"
TOOL_CLOSE = "</tool>"
DSML_MARKER_RE = re.compile(r"<\uFF5C+DSML")
DSML_INVOKE_CLOSE_RE = re.compile(r"</\uFF5C+DSML\uFF5C+invoke\b")
TOOL_OPEN_RE = re.compile(r"<tool(?=[\s>])")

DEFAULT_MAX_TOOL_BUFFER_BYTES = 1024 * 1024


class StreamInterruptedError(RuntimeError):
    """已展示 final 文本后发生流式中断，禁止自动重试。"""

    def __init__(self, message, *, error_type="stream_interrupted", partial_visible=True, visible_chars=0):
        super().__init__(message)
        self.error_type = error_type
        self.partial_visible = bool(partial_visible)
        self.visible_chars = int(visible_chars)


class StreamCancelledError(RuntimeError):
    """用户 Ctrl-C 或输出管道关闭导致的取消。"""

    def __init__(self, message="", *, partial_visible=False, visible_chars=0):
        super().__init__(message)
        self.partial_visible = bool(partial_visible)
        self.visible_chars = int(visible_chars)


@dataclass
class IncrementalRedactor:
    """对可见 final 增量做跨 chunk 脱敏。

    流式文本无法直接调用基于完整字符串的 ``security.redact_text``：一个 secret
    可能被拆到两个 chunk 里。这里不提前输出“可能是某个 secret 前缀”的尾部字符：
    只有确认该后缀不可能继续组成任何 secret 时才 flush 安全前缀；一旦凑齐完整
    secret，立即替换为 ``REDACTED_VALUE``。因此不会把 secret 的任一字符原样送出，
    也不会受 chunk 边界影响。
    """

    secret_values: tuple = field(default_factory=tuple)
    pending: str = field(default="", init=False)

    @classmethod
    def from_env(cls, secret_env_names=()):
        from .security import detected_secret_env_items

        values = tuple(
            value
            for _, value in detected_secret_env_items(secret_env_names=secret_env_names)
            if value
        )
        return cls(secret_values=values)

    def _drain(self, final):
        output = []
        while self.pending:
            match = None
            for value in self.secret_values:
                if not value:
                    continue
                start = self.pending.find(value)
                if start >= 0 and (match is None or (start, -len(value)) < (match[0], -len(match[1]))):
                    match = (start, value)

            if match is not None:
                start, value = match
                # start 之前是最早出现的完整 secret 的左侧前缀，不再可能与
                # 后续文本（从 REDACTED_VALUE 开始）拼成 secret，可安全输出。
                output.append(self.pending[:start])
                output.append(REDACTED_VALUE)
                self.pending = self.pending[start + len(value):]
                continue

            best_prefix = 0
            for value in self.secret_values:
                if not value:
                    continue
                limit = min(len(value), len(self.pending))
                for length in range(limit, 0, -1):
                    if self.pending.endswith(value[:length]):
                        best_prefix = max(best_prefix, length)
                        break

            if best_prefix == 0:
                output.append(self.pending)
                self.pending = ""
                break

            safe_len = len(self.pending) - best_prefix
            if safe_len:
                output.append(self.pending[:safe_len])
                self.pending = self.pending[safe_len:]
                continue
            break

        if final and self.pending:
            output.append(self.pending)
            self.pending = ""
        return "".join(output)

    def feed(self, text):
        self.pending += str(text)
        return self._drain(final=False)

    def flush(self):
        return self._drain(final=True)


class StreamingResponseParser:
    """增量解析模型文本中的 tool/final 协议。

    输入是 decoded 文本 chunk；输出是 dict 事件。``finish()`` 必须在底层
    response 结束后调用一次，以产出 fallback 文本或给出终态。
    """

    def __init__(self, max_tool_buffer_bytes=DEFAULT_MAX_TOOL_BUFFER_BYTES):
        self.max_tool_buffer_bytes = int(max_tool_buffer_bytes)
        self._chunks = []
        self._state = "waiting"
        self._status = ""
        self._visible_emitted = False
        self._final_closed = False
        self._final_tail = ""
        self._tool_ready = False
        self._tool_kind = ""

    @property
    def status(self):
        return self._status

    @property
    def visible_emitted(self):
        return self._visible_emitted

    @property
    def tool_ready(self):
        return self._tool_ready

    def full_raw(self):
        return "".join(self._chunks)

    def feed(self, text):
        text = str(text)
        if not text or self._state in {"done", "too_large"}:
            return []
        self._chunks.append(text)
        if self._state == "waiting":
            return self._commit_or_wait()
        if self._state == "final":
            self._final_tail += text
            return self._stream_final()
        if self._state == "tool":
            return self._scan_tool_close()
        return []

    def finish(self):
        events = []
        if self._state == "waiting":
            raw = self.full_raw()
            if not raw.strip():
                self._status = PARSER_STATUS_EMPTY
            else:
                self._status = PARSER_STATUS_FALLBACK_COMPLETE
                self._emit_visible(events, raw)
        elif self._state == "final":
            if self._final_closed:
                self._status = PARSER_STATUS_FINAL_COMPLETE
            else:
                self._status = PARSER_STATUS_UNCLOSED_FINAL
                self._emit_visible(events, self._final_tail)
                self._final_tail = ""
        elif self._state == "tool":
            if self._tool_ready:
                self._status = PARSER_STATUS_TOOL_COMPLETE
            else:
                self._status = PARSER_STATUS_UNCLOSED_TOOL
        elif self._state == "too_large":
            self._status = PARSER_STATUS_TOOL_TOO_LARGE
        return events

    def _emit_visible(self, events, text):
        if not text:
            return
        events.append({"type": EVENT_VISIBLE_DELTA, "text": text, "channel": CHANNEL_FINAL})
        self._visible_emitted = True

    def _commit_or_wait(self):
        raw = self.full_raw()
        candidates = []
        final_pos = raw.find(FINAL_OPEN)
        if final_pos >= 0:
            candidates.append((final_pos, "final"))

        tool_match = TOOL_OPEN_RE.search(raw)
        if tool_match:
            candidates.append((tool_match.start(), "tool"))

        dsml_match = DSML_MARKER_RE.search(raw)
        if dsml_match:
            candidates.append((dsml_match.start(), "dsml"))

        if not candidates:
            return []

        kind = min(candidates, key=lambda item: item[0])[1]
        if kind == "final":
            start = raw.find(FINAL_OPEN) + len(FINAL_OPEN)
            self._state = "final"
            self._final_tail = raw[start:]
            return self._stream_final()
        if kind == "tool":
            self._state = "tool"
            self._tool_kind = "tool"
            return self._scan_tool_close()
        self._state = "tool"
        self._tool_kind = "dsml"
        return self._scan_tool_close()

    def _stream_final(self):
        events = []
        tail = self._final_tail
        close_pos = tail.find(FINAL_CLOSE)
        if close_pos >= 0:
            self._emit_visible(events, tail[:close_pos])
            self._final_tail = ""
            self._final_closed = True
            self._status = PARSER_STATUS_FINAL_COMPLETE
            self._state = "done"
            return events

        safe_len = max(0, len(tail) - len(FINAL_CLOSE))
        if safe_len:
            safe, self._final_tail = tail[:safe_len], tail[safe_len:]
            self._emit_visible(events, safe)
        return events

    def _scan_tool_close(self):
        events = []
        raw = self.full_raw()
        if len(raw.encode("utf-8", errors="replace")) > self.max_tool_buffer_bytes:
            self._state = "too_large"
            self._status = PARSER_STATUS_TOOL_TOO_LARGE
            return events

        if self._tool_kind == "dsml":
            closed = DSML_INVOKE_CLOSE_RE.search(raw)
        else:
            closed = raw.find(TOOL_CLOSE) >= 0

        if not closed:
            return events

        self._tool_ready = True
        self._status = PARSER_STATUS_TOOL_COMPLETE
        self._state = "done"
        events.append({"type": EVENT_TOOL_CANDIDATE_READY, "raw_text": raw})
        return events
