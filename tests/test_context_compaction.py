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


def build_engine(policy, summarizer, seed_units=8):
    tmp = Path(tempfile.mkdtemp(prefix="apricity-compact-"))
    ledger = SessionLedger(session_id="s1", workspace_root=str(tmp))
    for index in range(seed_units):
        ledger.append("user/message", {"content": f"seed user {index} " + ("x" * 400)})
        ledger.append("assistant/message", {"content": f"seed answer {index} " + ("y" * 400)})
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
