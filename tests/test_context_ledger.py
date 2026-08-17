"""Interface tests for the lean SessionLedger."""

import json

import pytest

from apricity.context import (
    LedgerError,
    SessionLedger,
    canonical_json,
    render_surface_node,
)


def make_ledger(tmp_path, persist=None):
    return SessionLedger(
        session_id="s1",
        workspace_root=str(tmp_path),
        created_at="2026-08-16T00:00:00+00:00",
        persist_path=persist,
    )


def test_append_is_contiguous_and_immutable(tmp_path):
    ledger = make_ledger(tmp_path)
    mutable = {"content": "alpha", "nested": {"items": [1]}}
    event = ledger.append("user/message", mutable)
    mutable["content"] = "changed"
    mutable["nested"]["items"].append(2)

    assert event.seq == 0
    assert event.data["content"] == "alpha"
    assert event.data["nested"]["items"] == [1]
    assert ledger.event_by_seq(0) == event


def test_surface_appends_in_order(tmp_path):
    ledger = make_ledger(tmp_path)
    ledger.append("user/message", {"content": "hello"})
    ledger.append("assistant/message", {"content": "world"})

    assert [node.seq for node in ledger.surface] == [0, 1]
    assert ledger.surface_event_seqs() == [0, 1]
    assert "world" in render_surface_node(ledger.surface[-1])


def test_replacement_shadows_without_deleting_sources(tmp_path):
    ledger = make_ledger(tmp_path)
    ledger.append("user/message", {"content": "old fact"})
    ledger.append("assistant/message", {"content": "old answer"})
    ledger.append("context/summary", {"rendered": "summary"}, surface_op={"op": "replace", "start": 0, "end": 1}, source_event_seqs=(0, 1))

    assert ledger.surface_generation == 1
    assert [node.event_type for node in ledger.surface] == ["context/summary"]
    assert [event.seq for event in ledger.events] == [0, 1, 2]
    assert ledger.event_by_seq(0).data["content"] == "old fact"


def test_invalid_replacement_span_is_rejected(tmp_path):
    ledger = make_ledger(tmp_path)
    ledger.append("user/message", {"content": "hello"})

    with pytest.raises(LedgerError):
        ledger.append(
            "context/summary",
            {"rendered": "bad"},
            surface_op={"op": "replace", "start": 7, "end": 9},
            source_event_seqs=(0,),
        )

    assert ledger.surface_generation == 0
    assert [node.event_type for node in ledger.surface] == ["user/message"]


def test_saved_ledger_round_trips_replacements(tmp_path):
    path = tmp_path / "ledger.json"
    ledger = make_ledger(tmp_path, persist=path)
    ledger.append("user/message", {"content": "first"})
    ledger.append("assistant/message", {"content": "second"})
    ledger.append("context/summary", {"rendered": "summary"}, surface_op={"op": "replace", "start": 0, "end": 1}, source_event_seqs=(0, 1))
    ledger.flush_dispatch()

    loaded = SessionLedger.load(path)

    assert loaded.surface_generation == 1
    assert [node.event_type for node in loaded.surface] == ["context/summary"]
    assert [event.seq for event in loaded.events] == [0, 1, 2]


def test_saved_ledger_rejects_non_contiguous_events(tmp_path):
    path = tmp_path / "ledger.json"
    ledger = make_ledger(tmp_path, persist=path)
    ledger.append("user/message", {"content": "first"})
    ledger.flush_dispatch()

    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["events"][0]["seq"] = 7
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(LedgerError, match="not contiguous"):
        SessionLedger.load(path)


def test_saved_ledger_rejects_replacement_without_sources(tmp_path):
    path = tmp_path / "ledger.json"
    ledger = make_ledger(tmp_path, persist=path)
    ledger.append("user/message", {"content": "first"})
    ledger.append("context/summary", {"rendered": "summary"}, surface_op={"op": "replace", "start": 0, "end": 0}, source_event_seqs=(0,))
    ledger.flush_dispatch()

    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["events"][1]["source_event_seqs"] = []
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(LedgerError, match="must cite source_event_seqs"):
        SessionLedger.load(path)


def test_pairs_balanced_detects_orphan_results(tmp_path):
    ledger = make_ledger(tmp_path)
    ledger.append("tool/call", {"name": "read_file", "call_id": "c1", "args": {"path": "a.py"}})
    ledger.append("tool/result", {"tool_name": "read_file", "call_id": "c1", "content": "ok"})
    assert ledger.pairs_balanced()

    ledger.append("tool/result", {"tool_name": "read_file", "call_id": "c2", "content": "orphan"})
    assert not ledger.pairs_balanced()


def test_canonical_json_is_stable_across_key_order():
    assert canonical_json({"b": 1, "a": [2, 3]}) == canonical_json({"a": [2, 3], "b": 1})
    assert '"a":[2,3],"b":1' in canonical_json({"b": 1, "a": [2, 3]})
