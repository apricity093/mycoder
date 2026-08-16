"""Local scripted-model pilot runner for Context Horizon v3.

This runner exercises A/C/E policies with deterministic tool plans so pressure
profiles can be calibrated without paid model calls. The same task schema is
used later by the paid runner.
"""

from __future__ import annotations

import json
import re
import shutil
import uuid
from pathlib import Path
from typing import Any

from ...context import (
    ContextEngine,
    ContextPolicy,
    ContextPreparationError,
    DeterministicTestMeter,
    SessionLedger,
)
from .tasks import append_seed_events, build_seed_events
from .verifiers import artifact_verifier, context_verifier, hidden_verifier_isolation, mechanics_verifier

SYSTEM_PROMPT = (
    "You are a small coding agent. Use the provided tool evidence, keep earlier "
    "requirements intact, and return a final answer when done."
)

TOOLS = [
    {"name": "read_file", "schema": {"path": "str", "start": "int", "end": "int"}},
    {"name": "list_files", "schema": {"path": "str"}},
    {"name": "search", "schema": {"pattern": "str", "path": "str"}},
    {"name": "write_file", "schema": {"path": "str", "content": "str"}},
    {"name": "patch_file", "schema": {"path": "str", "old_text": "str", "new_text": "str"}},
    {"name": "run_process", "schema": {"program": "str", "args": "list[str]"}},
    {"name": "run_shell", "schema": {"command": "str"}},
]


def condition_policies(budget: int) -> dict[str, ContextPolicy]:
    return {
        "A": ContextPolicy(condition_id="A", condition_label="window", effective_input_budget=int(budget)),
        "C": ContextPolicy(
            condition_id="C",
            condition_label="prune",
            effective_input_budget=int(budget),
            tool_pruning=True,
        ),
        "E": ContextPolicy(
            condition_id="E",
            condition_label="structural",
            effective_input_budget=int(budget),
            tool_pruning=True,
            history_compaction=True,
            structural_compaction=True,
        ),
        "D": ContextPolicy(
            condition_id="D",
            condition_label="llm",
            effective_input_budget=int(budget),
            tool_pruning=True,
            history_compaction=True,
            chunked_llm_compaction=True,
        ),
    }


def _workspace_path(workspace: Path, raw_path: str) -> Path:
    path = Path(str(raw_path))
    if not path.is_absolute():
        path = workspace / path
    resolved = path.resolve()
    root = workspace.resolve()
    if root != resolved and root not in resolved.parents:
        raise ValueError(f"path escapes workspace: {raw_path}")
    return resolved


def execute_scripted_tool(workspace: Path, step: dict[str, Any]) -> dict[str, Any]:
    name = str(step.get("tool", ""))
    args = dict(step.get("args") or {})
    if name == "write_file":
        path = _workspace_path(workspace, str(args["path"]))
        content = str(args.get("content", ""))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return {"tool_name": name, "call_id": "", "content": f"wrote {path.name} ({len(content)} chars)", "metadata": {"status": "ok", "exit_code": 0}}
    if name == "patch_file":
        path = _workspace_path(workspace, str(args["path"]))
        text = path.read_text(encoding="utf-8")
        old = str(args.get("old_text", ""))
        new = str(args.get("new_text", ""))
        if text.count(old) != 1:
            raise ValueError(f"old_text must occur exactly once, found {text.count(old)}")
        path.write_text(text.replace(old, new), encoding="utf-8")
        return {"tool_name": name, "call_id": "", "content": f"patched {path.name}", "metadata": {"status": "ok", "exit_code": 0}}
    if name == "read_file":
        if "result" in step:
            return {"tool_name": name, "call_id": "", "content": str(step["result"]), "metadata": {"status": "ok", "exit_code": 0}}
        path = _workspace_path(workspace, str(args["path"]))
        return {"tool_name": name, "call_id": "", "content": path.read_text(encoding="utf-8", errors="replace"), "metadata": {"status": "ok", "exit_code": 0}}
    if name == "list_files":
        path = _workspace_path(workspace, str(args.get("path", ".")))
        lines = [str(item.relative_to(workspace)) for item in sorted(path.rglob("*")) if item.is_file()]
        return {"tool_name": name, "call_id": "", "content": "\n".join(lines), "metadata": {"status": "ok", "exit_code": 0}}
    if "result" in step:
        return {"tool_name": name, "call_id": "", "content": str(step["result"]), "metadata": dict(step.get("metadata") or {"status": "ok", "exit_code": 0})}
    return {"tool_name": name, "call_id": "", "content": f"scripted {name} result", "metadata": {"status": "ok", "exit_code": 0}}


_SALIENT_PATTERN = re.compile(r"(?i)(error|failed|failure|required|must|authoritative|round\d)")


class ScriptedChunkSummarizer:
    """Deterministic stand-in for the D-condition LLM summarizer.

    It extracts salient lines from chunk renderings and cites exact quotes, so
    local D pilots exercise the same schema/evidence path as a paid run.
    """

    def __call__(self, events, renderings: dict[int, str], max_tokens: int) -> str:
        del events, max_tokens
        facts = []
        for seq in sorted(renderings):
            for line in str(renderings[seq]).splitlines():
                if not _SALIENT_PATTERN.search(line):
                    continue
                quote = line[:240]
                facts.append({"text": line[:200], "status": "confirmed", "sources": [{"event_seq": seq, "quote": quote}]})
        if not facts:
            facts.append({"text": "no salient facts in chunk", "status": "open", "sources": []})
        return json.dumps({"schema_version": 1, "facts": facts[:8], "open_items": []}, ensure_ascii=False)


def _build_engine(*, condition: str, budget: int, ledger: SessionLedger, summarizer=None) -> ContextEngine:
    policies = condition_policies(budget)
    if condition not in policies:
        raise ValueError(f"unknown condition: {condition}")
    return ContextEngine(
        provider="scripted",
        model="scripted-context-model",
        system_prompt=SYSTEM_PROMPT,
        tools=TOOLS,
        meter=DeterministicTestMeter(),
        policy=policies[condition],
        ledger=ledger,
        summarizer=summarizer,
    )


def run_local_pilot(
    *,
    task: dict[str, Any],
    condition: str,
    fixture_root: str | Path,
    workspace_root: str | Path,
    hidden: dict[str, Any],
    budget: int = 4096,
    summarizer=None,
) -> dict[str, Any]:
    """Run one task/condition with a deterministic tool plan, no model calls."""
    fixture = Path(fixture_root)
    workspace = Path(workspace_root)
    if workspace.exists():
        shutil.rmtree(workspace)
    shutil.copytree(fixture, workspace)

    ledger = SessionLedger(
        session_id=f"{task['id']}-{condition}",
        workspace_root=str(workspace),
        created_at="2026-08-16T00:00:00+00:00",
        persist_path=workspace / ".ledger.json",
    )
    append_seed_events(ledger, build_seed_events(task))
    engine = _build_engine(condition=condition, budget=budget, ledger=ledger, summarizer=summarizer)

    steps: list[dict[str, Any]] = []
    tool_steps = 0
    stop_reason = "completed"
    plan = list(task.get("scripted_plan") or [])
    for plan_index, step in enumerate(plan):
        if tool_steps >= int(task.get("step_budget", 1)):
            stop_reason = "step_budget_exhausted"
            break
        try:
            prepared = engine.prepare(str(task.get("prompt", "")))
        except ContextPreparationError as exc:
            stop_reason = f"preparation_failed:{exc.code}"
            steps.append({"step": plan_index, "phase": "prepare", "error": exc.code})
            break
        steps.append(
            {
                "step": plan_index,
                "phase": "prepare",
                "estimated_input_tokens": prepared.metadata["estimated_input_tokens"],
                "pressure": prepared.metadata["pressure"],
                "surface_generation": prepared.surface_generation,
            }
        )
        if step.get("final"):
            ledger.append("assistant/message", {"content": str(step["final"])})
            break
        tool_steps += 1
        call_id = f"call-{condition}-{plan_index}-{uuid.uuid4().hex[:6]}"
        ledger.append("assistant/message", {"content": f"tool call {step.get('tool', '')}"})
        ledger.append("tool/call", {"name": str(step.get("tool", "")), "call_id": call_id, "args": dict(step.get("args") or {})})
        result = execute_scripted_tool(workspace, step)
        result["call_id"] = call_id
        ledger.append("tool/result", result)
        steps.append({"step": plan_index, "phase": "tool", "tool": step.get("tool", "")})

    context = context_verifier(task, ledger, hidden)
    artifact = artifact_verifier(task, workspace, hidden)
    mechanics = mechanics_verifier(task, ledger)
    pressure_events = [event for event in ledger.events if event.type == "context/pressure"]
    crossed = any(bool(event.data.get("initial_pressure")) for event in pressure_events)
    no_safe_range = stop_reason.startswith("preparation_failed:no_safe_range")
    return {
        "task_id": task["id"],
        "condition": condition,
        "budget": int(budget),
        "stop_reason": stop_reason,
        "tool_steps": tool_steps,
        "pressure_crossed": crossed,
        "no_safe_range": no_safe_range,
        "context": context,
        "artifact": artifact,
        "mechanics": mechanics,
        "hidden_isolated": hidden_verifier_isolation(workspace, hidden),
        "steps": steps,
        "events_written": len(ledger.events),
    }


def write_pilot_artifacts(*, output_dir: str | Path, rows: list[dict[str, Any]], taskset: dict[str, Any]) -> Path:
    output = Path(output_dir)
    output.mkdir(parents=True, exist_ok=True)
    (output / "tasks.snapshot.json").write_text(json.dumps(taskset, ensure_ascii=False, indent=2), encoding="utf-8")
    (output / "pilot-rows.json").write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    return output
