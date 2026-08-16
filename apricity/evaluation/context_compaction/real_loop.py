"""Ledger-driven real-model agent loop for the paid pilot.

This loop drives a real provider model through the ContextEngine seam for one
benchmark Run. It is deliberately separate from the legacy ``AgentLoop`` so
the experiment runs A/B/C/D on the identical ledger runtime without changing
the 246-test legacy path. It reuses the existing ``ToolExecutor`` and recovery
facade for tool safety, but the ledger is the sole source of model-visible
state.
"""

from __future__ import annotations

import uuid
from pathlib import Path

from ...context import (
    ContextPolicy,
    ContextPreparationError,
    LegacyContextEngine,
    ReplayableContextEngine,
)
from ...context.summarizer import RealModelSummarizer, SummaryUsage
from ...context.token_meter import DeterministicTestMeter
from ...evaluation.faults import FaultInjectingModelClient
from ...runtime import Apricity
from ...session_store import SessionStore
from ...run_store import RunStore
from ...workspace import WorkspaceContext
from .pressure import prepare_ledger_from_seed
from .verifiers import verify_run


def _tool_event_status(metadata: dict) -> str:
    return str(metadata.get("tool_status", "")).strip() or "ok"


class RealAgentLoop:
    """One real-model Run over the ledger, bounded by step/attempt budgets."""

    MAX_ATTEMPTS_PER_STEP = 4

    def __init__(
        self,
        *,
        task: dict,
        condition: str,
        budget: int,
        repetition: int,
        workspace_copy_root: Path,
        seed_root: Path,
        model_client_factory,
        provider: str,
        model: str,
        max_new_tokens: int = 2048,
        request_timeout: int = 300,
        fault_plan=None,
        artifact_verifier=None,
    ):
        self.task = task
        self.condition = condition
        self.budget = int(budget)
        self.repetition = int(repetition)
        self.workspace_copy_root = Path(workspace_copy_root)
        self.artifact_verifier = artifact_verifier or (
            lambda task, root, events=None, surface_text="": {"artifact_pass": None, "skipped": True}
        )
        self.seed_root = Path(seed_root)
        self.model_client_factory = model_client_factory
        self.provider = provider
        self.model = model
        self.max_new_tokens = int(max_new_tokens)
        self.request_timeout = int(request_timeout)
        self.fault_plan = fault_plan

        self.ledger = prepare_ledger_from_seed(
            self.seed_root / task["seed_event_log"],
            session_id=f"{task['id']}-{condition}-pilot",
            workspace_root=str(workspace_copy_root),
        )
        fixture_root = workspace_copy_root / Path(task["fixture_repo"]).name
        self.fixture_root = fixture_root
        self.ledger.persist_path = workspace_copy_root / ".apricity" / "sessions" / f"{self.ledger.session_id}.json"
        self.workspace = WorkspaceContext.build(fixture_root, repo_root_override=fixture_root)
        self.session_store = SessionStore(fixture_root / ".apricity" / "sessions")
        self.run_store = RunStore(fixture_root / ".apricity" / "runs")
        self.model_client = self._wrap_client(model_client_factory())
        self.facade = Apricity(
            model_client=self.model_client,
            workspace=self.workspace,
            session_store=self.session_store,
            run_store=self.run_store,
            approval_policy="auto",
            max_steps=int(task["step_budget"]),
            max_new_tokens=self.max_new_tokens,
            allowed_tools=task["allowed_tools"],
            recovery_mode="compatible",
        )
        self.summary_usage = SummaryUsage()
        self.main_usage = SummaryUsage()
        self.trace_events: list[dict] = []
        self.engine = self._build_engine()
    def _wrap_client(self, client):
        if self.fault_plan is not None:
            return FaultInjectingModelClient(client, self.fault_plan)
        return client

    def _build_engine(self):
        policy = self._policy()
        system_prompt = "You are apricity, a small local coding agent."
        memory_text = "Memory:\n- task: -"
        tools = []
        if self.condition == "A":
            return LegacyContextEngine(
                ledger=self.ledger,
                provider=self.provider,
                model=self.model,
                temperature=0.0,
                max_output_tokens=self.max_new_tokens,
                system_prompt=system_prompt,
                tools=tools,
                memory_text=memory_text,
                user_message=self.task["prompt"],
            )
        meter = DeterministicTestMeter()
        summarizer = None
        if self.condition == "D":
            summarizer = RealModelSummarizer(self._wrap_client(self.model_client_factory()), self.summary_usage)
        return ReplayableContextEngine(
            ledger=self.ledger,
            policy=policy,
            provider=self.provider,
            model=self.model,
            temperature=0.0,
            max_output_tokens=self.max_new_tokens,
            system_prompt=system_prompt,
            tools=tools,
            memory_text=memory_text,
            meter=meter,
            summarizer=summarizer,
            protected_predicates=self._protected_predicates(),
        )

    def _protected_predicates(self) -> tuple[str, ...]:
        from .runner import ContextCompactionRunner

        return ContextCompactionRunner._protected_predicates(self.task)

    def _policy(self) -> ContextPolicy:
        from .runner import condition_policies

        return condition_policies(self.budget)[self.condition]

    def _record_main_usage(self):
        metadata = dict(getattr(self.model_client, "last_completion_metadata", {}) or {})
        self.main_usage.input_tokens += int(metadata.get("input_tokens", 0) or 0)
        self.main_usage.output_tokens += int(metadata.get("output_tokens", 0) or 0)
        self.main_usage.total_tokens += int(metadata.get("total_tokens", 0) or 0)
        self.main_usage.calls += 1

    def run(self) -> dict:
        step_budget = int(self.task["step_budget"])
        tool_steps = 0
        attempts = 0
        final_answer = ""
        recovered_overflow = False
        observed_overflow = False
        prepared = None
        turn_message = self.task["prompt"]

        while tool_steps < step_budget and attempts < step_budget * self.MAX_ATTEMPTS_PER_STEP:
            trigger = "pressure"
            try:
                prepared = self.engine.prepare(turn_message, trigger=trigger)
            except ContextPreparationError as exc:
                if self.condition in ("B", "C", "D") and self._rebuild_context(turn_message):
                    continue
                return self._fail_row(prepared, observed_overflow, recovered_overflow, f"preparation_failed:{exc.code}", final_answer, attempts, tool_steps)
            attempts += 1
            try:
                raw = self.model_client.complete(prepared.prompt, self.max_new_tokens)
            except Exception as exc:
                if getattr(exc, "kind", "") == "context_overflow":
                    observed_overflow = True
                    if self.condition in ("B", "C", "D") and self._rebuild_context(turn_message):
                        recovered_overflow = True
                        continue
                    return self._fail_row(prepared, observed_overflow, recovered_overflow, "context_overflow_unrecovered", final_answer, attempts, tool_steps)
                return self._fail_row(prepared, observed_overflow, recovered_overflow, "model_stop_failure", final_answer, attempts, tool_steps)
            self._record_main_usage()
            self._record_trace("model_requested", {"attempt": attempts, "condition": self.condition})

            kind, payload = self._parse(raw)
            if kind == "final":
                final_answer = str(payload).strip()
                self.engine.ledger.append("assistant/message", {"content": final_answer})
                break
            if kind == "retry":
                self.engine.ledger.append("assistant/message", {"content": str(payload)})
                continue
            if kind == "tool":
                if tool_steps >= step_budget:
                    break
                name = str(payload.get("name", ""))
                args = payload.get("args", {}) or {}
                call_id = f"{name}-{uuid.uuid4().hex[:8]}"
                # The model's raw assistant output for this tool call is part
                # of the conversation the next request must reconstruct; record
                # it before the structured call/result pair.
                self.engine.ledger.append("assistant/message", {"content": str(raw)})
                # Durable-before-execute: the tool call event precedes the run.
                self.engine.ledger.append(
                    "tool/call",
                    {"tool_name": name, "call_id": call_id, "args": args},
                )
                self.engine.ledger.flush_dispatch()
                result = self.facade.execute_tool(name, args)
                self.engine.ledger.append(
                    "tool/result",
                    {
                        "tool_name": name,
                        "call_id": call_id,
                        "content": result.content,
                        "tool_status": _tool_event_status(result.metadata),
                        "workspace_changed": bool((result.metadata or {}).get("workspace_changed", False)),
                    },
                )
                self.engine.ledger.flush_dispatch()
                self._record_trace(
                    "tool_executed",
                    {"name": name, "workspace_changed": bool((result.metadata or {}).get("workspace_changed", False))},
                )
                tool_steps += 1
                continue

        prepared = prepared or self.engine.prepare(turn_message)
        surface_text = self.ledger.surface_rendering()
        events = self.ledger.events()
        artifact_result = self._artifact_result()
        verification = verify_run(
            events=events,
            trace_events=self.trace_events,
            surface_text=surface_text,
            user_request=turn_message,
            critical_facts=self.task["critical_facts"],
            process_invariants=self.task["process_invariants"],
            artifact_result=artifact_result,
        )
        self.ledger.flush_dispatch()
        compaction_ends = [e for e in events if e["type"] == "compaction/end"]
        pressure_events = [e for e in events if e["type"] == "context/pressure"]
        # pressure_triggered records the initial-pressure flag of the first
        # context/pressure event, not whether any later turn experienced pressure.
        initial_pressure = bool(pressure_events and pressure_events[0]["data"].get("initial_pressure"))
        summary_claims = 0
        summary_errors = 0
        for event in events:
            if event["type"] == "context/summary" and "compaction_id" in event["data"]:
                summary_claims += 1
                if not event["data"].get("validated", True):
                    summary_errors += 1
        return {
            "task_id": self.task["id"],
            "condition": self.condition,
            "repetition": self.repetition,
            "budget": self.budget,
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
            "observed_overflow": observed_overflow,
            "recovered_overflow": recovered_overflow,
            "runner_error": False,
            "surface_generation": prepared.surface_generation,
            "estimated_input_tokens": prepared.metadata.get("estimated_input_tokens", 0),
            "pressure_triggered": initial_pressure,
            "compaction_count": sum(1 for e in compaction_ends if e["data"].get("success")),
            "summary_total_claims": summary_claims,
            "summary_error_claims": summary_errors,
            "tool_steps": tool_steps,
            "attempts": attempts,
            "final_answer": final_answer,
            "canonical_request_sha256": prepared.canonical_request_sha256,
            "source_event_seqs": list(prepared.source_event_seqs),
            "workspace_root": str(self.workspace_copy_root),
            "events": events,
            "trace_events": self.trace_events,
            "surface_text": surface_text,
            "verifier": verification,
            "policy": self._policy().as_dict(),
            "main_usage": self.main_usage.as_dict(),
            "summary_usage": self.summary_usage.as_dict(),
        }

    def _artifact_result(self) -> dict:
        return self.artifact_verifier(
            self.task,
            self.fixture_root,
            self.ledger.events(),
            self.ledger.surface_rendering(),
        )

    def _rebuild_context(self, turn_message: str) -> bool:
        """Rebuild context after a typed failure; require token progress."""
        before = self.ledger.count_surface_tokens(DeterministicTestMeter())
        try:
            self.engine.prepare(turn_message, trigger="context-overflow")
        except ContextPreparationError:
            return False
        after = self.ledger.count_surface_tokens(DeterministicTestMeter())
        return after < before * 0.8

    def _parse(self, raw):
        from ...runtime import Apricity as _A

        return _A.parse(raw)

    def _record_trace(self, event: str, payload: dict):
        self.trace_events.append({"event": event, **payload})

    def _fail_row(
        self,
        prepared,
        observed_overflow: bool,
        recovered_overflow: bool,
        reason: str,
        final_answer: str,
        attempts: int,
        tool_steps: int,
    ) -> dict:
        events = self.ledger.events()
        surface_text = self.ledger.surface_rendering()
        artifact_result = self._artifact_result()
        verification = verify_run(
            events=events,
            trace_events=self.trace_events,
            surface_text=surface_text,
            user_request=self.task["prompt"],
            critical_facts=self.task["critical_facts"],
            process_invariants=self.task["process_invariants"],
            artifact_result=artifact_result,
        )
        generation = prepared.surface_generation if prepared is not None else self.ledger.surface_generation
        compaction_ends = [e for e in events if e["type"] == "compaction/end"]
        pressure_events = [e for e in events if e["type"] == "context/pressure"]
        # pressure_triggered records the initial-pressure flag of the first
        # context/pressure event, not whether any later turn experienced pressure.
        initial_pressure = bool(pressure_events and pressure_events[0]["data"].get("initial_pressure"))
        summary_claims = 0
        summary_errors = 0
        for event in events:
            if event["type"] == "context/summary" and "compaction_id" in event["data"]:
                summary_claims += 1
                if not event["data"].get("validated", True):
                    summary_errors += 1
        return {
            "task_id": self.task["id"],
            "condition": self.condition,
            "repetition": self.repetition,
            "budget": self.budget,
            "status": "fail",
            "run_pass": False,
            "mechanics_pass": bool(verification["mechanics_pass"]),
            "artifact_pass": verification["artifact_pass"],
            "artifact": verification["artifact"],
            "context_pass": bool(verification["context_pass"]),
            "trace_pass": bool(verification["trace_pass"]),
            "trace_checks": verification["trace_checks"],
            "context_fact_retention": verification["context"]["fact_retention"],
            "context_required_facts": verification["context"]["required_facts"],
            "observed_overflow": observed_overflow,
            "recovered_overflow": recovered_overflow,
            "runner_error": False,
            "surface_generation": generation,
            "estimated_input_tokens": (prepared.metadata.get("estimated_input_tokens", 0) if prepared is not None else 0),
            "pressure_triggered": initial_pressure,
            "compaction_count": sum(1 for e in compaction_ends if e["data"].get("success")),
            "summary_total_claims": summary_claims,
            "summary_error_claims": summary_errors,
            "tool_steps": tool_steps,
            "attempts": attempts,
            "final_answer": final_answer,
            "stop_reason": reason,
            "canonical_request_sha256": (prepared.canonical_request_sha256 if prepared is not None else ""),
            "source_event_seqs": (list(prepared.source_event_seqs) if prepared is not None else []),
            "workspace_root": str(self.workspace_copy_root),
            "events": events,
            "trace_events": self.trace_events,
            "surface_text": surface_text,
            "verifier": verification,
            "policy": self._policy().as_dict(),
            "main_usage": self.main_usage.as_dict(),
            "summary_usage": self.summary_usage.as_dict(),
        }
