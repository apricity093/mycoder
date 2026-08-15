"""Balanced schedule, workspace isolation, persistence, and resume for the
context-compaction experiment.

The runner owns how Runs are scheduled and persisted; it does NOT implement
Surface replay, compaction, or report prose. Hidden verifiers and hidden
oracles are loaded from a registry outside the Agent-visible workspace and
are never copied into fixture copies, so ``list_files``/``read_file``/``search``
and the model prompt cannot reach them.
"""

from __future__ import annotations

import itertools
import json
import os
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter

from ...context import (
    ContextPolicy,
    ContextPreparationError,
    DeterministicTestMeter,
    LegacyContextEngine,
    ReplayableContextEngine,
)
from ...context.ledger import render_surface_node
from ...providers.clients import FakeModelClient
from .metrics import aggregate_rows, condition_delta
from .pressure import load_seed_events, prepare_ledger_from_seed
from .report import render_report
from .schema import CONDITION_IDS, canonical_hash, load_taskset
from .verifiers import verify_run

MAIN_BUDGET = 4096
SENSITIVITY_BUDGET = 8192


def condition_policies(budget: int) -> dict[str, ContextPolicy]:
    common = {"effective_input_budget": budget}
    return {
        "A": ContextPolicy(**common),
        "B": ContextPolicy(token_budgeting=True, **common),
        "C": ContextPolicy(token_budgeting=True, tool_pruning=True, **common),
        "D": ContextPolicy(token_budgeting=True, tool_pruning=True, history_compaction=True, **common),
    }


def balanced_condition_order(seed: int = 0) -> list[str]:
    """Deterministic balanced order of A/B/C/D derived from a seed."""
    order = list(CONDITION_IDS)
    # Rotate by the seed so successive blocks lead with different conditions.
    return order[seed % len(order):] + order[: seed % len(order)]


def build_schedule(tasks, *, repetitions: int, budgets=(MAIN_BUDGET,), sensitivity_tasks=()):
    """Balanced (task_id, repetition, budget) blocks with interleaved
    conditions. Sensitivity tasks also run under the 8192 budget with A/D."""
    blocks = []
    block_index = 0
    for budget in budgets:
        for repetition in range(1, repetitions + 1):
            for task in tasks:
                if budget == SENSITIVITY_BUDGET and task["id"] not in sensitivity_tasks:
                    continue
                # Rotate the leading condition per block so A/B/C/D are
                # approximately balanced across first/second/third/fourth
                # positions over all tasks.
                conditions = (
                    ["A", "D"] if budget == SENSITIVITY_BUDGET
                    else balanced_condition_order(seed=block_index)
                )
                for condition in conditions:
                    blocks.append(
                        {
                            "task_id": task["id"],
                            "condition": condition,
                            "repetition": repetition,
                            "budget": budget,
                            "order_index": len(blocks),
                        }
                    )
                block_index += 1
    schedule_hash = canonical_hash(
        [
            (block["task_id"], block["condition"], block["repetition"], block["budget"])
            for block in blocks
        ]
    )
    return blocks, schedule_hash


class ContextCompactionRunner:
    def __init__(
        self,
        *,
        taskset_path,
        output_dir,
        workspace_root,
        seed_root=None,
        hidden_root=None,
        repo_root=None,
        repetitions=1,
        budgets=(MAIN_BUDGET,),
        sensitivity_tasks=(),
        model_client_factory=None,
        summarizer_factory=None,
        resume=False,
        allow_dirty=False,
        real=False,
        verify_artifacts=False,
        task_ids=None,
    ):
        self.taskset_path = Path(taskset_path)
        self.output_dir = Path(output_dir)
        self.workspace_root = Path(workspace_root)
        self.repo_root = Path(repo_root or self.taskset_path.resolve().parent.parent.parent)
        self.seed_root = Path(seed_root or (self.repo_root / "benchmarks" / "context_compaction" / "seeds"))
        self.hidden_root = Path(hidden_root or (self.repo_root / "benchmarks" / "context_compaction" / "hidden"))
        self.repetitions = int(repetitions)
        self.budgets = tuple(int(budget) for budget in budgets)
        self.sensitivity_tasks = tuple(sensitivity_tasks)
        self.model_client_factory = model_client_factory
        self.summarizer_factory = summarizer_factory
        self.resume = bool(resume)
        self.allow_dirty = bool(allow_dirty)
        self.real = bool(real)
        self.verify_artifacts = bool(verify_artifacts)
        self.task_ids = tuple(str(task_id) for task_id in (task_ids or ()))

        self.taskset = load_taskset(self.taskset_path, repo_root=self.repo_root, seed_root=self.seed_root)
        all_tasks_by_id = {task["id"]: task for task in self.taskset["tasks"]}
        unknown_task_ids = sorted(set(self.task_ids) - set(all_tasks_by_id))
        if unknown_task_ids:
            raise ValueError(f"unknown task ids: {unknown_task_ids}")
        selected_tasks = (
            [all_tasks_by_id[task_id] for task_id in self.task_ids]
            if self.task_ids
            else list(self.taskset["tasks"])
        )
        self.tasks_by_id = {task["id"]: task for task in selected_tasks}
        self.blocks, self.schedule_hash = build_schedule(
            selected_tasks,
            repetitions=self.repetitions,
            budgets=self.budgets,
            sensitivity_tasks=self.sensitivity_tasks,
        )
        self.config_hash = canonical_hash(
            {
                "repetitions": self.repetitions,
                "budgets": self.budgets,
                "sensitivity_tasks": self.sensitivity_tasks,
                "task_snapshot_hash": self.taskset["task_snapshot_hash"],
                "allow_dirty": self.allow_dirty,
                "task_ids": self.task_ids,
            }
        )

    # ------------------------------------------------------------------
    # Hidden verifier isolation
    # ------------------------------------------------------------------

    def hidden_verifier(self, task) -> dict:
        verifier_id = str(task["hidden_verifier_id"]).strip()
        path = (self.hidden_root / f"{verifier_id}.json").resolve()
        try:
            path.relative_to(self.hidden_root.resolve())
        except ValueError as exc:
            raise ValueError(f"hidden verifier escapes hidden root: {verifier_id}") from exc
        if not path.is_file():
            raise ValueError(f"hidden verifier not found: {verifier_id}")
        return json.loads(path.read_text(encoding="utf-8"))

    def hidden_verifier_isolated(self, task, workspace_copy_root: Path) -> bool:
        """Confirm the hidden verifier payload never lands in the workspace."""
        hidden = self.hidden_verifier(task)
        hidden_text = json.dumps(hidden, sort_keys=True)
        oracle_secret = str(hidden.get("oracle_secret") or "")
        for path in workspace_copy_root.rglob("*"):
            if path.is_file():
                try:
                    text = path.read_text(encoding="utf-8", errors="ignore")
                except OSError:
                    continue
                if hidden_text[:80] in text or (oracle_secret and oracle_secret in text):
                    return False
        return True

    def run_public_verifier(self, task, workspace_copy_root: Path) -> dict:
        """Execute the task's public verifier against the workspace copy."""
        argv = task.get("public_verifier")
        if not argv:
            return {"exit_code": None, "stdout": "", "stderr": "no public verifier configured"}
        import subprocess as _subprocess
        import sys as _sys

        argv = [str(item) for item in argv]
        if argv[0] in {"python", "python3"}:
            argv[0] = _sys.executable
        verifier_env = dict(os.environ)
        verifier_env["PYTHONUTF8"] = "1"
        verifier_env["PYTHONIOENCODING"] = "utf-8"
        try:
            result = _subprocess.run(
                argv,
                cwd=workspace_copy_root,
                shell=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=verifier_env,
                timeout=int(task.get("verifier_timeout_seconds", 60)),
            )
            return {
                "exit_code": result.returncode,
                "stdout": result.stdout,
                "stderr": result.stderr,
            }
        except _subprocess.TimeoutExpired as exc:
            return {
                "exit_code": None,
                "stdout": exc.stdout or "",
                "stderr": f"public verifier timed out\n{exc.stderr or ''}",
            }

    def artifact_result_for(
        self,
        task,
        workspace_copy_root: Path,
        events: list[dict] | None = None,
        surface_text: str = "",
    ) -> dict:
        """Run public + hidden artifact verifiers and produce the three-layer
        artifact result. When artifact verification is disabled (deterministic
        gate), the dimension is recorded as not evaluated."""
        from .verifiers import artifact_verifier_pass

        if not self.verify_artifacts:
            return {"artifact_pass": None, "skipped": True}
        public = self.run_public_verifier(task, workspace_copy_root)
        hidden = self.hidden_verifier(task)
        return artifact_verifier_pass(
            workspace_copy_root=workspace_copy_root,
            task=task,
            hidden_verifier=hidden,
            public_verifier_result=public,
            events=events,
            surface_text=surface_text,
        )

    # ------------------------------------------------------------------
    # Per-Run execution
    # ------------------------------------------------------------------

    def run_block(self, block: dict) -> dict:
        task = self.tasks_by_id[block["task_id"]]
        condition = block["condition"]
        budget = int(block["budget"])
        repetition = int(block["repetition"])
        started = perf_counter()
        try:
            row = self._run_one(task, condition, budget, repetition)
        except ContextPreparationError as exc:
            # A typed no-safe-range outcome is a strategy result, not an
            # infrastructure failure.
            row = {
                "task_id": task["id"],
                "condition": condition,
                "repetition": repetition,
                "budget": budget,
                "run_pass": False,
                "runner_error": False,
                "stop_reason": f"preparation_failed:{exc.code}",
                "error": f"{type(exc).__name__}: {exc}",
            }
        except Exception as exc:
            row = {
                "task_id": task["id"],
                "condition": condition,
                "repetition": repetition,
                "budget": budget,
                "run_pass": False,
                "runner_error": True,
                "error": f"{type(exc).__name__}: {exc}",
            }
        row["duration_ms"] = int((perf_counter() - started) * 1000)
        return row

    def _run_one(self, task, condition, budget, repetition) -> dict:
        policy = condition_policies(budget)[condition]
        workspace_copy_root = self.workspace_root / task["id"] / f"{condition}-r{repetition:02d}"
        if workspace_copy_root.exists():
            shutil.rmtree(workspace_copy_root)
        workspace_copy_root.mkdir(parents=True, exist_ok=True)
        fixture_source = self.repo_root / task["fixture_repo"]
        shutil.copytree(fixture_source, workspace_copy_root / fixture_source.name)

        if not self.hidden_verifier_isolated(task, workspace_copy_root):
            raise ValueError(f"hidden verifier leaked into workspace for {task['id']}")

        if self.real:
            return self._run_real(task, condition, budget, repetition, workspace_copy_root)

        return self._run_deterministic(task, condition, budget, repetition, workspace_copy_root, policy)

    def _run_real(self, task, condition, budget, repetition, workspace_copy_root) -> dict:
        from .real_loop import RealAgentLoop

        loop = RealAgentLoop(
            task=task,
            condition=condition,
            budget=budget,
            repetition=repetition,
            workspace_copy_root=workspace_copy_root,
            seed_root=self.seed_root,
            model_client_factory=self.model_client_factory,
            provider=self.provider_name(),
            model=self.model_name(),
            max_new_tokens=2048,
            fault_plan=None,
            artifact_verifier=self.artifact_result_for,
        )
        return loop.run()

    @staticmethod
    def _protected_predicates(task: dict) -> tuple[str, ...]:
        """Critical-fact predicates that whole-unit removal must never drop."""
        predicates = []
        for fact in task.get("critical_facts", []):
            predicate = str(fact.get("predicate", "")).strip()
            if predicate:
                predicates.append(predicate)
        return tuple(predicates)

    def provider_name(self) -> str:
        try:
            from ...providers.config import resolve_runtime_config

            return resolve_runtime_config().provider
        except Exception:
            return "deepseek"

    def model_name(self) -> str:
        try:
            from ...providers.config import resolve_runtime_config

            return resolve_runtime_config().model
        except Exception:
            return "deepseek-v4-flash"

    def _run_deterministic(self, task, condition, budget, repetition, workspace_copy_root, policy) -> dict:
        ledger = prepare_ledger_from_seed(
            self.seed_root / task["seed_event_log"],
            session_id=f"{task['id']}-{condition}-r{repetition}",
            workspace_root=str(workspace_copy_root),
        )
        fixture_copy_root = workspace_copy_root / Path(task["fixture_repo"]).name
        ledger.persist_path = workspace_copy_root / ".apricity" / "sessions" / f"{ledger.session_id}.json"
        provider = "deepseek"
        model = "deepseek-v4-flash"
        system_prompt = "You are apricity, a small local coding agent."
        if condition == "A":
            engine = LegacyContextEngine(
                ledger=ledger,
                provider=provider,
                model=model,
                temperature=0.0,
                max_output_tokens=512,
                system_prompt=system_prompt,
                tools=[],
                memory_text="Memory:\n- task: -",
                user_message=task["prompt"],
            )
        else:
            meter = DeterministicTestMeter()
            engine = ReplayableContextEngine(
                ledger=ledger,
                policy=policy,
                provider=provider,
                model=model,
                temperature=0.0,
                max_output_tokens=512,
                system_prompt=system_prompt,
                tools=[],
                memory_text="Memory:\n- task: -",
                meter=meter,
                summarizer=self.summarizer_factory(task, ledger) if self.summarizer_factory else None,
                protected_predicates=self._protected_predicates(task),
            )
        # Dispatch the first request for the run.
        prepared = engine.prepare(task["prompt"], trigger="pressure")
        events = ledger.events()
        requests = [
            event for event in events if event["type"] == "request/dispatch"
        ]
        surface_text = ledger.surface_rendering()
        artifact_result = self.artifact_result_for(task, fixture_copy_root)
        verification = verify_run(
            events=events,
            trace_events=[],
            surface_text=surface_text,
            user_request=task["prompt"],
            critical_facts=task["critical_facts"],
            process_invariants=task["process_invariants"],
            artifact_result=artifact_result,
        )
        compaction_ends = [e for e in events if e["type"] == "compaction/end"]
        return {
            "task_id": task["id"],
            "condition": condition,
            "repetition": repetition,
            "budget": budget,
            "status": "pass" if verification["run_pass"] is True else ("skipped" if verification["run_pass"] is None else "fail"),
            "run_pass": verification["run_pass"],
            "mechanics_pass": bool(verification["mechanics_pass"]),
            "artifact_pass": verification["artifact_pass"],
            "artifact": verification["artifact"],
            "context_pass": bool(verification["context_pass"]),
            "trace_pass": bool(verification["trace_pass"]),
            "trace_checks": verification["trace_checks"],
            "context_fact_retention": verification["context"]["fact_retention"],
            "context_required_facts": verification["context"]["required_facts"],
            "observed_overflow": False,
            "recovered_overflow": False,
            "runner_error": False,
            "surface_generation": prepared.surface_generation,
            "estimated_input_tokens": prepared.metadata.get("estimated_input_tokens", 0),
            "compaction_count": sum(1 for e in compaction_ends if e["data"].get("success")),
            "summary_total_claims": 0,
            "summary_error_claims": 0,
            "tool_steps": 0,
            "attempts": 1,
            "canonical_request_sha256": prepared.canonical_request_sha256,
            "source_event_seqs": list(prepared.source_event_seqs),
            "workspace_root": str(workspace_copy_root),
            "events": events,
            "requests": requests,
            "surface_text": surface_text,
            "verifier": verification,
            "policy": policy.as_dict(),
        }

    def persist_run(self, block: dict, row: dict) -> Path:
        run_dir = (
            self.output_dir
            / "runs"
            / f"{row['condition']}-{row['task_id']}-r{row['repetition']:02d}-b{int(row['budget'])}"
        )
        run_dir.mkdir(parents=True, exist_ok=True)
        report = {
            key: value for key, value in row.items()
            if key not in {"events", "trace_events", "surface_text", "verifier"}
        }
        report["verifier"] = row.get("verifier", {})
        (run_dir / "report.json").write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
        with (run_dir / "events.jsonl").open("w", encoding="utf-8") as handle:
            for event in row.get("events", []):
                handle.write(json.dumps(event, sort_keys=True, ensure_ascii=True))
                handle.write("\n")
        with (run_dir / "requests.jsonl").open("w", encoding="utf-8") as handle:
            for request in row.get("requests", []):
                handle.write(json.dumps(request, sort_keys=True, ensure_ascii=True))
                handle.write("\n")
        with (run_dir / "trace.jsonl").open("w", encoding="utf-8") as handle:
            for event in row.get("trace_events", []):
                handle.write(json.dumps(event, sort_keys=True, ensure_ascii=True))
                handle.write("\n")
        (run_dir / "verifier.json").write_text(
            json.dumps(row.get("verifier", {}), indent=2, sort_keys=True), encoding="utf-8"
        )
        return run_dir

    # ------------------------------------------------------------------
    # Resume
    # ------------------------------------------------------------------

    def validate_resume(self) -> None:
        manifest_path = self.output_dir / "manifest.json"
        if not manifest_path.exists():
            return
        existing = json.loads(manifest_path.read_text(encoding="utf-8"))
        mismatches = []
        if existing.get("task_snapshot_hash") != self.taskset["task_snapshot_hash"]:
            mismatches.append("task snapshot hash")
        if existing.get("schedule_hash") != self.schedule_hash:
            mismatches.append("schedule hash")
        if existing.get("config_hash") != self.config_hash:
            mismatches.append("experiment config hash")
        if existing.get("repetitions") != self.repetitions or existing.get("budgets") != list(self.budgets):
            mismatches.append("repetitions or budget")
        if mismatches:
            raise ValueError(f"cannot resume: mismatch in {', '.join(mismatches)}")

    # ------------------------------------------------------------------
    # Orchestration
    # ------------------------------------------------------------------

    def run(self) -> dict:
        self.output_dir.mkdir(parents=True, exist_ok=True)
        if self.resume:
            self.validate_resume()
        dirty = bool(self._git_value("status", "--porcelain"))
        if dirty and not self.allow_dirty:
            raise RuntimeError(
                "context-compaction run requires a clean worktree; "
                "use allow_dirty=True only for non-canonical diagnostics"
            )
        manifest = {
            "schema_version": 1,
            "experiment_id": "context-compaction-" + datetime.now(timezone.utc).strftime("%Y-%m-%d-%H%M%S"),
            "status": "running",
            "captured_at": datetime.now(timezone.utc).isoformat(),
            "commit_sha": self._git_value("rev-parse", "HEAD"),
            "branch": self._git_value("branch", "--show-current"),
            "dirty": dirty,
            # PRD §23.2: a dirty worktree may run with an explicit override, but
            # its manifest MUST be marked non-canonical and excluded from
            # canonical aggregates.
            "canonical": not dirty,
            "canonical_override_requested": bool(self.allow_dirty),
            "task_snapshot_hash": self.taskset["task_snapshot_hash"],
            "fixture_hashes": {
                task["id"]: task["fixture_tree_hash"] for task in self.taskset["tasks"]
            },
            "pressure_hashes": {
                task["id"]: task["seed_hash"] for task in self.taskset["tasks"]
            },
            "config_hash": self.config_hash,
            "schedule_hash": self.schedule_hash,
            "repetitions": self.repetitions,
            "budgets": list(self.budgets),
            "token_meter": {"name": "deterministic-test", "version": "v1", "exact": True},
        }
        (self.output_dir / "manifest.json").write_text(
            json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8"
        )
        (self.output_dir / "tasks.snapshot.json").write_text(
            json.dumps(self.taskset, indent=2, sort_keys=True), encoding="utf-8"
        )
        (self.output_dir / "schedule.json").write_text(
            json.dumps(self.blocks, indent=2, sort_keys=True), encoding="utf-8"
        )

        rows = []
        for block in self.blocks:
            run_dir = (
                self.output_dir
                / "runs"
                / f"{block['condition']}-{block['task_id']}-r{block['repetition']:02d}-b{int(block['budget'])}"
            )
            if self.resume and run_dir.exists() and (run_dir / "report.json").exists():
                report = json.loads((run_dir / "report.json").read_text(encoding="utf-8"))
                report["events"] = [
                    json.loads(line)
                    for line in (run_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
                ]
                report["verifier"] = json.loads((run_dir / "verifier.json").read_text(encoding="utf-8"))
                rows.append(report)
                continue
            row = self.run_block(block)
            self.persist_run(block, row)
            rows.append(row)

        aggregate = aggregate_rows(rows)
        payload = {
            **manifest,
            "status": "completed",
            "completed_at": datetime.now(timezone.utc).isoformat(),
            "aggregate": aggregate,
            "comparison": condition_delta(aggregate),
            "rows": [
                {key: value for key, value in row.items() if key not in {"events", "requests", "surface_text", "verifier"}}
                for row in rows
            ],
        }
        (self.output_dir / "aggregate.json").write_text(
            json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8"
        )
        (self.output_dir / "report.md").write_text(render_report(payload), encoding="utf-8")
        return payload

    def _git_value(self, *args):
        try:
            result = subprocess.run(
                ["git", *args],
                cwd=self.repo_root,
                capture_output=True,
                text=True,
                check=True,
                timeout=5,
            )
            return result.stdout.strip()
        except Exception:
            return ""
