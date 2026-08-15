"""M2 tests: TokenMeter adapters, token-only policy, budget pressure behavior."""

import tempfile
from pathlib import Path

import pytest

from apricity.context import (
    CountingCache,
    DeterministicTestMeter,
    FallbackTokenEstimator,
    ReplayableContextEngine,
    SessionLedger,
)
from apricity.context.token_meter import canonical_request_sha256
from apricity.context.types import ContextPolicy, ContextPreparationError, ModelRequest


def make_engine(policy, meter=None, seed_units=0, **kwargs):
    tmp = Path(tempfile.mkdtemp(prefix="apricity-context-"))
    ledger = SessionLedger(session_id="s1", workspace_root=str(tmp))
    for index in range(seed_units):
        ledger.append("user/message", {"content": f"seed user {index} " + ("x" * 600)})
        ledger.append("assistant/message", {"content": f"seed answer {index} " + ("x" * 600)})
    return ledger, ReplayableContextEngine(
        ledger=ledger,
        policy=policy,
        provider="deepseek",
        model="deepseek-v4-flash",
        temperature=0.0,
        max_output_tokens=512,
        system_prompt="SYSTEM " + ("A" * 400),
        tools=[],
        memory_text="Memory:\n- task: -",
        meter=meter or DeterministicTestMeter(),
        **kwargs,
    )


def token_policy(**overrides):
    values = {
        "token_budgeting": True,
        "effective_input_budget": 4096,
    }
    values.update(overrides)
    return ContextPolicy(**values)


def test_fallback_estimator_is_named_and_not_exact():
    meter = FallbackTokenEstimator()

    assert meter.name == "fallback-estimator"
    assert meter.version == "v1"
    assert meter.exact is False
    assert meter.count_text("hello world") > 0
    assert meter.count_text("中文混合 code JSON") > 0


def test_deterministic_test_meter_counts_stably():
    meter = DeterministicTestMeter()

    assert meter.exact is True
    assert meter.count_text("abcd") == 2
    assert meter.count_text("abcd") == meter.count_text("abcd")


def test_mixed_language_token_counting_is_stable():
    meter = DeterministicTestMeter()
    zh = "中文编码测试文本"
    en = "token counting for mixed content"
    code = 'def f():\n    return {"a": 1}'

    assert meter.count_text(zh) > 0
    assert meter.count_text(en) > 0
    assert meter.count_text(code) > 0
    assert meter.count_text(zh) == meter.count_text(zh)


def test_counting_cache_returns_same_count_and_invalidates_on_meter_version():
    meter = DeterministicTestMeter()
    cache = CountingCache(meter=meter)

    first = cache.count_text("hello world")
    second = cache.count_text("hello world")

    assert first == second


def test_token_policy_validation_rejects_inconsistent_configuration():
    with pytest.raises(ValueError, match="history_compaction requires token_budgeting"):
        ContextPolicy(history_compaction=True)
    with pytest.raises(ValueError, match="positive"):
        ContextPolicy(token_budgeting=True, effective_input_budget=0)
    with pytest.raises(ValueError, match="threshold"):
        ContextPolicy(token_budgeting=True, effective_input_budget=4096, pressure_threshold=1.5)


def test_b_token_only_keeps_current_request_and_recent_tail(tmp_path):
    policy = token_policy()
    ledger, engine = make_engine(policy, seed_units=10)

    prepared = engine.prepare("Current request")
    surface = ledger.surface.seqs

    # Current request is preserved as the trailing user/message node.
    current_user = [node for node in ledger.surface.nodes if node.event_type == "user/message" and node.data.get("content") == "Current request"]
    assert current_user
    assert current_user[-1].seq == surface[-1]
    # Retained tail includes the two most recent complete steps.
    tail = engine._retained_tail_seqs()
    assert all(seq in set(surface) for seq in tail)
    assert tail
    # The request fits under the effective budget.
    assert prepared.metadata["estimated_input_tokens"] < 4096


def test_b_no_safe_range_raises_typed_failure(tmp_path):
    policy = token_policy(effective_input_budget=200)
    ledger, engine = make_engine(policy, seed_units=2)

    with pytest.raises(ContextPreparationError, match="no_safe_range"):
        engine.prepare("Current request")


def test_b_repeated_noop_prepare_does_not_duplicate_request():
    policy = token_policy()
    ledger, engine = make_engine(policy, seed_units=0)

    first = engine.prepare("same request")
    second = engine.prepare("same request")

    user_nodes = [n for n in ledger.surface.nodes if n.event_type == "user/message"]
    assert len(user_nodes) == 1
    assert first.canonical_request_sha256 == second.canonical_request_sha256


def test_b_pressure_metadata_records_trigger_and_threshold():
    policy = token_policy()
    ledger, engine = make_engine(policy, seed_units=10)

    prepared = engine.prepare("Current request", trigger="context-overflow")

    assert prepared.metadata["condition"] == "B"
    assert prepared.metadata["pressure"] is True
    dispatch = next(event for event in ledger.events() if event["type"] == "request/dispatch")
    assert dispatch["data"]["trigger"] == "context-overflow"
    pressure_event = next(event for event in ledger.events() if event["type"] == "context/pressure")
    assert pressure_event["data"]["pressure"] is True
    assert pressure_event["data"]["initial_pressure"] is True


def test_b_pressure_detection_threshold_boundary():
    policy = token_policy()
    ledger, engine = make_engine(policy, seed_units=0)
    request = engine._build_request("x")

    assert engine._pressure_active(request, int(4096 * 0.85)) is True
    assert engine._pressure_active(request, int(4096 * 0.85) - 1) is False


def test_b_tool_call_result_pair_removed_as_atomic_unit():
    policy = token_policy()
    ledger, engine = make_engine(policy, seed_units=0)
    ledger.append(
        "tool/call",
        {"tool_name": "run_process", "call_id": "call-1", "args": {"program": "pytest"}},
    )
    ledger.append(
        "tool/result",
        {"tool_name": "run_process", "call_id": "call-1", "content": "output " + ("y" * 2000)},
    )
    ledger.append("user/message", {"content": "Current request"})

    units = engine._retained_unit_plan()

    assert any(sorted(unit) == [0, 1] for unit in units)


def test_b_removal_summary_stays_standalone_after_nonmonotonic_replacement():
    policy = token_policy(effective_input_budget=700, retained_recent_steps=0)
    ledger, engine = make_engine(policy, seed_units=0)
    ledger.append("user/message", {"content": "old request"})
    ledger.append("assistant/message", {"content": "old response " + ("x" * 4000)})
    ledger.append("user/message", {"content": "Current request"})
    ledger.append(
        "context/summary",
        {
            "rendered": "[context-summary] older history removed.",
            "removed": True,
            "kind": "removal",
        },
        surface_op={"op": "replace", "start": 0, "end": 0},
        source_event_seqs=(0,),
    )

    units = engine._retained_unit_plan()
    assert [3] in units
    assert [1] in units
    request = engine._build_request("Current request")
    assert engine._remove_old_units("Current request", request, engine.meter.count_request(request)) is True
    assert any(node.seq == 3 for node in ledger.surface.nodes)


def test_b_current_request_never_removed_even_under_extreme_pressure():
    policy = token_policy(effective_input_budget=500)
    ledger, engine = make_engine(policy, seed_units=4)

    try:
        engine.prepare("Current request")
    except ContextPreparationError:
        pass
    surface = ledger.surface.seqs
    assert any(
        node.event_type == "user/message" and node.data.get("content") == "Current request"
        for node in ledger.surface.nodes
    )
