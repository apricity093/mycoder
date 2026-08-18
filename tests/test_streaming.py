"""流式输出功能测试：parser、增量脱敏、AgentLoop 与 provider adapter。"""

import io
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from apricity import cli as cli_module
from apricity.cli import build_arg_parser
from apricity.providers.clients import (
    AnthropicCompatibleModelClient,
    DeepSeekModelClient,
    OllamaModelClient,
    OpenAICompatibleModelClient,
)
from apricity.runtime import Apricity, SessionStore
from apricity.run_store import RunStore
from apricity.streaming import (
    IncrementalRedactor,
    PARSER_STATUS_FALLBACK_COMPLETE,
    PARSER_STATUS_FINAL_COMPLETE,
    PARSER_STATUS_TOOL_COMPLETE,
    PARSER_STATUS_TOOL_TOO_LARGE,
    PARSER_STATUS_UNCLOSED_FINAL,
    StreamingResponseParser,
)
from apricity.task_state import (
    STOP_REASON_STREAM_CANCELLED,
    STOP_REASON_STREAM_INTERRUPTED,
)
from apricity.workspace import WorkspaceContext


class FakeStreamModelClient:
    """按预设 generator 依次产出 provider 流事件的测试 client。"""

    supports_prompt_cache = False
    prompt_cache_mode = "none"

    def __init__(self, turns):
        self.turns = list(turns)
        self.calls = 0
        self.last_completion_metadata = {}

    def stream_complete(self, prompt, max_new_tokens, **kwargs):
        self.calls += 1
        if not self.turns:
            raise RuntimeError("fake stream client ran out of turns")
        turn = self.turns.pop(0)
        if isinstance(turn, Exception):
            raise turn
        yield from turn


def _complete_metadata(provider="fake"):
    return {"provider": provider, "model": "fake-model", "finish_reason": "stop"}


def _final_turn(text="<final>OK</final>"):
    return [
        {"type": "content_delta", "text": text},
        {"type": "response_completed", "metadata": _complete_metadata(), "finish_reason": "stop"},
    ]


def _tool_turn():
    return [
        {"type": "content_delta", "text": '<tool>{"name":"list_files","args":{"path":"."}}'},
        {"type": "content_delta", "text": "</tool>"},
        {"type": "response_completed", "metadata": _complete_metadata(), "finish_reason": "stop"},
    ]


class FakeCLIAgent:
    def __init__(self):
        self.last_ask = None
        self.model_client = type("Model", (), {"model": "fake", "base_url": "http://fake"})()
        self.workspace = type("Workspace", (), {"cwd": str(Path(".")), "branch": "main"})()
        self.approval_policy = "never"
        self.session = {"id": "fake-session"}
        self.session_path = "fake-session.json"

    def memory_text(self):
        return "memory"

    def ask(self, message, event_sink=None, stream=None):
        self.last_ask = {"message": message, "event_sink": event_sink, "stream": stream}
        return "OK"


class TTYStringIO(io.StringIO):
    def isatty(self):
        return True


def run_cli_main(monkeypatch, argv, stdout):
    agent = FakeCLIAgent()
    monkeypatch.setattr(sys, "stdout", stdout)
    monkeypatch.setattr(cli_module, "build_agent", lambda args: agent)
    cli_module.main(argv)
    return agent


def build_stream_agent(tmp_path, client):
    workspace = WorkspaceContext.build(tmp_path)
    agent = Apricity(
        client,
        workspace,
        SessionStore(tmp_path / ".apricity" / "sessions"),
        run_store=RunStore(tmp_path / ".apricity" / "runs"),
        approval_policy="auto",
        max_steps=2,
    )
    return agent


def test_parser_streams_final_without_tail_marker_and_handles_cross_chunk_marker():
    parser = StreamingResponseParser()

    assert parser.feed("<fin") == []
    assert parser.feed("al>Hello ") == []
    events = parser.feed("world</final>ignored")

    assert [event["text"] for event in events] == ["Hello world"]
    assert parser.status == PARSER_STATUS_FINAL_COMPLETE
    assert parser.finish() == []


def test_parser_handles_unicode_content_split_across_chunks():
    parser = StreamingResponseParser()

    events = parser.feed("<final>\u4f60")
    events.extend(parser.feed("\u597d</final>"))
    events.extend(parser.finish())

    assert parser.status == PARSER_STATUS_FINAL_COMPLETE
    assert "".join(event["text"] for event in events) == "\u4f60\u597d"


def test_parser_buffers_json_tool_until_closed():
    parser = StreamingResponseParser()

    assert parser.feed('<tool>{"name":"list_files","args":{"path":"."}}') == []
    events = parser.feed("</tool>")

    assert events[0]["type"] == "tool_candidate_ready"
    assert parser.status == PARSER_STATUS_TOOL_COMPLETE


def test_parser_fallback_is_only_emitted_at_finish():
    parser = StreamingResponseParser()

    assert parser.feed("plain answer") == []
    events = parser.finish()

    assert [event["text"] for event in events] == ["plain answer"]
    assert parser.status == PARSER_STATUS_FALLBACK_COMPLETE


def test_parser_marks_unclosed_final_and_flushes_held_tail():
    parser = StreamingResponseParser()

    events = parser.feed("<final>partial answer")
    events.extend(parser.finish())

    assert parser.status == PARSER_STATUS_UNCLOSED_FINAL
    assert "partial answer" in "".join(event["text"] for event in events)


def test_parser_enforces_tool_buffer_limit():
    parser = StreamingResponseParser(max_tool_buffer_bytes=16)

    parser.feed("<tool>" + "x" * 30)
    parser.finish()

    assert parser.status == PARSER_STATUS_TOOL_TOO_LARGE


def test_parser_uses_first_marker_commit_for_final_then_dsml():
    parser = StreamingResponseParser()

    events = parser.feed("<final>done</final>")

    assert parser.status == PARSER_STATUS_FINAL_COMPLETE
    assert [event["text"] for event in events] == ["done"]


def test_incremental_redactor_catches_secret_split_across_chunks():
    redactor = IncrementalRedactor(secret_values=("sk-super-secret",))

    first = redactor.feed("prefix sk-super-")
    second = redactor.feed("secret suffix")
    tail = redactor.flush()

    assert "sk-super-secret" not in first + second + tail
    assert "<redacted>" in second + tail or "<redacted>" in first + tail


def test_parse_uses_first_marker_commit():
    dsml = (
        '<\uff5cDSML\uff5cinvoke name="ReadFile">'
        '<\uff5cDSML\uff5cparameter name="path">README.md</\uff5cDSML\uff5cparameter>'
        '</\uff5cDSML\uff5cinvoke>'
    )

    assert Apricity.parse("<final>done</final>" + dsml) == ("final", "done")
    assert Apricity.parse(dsml + "<final>late</final>") == (
        "tool",
        {"name": "read_file", "args": {"path": "README.md"}},
    )


def test_parse_rejects_unclosed_final():
    kind, payload = Apricity.parse("<final>not closed")

    assert kind == "retry"
    assert "unclosed" in payload


def test_agent_streams_final_and_returns_same_answer(tmp_path):
    client = FakeStreamModelClient([_final_turn("<final>Hello stream</final>")])
    agent = build_stream_agent(tmp_path, client)
    seen = []

    answer = agent.ask("hi", event_sink=seen.append, stream=True)

    assert answer == "Hello stream"
    assert seen == [{"type": "visible_delta", "text": "Hello stream"}]
    assert agent.current_task_state.partial_visible is False
    assert agent.current_task_state.visible_chars == len("Hello stream")


def test_agent_buffers_tool_and_does_not_leak_protocol(tmp_path):
    client = FakeStreamModelClient([_tool_turn(), _final_turn("<final>listed</final>")])
    agent = build_stream_agent(tmp_path, client)
    seen = []

    answer = agent.ask("list", event_sink=seen.append, stream=True)

    assert answer == "listed"
    assert agent.current_task_state.tool_steps == 1
    assert all("<tool" not in event["text"] for event in seen)
    assert seen == [{"type": "visible_delta", "text": "listed"}]


def test_agent_does_not_retry_after_visible_text(tmp_path):
    def failing_turn():
        yield {"type": "content_delta", "text": "<final>partial answer long enough"}
        raise RuntimeError("connection lost")

    client = FakeStreamModelClient([failing_turn()])
    agent = build_stream_agent(tmp_path, client)
    seen = []

    answer = agent.ask("hi", event_sink=seen.append, stream=True)

    assert client.calls == 1
    assert agent.current_task_state.stop_reason == STOP_REASON_STREAM_INTERRUPTED
    assert agent.current_task_state.partial_visible is True
    assert "Stopped" in answer
    assert seen


def test_agent_cancels_when_sink_raises_broken_pipe_after_visible_text(tmp_path):
    client = FakeStreamModelClient([_final_turn("<final>hello pipe</final>")])
    agent = build_stream_agent(tmp_path, client)

    def broken_sink(event):
        raise BrokenPipeError("pipe closed")

    answer = agent.ask("hi", event_sink=broken_sink, stream=True)

    assert agent.current_task_state.stop_reason == STOP_REASON_STREAM_CANCELLED
    assert agent.current_task_state.partial_visible is True
    assert "Stopped" in answer


def test_agent_cancels_on_keyboard_interrupt_after_visible_text(tmp_path):
    def interrupt_turn():
        yield {"type": "content_delta", "text": "<final>partial answer long enough"}
        raise KeyboardInterrupt

    client = FakeStreamModelClient([interrupt_turn()])
    agent = build_stream_agent(tmp_path, client)
    seen = []

    answer = agent.ask("hi", event_sink=seen.append, stream=True)

    assert agent.current_task_state.stop_reason == STOP_REASON_STREAM_CANCELLED
    assert agent.current_task_state.partial_visible is True
    assert agent.current_task_state.visible_chars > 0
    assert "Stopped" in answer


def test_agent_treats_missing_response_completed_as_interruption_after_visible(tmp_path):
    def incomplete_turn():
        yield {"type": "content_delta", "text": "<final>partial answer long enough"}
        # 故意不产出 response_completed

    client = FakeStreamModelClient([incomplete_turn()])
    agent = build_stream_agent(tmp_path, client)
    seen = []

    answer = agent.ask("hi", event_sink=seen.append, stream=True)

    assert client.calls == 1
    assert agent.current_task_state.stop_reason == STOP_REASON_STREAM_INTERRUPTED
    assert agent.current_task_state.partial_visible is True
    assert "Stopped" in answer


def test_agent_retries_when_stream_ends_before_any_content(tmp_path):
    client = FakeStreamModelClient([[], _final_turn("<final>recovered</final>")])
    agent = build_stream_agent(tmp_path, client)
    seen = []

    answer = agent.ask("hi", event_sink=seen.append, stream=True)

    assert client.calls == 2
    assert answer == "recovered"
    assert seen == [{"type": "visible_delta", "text": "recovered"}]


def test_agent_ignores_events_after_response_completed(tmp_path):
    def trailing_turn():
        yield {"type": "content_delta", "text": "<final>OK</final>"}
        yield {"type": "response_completed", "metadata": _complete_metadata(), "finish_reason": "stop"}
        yield {"type": "content_delta", "text": "<final>SHOULD NOT SHOW</final>"}
        yield {"type": "response_completed", "metadata": _complete_metadata(), "finish_reason": "stop"}

    client = FakeStreamModelClient([trailing_turn()])
    agent = build_stream_agent(tmp_path, client)
    seen = []

    answer = agent.ask("hi", event_sink=seen.append, stream=True)

    assert answer == "OK"
    assert seen == [{"type": "visible_delta", "text": "OK"}]


def test_agent_retries_transient_failure_before_visible_text(tmp_path):
    def first_turn():
        yield {
            "type": "response_failed",
            "message": "provider unavailable",
            "retryable": True,
            "status_code": 503,
        }

    client = FakeStreamModelClient([first_turn(), _final_turn("<final>recovered</final>")])
    agent = build_stream_agent(tmp_path, client)
    seen = []

    answer = agent.ask("hi", event_sink=seen.append, stream=True)

    assert client.calls == 2
    assert answer == "recovered"
    assert agent.current_task_state.recovery_actions == 1


def test_old_fake_client_fallback_wraps_complete_as_single_delta(tmp_path):
    from apricity.providers.clients import FakeModelClient

    client = FakeModelClient(["<final>legacy</final>"])
    agent = build_stream_agent(tmp_path, client)
    seen = []

    answer = agent.ask("hi", event_sink=seen.append, stream=True)

    assert answer == "legacy"
    assert seen == [{"type": "visible_delta", "text": "legacy"}]


class _SSEResponse:
    headers = {"Content-Type": "text/event-stream"}

    def __init__(self, lines):
        self.lines = iter(lines)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def __iter__(self):
        return self

    def __next__(self):
        return next(self.lines)

    def readline(self):
        try:
            return next(self.lines)
        except StopIteration:
            return b""


class _NDJSONResponse(_SSEResponse):
    headers = {"Content-Type": "application/x-ndjson"}


class _JSONResponse(_SSEResponse):
    headers = {"Content-Type": "application/json"}


class _ChunkedResponse:
    """模拟 HTTP body 按任意字节分块到达，而不是按行到达。"""

    headers = {"Content-Type": "text/event-stream"}

    def __init__(self, chunks):
        self.chunks = iter(chunks)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def __iter__(self):
        return self

    def __next__(self):
        return next(self.chunks)


def test_deepseek_stream_complete_sends_stream_options_and_extracts_deltas():
    response = _SSEResponse(
        [
            b'data: {"choices":[{"delta":{"reasoning_content":"think","content":""}}]}\n',
            b"\n",
            b'data: {"choices":[{"delta":{"content":"<final>"}}]}\n',
            b"\n",
            (
                b'data: {"choices":[{"delta":{"content":"OK"},"finish_reason":"stop"}],'
                b'"usage":{"prompt_tokens":100,"completion_tokens":10,'
                b'"prompt_cache_hit_tokens":90,"prompt_cache_miss_tokens":10}}\n'
            ),
            b"\n",
            b"data: [DONE]\n",
        ]
    )
    client = DeepSeekModelClient(
        model="deepseek-v4-flash",
        base_url="https://api.deepseek.com",
        api_key="sk-test",
        temperature=0.0,
        timeout=30,
    )

    with patch("urllib.request.urlopen", return_value=response):
        events = list(client.stream_complete("prompt", 512, request_id="r1"))

    texts = [event["text"] for event in events if event["type"] == "content_delta"]
    assert texts == ["<final>", "OK"]
    assert client.last_completion_metadata["cache_hit"] is True
    assert client.last_completion_metadata["cached_tokens"] == 90
    assert client.last_completion_metadata["finish_reason"] == "stop"


def test_ollama_stream_complete_extracts_ndjson_and_usage():
    response = _NDJSONResponse(
        [
            b'{"response":"<final>","done":false}\n',
            b'{"response":"OK</final>","done":true,"done_reason":"stop",'
            b'"prompt_eval_count":100,"eval_count":10}\n',
        ]
    )
    client = OllamaModelClient(
        model="qwen3.5:4b",
        base_url="http://127.0.0.1:11434",
        temperature=0.0,
        top_p=0.9,
        timeout=30,
    )

    with patch("urllib.request.urlopen", return_value=response):
        events = list(client.stream_complete("prompt", 32, request_id="r1"))

    texts = [event["text"] for event in events if event["type"] == "content_delta"]
    assert texts == ["<final>", "OK</final>"]
    assert client.last_completion_metadata["input_tokens"] == 100
    assert client.last_completion_metadata["output_tokens"] == 10


def test_anthropic_stream_complete_extracts_text_delta_and_usage():
    response = _SSEResponse(
        [
            b'data: {"type":"message_start","message":{"usage":{"input_tokens":100}}}\n',
            b"\n",
            b'data: {"type":"content_block_delta","delta":{"type":"text_delta","text":"<final>OK</final>"}}\n',
            b"\n",
            b'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"},"usage":{"output_tokens":10}}\n',
            b"\n",
            b"data: [DONE]\n",
        ]
    )
    client = AnthropicCompatibleModelClient(
        model="claude-test",
        base_url="https://api.anthropic.com/v1",
        api_key="sk-test",
        temperature=0.0,
        timeout=30,
    )

    with patch("urllib.request.urlopen", return_value=response):
        events = list(client.stream_complete("prompt", 32, request_id="r1"))

    texts = [event["text"] for event in events if event["type"] == "content_delta"]
    assert texts == ["<final>OK</final>"]
    assert client.last_completion_metadata["input_tokens"] == 100
    assert client.last_completion_metadata["output_tokens"] == 10
    assert client.last_completion_metadata["finish_reason"] == "end_turn"


def test_openai_stream_complete_extracts_deltas_without_duplicate_full_text():
    response = _SSEResponse(
        [
            b'data: {"type":"response.output_text.delta","delta":"<final>"}\n',
            b"\n",
            b'data: {"type":"response.output_text.delta","delta":"OK"}\n',
            b"\n",
            b'data: {"type":"response.output_text.done","text":"<final>OK</final>"}\n',
            b"\n",
            b'data: {"type":"response.completed","response":{"output":[{"content":[{"text":"<final>OK</final>"}]}],"usage":{"input_tokens":100,"output_tokens":10}}}\n',
            b"\n",
            b"data: [DONE]\n",
        ]
    )
    client = OpenAICompatibleModelClient(
        model="gpt-test",
        base_url="https://api.openai.com/v1",
        api_key="sk-test",
        temperature=0.0,
        timeout=30,
    )

    with patch("urllib.request.urlopen", return_value=response):
        events = list(client.stream_complete("prompt", 32, request_id="r1"))

    texts = [event["text"] for event in events if event["type"] == "content_delta"]
    assert texts == ["<final>", "OK"]
    assert client.last_completion_metadata["input_tokens"] == 100
    assert client.last_completion_metadata["output_tokens"] == 10


def test_ollama_stream_complete_handles_plain_json_fallback():
    response = _JSONResponse(
        [
            b'{"response":"<final>ok</final>","done":true,"prompt_eval_count":100,"eval_count":10}\n'
        ]
    )
    client = OllamaModelClient(
        model="qwen3.5:4b",
        base_url="http://127.0.0.1:11434",
        temperature=0.0,
        top_p=0.9,
        timeout=30,
    )

    with patch("urllib.request.urlopen", return_value=response):
        events = list(client.stream_complete("prompt", 32, request_id="r1"))

    texts = [event["text"] for event in events if event["type"] == "content_delta"]
    assert texts == ["<final>ok</final>"]
    assert client.last_completion_metadata["input_tokens"] == 100


def test_openai_stream_complete_handles_plain_json_fallback():
    response = _JSONResponse(
        [
            b'{"output_text":"<final>ok</final>","usage":{"input_tokens":100,"output_tokens":10}}\n'
        ]
    )
    client = OpenAICompatibleModelClient(
        model="gpt-test",
        base_url="https://api.openai.com/v1",
        api_key="sk-test",
        temperature=0.0,
        timeout=30,
    )

    with patch("urllib.request.urlopen", return_value=response):
        events = list(client.stream_complete("prompt", 32, request_id="r1"))

    texts = [event["text"] for event in events if event["type"] == "content_delta"]
    assert texts == ["<final>ok</final>"]
    assert client.last_completion_metadata["input_tokens"] == 100


def test_deepseek_stream_complete_handles_plain_json_fallback():
    response = _JSONResponse(
        [
            b'{"choices":[{"message":{"content":"<final>ok</final>"}}],'
            b'"usage":{"prompt_tokens":100,"completion_tokens":10,'
            b'"prompt_cache_hit_tokens":90,"prompt_cache_miss_tokens":10}}\n'
        ]
    )
    client = DeepSeekModelClient(
        model="deepseek-v4-flash",
        base_url="https://api.deepseek.com",
        api_key="sk-test",
        temperature=0.0,
        timeout=30,
    )

    with patch("urllib.request.urlopen", return_value=response):
        events = list(client.stream_complete("prompt", 32, request_id="r1"))

    texts = [event["text"] for event in events if event["type"] == "content_delta"]
    assert texts == ["<final>ok</final>"]
    assert client.last_completion_metadata["cache_hit"] is True


def test_anthropic_stream_complete_handles_plain_json_fallback():
    response = _JSONResponse(
        [
            b'{"content":[{"type":"text","text":"<final>ok</final>"}],'
            b'"usage":{"input_tokens":100,"output_tokens":10}}\n'
        ]
    )
    client = AnthropicCompatibleModelClient(
        model="claude-test",
        base_url="https://api.anthropic.com/v1",
        api_key="sk-test",
        temperature=0.0,
        timeout=30,
    )

    with patch("urllib.request.urlopen", return_value=response):
        events = list(client.stream_complete("prompt", 32, request_id="r1"))

    texts = [event["text"] for event in events if event["type"] == "content_delta"]
    assert texts == ["<final>ok</final>"]
    assert client.last_completion_metadata["input_tokens"] == 100


def test_deepseek_stream_handles_crlf_multiline_data_malformed_line_and_duplicate_done():
    response = _SSEResponse(
        [
            b'data: {"choices":[{"delta":{"content":"<final>"}}],\r\n',
            b'data: "usage":{"prompt_tokens":100,"completion_tokens":10}}\r\n',
            b"\r\n",
            b"data: {this is not valid json}\r\n",
            b"\r\n",
            b"data: [DONE]\r\n",
            b"data: [DONE]\r\n",
            b"\r\n",
        ]
    )
    client = DeepSeekModelClient(
        model="deepseek-v4-flash",
        base_url="https://api.deepseek.com",
        api_key="sk-test",
        temperature=0.0,
        timeout=30,
    )

    with patch("urllib.request.urlopen", return_value=response):
        events = list(client.stream_complete("prompt", 32, request_id="r1"))

    texts = [event["text"] for event in events if event["type"] == "content_delta"]
    assert texts == ["<final>"]
    assert client.last_completion_metadata["input_tokens"] == 100


def test_stream_transport_decodes_multibyte_utf8_split_across_chunks():
    response = _ChunkedResponse(
        [
            b'data: {"choices":[{"delta":{"content":"<final>\xe4\xbd',
            b'\xa0\xe5\xa5\xbd</final>"}}]}\n',
            b"\n",
            b"data: [DONE]\n",
        ]
    )
    client = DeepSeekModelClient(
        model="deepseek-v4-flash",
        base_url="https://api.deepseek.com",
        api_key="sk-test",
        temperature=0.0,
        timeout=30,
    )

    with patch("urllib.request.urlopen", return_value=response):
        events = list(client.stream_complete("prompt", 32, request_id="r1"))

    texts = [event["text"] for event in events if event["type"] == "content_delta"]
    assert texts == ["<final>\u4f60\u597d</final>"]


def test_cli_tty_default_uses_streaming(monkeypatch):
    agent = run_cli_main(monkeypatch, ["--model", "fake", "hello"], TTYStringIO())

    assert agent.last_ask["stream"] is True
    assert agent.last_ask["event_sink"] is not None


def test_cli_non_tty_default_uses_no_stream(monkeypatch):
    agent = run_cli_main(monkeypatch, ["--model", "fake", "hello"], io.StringIO())

    assert agent.last_ask["stream"] is False
    assert agent.last_ask["event_sink"] is None


def test_cli_stream_flag_forces_streaming_on_non_tty(monkeypatch):
    agent = run_cli_main(monkeypatch, ["--model", "fake", "--stream", "hello"], io.StringIO())

    assert agent.last_ask["stream"] is True
    assert agent.last_ask["event_sink"] is not None


def test_cli_no_stream_flag_never_calls_event_sink(monkeypatch):
    agent = run_cli_main(monkeypatch, ["--model", "fake", "--no-stream", "hello"], TTYStringIO())

    assert agent.last_ask["stream"] is False
    assert agent.last_ask["event_sink"] is None


def test_cli_stream_flags_are_mutually_exclusive_with_auto_default():
    parser = build_arg_parser()

    assert parser.parse_args([]).stream_mode is None
    assert parser.parse_args(["--stream"]).stream_mode is True
    assert parser.parse_args(["--no-stream"]).stream_mode is False
    with pytest.raises(SystemExit):
        parser.parse_args(["--stream", "--no-stream"])
