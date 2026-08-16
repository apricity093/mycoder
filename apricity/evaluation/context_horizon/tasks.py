"""Task schema and deterministic seed construction for Context Horizon v3."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from ...context import SessionLedger, canonical_json

TASK_SCHEMA_VERSION = 1
MAIN_BUDGET = 4096


class TaskSetError(ValueError):
    pass


def _read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def load_taskset(path: str | Path, *, repo_root: str | Path) -> dict[str, Any]:
    taskset = _read_json(Path(path))
    if int(taskset.get("schema_version", 0)) != TASK_SCHEMA_VERSION:
        raise TaskSetError(f"unsupported taskset schema_version: {taskset.get('schema_version')!r}")
    fixture_repo = Path(str(taskset.get("fixture_repo", "")))
    if not fixture_repo.is_absolute():
        fixture_repo = Path(repo_root) / fixture_repo
    if not fixture_repo.is_dir():
        raise TaskSetError(f"fixture repo does not exist: {fixture_repo}")
    tasks = list(taskset.get("tasks") or [])
    if not tasks:
        raise TaskSetError("taskset contains no tasks")
    ids = []
    for task in tasks:
        task_id = str(task.get("id", "")).strip()
        if not task_id:
            raise TaskSetError("task id is required")
        if task_id in ids:
            raise TaskSetError(f"duplicate task id: {task_id}")
        ids.append(task_id)
        if int(task.get("step_budget", 0)) <= 0:
            raise TaskSetError(f"{task_id}: step_budget must be positive")
        spec = task.get("seed_spec") or {}
        if int(spec.get("pre_steps", 0)) < 0:
            raise TaskSetError(f"{task_id}: seed_spec.pre_steps must be non-negative")
        if int(spec.get("noise_lines", 0)) <= 0:
            raise TaskSetError(f"{task_id}: seed_spec.noise_lines must be positive")
    taskset["fixture_repo_abs"] = str(fixture_repo)
    taskset["task_snapshot_hash"] = hashlib.sha256(
        canonical_json({"schema_version": TASK_SCHEMA_VERSION, "tasks": tasks}).encode("utf-8")
    ).hexdigest()
    return taskset


def task_by_id(taskset: dict[str, Any], task_id: str) -> dict[str, Any]:
    for task in taskset["tasks"]:
        if task["id"] == task_id:
            return task
    raise TaskSetError(f"unknown task id: {task_id}")


def load_hidden_verifier(task: dict[str, Any], hidden_root: str | Path) -> dict[str, Any]:
    verifier_id = str(task.get("hidden_verifier_id", ""))
    if not verifier_id:
        raise TaskSetError(f"{task['id']}: hidden_verifier_id is required")
    path = (Path(hidden_root) / f"{verifier_id}.json").resolve()
    if not path.is_file():
        raise TaskSetError(f"hidden verifier not found: {path}")
    payload = _read_json(path)
    expected_id = verifier_id.split("/", 1)[-1]
    if payload.get("id") != expected_id:
        raise TaskSetError(f"hidden verifier id mismatch for {task['id']}: expected {expected_id}")
    return payload


def build_seed_events(task: dict[str, Any]) -> list[dict[str, Any]]:
    """Deterministically build pre-seeded completed tool steps.

    Hidden verifier predicates are never consulted here. Facts in the seed are
    public task evidence that the eventual agent is expected to discover.
    """
    spec = task.get("seed_spec") or {}
    pre_steps = int(spec.get("pre_steps", 0))
    noise_lines = int(spec.get("noise_lines", 1))
    template = str(spec.get("line_template", "noise {step} {index}"))
    facts = list(spec.get("facts") or [])
    positions = list(spec.get("fact_positions") or [])
    events: list[dict[str, Any]] = []
    for step in range(pre_steps):
        events.append({"type": "user/message", "data": {"content": f"Investigation step {step + 1}"}})
        events.append({"type": "assistant/message", "data": {"content": "noted"}})
        events.append(
            {
                "type": "tool/call",
                "data": {"name": "read_file", "call_id": f"seed-call-{step}", "args": {"path": f"seed_{step}.txt", "start": 1, "end": 200}},
            }
        )
        lines = [template.format(step=step, index=index) for index in range(noise_lines)]
        for fact_index, (fact, position) in enumerate(zip(facts, positions)):
            if not fact:
                continue
            fact_step = fact_index % max(1, pre_steps)
            if step != fact_step:
                continue
            if position == "early":
                at = min(1, len(lines) - 1)
            elif position == "middle":
                at = len(lines) // 2
            else:
                at = max(0, len(lines) - 3)
            lines[at] = f"{lines[at]} :: {fact}"
        events.append(
            {
                "type": "tool/result",
                "data": {
                    "tool_name": "read_file",
                    "call_id": f"seed-call-{step}",
                    "content": "\n".join(lines),
                    "metadata": {"status": "ok", "exit_code": 0},
                },
            }
        )
    return events


def append_seed_events(ledger: SessionLedger, events: list[dict[str, Any]]) -> None:
    for event in events:
        ledger.append(event["type"], dict(event["data"]))


def seed_surface_chars(task: dict[str, Any]) -> int:
    from ...context import render_surface_node

    events = build_seed_events(task)
    return sum(len(render_surface_node(type("N", (), {"seq": i, "event_type": e["type"], "data": e["data"]})())) for i, e in enumerate(events))


def freeze_seed_files(taskset: dict[str, Any], seeds_root: str | Path) -> dict[str, str]:
    root = Path(seeds_root)
    root.mkdir(parents=True, exist_ok=True)
    hashes: dict[str, str] = {}
    for task in taskset["tasks"]:
        events = build_seed_events(task)
        payload = "\n".join(json.dumps(event, ensure_ascii=False, sort_keys=True) for event in events) + "\n"
        path = root / f"{task['id']}.jsonl"
        path.write_text(payload, encoding="utf-8")
        hashes[task["id"]] = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return hashes
