"""Strict input and artifact validation for the context-compaction benchmark.

Task definitions use schema v2 and MUST NOT select A/B/C/D behavior. Unknown
condition-specific fields are rejected. Every task/fixture/pressure/config/
schedule payload is reduced to an immutable SHA-256 hash for resume checks and
manifest integrity.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

TASK_SCHEMA_VERSION = 2
ARTIFACT_SCHEMA_VERSION = 1

REQUIRED_TASK_KEYS = {
    "id",
    "suite",
    "task_style",
    "category",
    "prompt",
    "fixture_repo",
    "seed_event_log",
    "allowed_tools",
    "step_budget",
    "pressure_profile",
    "critical_facts",
    "public_verifier",
    "hidden_verifier_id",
    "process_invariants",
}
ALLOWED_TASK_KEYS = REQUIRED_TASK_KEYS | {
    "artifact_path",
    "verifier_timeout_seconds",
    "checkpoint_id",
}
VALID_TASK_STYLES = {"controlled", "realistic"}
VALID_INVARIANTS = {
    "tool_pairs_balanced",
    "current_request_preserved",
    "request_replayable",
    "partial_transaction_zero",
    "duplicate_side_effect_zero",
}
VALID_CATEGORIES = {
    "early-fact",
    "middle-evidence",
    "tail-evidence",
    "stale-conflict",
    "tool-pair-boundary",
    "repeated-compaction",
    "oversized-result-selection",
    "overflow-recovery",
    "realistic",
}
CONDITION_IDS = ("A", "B", "C", "D")


def sha256_of_bytes(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def sha256_of_text(text: str) -> str:
    return sha256_of_bytes(str(text).encode("utf-8"))


def canonical_hash(payload) -> str:
    return sha256_of_text(json.dumps(payload, sort_keys=True, ensure_ascii=True, separators=(",", ":")))


def _normalized_allowed_tools(allowed_tools):
    normalized = [str(name).strip() for name in allowed_tools]
    if not normalized or any(not name for name in normalized):
        raise ValueError("allowed_tools must be a non-empty list of tool names")
    return sorted(normalized)


def validate_task(task: dict, *, repo_root: Path, seed_root: Path) -> dict:
    if not isinstance(task, dict):
        raise ValueError("task must be a mapping")
    unknown = set(task) - ALLOWED_TASK_KEYS
    if unknown:
        raise ValueError(f"task has unknown keys: {', '.join(sorted(unknown))}")
    missing = REQUIRED_TASK_KEYS - set(task)
    if missing:
        raise ValueError(f"task is missing required keys: {', '.join(sorted(missing))}")

    task_id = str(task["id"]).strip()
    if not task_id:
        raise ValueError("task id must not be empty")
    if str(task["suite"]).strip() != "context-compaction":
        raise ValueError(f"task {task_id} suite must be 'context-compaction'")
    task_style = str(task["task_style"]).strip()
    if task_style not in VALID_TASK_STYLES:
        raise ValueError(f"task {task_id} has invalid task_style: {task_style}")
    category = str(task["category"]).strip()
    if category not in VALID_CATEGORIES:
        raise ValueError(f"task {task_id} has invalid category: {category}")

    fixture_repo = (repo_root / str(task["fixture_repo"])).resolve()
    if not fixture_repo.is_dir():
        raise ValueError(f"task {task_id} fixture repo does not exist: {task['fixture_repo']}")
    seed_path = (seed_root / str(task["seed_event_log"])).resolve()
    if not seed_path.is_file():
        raise ValueError(f"task {task_id} seed event log does not exist: {task['seed_event_log']}")

    normalized_allowed = _normalized_allowed_tools(task["allowed_tools"])
    step_budget = int(task["step_budget"])
    if step_budget < 1:
        raise ValueError(f"task {task_id} step_budget must be positive")

    pressure = task.get("pressure_profile")
    if not isinstance(pressure, dict) or not pressure:
        raise ValueError(f"task {task_id} pressure_profile must be a non-empty mapping")
    if not isinstance(pressure.get("target_input_tokens"), int) or int(pressure["target_input_tokens"]) <= 0:
        raise ValueError(f"task {task_id} pressure_profile.target_input_tokens must be positive")

    facts = task.get("critical_facts")
    if not isinstance(facts, list) or not facts:
        raise ValueError(f"task {task_id} critical_facts must be a non-empty list")
    for fact in facts:
        if not isinstance(fact, dict):
            raise ValueError(f"task {task_id} critical_facts entries must be mappings")
        for key in ("id", "predicate", "contradiction_predicate"):
            if key not in fact:
                raise ValueError(f"task {task_id} critical_facts entry is missing {key}")

    process_invariants = task.get("process_invariants")
    if not isinstance(process_invariants, list) or not process_invariants:
        raise ValueError(f"task {task_id} process_invariants must be a non-empty list")
    invalid_invariants = set(process_invariants) - VALID_INVARIANTS
    if invalid_invariants:
        raise ValueError(f"task {task_id} has unknown process invariants: {sorted(invalid_invariants)}")

    normalized = dict(task)
    normalized["id"] = task_id
    normalized["task_style"] = task_style
    normalized["category"] = category
    normalized["allowed_tools"] = normalized_allowed
    normalized["step_budget"] = step_budget
    normalized["process_invariants"] = sorted(process_invariants)
    task_hash_fields = {key: normalized[key] for key in sorted(REQUIRED_TASK_KEYS)}
    task_hash_fields["artifact_path"] = normalized.get("artifact_path", "")
    task_hash_fields["verifier_timeout_seconds"] = int(normalized.get("verifier_timeout_seconds", 60))
    normalized["task_hash"] = canonical_hash(task_hash_fields)
    normalized["critical_facts_hash"] = canonical_hash(facts)
    normalized["fixture_tree_hash"] = fixture_tree_hash(fixture_repo)
    normalized["seed_hash"] = sha256_of_bytes(seed_path.read_bytes())
    return normalized


def fixture_tree_hash(fixture_repo: Path) -> str:
    sha = hashlib.sha256()
    for path in sorted((item for item in fixture_repo.rglob("*") if item.is_file()), key=lambda p: str(p.relative_to(fixture_repo))):
        sha.update(str(path.relative_to(fixture_repo)).encode("utf-8"))
        sha.update(b"\0")
        sha.update(path.read_bytes())
        sha.update(b"\0")
    return "sha256:" + sha.hexdigest()


def validate_taskset(data: dict, *, repo_root: Path, seed_root: Path) -> dict:
    if not isinstance(data, dict):
        raise ValueError("task set must be a mapping")
    if int(data.get("schema_version", 0)) != TASK_SCHEMA_VERSION:
        raise ValueError("unsupported task schema_version")
    unknown = set(data) - {"schema_version", "description", "tasks"}
    if unknown:
        raise ValueError(f"task set has unknown keys: {', '.join(sorted(unknown))}")
    tasks = data.get("tasks")
    if not isinstance(tasks, list) or not tasks:
        raise ValueError("tasks must be a non-empty list")
    seen_ids = set()
    controlled = 0
    realistic = 0
    normalized_tasks = []
    for task in tasks:
        normalized = validate_task(task, repo_root=repo_root, seed_root=seed_root)
        if normalized["id"] in seen_ids:
            raise ValueError(f"duplicate task id: {normalized['id']}")
        seen_ids.add(normalized["id"])
        if normalized["task_style"] == "controlled":
            controlled += 1
        else:
            realistic += 1
        normalized_tasks.append(normalized)
    if controlled < 8:
        raise ValueError(f"task set must include at least 8 controlled tasks, found {controlled}")
    if realistic < 4:
        raise ValueError(f"task set must include at least 4 realistic tasks, found {realistic}")
    return {
        "schema_version": TASK_SCHEMA_VERSION,
        "description": str(data.get("description", "")),
        "tasks": normalized_tasks,
        "task_snapshot_hash": canonical_hash(
            [(task["id"], task["task_hash"]) for task in normalized_tasks]
        ),
    }


def load_taskset(path: Path, *, repo_root: Path, seed_root: Path) -> dict:
    path = Path(path)
    data = json.loads(path.read_text(encoding="utf-8"))
    return validate_taskset(data, repo_root=repo_root, seed_root=seed_root)
