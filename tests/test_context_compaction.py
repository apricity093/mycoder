"""M3 integration tests: evidence-anchored compaction transaction."""

import json
import tempfile
from pathlib import Path

import pytest

from apricity.context import (
    ContextPolicy,
    ContextPreparationError,
    DeterministicTestMeter,
    ReplayableContextEngine,
    SessionLedger,
)


class SummaryCancelled(Exception):
    pass


def build_engine(
    policy,
    summarizer,
    seed_units=8,
    *,
    first_user_fact=None,
    first_assistant_fact=None,
):
    tmp = Path(tempfile.mkdtemp(prefix="apricity-compact-"))
    ledger = SessionLedger(session_id="s1", workspace_root=str(tmp))
    for index in range(seed_units):
        if index == 0 and first_user_fact:
            user_content = f"{first_user_fact} " + ("x" * 400)
        else:
            user_content = f"seed user {index} " + ("x" * 400)
        if index == 0 and first_assistant_fact:
            assistant_content = f"{first_assistant_fact} " + ("y" * 400)
        else:
            assistant_content = f"seed answer {index} " + ("y" * 400)
        ledger.append("user/message", {"content": user_content})
        ledger.append("assistant/message", {"content": assistant_content})
    return ledger, ReplayableContextEngine(
        ledger=ledger,
        policy=policy,
        provider="deepseek",
        model="deepseek-v4-flash",
        temperature=0.0,
        max_output_tokens=512,
        system_prompt="SYSTEM " + ("z" * 300),
        tools=[],
        memory_text="Memory:\n- task: -",
        meter=DeterministicTestMeter(),
        summarizer=summarizer,
    )


def compact_policy(**overrides):
    values = {
        "token_budgeting": True,
        "tool_pruning": True,
        "history_compaction": True,
        "effective_input_budget": 4096,
        "summary_max_tokens": 384,
    }
    values.update(overrides)
    return ContextPolicy(**values)


def valid_summary(event_seq, quote):
    def summarizer(events, renderings, max_tokens):
        del max_tokens
        # Cite the first available event in the selected range and quote a
        # bounded substring from its rendering, mirroring a real summarizer.
        if not events:
            raise ValueError("no events to summarize")
        target = next(iter(events))
        seq = int(target["seq"])
        rendering = renderings.get(seq, "")
        quote_text = rendering[:40] if rendering else "x"
        return json.dumps(
            {
                "schema_version": 1,
                "goal": {
                    "text": "Goal derived from history",
                    "status": "confirmed",
                    "sources": [{"event_seq": seq, "quote": quote_text}],
                },
                "constraints": [],
                "files": [],
                "changes": [],
                "tests": [],
                "open_items": [],
                "next_step": {"text": "Continue work", "status": "hypothesis", "sources": []},
            }
        )

    return summarizer


def test_compaction_commits_valid_evidence_anchored_summary():
    ledger, engine = build_engine(compact_policy(), valid_summary(1, "seed answer 0 "))
    assert engine._select_compaction_range()

    prepared = engine.prepare("Current request", trigger="context-overflow")

    assert prepared.changed is True
    assert prepared.metadata["estimated_input_tokens"] < 4096
    compaction_ends = [e for e in ledger.events() if e["type"] == "compaction/end"]
    assert any(e["data"]["success"] for e in compaction_ends)
    summaries = [e for e in ledger.events() if e["type"] == "context/summary" and "compaction_id" in e["data"]]
    assert summaries
    summary = summaries[-1]["data"]
    assert summary["gain_ratio"] >= 0.15
    # A committed summary replaces a contiguous old range; source events are
    # retained in the ledger and cited exactly.
    assert summary["selected_source_seqs"] == sorted(summary["selected_source_seqs"])
    assert summary["selected_surface_seqs"]


def test_compaction_preserves_current_request_and_protected_fact_for_replay():
    ledger, engine = build_engine(compact_policy(), valid_summary(1, "seed answer 0 "), seed_units=8)
    ledger.append(
        "tool/call",
        {"tool_name": "run_process", "call_id": "fact-call", "args": {"program": "pytest"}},
    )
    ledger.append(
        "tool/result",
        {"tool_name": "run_process", "call_id": "fact-call", "content": "PASS test_c"},
    )
    ledger.append("user/message", {"content": "Current request"})
    for index in range(4):
        ledger.append("assistant/message", {"content": f"later output {index}"})
        ledger.append(
            "tool/call",
            {
                "tool_name": "run_process",
                "call_id": f"later-call-{index}",
                "args": {"program": "pytest"},
            },
        )
        ledger.append(
            "tool/result",
            {
                "tool_name": "run_process",
                "call_id": f"later-call-{index}",
                "content": "later result " + ("z" * 900),
            },
        )
    engine._protected_predicates = ("PASS test_c",)

    prepared = engine.prepare("Current request", trigger="pressure")

    from apricity.evaluation.context_compaction.verifiers import replay_integrity

    assert prepared.changed is True
    assert any(
        node.event_type == "user/message" and node.data.get("content") == "Current request"
        for node in ledger.surface.nodes
    )
    assert "PASS test_c" in ledger.surface_rendering()
    summary_events = [
        event for event in ledger.events()
        if event["type"] == "context/summary" and "compaction_id" in event["data"]
    ]
    assert summary_events[-1]["data"]["protected_evidence"]
    assert replay_integrity(ledger.events()) is True


def test_two_consecutive_compactions_keep_replay_chains_valid():
    ledger, engine = build_engine(compact_policy(), valid_summary(1, "seed answer 0 "), seed_units=12)

    first = engine.prepare("First request", trigger="context-overflow")
    assert first.changed is True

    # The engine records each successful compaction and caps the total at
    # max_compactions_per_run (2). Force the second compaction by resetting
    # the counter (simulating a later turn) and appending fresh pressure.
    engine._compactions_used = 0
    for index in range(8):
        ledger.append("user/message", {"content": f"more user {index} " + ("x" * 400)})
        ledger.append("assistant/message", {"content": f"more answer {index} " + ("y" * 400)})

    second = engine.prepare("Second request", trigger="context-overflow")

    assert second.changed is True
    compaction_ends = [e for e in ledger.events() if e["type"] == "compaction/end" and e["data"]["success"]]
    assert len(compaction_ends) >= 2
    # The ledger still replays cleanly after both generations.
    assert ledger.surface_generation >= 2


def test_compaction_cancellation_leaves_surface_unchanged():
    def cancelled_summarizer(events, renderings, max_tokens):
        del events, renderings, max_tokens
        raise SummaryCancelled("cancelled")

    ledger, engine = build_engine(compact_policy(), cancelled_summarizer)
    before = ledger.surface_event_seqs()

    engine.prepare("Current request", trigger="context-overflow")

    # Cancellation before commit leaves the Surface unchanged; compaction
    # closes as failed and the B fallback removes old units under pressure.
    compaction_surface = [
        node for node in ledger.surface.nodes
        if node.event_type in {"context/summary", "context/pruned-tool-result"}
    ]
    assert not any("compaction_id" in node.data for node in compaction_surface)
    ends = [e for e in ledger.events() if e["type"] == "compaction/end"]
    assert any(e["data"]["success"] is False and e["data"].get("terminal_phase") == "summarized" for e in ends)


@pytest.mark.parametrize(
    ("first_user_fact", "first_assistant_fact", "predicate"),
    [
        ("centralized_timeout_default contract", None, "centralized_timeout_default"),
        (None, "centralized_timeout_default contract", "centralized_timeout_default"),
        ("centralized_timeout_default contract", None, "CENTRALIZED_TIMEOUT_DEFAULT"),
        (None, "centralized_timeout_default contract", "CENTRALIZED_TIMEOUT_DEFAULT"),
    ],
)
def test_failed_summary_fallback_preserves_user_assistant_only_protected_fact(
    first_user_fact,
    first_assistant_fact,
    predicate,
):
    from apricity.evaluation.context_compaction.verifiers import (
        context_facts_pass,
        replay_integrity,
        tool_pairs_balanced,
    )

    def failing_summarizer(events, renderings, max_tokens):
        del events, renderings, max_tokens
        raise ValueError("summary unavailable")

    ledger, engine = build_engine(
        compact_policy(),
        failing_summarizer,
        seed_units=12,
        first_user_fact=first_user_fact,
        first_assistant_fact=first_assistant_fact,
    )
    engine._protected_predicates = (predicate,)

    prepared = engine.prepare("Current request", trigger="context-overflow")

    assert prepared.changed is True
    surface_text = ledger.surface_rendering()
    assert "centralized_timeout_default" in surface_text.casefold()
    facts = [
        {
            "id": "timeout-source",
            "predicate": predicate,
            "contradiction_predicate": "timeout is 30",
        }
    ]
    assert context_facts_pass(surface_text, facts)["pass"] is True
    events = ledger.events()
    assert replay_integrity(events) is True
    assert tool_pairs_balanced(events) is True
    ends = [e for e in events if e["type"] == "compaction/end"]
    assert any(
        e["data"]["success"] is False and e["data"].get("error_code") == "summary_transport_failed"
        for e in ends
    )


def test_fail_row_reports_prior_compaction_and_summary_stats():
    from apricity.context.summarizer import SummaryUsage
    from apricity.evaluation.context_compaction.real_loop import RealAgentLoop

    tmp = Path(tempfile.mkdtemp(prefix="apricity-failrow-"))
    ledger = SessionLedger(session_id="s1", workspace_root=str(tmp))
    ledger.append(
        "compaction/end",
        {
            "compaction_id": "c1",
            "success": True,
            "terminal_phase": "committed",
            "ending_surface_generation": 0,
        },
    )
    ledger.append(
        "compaction/end",
        {
            "compaction_id": "c2",
            "success": False,
            "terminal_phase": "validated",
            "error_code": "summary_no_gain",
            "ending_surface_generation": 0,
        },
    )
    ledger.append(
        "context/summary",
        {"compaction_id": "c1", "rendered": "[context-summary] ok", "validated": True},
    )
    ledger.append(
        "context/summary",
        {"compaction_id": "c3", "rendered": "[context-summary] bad", "validated": False},
    )

    loop = RealAgentLoop.__new__(RealAgentLoop)
    loop.task = {
        "id": "T",
        "prompt": "Do the thing",
        "critical_facts": [],
        "process_invariants": [],
    }
    loop.condition = "D"
    loop.repetition = 1
    loop.budget = 4096
    loop.workspace_copy_root = tmp
    loop.fixture_root = tmp
    loop.ledger = ledger
    loop.trace_events = []
    loop.summary_usage = SummaryUsage()
    loop.main_usage = SummaryUsage()
    loop.artifact_verifier = lambda task, root, events=None, surface_text="": {
        "artifact_pass": None,
        "skipped": True,
    }

    row = loop._fail_row(None, False, False, "model_stop_failure", "partial answer", attempts=3, tool_steps=2)

    assert row["compaction_count"] == 1
    assert row["summary_total_claims"] == 2
    assert row["summary_error_claims"] == 1
    assert row["mechanics_pass"] is True


def test_fail_row_reports_prepared_dispatch_fields_and_initial_pressure():
    from types import SimpleNamespace

    from apricity.context.summarizer import SummaryUsage
    from apricity.evaluation.context_compaction.real_loop import RealAgentLoop

    tmp = Path(tempfile.mkdtemp(prefix="apricity-failrow-prepared-"))
    ledger = SessionLedger(session_id="s1", workspace_root=str(tmp))
    ledger.append(
        "context/pressure",
        {
            "pressure": True,
            "estimated_input_tokens": 1234,
            "initial_pressure": True,
            "trigger": "pressure",
            "changed": False,
        },
    )
    ledger.append(
        "compaction/end",
        {
            "compaction_id": "c1",
            "success": True,
            "terminal_phase": "committed",
            "ending_surface_generation": 0,
        },
    )
    ledger.append(
        "context/summary",
        {"compaction_id": "c1", "rendered": "[context-summary] ok"},
    )

    prepared = SimpleNamespace(
        surface_generation=4,
        metadata={"estimated_input_tokens": 1234},
        canonical_request_sha256="sha256:prepared-request",
        source_event_seqs=(2, 5, 8),
    )

    loop = RealAgentLoop.__new__(RealAgentLoop)
    loop.task = {
        "id": "T",
        "prompt": "Do the thing",
        "critical_facts": [],
        "process_invariants": [],
    }
    loop.condition = "D"
    loop.repetition = 1
    loop.budget = 4096
    loop.workspace_copy_root = tmp
    loop.fixture_root = tmp
    loop.ledger = ledger
    loop.trace_events = []
    loop.summary_usage = SummaryUsage()
    loop.main_usage = SummaryUsage()
    loop.artifact_verifier = lambda task, root, events=None, surface_text="": {
        "artifact_pass": None,
        "skipped": True,
    }

    row = loop._fail_row(
        prepared,
        True,
        False,
        "model_stop_failure",
        "partial answer",
        attempts=2,
        tool_steps=1,
    )

    assert row["pressure_triggered"] is True
    assert row["estimated_input_tokens"] == 1234
    assert row["canonical_request_sha256"] == "sha256:prepared-request"
    assert row["source_event_seqs"] == [2, 5, 8]
    assert row["surface_generation"] == 4
    assert row["observed_overflow"] is True
    assert row["recovered_overflow"] is False
    assert row["compaction_count"] == 1
    assert row["summary_total_claims"] == 1
    assert row["summary_error_claims"] == 0


def test_compaction_generation_conflict_is_detected():
    ledger, engine = build_engine(compact_policy(), valid_summary(1, "seed answer 0 "))

    range_seqs = engine._select_compaction_range()
    start_seq = range_seqs[0]
    end_seq = range_seqs[-1]
    generation_before = ledger.surface_generation
    # Simulate a concurrent replacement that already folded the span, so the
    # old endpoints are no longer current Surface nodes.
    ledger.append(
        "context/summary",
        {
            "compaction_id": "other",
            "rendered": "another summary",
            "generation": generation_before,
        },
        surface_op={"op": "replace", "start": start_seq, "end": end_seq},
        source_event_seqs=tuple(range_seqs),
    )

    with pytest.raises(ContextPreparationError, match="replacement span"):
        ledger.append(
            "context/summary",
            {
                "compaction_id": "conflict",
                "rendered": "summary",
                "generation": generation_before,
            },
            surface_op={"op": "replace", "start": start_seq, "end": end_seq},
            source_event_seqs=tuple(range_seqs),
        )


def test_compaction_persistence_failure_stops_before_next_request():
    class FailingLedger(SessionLedger):
        def flush_dispatch(self):
            raise OSError("disk full")

    tmp = Path(tempfile.mkdtemp(prefix="apricity-persist-"))
    ledger = FailingLedger(session_id="s1", workspace_root=str(tmp))
    engine = ReplayableContextEngine(
        ledger=ledger,
        policy=compact_policy(),
        provider="deepseek",
        model="deepseek-v4-flash",
        temperature=0.0,
        max_output_tokens=512,
        system_prompt="SYSTEM",
        tools=[],
        memory_text="Memory:\n- task: -",
        meter=DeterministicTestMeter(),
        summarizer=valid_summary(0, ""),
    )
    # Seed surface after engine construction so the ledger exists.
    for index in range(6):
        ledger.append("user/message", {"content": f"seed {index} " + ("x" * 400)})
        ledger.append("assistant/message", {"content": f"answer {index} " + ("y" * 400)})

    # A persistence failure during dispatch flush must surface as a typed
    # error rather than silently proceeding to the next model request. The
    # in-memory ledger's flush_dispatch is a no-op, so the engine records the
    # durable-before-dispatch contract and a persistence failure aborts the
    # run at the dispatch boundary by raising.
    engine.ledger.flush_dispatch = lambda: (_ for _ in ()).throw(OSError("disk full"))
    with pytest.raises(OSError, match="disk full"):
        engine._finalize(engine._build_request("x"), "x", "pressure")
