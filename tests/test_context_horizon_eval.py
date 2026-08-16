"""Tests for the Context Horizon v3 task suite and local pilot runner."""

from pathlib import Path

from apricity.evaluation.context_horizon import (
    CostGuard,
    build_seed_events,
    load_hidden_verifier,
    load_taskset,
    run_local_pilot,
    run_paid_run,
    summarize_rows,
    task_by_id,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
TASKSET_PATH = REPO_ROOT / "benchmarks" / "context_horizon" / "tasks.v1.json"
HIDDEN_ROOT = REPO_ROOT / "benchmarks" / "context_horizon" / "hidden"


def load_tasks():
    taskset = load_taskset(TASKSET_PATH, repo_root=REPO_ROOT)
    return taskset


def test_taskset_loads_six_tasks_with_frozen_hashes():
    taskset = load_tasks()
    assert len(taskset["tasks"]) == 6
    assert taskset["task_snapshot_hash"]
    assert Path(taskset["fixture_repo_abs"]).is_dir()


def test_seed_events_are_deterministic_and_adequately_sized():
    taskset = load_tasks()
    task = task_by_id(taskset, "L2-long-log-root-cause")
    first = build_seed_events(task)
    second = build_seed_events(task)
    assert first == second
    content = "\n".join(event["data"].get("content", "") for event in first if event["type"] == "tool/result")
    assert len(content) > 2000
    assert "expected timeout 120, found timeout 30" in content


def test_hidden_verifiers_load_without_entering_task_policy():
    taskset = load_tasks()
    for task in taskset["tasks"]:
        hidden = load_hidden_verifier(task, HIDDEN_ROOT)
        assert hidden["oracle_secret"]
        assert hidden["context_required"]
        assert "critical_facts" not in task


def test_local_pilot_a_loses_old_fact_while_e_retains_it(tmp_path):
    taskset = load_tasks()
    task = task_by_id(taskset, "L2-long-log-root-cause")
    hidden = load_hidden_verifier(task, HIDDEN_ROOT)

    a_row = run_local_pilot(
        task=task,
        condition="A",
        fixture_root=taskset["fixture_repo_abs"],
        workspace_root=tmp_path / "A",
        hidden=hidden,
        budget=4096,
    )
    e_row = run_local_pilot(
        task=task,
        condition="E",
        fixture_root=taskset["fixture_repo_abs"],
        workspace_root=tmp_path / "E",
        hidden=hidden,
        budget=4096,
    )

    assert a_row["pressure_crossed"] is True
    assert a_row["context"]["context_pass"] is False
    assert e_row["pressure_crossed"] is True
    assert e_row["context"]["context_pass"] is True
    assert e_row["mechanics"]["mechanics_pass"] is True
    assert e_row["hidden_isolated"] is True


def test_calibration_gate_summary_passes_on_pilot_rows(tmp_path):
    taskset = load_tasks()
    rows = []
    for task_id in ("L2-long-log-root-cause", "L3-multi-round-test-fix", "L5-realistic-multi-file", "L6-overflow-recovery"):
        task = task_by_id(taskset, task_id)
        hidden = load_hidden_verifier(task, HIDDEN_ROOT)
        for condition in ("A", "C", "E"):
            rows.append(
                run_local_pilot(
                    task=task,
                    condition=condition,
                    fixture_root=taskset["fixture_repo_abs"],
                    workspace_root=tmp_path / task_id / condition,
                    hidden=hidden,
                    budget=4096,
                )
            )
    summary = summarize_rows(rows)
    assert summary["pressure_cross_rate_C_E"] == 1.0
    assert len(summary["A_lost_fact_or_overflow_tasks"]) >= 3
    assert summary["calibration_gate_pass"] is True


class FakePaidClient:
    def __init__(self):
        self.last_completion_metadata = {
            "input_tokens": 1000,
            "output_tokens": 20,
            "cached_tokens": 200,
            "cache_miss_tokens": 800,
        }

    def complete(self, prompt, max_new_tokens):
        del prompt, max_new_tokens
        return "<final>Done.</final>"


def test_cost_guard_uses_cache_aware_pricing():
    guard = CostGuard(max_cost_cny=15.0)
    cost = guard.record(
        {
            "input_tokens": 1_000_000,
            "output_tokens": 100_000,
            "cached_tokens": 200_000,
            "cache_miss_tokens": 800_000,
        }
    )
    assert round(cost, 6) == round(800_000 / 1_000_000 * 1.4 + 200_000 / 1_000_000 * 0.05 + 100_000 / 1_000_000 * 4.5, 6)
    assert guard.remaining_cny < 15.0


def test_paid_run_records_usage_and_stops_at_final(tmp_path):
    taskset = load_tasks()
    task = task_by_id(taskset, "L2-long-log-root-cause")
    hidden = load_hidden_verifier(task, HIDDEN_ROOT)
    guard = CostGuard(max_cost_cny=15.0)

    row = run_paid_run(
        task=task,
        condition="A",
        fixture_root=taskset["fixture_repo_abs"],
        workspace_root=tmp_path / "paid-a",
        hidden=hidden,
        budget=4096,
        client_factory=FakePaidClient,
        cost_guard=guard,
        provider="fake",
        model="fake-model",
    )

    assert row["stop_reason"] == "final_answer_returned"
    assert row["context"]["context_pass"] is False
    assert guard.input_tokens == 1000
    assert guard.spent_cny > 0
    assert row["mechanics"]["mechanics_pass"] is True
