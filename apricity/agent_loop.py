"""Agent control loop extracted from the runtime facade."""

import time
import uuid

from .checkpoint import CHECKPOINT_NONE_STATUS, CHECKPOINT_PARTIAL_STALE_STATUS, CHECKPOINT_WORKSPACE_MISMATCH_STATUS
from .recovery import (
    DECISION_CORRECT_PROTOCOL,
    DECISION_REBUILD_CONTEXT,
    DECISION_RETRY_MODEL,
    FAILURE_PERMANENT_PROVIDER,
    FAILURE_PROTOCOL,
    FAILURE_TRANSIENT_PROVIDER,
    Failure,
    ProviderRequestError,
    failure_from_exception,
)
from .streaming import (
    EVENT_CONTENT_DELTA,
    EVENT_RESPONSE_COMPLETED,
    EVENT_RESPONSE_FAILED,
    EVENT_RESPONSE_STARTED,
    EVENT_TOOL_CANDIDATE_READY,
    EVENT_VISIBLE_DELTA,
    IncrementalRedactor,
    PARSER_STATUS_UNCLOSED_FINAL,
    StreamCancelledError,
    StreamInterruptedError,
    StreamingResponseParser,
)
from .task_state import (
    STATUS_FAILED,
    STATUS_STOPPED,
    STOP_REASON_STREAM_CANCELLED,
    STOP_REASON_STREAM_INTERRUPTED,
    TaskState,
)
from .workspace import clip, now


class AgentLoop:
    MAX_INVALID_TOOL_ATTEMPTS = 3

    @staticmethod
    def _is_successful_test_run(name, args, result, metadata):
        if name not in {"run_process", "run_shell"}:
            return False
        if str((metadata or {}).get("tool_status", "")) != "ok":
            return False
        if "exit_code: 0" not in str(result):
            return False
        args = args if isinstance(args, dict) else {}
        if name == "run_process":
            command_text = " ".join(
                [str(args.get("program", "")), *(str(item) for item in args.get("args", []))]
            )
        else:
            command_text = str(args.get("command", ""))
        command_text = command_text.casefold()
        return "pytest" in command_text or "unittest" in command_text

    @staticmethod
    def _record_recovery(agent, task_state, lifecycle_point, failure, decision):
        if decision.intervention:
            task_state.record_recovery(decision.policy)
            agent.run_store.write_task_state(task_state)
        agent.emit_trace(
            task_state,
            "recovery_decided",
            {
                "lifecycle_point": lifecycle_point,
                "failure_type": failure.kind,
                "failure_message": failure.message,
                "policy": decision.policy,
                "decision": decision.action,
                "reason": decision.reason,
                "delay_seconds": decision.delay_seconds,
                "recovery_actions": task_state.recovery_actions,
                "remaining_recovery_budget": max(0, 4 - task_state.recovery_actions),
                **dict(decision.details),
            },
        )

    @staticmethod
    def _persist_model_failure(
        agent,
        task_state,
        user_message,
        prompt_metadata,
        error,
        model_started_at,
        run_started_at,
        stop_reason="model_error",
    ):
        completion_metadata = dict(getattr(agent.model_client, "last_completion_metadata", {}) or {})
        if completion_metadata:
            prompt_metadata.update(completion_metadata)
        agent.last_completion_metadata = completion_metadata
        agent.last_prompt_metadata = prompt_metadata
        error_text = agent.redact_artifact(f"{type(error).__name__}: {error}")
        final = f"Stopped because the model provider failed: {error_text}"
        if stop_reason == "model_error":
            task_state.stop_model_error(final)
        else:
            task_state.stop(stop_reason, status=STATUS_FAILED, final_answer=final)
        agent.record({"role": "assistant", "content": final, "created_at": now()})
        agent.run_store.write_task_state(task_state)
        agent.emit_trace(
            task_state,
            "model_failed",
            {
                "error_type": type(error).__name__,
                "error": clip(str(error), 500),
                "completion_metadata": completion_metadata,
                "duration_ms": int((time.monotonic() - model_started_at) * 1000),
            },
        )
        checkpoint = agent.create_checkpoint(task_state, user_message, trigger="model_error")
        agent.run_store.write_task_state(task_state)
        agent.emit_trace(
            task_state,
            "checkpoint_created",
            {"checkpoint_id": checkpoint["checkpoint_id"], "trigger": "model_error"},
        )
        agent.emit_trace(
            task_state,
            "run_finished",
            {
                "status": task_state.status,
                "stop_reason": task_state.stop_reason,
                "final_answer": final,
                "run_duration_ms": int((time.monotonic() - run_started_at) * 1000),
            },
        )
        agent.run_store.write_report(task_state, agent.redact_artifact(agent.build_report(task_state)))
        return final

    @staticmethod
    def _persist_stream_terminal(
        agent,
        task_state,
        user_message,
        stop_reason,
        final,
        *,
        status=STATUS_FAILED,
        partial_visible=False,
        visible_chars=0,
        request_id="",
        error_type="",
    ):
        task_state.stop(stop_reason, status=status, final_answer=final)
        task_state.partial_visible = bool(partial_visible)
        task_state.visible_chars = int(visible_chars)
        task_state.request_id = str(request_id)
        agent.record({"role": "assistant", "content": final, "created_at": now()})
        agent.run_store.write_task_state(task_state)
        agent.emit_trace(
            task_state,
            "stream_terminal",
            {
                "stop_reason": stop_reason,
                "error_type": error_type,
                "partial_visible": task_state.partial_visible,
                "visible_chars": task_state.visible_chars,
                "request_id": task_state.request_id,
            },
        )
        checkpoint = agent.create_checkpoint(task_state, user_message, trigger=stop_reason)
        agent.run_store.write_task_state(task_state)
        agent.emit_trace(
            task_state,
            "checkpoint_created",
            {"checkpoint_id": checkpoint["checkpoint_id"], "trigger": stop_reason},
        )
        agent.emit_trace(
            task_state,
            "run_finished",
            {
                "status": task_state.status,
                "stop_reason": task_state.stop_reason,
                "final_answer": final,
            },
        )
        agent.run_store.write_report(task_state, agent.redact_artifact(agent.build_report(task_state)))
        return final

    def __init__(self, agent):
        self.agent = agent

    def _emit_visible(self, agent, task_state, redactor, event_sink, text, stats):
        visible = redactor.feed(text)
        if not visible:
            return
        stats["visible_chars"] += len(visible)
        stats["chunk_count"] += 1
        task_state.partial_visible = True
        task_state.visible_chars = stats["visible_chars"]
        now_monotonic = time.monotonic()
        if stats["first_delta_at"] is None:
            stats["first_delta_at"] = now_monotonic
            agent.emit_trace(
                task_state,
                "first_token",
                {
                    "request_id": stats["request_id"],
                    "ttft_ms": int((now_monotonic - stats["model_started_at"]) * 1000),
                },
            )
        stats["last_delta_at"] = now_monotonic
        if event_sink is None:
            return
        try:
            event_sink({"type": EVENT_VISIBLE_DELTA, "text": visible})
        except (BrokenPipeError, OSError) as exc:
            raise StreamCancelledError(
                f"visible output sink failed: {type(exc).__name__}",
                partial_visible=task_state.partial_visible,
                visible_chars=stats["visible_chars"],
            ) from exc
        except Exception as exc:
            stats["sink_errors"] += 1
            agent.emit_trace(
                task_state,
                "sink_error",
                {
                    "request_id": stats["request_id"],
                    "error": clip(f"{type(exc).__name__}: {exc}", 300),
                },
            )

    def _handle_parser_events(self, agent, task_state, events, redactor, event_sink, stats):
        for event in events or []:
            if event.get("type") == EVENT_VISIBLE_DELTA:
                self._emit_visible(agent, task_state, redactor, event_sink, event.get("text", ""), stats)
            elif event.get("type") == EVENT_TOOL_CANDIDATE_READY:
                # candidate 只做缓冲标记；工具执行必须等 response_completed。
                stats["tool_candidate_ready"] = True

    def _emit_stream_summary(self, agent, task_state, stats):
        agent.emit_trace(
            task_state,
            "stream_summary",
            {
                "request_id": stats["request_id"],
                "streaming": True,
                "visible_chars": stats["visible_chars"],
                "chunk_count": stats["chunk_count"],
                "provider_event_count": stats["provider_event_count"],
                "sink_errors": stats["sink_errors"],
                "ttft_ms": (
                    int((stats["first_delta_at"] - stats["model_started_at"]) * 1000)
                    if stats["first_delta_at"] is not None
                    else None
                ),
                "first_delta_at": stats["first_delta_at"],
                "last_delta_at": stats["last_delta_at"],
                "tool_candidate_ready": stats["tool_candidate_ready"],
            },
        )

    def _stream_model_call(
        self,
        agent,
        task_state,
        prompt,
        prompt_cache_key,
        prompt_cache_retention,
        event_sink,
        request_id,
        model_started_at,
    ):
        parser = StreamingResponseParser()
        redactor = IncrementalRedactor.from_env(getattr(agent, "secret_env_names", ()))
        stats = {
            "request_id": request_id,
            "model_started_at": model_started_at,
            "visible_chars": 0,
            "chunk_count": 0,
            "provider_event_count": 0,
            "sink_errors": 0,
            "first_delta_at": None,
            "last_delta_at": None,
            "tool_candidate_ready": False,
        }
        generator = agent.model_client.stream_complete(
            prompt,
            agent.max_new_tokens,
            prompt_cache_key=prompt_cache_key,
            prompt_cache_retention=prompt_cache_retention,
            request_id=request_id,
        )
        try:
            for event in generator:
                stats["provider_event_count"] += 1
                event_type = str(event.get("type", ""))
                if event_type == EVENT_CONTENT_DELTA:
                    self._handle_parser_events(
                        agent,
                        task_state,
                        parser.feed(event.get("text", "")),
                        redactor,
                        event_sink,
                        stats,
                    )
                elif event_type == EVENT_RESPONSE_COMPLETED:
                    metadata = dict(event.get("metadata") or {})
                    if metadata:
                        agent.model_client.last_completion_metadata = metadata
                    agent.last_completion_metadata = metadata
                elif event_type == EVENT_RESPONSE_FAILED:
                    raise ProviderRequestError(
                        str(event.get("message", "") or "provider stream failed"),
                        kind=(
                            FAILURE_TRANSIENT_PROVIDER
                            if bool(event.get("retryable", False))
                            else FAILURE_PERMANENT_PROVIDER
                        ),
                        status_code=event.get("status_code"),
                        retry_after=event.get("retry_after"),
                    )
                elif event_type == EVENT_RESPONSE_STARTED:
                    continue
        except Exception as exc:
            if isinstance(exc, (StreamCancelledError, StreamInterruptedError)):
                raise
            if parser.visible_emitted:
                raise StreamInterruptedError(
                    clip(str(exc), 500),
                    error_type=type(exc).__name__,
                    visible_chars=stats["visible_chars"],
                ) from exc
            raise
        finally:
            close = getattr(generator, "close", None)
            if callable(close):
                close()

        self._handle_parser_events(
            agent,
            task_state,
            parser.finish(),
            redactor,
            event_sink,
            stats,
        )
        tail = redactor.flush()
        if tail:
            self._emit_visible(agent, task_state, redactor, event_sink, tail, stats)
        self._emit_stream_summary(agent, task_state, stats)

        if parser.status == PARSER_STATUS_UNCLOSED_FINAL and stats["visible_chars"]:
            raise StreamInterruptedError(
                "model stream ended before the <final> envelope was closed",
                error_type="unclosed_final",
                visible_chars=stats["visible_chars"],
            )
        return parser.full_raw()

    def _complete_model(
        self,
        agent,
        task_state,
        prompt,
        prompt_cache_key,
        prompt_cache_retention,
        event_sink,
        stream,
        request_id,
        model_started_at,
    ):
        if not stream or not hasattr(agent.model_client, "stream_complete"):
            raw = agent.model_client.complete(
                prompt,
                agent.max_new_tokens,
                prompt_cache_key=prompt_cache_key,
                prompt_cache_retention=prompt_cache_retention,
            )
            if stream:
                # 旧 adapter 兼容路径：把完整响应包装成单个增量，经 parser 走一遍
                # 展示与协议缓冲，保证带 event_sink 的调用行为一致。
                parser = StreamingResponseParser()
                redactor = IncrementalRedactor.from_env(getattr(agent, "secret_env_names", ()))
                stats = {
                    "request_id": request_id,
                    "model_started_at": model_started_at,
                    "visible_chars": 0,
                    "chunk_count": 0,
                    "provider_event_count": 1,
                    "sink_errors": 0,
                    "first_delta_at": None,
                    "last_delta_at": None,
                    "tool_candidate_ready": False,
                }
                self._handle_parser_events(
                    agent,
                    task_state,
                    parser.feed(raw),
                    redactor,
                    event_sink,
                    stats,
                )
                self._handle_parser_events(
                    agent,
                    task_state,
                    parser.finish(),
                    redactor,
                    event_sink,
                    stats,
                )
                tail = redactor.flush()
                if tail:
                    self._emit_visible(agent, task_state, redactor, event_sink, tail, stats)
                self._emit_stream_summary(agent, task_state, stats)
            return raw
        return self._stream_model_call(
            agent,
            task_state,
            prompt,
            prompt_cache_key,
            prompt_cache_retention,
            event_sink,
            request_id,
            model_started_at,
        )

    def run(self, user_message, event_sink=None, stream=False):
        agent = self.agent
        run_started_at = time.monotonic()
        agent.memory.set_task_summary(user_message)
        agent.record({"role": "user", "content": user_message, "created_at": now()})

        task_state = TaskState.create(run_id=agent.new_run_id(),
                                      task_id=agent.new_task_id(),
                                      user_request=user_message)
        task_state.resume_status = agent.resume_state.get("status", CHECKPOINT_NONE_STATUS)
        agent.current_task_state = task_state
        agent.current_run_dir = agent.run_store.start_run(task_state)
        agent.emit_trace(
            task_state,
            "run_started",
            {
                "task_id": task_state.task_id,
                "user_request": clip(user_message, 300),
            },
        )

        tool_steps = 0
        attempts = 0
        max_attempts = max(agent.max_steps * 3, agent.max_steps + 4)
        invalid_tool_attempts = 0
        finalization_attempted = False
        forced_stop_reason = ""

        # 这是 agent 的主循环，可以按“感知 -> 决策 -> 行动 -> 记录”来理解：
        # 1. 感知：重新组 prompt，把当前状态整理给模型看
        # 2. 决策：让模型返回一个工具调用，或一个最终答案
        # 3. 行动：如果是工具调用，就执行工具
        # 4. 记录：把结果写回 history / task_state / trace / memory
        # 然后进入下一轮，直到停机条件满足
        while (
            (
                tool_steps < agent.max_steps
                and attempts < max_attempts
                and invalid_tool_attempts < self.MAX_INVALID_TOOL_ATTEMPTS
            )
            or (tool_steps >= agent.max_steps and not finalization_attempted)
        ):
            final_only = tool_steps >= agent.max_steps
            if final_only:
                finalization_attempted = True
            attempts += 1
            task_state.record_attempt()
            agent.run_store.write_task_state(task_state)
            prompt_started_at = time.monotonic()
            prompt, prompt_metadata = agent._build_prompt_and_metadata(user_message)
            if final_only:
                prompt += (
                    "\n\nRuntime notice: the tool step limit has been reached. "
                    "Do not request another tool. Use the available tool results and return "
                    "one non-empty <final> answer now."
                )
                prompt_metadata["finalization_only"] = True
            agent.emit_trace(
                task_state,
                "prompt_built",
                {
                    "prompt_metadata": prompt_metadata,
                    "duration_ms": int((time.monotonic() - prompt_started_at) * 1000),
                },
            )
            if prompt_metadata.get("resume_status") == CHECKPOINT_PARTIAL_STALE_STATUS:
                checkpoint = agent.create_checkpoint(task_state, user_message, trigger="freshness_mismatch")
                agent.run_store.write_task_state(task_state)
                agent.emit_trace(
                    task_state,
                    "checkpoint_created",
                    {
                        "checkpoint_id": checkpoint["checkpoint_id"],
                        "trigger": "freshness_mismatch",
                    },
                )
            elif prompt_metadata.get("resume_status") == CHECKPOINT_WORKSPACE_MISMATCH_STATUS:
                agent.emit_trace(
                    task_state,
                    "runtime_identity_mismatch",
                    {
                        "fields": list(prompt_metadata.get("runtime_identity_mismatch_fields", [])),
                    },
                )
                checkpoint = agent.create_checkpoint(task_state, user_message, trigger="workspace_mismatch")
                agent.run_store.write_task_state(task_state)
                agent.emit_trace(
                    task_state,
                    "checkpoint_created",
                    {
                        "checkpoint_id": checkpoint["checkpoint_id"],
                        "trigger": "workspace_mismatch",
                    },
                )
            if prompt_metadata.get("budget_reductions"):
                checkpoint = agent.create_checkpoint(task_state, user_message, trigger="context_reduction")
                agent.run_store.write_task_state(task_state)
                agent.emit_trace(
                    task_state,
                    "checkpoint_created",
                    {
                        "checkpoint_id": checkpoint["checkpoint_id"],
                        "trigger": "context_reduction",
                    },
                )
            prompt_cache_key = None
            prompt_cache_retention = None
            cache_mode = getattr(
                agent.model_client,
                "prompt_cache_mode",
                "explicit_key" if getattr(agent.model_client, "supports_prompt_cache", False) else "none",
            )
            if cache_mode == "explicit_key":
                # 只有后端明确支持时，才把稳定前缀的 hash 作为 cache key 发出去。
                prompt_cache_key = prompt_metadata.get("prompt_cache_key")
                prompt_cache_retention = "in_memory"
            elif cache_mode == "automatic_prefix":
                # 自动前缀缓存不接收控制参数，但保留本地 key 供 trace 关联。
                prompt_cache_key = prompt_metadata.get("prompt_cache_key")
            request_id = "req_" + uuid.uuid4().hex[:12]
            task_state.request_id = request_id
            task_state.partial_visible = False
            task_state.visible_chars = 0
            agent.emit_trace(
                task_state,
                "model_requested",
                {
                    "attempts": task_state.attempts,
                    "tool_steps": task_state.tool_steps,
                    "invalid_tool_attempts": task_state.invalid_tool_attempts,
                    "finalization_only": final_only,
                    "prompt_cache_key": prompt_metadata.get("prompt_cache_key"),
                    "request_id": request_id,
                    "streaming": bool(stream),
                },
            )
            model_started_at = time.monotonic()
            try:
                raw = self._complete_model(
                    agent,
                    task_state,
                    prompt,
                    prompt_cache_key,
                    prompt_cache_retention,
                    event_sink,
                    stream,
                    request_id,
                    model_started_at,
                )
            except (StreamCancelledError, KeyboardInterrupt) as exc:
                partial_visible = bool(getattr(exc, "partial_visible", False) or task_state.partial_visible)
                visible_chars = int(getattr(exc, "visible_chars", 0) or task_state.visible_chars)
                final = "Stopped: the stream was cancelled before the answer completed."
                return self._persist_stream_terminal(
                    agent,
                    task_state,
                    user_message,
                    STOP_REASON_STREAM_CANCELLED,
                    final,
                    status=STATUS_STOPPED,
                    partial_visible=partial_visible,
                    visible_chars=visible_chars,
                    request_id=request_id,
                    error_type=type(exc).__name__,
                )
            except StreamInterruptedError as exc:
                final = f"Stopped: the model stream was interrupted ({exc.error_type})."
                return self._persist_stream_terminal(
                    agent,
                    task_state,
                    user_message,
                    STOP_REASON_STREAM_INTERRUPTED,
                    final,
                    status=STATUS_FAILED,
                    partial_visible=exc.partial_visible,
                    visible_chars=exc.visible_chars,
                    request_id=request_id,
                    error_type=exc.error_type,
                )
            except Exception as exc:
                failure = failure_from_exception(exc)
                decision = agent.recovery_engine.handle_failure(
                    failure,
                    agent.recovery_snapshot(
                        "model_request_failed",
                        prompt_chars=len(prompt),
                        finalization_only=final_only,
                    ),
                )
                self._record_recovery(agent, task_state, "model_request_failed", failure, decision)
                if decision.action == DECISION_RETRY_MODEL:
                    if decision.delay_seconds:
                        time.sleep(decision.delay_seconds)
                    continue
                if decision.action == DECISION_REBUILD_CONTEXT:
                    before_chars = len(prompt)
                    agent.context_manager.tighten_for_recovery(ratio=0.25)
                    rebuilt_prompt, rebuilt_metadata = agent._build_prompt_and_metadata(user_message)
                    after_chars = len(rebuilt_prompt)
                    reduction_ratio = (before_chars - after_chars) / before_chars if before_chars else 0.0
                    agent.emit_trace(
                        task_state,
                        "context_rebuilt",
                        {
                            "before_chars": before_chars,
                            "after_chars": after_chars,
                            "reduction_ratio": reduction_ratio,
                            "prompt_metadata": rebuilt_metadata,
                        },
                    )
                    if reduction_ratio >= float(decision.details.get("minimum_reduction_ratio", 0.20)):
                        continue
                    return self._persist_model_failure(
                        agent,
                        task_state,
                        user_message,
                        prompt_metadata,
                        exc,
                        model_started_at,
                        run_started_at,
                        stop_reason="context_recovery_no_progress",
                    )
                return self._persist_model_failure(
                    agent,
                    task_state,
                    user_message,
                    prompt_metadata,
                    exc,
                    model_started_at,
                    run_started_at,
                    stop_reason=decision.reason if decision.reason.endswith("_exhausted") else "model_error",
                )
            completion_metadata = dict(getattr(agent.model_client, "last_completion_metadata", {}) or {})
            if completion_metadata:
                # 把后端返回的 usage/cache 统计并回 prompt_metadata，
                # 方便统一写入 report 和 trace。
                prompt_metadata.update(completion_metadata)
            agent.last_completion_metadata = completion_metadata
            agent.last_prompt_metadata = prompt_metadata
            kind, payload = agent.parse(raw)
            agent.emit_trace(
                task_state,
                "model_parsed",
                {
                    "kind": kind,
                    "completion_metadata": completion_metadata,
                    "duration_ms": int((time.monotonic() - model_started_at) * 1000),
                },
            )

            if kind == "tool":
                if final_only:
                    agent.record(
                        {
                            "role": "assistant",
                            "content": "Runtime notice: tool step limit reached; the additional tool call was not executed.",
                            "created_at": now(),
                        }
                    )
                    agent.run_store.write_task_state(task_state)
                    break
                name = payload.get("name", "")
                args = payload.get("args", {})
                tool_started_at = time.monotonic()
                tool_result = agent.execute_tool(name, args)
                result = tool_result.content
                tool_status = str((tool_result.metadata or {}).get("tool_status", ""))
                if tool_status == "rejected":
                    invalid_tool_attempts += 1
                    task_state.record_invalid_tool()
                else:
                    tool_steps += 1
                    task_state.record_tool(name)
                agent.record(
                    {
                        "role": "tool",
                        "name": name,
                        "args": args,
                        "content": result,
                        "created_at": now(),
                    }
                )
                agent.run_store.write_task_state(task_state)
                agent.emit_trace(
                    task_state,
                    "tool_executed",
                    {
                        "name": name,
                        "args": args,
                        "result": clip(result, 500),
                        "duration_ms": int((time.monotonic() - tool_started_at) * 1000),
                        **dict(tool_result.metadata or {}),
                    },
                )
                protocol_decision = None
                tool_error_code = str((tool_result.metadata or {}).get("tool_error_code", ""))
                if tool_status == "rejected" and tool_error_code in {
                    "unknown_tool",
                    "invalid_arguments",
                    "repeated_identical_call",
                }:
                    failure = Failure(
                        kind=FAILURE_PROTOCOL,
                        message=(
                            f"Runtime notice: tool call rejected ({tool_error_code}). "
                            "Correct the tool name or arguments before continuing."
                        ),
                        error_code=tool_error_code,
                    )
                    protocol_decision = agent.recovery_engine.handle_failure(
                        failure,
                        agent.recovery_snapshot("model_response_rejected"),
                    )
                    self._record_recovery(
                        agent,
                        task_state,
                        "model_response_rejected",
                        failure,
                        protocol_decision,
                    )
                    if protocol_decision.action == DECISION_CORRECT_PROTOCOL and protocol_decision.intervention:
                        agent.record(
                            {"role": "assistant", "content": protocol_decision.notice, "created_at": now()}
                        )
                if self._is_successful_test_run(name, args, result, tool_result.metadata):
                    notice = (
                        "Runtime notice: the verification command succeeded with exit_code 0. "
                        "If the requested task is complete, return one non-empty <final> answer now "
                        "instead of doing more exploration."
                    )
                    agent.record({"role": "assistant", "content": notice, "created_at": now()})
                    agent.emit_trace(
                        task_state,
                        "verification_succeeded",
                        {"name": name, "tool_steps": task_state.tool_steps},
                    )
                checkpoint = agent.create_checkpoint(task_state, user_message, trigger="tool_executed")
                agent.run_store.write_task_state(task_state)
                agent.emit_trace(
                    task_state,
                    "checkpoint_created",
                    {
                        "checkpoint_id": checkpoint["checkpoint_id"],
                        "trigger": "tool_executed",
                    },
                )
                if protocol_decision is not None and protocol_decision.action != DECISION_CORRECT_PROTOCOL:
                    forced_stop_reason = protocol_decision.reason
                    break
                continue

            if kind == "retry":
                failure = Failure(kind=FAILURE_PROTOCOL, message=str(payload), error_code="malformed_response")
                decision = agent.recovery_engine.handle_failure(
                    failure,
                    agent.recovery_snapshot(
                        "model_response_rejected",
                        prompt_chars=len(prompt),
                        finalization_only=final_only,
                    ),
                )
                self._record_recovery(agent, task_state, "model_response_rejected", failure, decision)
                if decision.action != DECISION_CORRECT_PROTOCOL:
                    forced_stop_reason = decision.reason
                    break
                agent.record({"role": "assistant", "content": decision.notice or payload, "created_at": now()})
                agent.run_store.write_task_state(task_state)
                if final_only:
                    break
                continue

            final = (payload or raw).strip()
            agent.record({"role": "assistant", "content": final, "created_at": now()})
            task_state.finish_success(final)
            agent.promote_durable_memory(user_message, final)
            checkpoint = agent.create_checkpoint(task_state, user_message, trigger="run_finished")
            agent.run_store.write_task_state(task_state)
            agent.emit_trace(
                task_state,
                "checkpoint_created",
                {
                    "checkpoint_id": checkpoint["checkpoint_id"],
                    "trigger": "run_finished",
                },
            )
            agent.emit_trace(
                task_state,
                "run_finished",
                {
                    "status": task_state.status,
                    "stop_reason": task_state.stop_reason,
                    "final_answer": final,
                    "run_duration_ms": int((time.monotonic() - run_started_at) * 1000),
                },
            )
            agent.run_store.write_report(task_state, agent.redact_artifact(agent.build_report(task_state)))
            return final

        if forced_stop_reason:
            final = f"Stopped by recovery policy: {forced_stop_reason}."
            task_state.stop(forced_stop_reason, final_answer=final)
        elif invalid_tool_attempts >= self.MAX_INVALID_TOOL_ATTEMPTS:
            final = "Stopped after too many invalid or rejected tool calls."
            task_state.stop_invalid_tool_limit(final)
        elif attempts >= max_attempts and tool_steps < agent.max_steps:
            final = "Stopped after too many malformed model responses without a valid tool call or final answer."
            task_state.stop_retry_limit(final)
        else:
            final = "Stopped after reaching the step limit without a final answer."
            task_state.stop_step_limit(final)
        agent.record({"role": "assistant", "content": final, "created_at": now()})
        agent.promote_durable_memory(user_message, final)
        agent.run_store.write_task_state(task_state)
        checkpoint = agent.create_checkpoint(task_state, user_message, trigger=task_state.stop_reason or "run_stopped")
        agent.emit_trace(
            task_state,
            "checkpoint_created",
            {
                "checkpoint_id": checkpoint["checkpoint_id"],
                "trigger": task_state.stop_reason or "run_stopped",
            },
        )
        agent.emit_trace(
            task_state,
            "run_finished",
            {
                "status": task_state.status,
                "stop_reason": task_state.stop_reason,
                "final_answer": final,
                "run_duration_ms": int((time.monotonic() - run_started_at) * 1000),
            },
        )
        agent.run_store.write_report(task_state, agent.redact_artifact(agent.build_report(task_state)))
        return final
