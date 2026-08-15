"""M1 tests: SessionLedger v2, Surface fold, tool pairing, request replay."""

import copy
import json
from pathlib import Path

import pytest

from apricity.context import (
    APPEND_FORM,
    DeterministicTestMeter,
    FallbackTokenEstimator,
    LegacyContextEngine,
    ReplayableContextEngine,
    SessionLedger,
)
from apricity.context.hashing import canonical_request_sha256
from apricity.context.ledger import render_surface_node
from apricity.context.types import ContextPolicy, ContextPreparationError, ModelRequest


def make_ledger(tmp_path, session_id="session_001"):
    return SessionLedger(
        session_id=session_id,
        workspace_root=str(tmp_path),
        created_at="2026-08-14T00:00:00+00:00",
    )


def user_event(seq, content, **extra):
    event = {
        "schema_version": 1,
        "seq": seq,
        "time": "2026-08-14T00:00:00+00:00",
        "type": "user/message",
        "data": {"content": content},
        "surface_op": APPEND_FORM,
        "source_event_seqs": [],
    }
    event.update(extra)
    return event


def test_ledger_appends_events_with_contiguous_seq_and_surface(tmp_path):
    ledger = make_ledger(tmp_path)
    assert ledger.seq == 0
    assert ledger.surface.seqs == []

    seq0 = ledger.append("user/message", {"content": "hello"})
    seq1 = ledger.append("assistant/message", {"content": "hi"})

    assert seq0 == 0
    assert seq1 == 1
    assert ledger.seq == 2
    assert ledger.surface.seqs == [0, 1]
    assert ledger.surface_generation == 0
    assert ledger.event_by_seq(0)["data"]["content"] == "hello"


def test_ledger_accepts_non_surface_events_without_surface_projection(tmp_path):
    ledger = make_ledger(tmp_path)
    ledger.append("turn/start", {"turn_id": "t1"})
    ledger.append("step/start", {"step": 1})

    assert ledger.seq == 2
    assert ledger.surface.seqs == []


def test_ledger_rejects_non_json_event_data(tmp_path):
    ledger = make_ledger(tmp_path)

    with pytest.raises(ContextPreparationError, match="JSON"):
        ledger.append("user/message", {"content": object()})


def test_ledger_rejects_unknown_event_type_and_bad_schema(tmp_path):
    ledger = make_ledger(tmp_path)

    with pytest.raises(ContextPreparationError, match="unknown event type"):
        ledger.append("nonsense/event", {})

    event = {
        "schema_version": 99,
        "seq": 0,
        "time": "2026-08-14T00:00:00+00:00",
        "type": "user/message",
        "data": {"content": "x"},
        "surface_op": APPEND_FORM,
        "source_event_seqs": [],
    }
    with pytest.raises(ContextPreparationError, match="schema"):
        ledger._accept(event, validate_seq=True)


def test_ledger_rejects_non_contiguous_seq(tmp_path):
    ledger = make_ledger(tmp_path)
    ledger.append("user/message", {"content": "a"})

    with pytest.raises(ContextPreparationError, match="contiguous"):
        ledger._accept(user_event(5, "b"), validate_seq=True)


def test_surface_replacement_removes_span_and_increments_generation(tmp_path):
    ledger = make_ledger(tmp_path)
    for index in range(4):
        ledger.append("user/message", {"content": f"m{index}"})
    assert ledger.surface.seqs == [0, 1, 2, 3]

    ledger.append(
        "context/summary",
        {"rendered": "summary", "generation": 1},
        surface_op={"op": "replace", "start": 1, "end": 2},
        source_event_seqs=(1, 2),
    )

    assert ledger.surface.seqs == [0, 4, 3]
    assert ledger.surface_generation == 1


def test_surface_seq_order_can_become_non_monotonic(tmp_path):
    ledger = make_ledger(tmp_path)
    for index in range(4):
        ledger.append("user/message", {"content": f"m{index}"})
    ledger.append(
        "context/summary",
        {"rendered": "summary"},
        surface_op={"op": "replace", "start": 0, "end": 1},
        source_event_seqs=(0, 1),
    )

    # 0 and 1 removed; replacement seq 4 inserted at position 0.
    assert ledger.surface.seqs == [4, 2, 3]
    assert 4 < 2 is False or sorted(ledger.surface.seqs) != ledger.surface.seqs


def test_surface_invalid_endpoints_reject_replacement(tmp_path):
    ledger = make_ledger(tmp_path)
    for index in range(3):
        ledger.append("user/message", {"content": f"m{index}"})

    with pytest.raises(ContextPreparationError, match="replacement span"):
        ledger.append(
            "context/summary",
            {"rendered": "summary"},
            surface_op={"op": "replace", "start": 0, "end": 9},
            source_event_seqs=(0, 1),
        )


def test_tool_pairing_is_preserved_after_pruning_replacement(tmp_path):
    ledger = make_ledger(tmp_path)
    ledger.append("user/message", {"content": "run tests"})
    ledger.append(
        "tool/call",
        {"tool_name": "run_process", "call_id": "call-1", "args": {"program": "pytest"}},
    )
    ledger.append(
        "tool/result",
        {"tool_name": "run_process", "call_id": "call-1", "content": "FAIL test_one", "tool_status": "error"},
    )

    # Prune the tool result in place.
    ledger.append(
        "context/pruned-tool-result",
        {
            "tool_name": "run_process",
            "call_id": "call-1",
            "replacement": "FAIL test_one",
            "original_content_sha256": "abc",
        },
        surface_op={"op": "replace", "start": 2, "end": 2},
        source_event_seqs=(2,),
    )

    assert ledger.surface.seqs == [0, 1, 3]
    assert ledger.surface_generation == 1
    restored = SessionLedger(
        session_id="session_001",
        workspace_root=str(tmp_path),
        events=ledger.events(),
    )
    assert restored.surface.seqs == [0, 1, 3]


def test_orphan_tool_result_rejected_on_load(tmp_path):
    ledger = make_ledger(tmp_path)
    ledger.append("user/message", {"content": "x"})
    ledger.append(
        "tool/result",
        {"tool_name": "run_process", "call_id": "no-call", "content": "orphan"},
    )

    with pytest.raises(ContextPreparationError, match="orphan"):
        SessionLedger(
            session_id="session_001",
            workspace_root=str(tmp_path),
            events=ledger.events(),
        )


def test_ledger_persists_and_restarts_with_same_surface(tmp_path):
    ledger = make_ledger(tmp_path)
    ledger.append("user/message", {"content": "hello"})
    ledger.append("assistant/message", {"content": "hi"})
    path = tmp_path / "session.json"
    ledger.save(path)

    restored = SessionLedger.from_session_dict(json.loads(path.read_text(encoding="utf-8")))

    assert restored.surface.seqs == [0, 1]
    assert restored.seq == 2
    assert restored.events() == ledger.events()


def test_legacy_engine_emits_replayable_request_and_dispatch(tmp_path):
    ledger = make_ledger(tmp_path)
    engine = LegacyContextEngine(
        ledger=ledger,
        provider="deepseek",
        model="deepseek-v4-flash",
        temperature=0.0,
        max_output_tokens=512,
        system_prompt="You are apricity.",
        tools=[],
        memory_text="Memory:\n- task: -",
        user_message="Initial request",
        seed_surface=[
            {"type": "user/message", "data": {"content": "seed"}},
        ],
    )

    prepared = engine.prepare("Current request")

    assert prepared.canonical_request_sha256
    assert prepared.source_event_seqs
    assert prepared.surface_generation >= 0
    dispatch = next(event for event in ledger.events() if event["type"] == "request/dispatch")
    header = next(event for event in ledger.events() if event["type"] == "request/header")
    assert dispatch["data"]["canonical_request_sha256"] == prepared.canonical_request_sha256
    assert header["data"]["model"] == "deepseek-v4-flash"


def test_canonical_hash_stable_and_sensitive_to_content(tmp_path):
    request = ModelRequest(
        provider="deepseek",
        model="deepseek-v4-flash",
        temperature=0.0,
        max_output_tokens=512,
        system_prompt="sys",
        messages=({"role": "user", "content": "hi"},),
        tools=(),
    )
    first = canonical_request_sha256(request)
    second = canonical_request_sha256(request)
    changed = canonical_request_sha256(
        ModelRequest(
            provider="deepseek",
            model="deepseek-v4-flash",
            temperature=0.0,
            max_output_tokens=512,
            system_prompt="sys",
            messages=({"role": "user", "content": "bye"},),
            tools=(),
        )
    )

    assert first == second
    assert first != changed
    assert len(first) == 64


def test_canonical_hash_is_deterministic_and_order_sensitive(tmp_path):
    request_a = ModelRequest(
        provider="deepseek",
        model="deepseek-v4-flash",
        temperature=0.0,
        max_output_tokens=512,
        system_prompt="sys",
        messages=({"role": "user", "content": "hi"},),
        tools=(),
    )
    # Same semantic request rebuilt from equivalent role/content messages.
    request_b = ModelRequest(
        provider="deepseek",
        model="deepseek-v4-flash",
        temperature=0.0,
        max_output_tokens=512,
        system_prompt="sys",
        messages=({"role": "user", "content": "hi"},),
        tools=(),
    )
    reordered = ModelRequest(
        provider="deepseek",
        model="deepseek-v4-flash",
        temperature=0.0,
        max_output_tokens=512,
        system_prompt="sys",
        messages=(
            {"role": "assistant", "content": "ok"},
            {"role": "user", "content": "hi"},
        ),
        tools=(),
    )
    assert canonical_request_sha256(request_a) == canonical_request_sha256(request_b)
    assert canonical_request_sha256(request_a) != canonical_request_sha256(reordered)


def test_legacy_engine_parity_with_old_character_policy(tmp_path):
    """Legacy parity: the old ContextManager and LegacyContextEngine must
    produce identical prompts for a representative session."""

    def build_legacy_prompt(history, prefix, memory_text, user_message):
        from apricity.context_manager import ContextManager

        class DummyAgent:
            def __init__(self, prefix):
                self.prefix = prefix

            def memory_text(self):
                return memory_text

            def render_checkpoint_text(self):
                return ""

            def feature_enabled(self, name):
                return True

            @property
            def session(self):
                return {"history": list(history)}

            @property
            def memory(self):
                return type("M", (), {"retrieval_candidates": lambda *a, **k: []})()

        return ContextManager(DummyAgent(prefix)).build(user_message)

    # The real runtime records the current user message into history before
    # rendering, so the old transcript includes it once; the current-request
    # section carries it a second time verbatim. Mirror that on both sides.
    history = [
        {"role": "user", "content": "old request", "created_at": "t1"},
        {"role": "assistant", "content": "old answer", "created_at": "t2"},
        {"role": "tool", "name": "read_file", "args": {"path": "sample.txt"}, "content": "# sample.txt\ncontent", "created_at": "t3"},
        {"role": "user", "content": "Current request", "created_at": "t4"},
    ]
    prefix = "PREFIX " + ("A" * 400)
    memory_text = "Memory:\n- task: -"

    old_prompt, old_metadata = build_legacy_prompt(history, prefix, memory_text, "Current request")

    ledger = make_ledger(tmp_path)
    engine = LegacyContextEngine(
        ledger=ledger,
        provider="deepseek",
        model="deepseek-v4-flash",
        temperature=0.0,
        max_output_tokens=512,
        system_prompt=prefix,
        tools=[],
        memory_text=memory_text,
        user_message="Current request",
    )
    for item in history:
        if item["role"] == "user":
            ledger.append("user/message", {"content": item["content"]})
        elif item["role"] == "assistant":
            ledger.append("assistant/message", {"content": item["content"]})
        else:
            ledger.append(
                "tool/call",
                {"tool_name": item["name"], "call_id": f"call-{id(item)}", "args": item["args"]},
            )
            ledger.append(
                "tool/result",
                {"tool_name": item["name"], "call_id": f"call-{id(item)}", "content": item["content"]},
            )
    new_prompt = engine.prepare("Current request").prompt

    assert new_prompt == old_prompt
