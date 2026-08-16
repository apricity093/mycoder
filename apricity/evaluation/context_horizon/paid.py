"""Paid-model runner for the Context Horizon main experiment.

The control meter is a named fallback estimator (never provider-exact); the
provider-reported usage is recorded separately and drives the cost guard.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ...context import ContextEngine, FallbackTokenEstimator, SessionLedger
from .runner import TOOLS, SYSTEM_PROMPT, condition_policies, execute_scripted_tool
from .tasks import append_seed_events, build_seed_events
from .verifiers import artifact_verifier, context_verifier, hidden_verifier_isolation, mechanics_verifier

PRICING = {
    "input_cache_miss_cny_per_1m": 1.4,
    "input_cache_hit_cny_per_1m": 0.05,
    "output_cny_per_1m": 4.5,
}

MAX_ATTEMPTS_PER_STEP = 3


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class CostGuard:
    max_cost_cny: float
    spent_cny: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0
    cache_miss_tokens: int = 0

    def record(self, metadata: dict[str, Any]) -> float:
        input_tokens = int(metadata.get("input_tokens", 0) or 0)
        output_tokens = int(metadata.get("output_tokens", 0) or 0)
        cached_tokens = int(metadata.get("cached_tokens", 0) or 0)
        cache_miss_tokens = int(metadata.get("cache_miss_tokens", 0) or 0)
        if input_tokens and cached_tokens + cache_miss_tokens == 0:
            cache_miss_tokens = input_tokens
        cost = (
            cache_miss_tokens / 1_000_000 * PRICING["input_cache_miss_cny_per_1m"]
            + cached_tokens / 1_000_000 * PRICING["input_cache_hit_cny_per_1m"]
            + output_tokens / 1_000_000 * PRICING["output_cny_per_1m"]
        )
        self.spent_cny += cost
        self.input_tokens += input_tokens
        self.output_tokens += output_tokens
        self.cached_tokens += cached_tokens
        self.cache_miss_tokens += cache_miss_tokens
        return cost

    @property
    def remaining_cny(self) -> float:
        return float(self.max_cost_cny) - self.spent_cny


class ModelSummarizer:
    """Adapter from the chunked compactor to a paid provider client."""

    def __init__(self, client, usage: dict[str, Any]):
        self.client = client
        self.usage = usage

    def __call__(self, events, renderings, max_tokens: int) -> str:
        from ...context.chunked import build_summary_request

        prompt = build_summary_request(events, renderings, max_tokens)
        raw = self.client.complete(prompt, max(int(max_tokens), 2048))
        metadata = dict(getattr(self.client, "last_completion_metadata", {}) or {})
        for key in ("input_tokens", "output_tokens", "cached_tokens", "cache_miss_tokens"):
            self.usage[key] = int(self.usage.get(key, 0)) + int(metadata.get(key, 0) or 0)
        self.usage["calls"] = int(self.usage.get("calls", 0)) + 1
        return raw


def execute_real_tool(workspace: Path, name: str, args: dict[str, Any]) -> dict[str, Any]:
    """Execute tool payloads from a real model. Process tools run for real."""
    if name == "run_process":
        program = str(args.get("program", "python"))
        argv = [program, *[str(item) for item in (args.get("args") or [])]]
        completed = subprocess.run(argv, cwd=workspace, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30)
        content = (completed.stdout or "") + (completed.stderr or "")
        return {"tool_name": name, "content": content, "metadata": {"status": "failed" if completed.returncode else "ok", "exit_code": completed.returncode}}
    if name == "run_shell":
        command = str(args.get("command", ""))
        completed = subprocess.run(command, cwd=workspace, shell=True, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30)
        content = (completed.stdout or "") + (completed.stderr or "")
        return {"tool_name": name, "content": content, "metadata": {"status": "failed" if completed.returncode else "ok", "exit_code": completed.returncode}}
    step = {"tool": name, "args": args}
    return execute_scripted_tool(workspace, step)


def run_paid_run(
    *,
    task: dict[str, Any],
    condition: str,
    fixture_root: str | Path,
    workspace_root: str | Path,
    hidden: dict[str, Any],
    budget: int,
    client_factory,
    summarizer_factory=None,
    cost_guard: CostGuard | None = None,
    max_new_tokens: int = 1024,
    provider: str = "deepseek",
    model: str = "",
) -> dict[str, Any]:
    import shutil
    from ...runtime import Apricity

    workspace = Path(workspace_root)
    if workspace.exists():
        shutil.rmtree(workspace)
    shutil.copytree(Path(fixture_root), workspace)

    ledger = SessionLedger(
        session_id=f"{task['id']}-{condition}",
        workspace_root=str(workspace),
        created_at=now_iso(),
        persist_path=workspace / ".ledger.json",
    )
    append_seed_events(ledger, build_seed_events(task))
    summary_usage: dict[str, Any] = {"input_tokens": 0, "output_tokens": 0, "cached_tokens": 0, "cache_miss_tokens": 0, "calls": 0}
    summarizer = None
    if condition == "D":
        if summarizer_factory is None:
            raise ValueError("condition D requires a summarizer factory")
        summarizer = summarizer_factory()
    engine = ContextEngine(
        provider=provider,
        model=model,
        system_prompt=SYSTEM_PROMPT,
        tools=TOOLS,
        memory_text="",
        meter=FallbackTokenEstimator(),
        policy=condition_policies(budget)[condition],
        ledger=ledger,
        summarizer=summarizer,
    )
    client = client_factory()

    attempts = 0
    tool_steps = 0
    stop_reason = "completed"
    observed_overflow = False
    model_error = ""
    step_budget = int(task.get("step_budget", 1))
    while tool_steps < step_budget and attempts < step_budget * MAX_ATTEMPTS_PER_STEP:
        try:
            prepared = engine.prepare(str(task["prompt"]))
        except Exception as exc:
            stop_reason = f"preparation_failed:{getattr(exc, 'code', type(exc).__name__)}"
            break
        attempts += 1
        try:
            raw = client.complete(prepared.prompt, max_new_tokens)
        except Exception as exc:
            kind = getattr(exc, "kind", "")
            model_error = f"{type(exc).__name__}: {str(exc)[:200]}"
            if kind == "context_overflow":
                observed_overflow = True
                stop_reason = "context_overflow_unrecovered"
            else:
                stop_reason = "model_stop_failure"
            break
        if cost_guard is not None:
            cost_guard.record(dict(getattr(client, "last_completion_metadata", {}) or {}))
        ledger.append("assistant/message", {"content": str(raw)})
        kind, payload = Apricity.parse(str(raw))
        if kind == "final":
            stop_reason = "final_answer_returned"
            break
        if kind == "retry":
            continue
        if kind != "tool":
            stop_reason = "model_stop_failure"
            break
        tool_steps += 1
        call_id = f"call-{condition}-{attempts}-{len(ledger.events)}"
        name = str(payload.get("name", ""))
        args = dict(payload.get("args") or {})
        ledger.append("tool/call", {"name": name, "call_id": call_id, "args": args})
        try:
            result = execute_real_tool(workspace, name, args)
        except Exception as exc:
            result = {"tool_name": name, "content": f"tool execution failed: {exc}", "metadata": {"status": "failed", "exit_code": -1}}
        result["call_id"] = call_id
        ledger.append("tool/result", result)

    if summarizer is not None:
        summary_usage = dict(getattr(summarizer, "usage", summary_usage))
    if summarizer is not None and cost_guard is not None and summary_usage.get("calls"):
        # The summary client metadata is not on the main client. Reconstruct a
        # cost-only record from the accumulated usage counters.
        fake_metadata = {
            "input_tokens": summary_usage.get("input_tokens", 0),
            "output_tokens": summary_usage.get("output_tokens", 0),
            "cached_tokens": summary_usage.get("cached_tokens", 0),
            "cache_miss_tokens": summary_usage.get("cache_miss_tokens", 0),
        }
        cost_guard.record(fake_metadata)

    context = context_verifier(task, ledger, hidden)
    artifact = artifact_verifier(task, workspace, hidden)
    mechanics = mechanics_verifier(task, ledger)
    return {
        "task_id": task["id"],
        "condition": condition,
        "budget": budget,
        "stop_reason": stop_reason,
        "model_error": model_error,
        "tool_steps": tool_steps,
        "attempts": attempts,
        "observed_overflow": observed_overflow,
        "context": context,
        "artifact": artifact,
        "mechanics": mechanics,
        "hidden_isolated": hidden_verifier_isolation(workspace, hidden),
        "summary_usage": summary_usage,
        "cost_guard_snapshot": {
            "spent_cny": cost_guard.spent_cny if cost_guard else 0.0,
            "input_tokens": cost_guard.input_tokens if cost_guard else 0,
            "output_tokens": cost_guard.output_tokens if cost_guard else 0,
            "cached_tokens": cost_guard.cached_tokens if cost_guard else 0,
            "cache_miss_tokens": cost_guard.cache_miss_tokens if cost_guard else 0,
        },
    }


def build_paid_schedule(tasks: list[dict[str, Any]], conditions: list[str]) -> list[dict[str, str]]:
    schedule = []
    for index, task in enumerate(tasks):
        order = conditions[index % len(conditions) :] + conditions[: index % len(conditions)]
        for condition in order:
            schedule.append({"task_id": task["id"], "condition": condition})
    return schedule
