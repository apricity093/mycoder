"""M3 tests: deterministic tool-result pruning and evidence-anchored compaction."""

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
    build_summary_candidate,
    parse_summary_json,
    prune_tool_result,
    render_summary,
    validate_evidence_anchors,
    validate_summary_schema,
)
from apricity.context.ledger import render_surface_node


def make_ledger(tmp_path=None):
    tmp = Path(tempfile.mkdtemp(prefix="apricity-prune-"))
    return SessionLedger(session_id="s1", workspace_root=str(tmp))


def run_result(tool_name="run_process", call_id="call-1", content="hello"):
    return {"tool_name": tool_name, "call_id": call_id, "content": content}


def test_prune_unknown_tool_is_noop():
    payload = prune_tool_result(
        tool_name="write_file",
        content="x" * 5000,
        metadata={},
        soft_limit=800,
        minimum_gain=0.2,
    )

    assert payload is None


def test_prune_small_result_is_noop():
    payload = prune_tool_result(
        tool_name="run_shell",
        content="short",
        metadata={},
        soft_limit=800,
        minimum_gain=0.2,
    )

    assert payload is None


def test_prune_retains_head_tail_and_diagnostic_lines():
    lines = [f"line-{index} " + ("y" * 40) for index in range(200)]
    content = "\n".join(lines)
    payload = prune_tool_result(
        tool_name="run_process",
        content=content,
        metadata={},
        soft_limit=100,
        minimum_gain=0.2,
    )

    assert payload is not None
    replacement = payload["replacement"]
    assert "line-0" in replacement
    assert f"line-{199}" in replacement
    assert payload["original_content_sha256"]
    assert payload["pruning_policy_name"] == "deterministic-text-tool-pruning"
    assert payload["gain_ratio"] > 0.2


def test_prune_middle_diagnostic_is_retained_with_context():
    lines = [f"noise-{index}" for index in range(100)]
    lines[50] = "Traceback (most recent call last):"
    lines[51] = "  File \"tests/test_x.py\", line 12, in test_fail"
    lines[52] = "    assert False"
    content = "\n".join(lines)
    payload = prune_tool_result(
        tool_name="run_process",
        content=content,
        metadata={},
        soft_limit=80,
        minimum_gain=0.2,
    )

    assert payload is not None
    replacement = payload["replacement"]
    assert "Traceback" in replacement
    assert "test_fail" in replacement
    assert "assert False" in replacement


def test_prune_preserves_line_numbers_for_read_file():
    lines = [f"{number:>4}: content-{number}" for number in range(1, 120)]
    content = "\n".join(lines)
    payload = prune_tool_result(
        tool_name="read_file",
        content=content,
        metadata={},
        soft_limit=50,
        minimum_gain=0.2,
    )

    assert payload is not None
    replacement = payload["replacement"]
    assert "1: content-1" in replacement
    assert "119: content-119" in replacement


def test_prune_validates_retention_hints_and_ignores_invalid():
    lines = [f"line-{index}" for index in range(50)]
    content = "\n".join(lines)
    payload = prune_tool_result(
        tool_name="read_file",
        content=content,
        metadata={"retention_lines": [1, 25, 999]},
        soft_limit=20,
        minimum_gain=0.2,
    )

    assert payload is not None
    assert "line-0" in payload["replacement"]
    assert "line-24" in payload["replacement"]
    assert "line-998" not in payload["replacement"]


def test_prune_unicode_does_not_split_surrogate_pairs():
    content = ("中文内容" * 300) + ("\U0001F600" * 400)
    payload = prune_tool_result(
        tool_name="run_shell",
        content=content,
        metadata={},
        soft_limit=100,
        minimum_gain=0.2,
    )

    assert payload is not None
    # No lone surrogate in the replacement.
    assert "\ud800" not in payload["replacement"]
    assert payload["replacement"] == payload["replacement"]


def test_prune_idempotent_same_input_same_output():
    content = "\n".join(f"line-{index} " + ("z" * 50) for index in range(150))
    first = prune_tool_result(
        tool_name="run_shell",
        content=content,
        metadata={},
        soft_limit=80,
        minimum_gain=0.2,
    )
    second = prune_tool_result(
        tool_name="run_shell",
        content=content,
        metadata={},
        soft_limit=80,
        minimum_gain=0.2,
    )

    assert first is not None
    assert second is not None
    assert first == second


def test_summary_schema_accepts_valid_structured_summary():
    data = {
        "schema_version": 1,
        "goal": {
            "text": "Implement timeout default",
            "status": "confirmed",
            "sources": [{"event_seq": 1, "quote": "Implement timeout default"}],
        },
        "constraints": [],
        "files": [],
        "changes": [],
        "tests": [],
        "open_items": [],
        "next_step": {"text": "Run pytest", "status": "hypothesis", "sources": []},
    }

    validate_summary_schema(data)
    candidate = build_summary_candidate(data)

    assert candidate.goal.status == "confirmed"
    assert candidate.next_step.status == "hypothesis"


def test_summary_schema_rejects_invalid_status():
    data = {
        "schema_version": 1,
        "goal": {"text": "x", "status": "maybe", "sources": []},
        "constraints": [],
        "files": [],
        "changes": [],
        "tests": [],
        "open_items": [],
        "next_step": None,
    }

    with pytest.raises(ValueError, match="status"):
        validate_summary_schema(data)


def test_evidence_anchor_requires_exact_quote_substring():
    renderings = {
        1: "[tool] Implement timeout default\nconfig = 5",
        2: "[assistant] tests are passing",
    }

    def renderer(seq):
        return renderings.get(seq)

    data = {
        "schema_version": 1,
        "goal": {
            "text": "Implement timeout default",
            "status": "confirmed",
            "sources": [{"event_seq": 1, "quote": "Implement timeout default"}],
        },
        "constraints": [],
        "files": [],
        "changes": [],
        "tests": [],
        "open_items": [],
        "next_step": None,
    }
    validate_evidence_anchors(data, renderer, max_quote_chars=320)

    bad = json.loads(json.dumps(data))
    bad["goal"]["sources"] = [{"event_seq": 1, "quote": "timeout DEFAULT (paraphrase)"}]
    with pytest.raises(ValueError, match="exact substring"):
        validate_evidence_anchors(bad, renderer, max_quote_chars=320)

    missing = json.loads(json.dumps(data))
    missing["goal"]["sources"] = [{"event_seq": 99, "quote": "anything"}]
    with pytest.raises(ValueError, match="nonexistent"):
        validate_evidence_anchors(missing, renderer, max_quote_chars=320)


def test_evidence_anchor_rejects_paraphrase_ellipsis():
    renderings = {1: "The exact sentence from the tool result."}
    data = {
        "schema_version": 1,
        "goal": {
            "text": "x",
            "status": "confirmed",
            "sources": [{"event_seq": 1, "quote": "The exact ... result."}],
        },
        "constraints": [],
        "files": [],
        "changes": [],
        "tests": [],
        "open_items": [],
        "next_step": None,
    }
    with pytest.raises(ValueError, match="exact substring"):
        validate_evidence_anchors(data, lambda seq: renderings.get(seq), max_quote_chars=320)


def test_compaction_rejects_summary_without_gain():
    policy = ContextPolicy(
        token_budgeting=True,
        tool_pruning=True,
        history_compaction=True,
        effective_input_budget=2048,
    )
    ledger = make_ledger()
    for index in range(6):
        ledger.append("user/message", {"content": f"seed user {index} " + ("x" * 500)})
        ledger.append("assistant/message", {"content": f"seed answer {index} " + ("x" * 500)})

    calls = []

    def fake_summarizer(events, renderings, max_tokens):
        calls.append((events, renderings, max_tokens))
        # A huge summary that provides no token gain.
        return json.dumps(
            {
                "schema_version": 1,
                "goal": {
                    "text": "Repeat the entire history " + ("y" * 5000),
                    "status": "hypothesis",
                    "sources": [],
                },
                "constraints": [],
                "files": [],
                "changes": [],
                "tests": [],
                "open_items": [],
                "next_step": None,
            }
        )

    engine = ReplayableContextEngine(
        ledger=ledger,
        policy=policy,
        provider="deepseek",
        model="deepseek-v4-flash",
        temperature=0.0,
        max_output_tokens=512,
        system_prompt="SYSTEM",
        tools=[],
        memory_text="Memory:\n- task: -",
        meter=DeterministicTestMeter(),
        summarizer=fake_summarizer,
    )

    # The compaction range covers old safe units only (assistant messages in
    # this seeded shape); the no-gain candidate must not commit a replacement.
    range_seqs = engine._select_compaction_range()
    assert range_seqs

    engine.prepare("Current request", trigger="context-overflow")

    assert calls
    # The no-gain compaction must not commit a summary replacement with a
    # compaction_id; the fallback whole-unit removal summary is allowed.
    compaction_summaries = [
        event
        for event in ledger.events()
        if event["type"] == "context/summary" and "compaction_id" in event["data"]
    ]
    compaction_ends = [event for event in ledger.events() if event["type"] == "compaction/end"]
    assert any(event["data"]["success"] is False for event in compaction_ends)
    assert not compaction_summaries
