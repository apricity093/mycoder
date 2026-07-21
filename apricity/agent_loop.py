"""Agent control loop extracted from the runtime facade."""

import time

from .checkpoint import CHECKPOINT_NONE_STATUS, CHECKPOINT_PARTIAL_STALE_STATUS, CHECKPOINT_WORKSPACE_MISMATCH_STATUS
from .task_state import TaskState
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
    def _persist_model_failure(
        agent,
        task_state,
        user_message,
        prompt_metadata,
        error,
        model_started_at,
        run_started_at,
    ):
        completion_metadata = dict(getattr(agent.model_client, "last_completion_metadata", {}) or {})
        if completion_metadata:
            prompt_metadata.update(completion_metadata)
        agent.last_completion_metadata = completion_metadata
        agent.last_prompt_metadata = prompt_metadata
        error_text = agent.redact_artifact(f"{type(error).__name__}: {error}")
        final = f"Stopped because the model provider failed: {error_text}"
        task_state.stop_model_error(final)
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

    def __init__(self, agent):
        self.agent = agent

    def run(self, user_message):
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
            agent.emit_trace(
                task_state,
                "model_requested",
                {
                    "attempts": task_state.attempts,
                    "tool_steps": task_state.tool_steps,
                    "invalid_tool_attempts": task_state.invalid_tool_attempts,
                    "finalization_only": final_only,
                    "prompt_cache_key": prompt_metadata.get("prompt_cache_key"),
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
            model_started_at = time.monotonic()
            try:
                raw = agent.model_client.complete(
                    prompt,
                    agent.max_new_tokens,
                    prompt_cache_key=prompt_cache_key,
                    prompt_cache_retention=prompt_cache_retention,
                )
            except Exception as exc:
                return self._persist_model_failure(
                    agent,
                    task_state,
                    user_message,
                    prompt_metadata,
                    exc,
                    model_started_at,
                    run_started_at,
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
                continue

            if kind == "retry":
                agent.record({"role": "assistant", "content": payload, "created_at": now()})
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

        if invalid_tool_attempts >= self.MAX_INVALID_TOOL_ATTEMPTS:
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
