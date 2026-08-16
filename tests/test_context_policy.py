"""Interface tests for ContextEngine conditions A and C."""

import pytest

from apricity.context import (
    ContextEngine,
    ContextPolicy,
    ContextPreparationError,
    DeterministicTestMeter,
    SessionLedger,
)


def build_engine(*, policy, budget=4096, meter=None, ledger=None):
    return ContextEngine(
        provider="scripted",
        model="test-model",
        system_prompt="You are a test agent.",
        tools=[{"name": "read_file", "schema": {"path": "str"}}],
        memory_text="",
        meter=meter or DeterministicTestMeter(),
        policy=policy,
        ledger=ledger,
    )


def seed_step(ledger, user, assistant, tool_result="result"):
    ledger.append("user/message", {"content": user})
    ledger.append("assistant/message", {"content": assistant})
    ledger.append("tool/call", {"name": "read_file", "call_id": f"call-{len(ledger.events)}", "args": {"path": "a.py"}})
    ledger.append("tool/result", {"tool_name": "read_file", "call_id": f"call-{len(ledger.events) - 1}", "content": tool_result})


def test_condition_a_removes_oldest_units_under_pressure(tmp_path):
    ledger = SessionLedger(session_id="s", workspace_root=str(tmp_path), created_at="t")
    for index in range(6):
        seed_step(ledger, f"old user {index}", f"old assistant {index}", f"old result {index}" * 8)

    policy = ContextPolicy(
        condition_id="A",
        condition_label="window",
        effective_input_budget=800,
        pressure_threshold=0.85,
        retained_recent_steps=1,
    )
    engine = build_engine(policy=policy, ledger=ledger)

    prepared = engine.prepare("current request")

    assert prepared.changed
    assert "current request" in prepared.prompt
    assert "[user] old user 0" not in prepared.prompt
    assert "[user] old user 5" in prepared.prompt
    assert prepared.metadata["tool_pairs_balanced"] is True
    assert engine.ledger.pairs_balanced()
    assert any(event.type == "context/summary" for event in engine.ledger.events)


def test_condition_c_prunes_oversized_tool_result(tmp_path):
    ledger = SessionLedger(session_id="s", workspace_root=str(tmp_path), created_at="t")
    content_lines = ["head line"] + [f"NOISE_SHOULD_DROP {index}" for index in range(200)] + ["ERROR: boom", "tail line"]
    content = "\n".join(content_lines)
    ledger.append("user/message", {"content": "investigate"})
    ledger.append("tool/call", {"name": "run_process", "call_id": "c1", "args": {"program": "python"}})
    ledger.append(
        "tool/result",
        {"tool_name": "run_process", "call_id": "c1", "content": content, "metadata": {"status": "failed", "exit_code": 1}},
    )

    policy = ContextPolicy(
        condition_id="C",
        condition_label="prune",
        effective_input_budget=450,
        pressure_threshold=0.85,
        tool_pruning=True,
        tool_result_soft_limit=120,
        tool_pruning_minimum_gain=0.1,
        retained_recent_steps=2,
    )
    engine = build_engine(policy=policy, ledger=ledger)

    prepared = engine.prepare("current request")

    assert prepared.changed
    assert "pruned run_process result" in prepared.prompt
    assert "ERROR: boom" in prepared.prompt
    assert "NOISE_SHOULD_DROP 150" not in prepared.prompt
    assert any(node.event_type == "context/pruned-tool-result" for node in engine.ledger.surface)
    assert any(event.data.get("content") == content for event in engine.ledger.events if event.type == "tool/result")
    assert engine.ledger.pairs_balanced()


def test_prepare_raises_typed_error_when_no_safe_reduction_exists(tmp_path):
    ledger = SessionLedger(session_id="s", workspace_root=str(tmp_path), created_at="t")
    policy = ContextPolicy(
        condition_id="A",
        condition_label="window",
        effective_input_budget=60,
        pressure_threshold=0.5,
        retained_recent_steps=0,
    )
    engine = build_engine(policy=policy, ledger=ledger)

    with pytest.raises(ContextPreparationError) as excinfo:
        engine.prepare("this current request is much larger than the retained budget")

    assert excinfo.value.code == "no_safe_range"
    assert any(event.type == "context/pressure" for event in engine.ledger.events)


def test_dispatch_hash_replay_is_stable_when_surface_unchanged(tmp_path):
    ledger = SessionLedger(session_id="s", workspace_root=str(tmp_path), created_at="t")
    seed_step(ledger, "user", "assistant", "result")
    engine = build_engine(policy=ContextPolicy(), ledger=ledger)

    first = engine.prepare("request")
    second_hash = engine._dispatch_hash()

    assert first.canonical_request_sha256 == second_hash
    assert first.metadata["token_meter"]["name"] == "deterministic-test"
    assert first.metadata["token_meter"]["exact"] is True
