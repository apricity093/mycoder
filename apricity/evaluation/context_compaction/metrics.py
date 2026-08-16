"""Deterministic aggregation and failure precedence for context-compaction Runs.

Pure functions: every number in the aggregate traces to immutable Run records.
The primary failure category follows the PRD precedence, and a runner_error
invalidates the paired observation instead of being counted as a strategy
failure.
"""

from __future__ import annotations

import random
from collections import Counter, defaultdict

FAILURE_PRECEDENCE = [
    "runner_error",
    "replay_mismatch",
    "tool_pair_broken",
    "compaction_commit_failed",
    "summary_invalid",
    "critical_fact_lost",
    "context_overflow_unrecovered",
    "budget_exhausted",
    "artifact_verifier_failed",
    "model_stop_failure",
]


def _ratio(numerator, denominator):
    return numerator / denominator if denominator else 0.0


def _mean(values):
    values = list(values)
    return sum(values) / len(values) if values else 0.0


def _percentile(values, q: float) -> float:
    """Linear-interpolation percentile over non-negative numeric values."""
    values = sorted(float(value) for value in values)
    if not values:
        return 0.0
    if len(values) == 1:
        return values[0]
    rank = (len(values) - 1) * q
    lower = int(rank)
    upper = lower + 1
    if upper >= len(values):
        return values[-1]
    fraction = rank - lower
    return values[lower] * (1.0 - fraction) + values[upper] * fraction


def _block_key(row: dict) -> tuple:
    return (row.get("task_id"), row.get("repetition"), row.get("budget"))


def _row_dispatch_count(row: dict) -> int:
    events = row.get("events") or []
    return sum(1 for event in events if event.get("type") == "request/dispatch")


def _row_compaction_attempts(row: dict) -> int:
    events = row.get("events") or []
    return sum(1 for event in events if event.get("type") == "compaction/end")


def _row_compaction_successes(row: dict) -> int:
    events = row.get("events") or []
    return sum(
        1 for event in events
        if event.get("type") == "compaction/end" and event.get("data", {}).get("success")
    )


def _row_summary_fallback_count(row: dict) -> int:
    """Count failed compaction attempts that were followed by whole-unit removal.

    A summary fallback is a failed ``compaction/end`` whose remaining pressure
    was then handled by deterministic whole-unit removal (a ``context/summary``
    event with ``kind == "removal"``) before the next request dispatch.
    """
    events = row.get("events") or []
    failed_ends = [
        event for event in events
        if event.get("type") == "compaction/end" and not event.get("data", {}).get("success")
    ]
    fallbacks = 0
    for failed in failed_ends:
        failed_seq = int(failed.get("seq", -1))
        next_dispatch_seq = next(
            (
                int(event.get("seq", -1))
                for event in events
                if event.get("type") == "request/dispatch" and int(event.get("seq", -1)) > failed_seq
            ),
            None,
        )
        for event in events:
            seq = int(event.get("seq", -1))
            if failed_seq < seq and (next_dispatch_seq is None or seq < next_dispatch_seq):
                if (
                    event.get("type") == "context/summary"
                    and event.get("data", {}).get("kind") == "removal"
                ):
                    fallbacks += 1
                    break
    return fallbacks


def _row_cache_telemetry(row: dict) -> dict:
    """Cache telemetry recorded by the public-verifier subprocess, if any."""
    artifact = row.get("artifact") or {}
    public = artifact.get("public_verifier") or {}
    telemetry = public.get("cache_telemetry") or {}
    return telemetry if isinstance(telemetry, dict) else {}


def _row_has_cache_telemetry(row: dict) -> bool:
    return isinstance(_row_cache_telemetry(row), dict) and bool(
        (row.get("artifact") or {}).get("public_verifier", {}).get("cache_telemetry")
    )


def _usage_totals(rows: list[dict]) -> dict:
    """Aggregate provider usage and summary-call usage token totals.

    ``SummaryUsage.as_dict`` serializes all counters with a ``summary_``
    prefix, so both the main loop usage and the summarizer usage read from the
    same key names even though only one of them is a summary-call meter.
    """
    totals = {
        "main_usage_calls": 0,
        "main_usage_input_tokens": 0,
        "main_usage_output_tokens": 0,
        "main_usage_total_tokens": 0,
        "summary_usage_calls": 0,
        "summary_usage_input_tokens": 0,
        "summary_usage_output_tokens": 0,
        "summary_usage_total_tokens": 0,
        "total_usage_calls": 0,
        "total_usage_input_tokens": 0,
        "total_usage_output_tokens": 0,
        "total_usage_tokens": 0,
    }
    for row in rows:
        main = row.get("main_usage") or {}
        summary = row.get("summary_usage") or {}
        totals["main_usage_calls"] += int(main.get("summary_calls", 0) or 0)
        totals["main_usage_input_tokens"] += int(main.get("summary_input_tokens", 0) or 0)
        totals["main_usage_output_tokens"] += int(main.get("summary_output_tokens", 0) or 0)
        totals["main_usage_total_tokens"] += int(main.get("summary_total_tokens", 0) or 0)
        totals["summary_usage_calls"] += int(summary.get("summary_calls", 0) or 0)
        totals["summary_usage_input_tokens"] += int(summary.get("summary_input_tokens", 0) or 0)
        totals["summary_usage_output_tokens"] += int(summary.get("summary_output_tokens", 0) or 0)
        totals["summary_usage_total_tokens"] += int(summary.get("summary_total_tokens", 0) or 0)
    totals["total_usage_calls"] = totals["main_usage_calls"] + totals["summary_usage_calls"]
    totals["total_usage_input_tokens"] = totals["main_usage_input_tokens"] + totals["summary_usage_input_tokens"]
    totals["total_usage_output_tokens"] = totals["main_usage_output_tokens"] + totals["summary_usage_output_tokens"]
    totals["total_usage_tokens"] = totals["main_usage_total_tokens"] + totals["summary_usage_total_tokens"]
    return totals


def _paired_valid_rows(rows: list[dict]) -> list[dict]:
    """Rows whose (task_id, repetition, budget) block has no runner error."""
    blocks = defaultdict(list)
    for row in rows:
        blocks[_block_key(row)].append(row)
    invalid_blocks = {
        key for key, items in blocks.items() if any(row.get("runner_error") for row in items)
    }
    return [row for row in rows if _block_key(row) not in invalid_blocks]


def _failure_flags(row: dict) -> dict:
    """Deterministic failure flags from observable Run fields.

    These flags feed the PRD failure precedence. A row may carry more than
    one flag; ``_primary_failure`` selects the highest-precedence one.
    """
    flags = {}
    if row.get("runner_error"):
        flags["runner_error"] = True
        return flags
    if row.get("run_pass") is True or row.get("run_pass") is None:
        return flags

    trace_checks = row.get("trace_checks") or {}
    if trace_checks.get("request_replayable") is False and _row_dispatch_count(row) > 0:
        flags["replay_mismatch"] = True
    if trace_checks.get("tool_pairs_balanced") is False:
        flags["tool_pair_broken"] = True
    if trace_checks.get("partial_transaction_zero") is False:
        flags["compaction_commit_failed"] = True

    if row.get("summary_error_claims", 0) > 0:
        flags["summary_invalid"] = True

    if row.get("context_pass") is False:
        # Fact retention failure is the dangerous compaction outcome. The
        # context oracle may also fail for other contradiction reasons, so
        # only set critical_fact_lost when a required fact is missing.
        if int(row.get("context_fact_retention", 0) or 0) < int(
            row.get("context_required_facts", 0) or 0
        ):
            flags["critical_fact_lost"] = True

    if row.get("observed_overflow") and not row.get("recovered_overflow"):
        flags["context_overflow_unrecovered"] = True

    if row.get("artifact_pass") is False:
        flags["artifact_verifier_failed"] = True

    stop_reason = str(row.get("stop_reason") or "")
    if stop_reason == "model_stop_failure":
        flags["model_stop_failure"] = True
    elif stop_reason.startswith("preparation_failed:"):
        code = stop_reason.split(":", 1)[1]
        if code == "no_safe_range":
            flags["budget_exhausted"] = True

    return flags


def _primary_failure(row: dict) -> str:
    """Choose the primary failure by PRD precedence, then secondary tags."""
    if row.get("runner_error"):
        return "runner_error"
    flags = _failure_flags(row)
    for category in FAILURE_PRECEDENCE:
        if flags.get(category) or row.get(f"fail_{category}"):
            return category
    stop_reason = str(row.get("stop_reason") or "")
    if stop_reason.startswith("preparation_failed:"):
        return stop_reason.split(":", 1)[1]
    if stop_reason:
        return stop_reason
    return row.get("primary_failure") or "unknown"


def primary_failure_for_row(row: dict) -> str | None:
    """Return the persisted failure category for a failed run only."""
    if row.get("run_pass") is not False and not row.get("runner_error"):
        return None
    return _primary_failure(row)


def _condition_summary(items: list[dict]) -> dict:
    evaluated_items = [row for row in items if row.get("artifact_pass") is not None]
    replay_rows = [
        row for row in items
        if "request_replayable" in (row.get("trace_checks") or {})
        and _row_dispatch_count(row) > 0
    ]
    return {
        "runs": len(items),
        "evaluated_runs": len(evaluated_items),
        "passed": sum(1 for row in evaluated_items if row.get("run_pass") is True),
        "pass_rate": _ratio(
            sum(1 for row in evaluated_items if row.get("run_pass") is True),
            len(evaluated_items),
        ),
        "mechanics_pass_rate": _ratio(
            sum(1 for row in items if row.get("mechanics_pass", row.get("run_pass")) is True),
            len(items),
        ),
        "unrecovered_overflow_rate": _ratio(
            sum(1 for row in items if row.get("observed_overflow") and not row.get("recovered_overflow")),
            len(items),
        ),
        "critical_fact_retention": _ratio(
            sum(row.get("context_fact_retention", 0) for row in items),
            sum(row.get("context_required_facts", 0) for row in items),
        ),
        "summary_factual_error_rate": _ratio(
            sum(row.get("summary_error_claims", 0) for row in items),
            sum(row.get("summary_total_claims", 0) for row in items),
        ),
        "tool_pair_integrity_rate": _ratio(
            sum(1 for row in items if row.get("trace_checks", {}).get("tool_pairs_balanced")),
            len(items),
        ),
        "replay_integrity_rate": _ratio(
            sum(1 for row in replay_rows if row.get("trace_checks", {}).get("request_replayable")),
            len(replay_rows),
        ),
        "replay_evaluated_runs": len(replay_rows),
        "dispatch_bearing_runs": sum(1 for row in items if _row_dispatch_count(row) > 0),
        "summary_fallback_count": sum(_row_summary_fallback_count(row) for row in items),
        "compaction_attempts": sum(_row_compaction_attempts(row) for row in items),
        "compaction_successes": sum(_row_compaction_successes(row) for row in items),
    }


def aggregate_rows(rows: list[dict]) -> dict:
    rows = list(rows)
    evaluated = [row for row in rows if row.get("artifact_pass") is not None]
    passed = [row for row in evaluated if row.get("run_pass") is True]
    completed = [row for row in rows if not row.get("runner_error")]
    artifact_evaluated = evaluated
    overflow_rows = [row for row in rows if row.get("observed_overflow")]
    recovered = [row for row in overflow_rows if row.get("recovered_overflow")]

    condition_rows = defaultdict(list)
    for row in rows:
        condition_rows[str(row.get("condition", "unknown"))].append(row)

    failure_counts = Counter()
    for row in rows:
        if row.get("run_pass") is False:
            failure_counts[_primary_failure(row)] += 1

    # Paired-block handling: a runner_error invalidates the whole
    # (task_id, repetition, budget) block for paired comparisons.
    blocks = defaultdict(list)
    for row in rows:
        blocks[_block_key(row)].append(row)
    blocks_with_runner_errors = {
        key for key, items in blocks.items() if any(row.get("runner_error") for row in items)
    }
    paired_valid_rows = [
        row for row in rows if _block_key(row) not in blocks_with_runner_errors
    ]
    paired_condition_rows = defaultdict(list)
    for row in paired_valid_rows:
        paired_condition_rows[str(row.get("condition", "unknown"))].append(row)

    durations = [float(row.get("duration_ms", 0) or 0) for row in rows]
    cache_rows = [row for row in rows if _row_has_cache_telemetry(row)]
    usage_totals = _usage_totals(rows)

    return {
        "total_runs": len(rows),
        "completed_runs": len(completed),
        "runner_errors": len(rows) - len(completed),
        "passed_runs": len(passed),
        "pass_rate": _ratio(len(passed), len(evaluated)),
        "completed_pass_rate": _ratio(
            len(passed), len([row for row in completed if row.get("artifact_pass") is not None])
        ),
        "mechanics_pass_rate": _ratio(
            sum(1 for row in rows if row.get("mechanics_pass", row.get("run_pass")) is True),
            len(completed),
        ),
        "pass_rate_scope": "artifact_evaluated_runs",
        "artifact_evaluated_runs": len(artifact_evaluated),
        "artifact_pass_rate": _ratio(
            sum(1 for row in artifact_evaluated if row.get("artifact_pass")),
            len(artifact_evaluated),
        ),
        "first_overflow_rate": _ratio(
            sum(1 for row in rows if row.get("observed_overflow")),
            len(completed),
        ),
        "overflow_recovery_rate": _ratio(len(recovered), len(overflow_rows)),
        "unrecovered_overflow_rate": _ratio(
            sum(1 for row in overflow_rows if not row.get("recovered_overflow")),
            len(overflow_rows),
        ),
        "critical_fact_retention_rate": _ratio(
            sum(row.get("context_fact_retention", 0) for row in rows),
            sum(row.get("context_required_facts", 0) for row in rows),
        ),
        "summary_factual_error_rate": _ratio(
            sum(row.get("summary_error_claims", 0) for row in rows),
            sum(row.get("summary_total_claims", 0) for row in rows),
        ),
        "failure_category_counts": dict(sorted(failure_counts.items())),
        "conditions": {
            condition: _condition_summary(items)
            for condition, items in sorted(condition_rows.items())
        },
        # Paired-block metrics over blocks without runner errors.
        "blocks_with_runner_errors": len(blocks_with_runner_errors),
        "paired_valid_blocks": len(
            {
                _block_key(row)
                for row in paired_valid_rows
                if row.get("condition") in ("A", "B", "C", "D")
            }
        ),
        "paired_conditions": {
            condition: _condition_summary(items)
            for condition, items in sorted(paired_condition_rows.items())
        },
        "avg_tool_steps": _mean(row.get("tool_steps", 0) for row in rows),
        "avg_attempts": _mean(row.get("attempts", 0) for row in rows),
        "avg_input_tokens": _mean(row.get("estimated_input_tokens", 0) for row in rows),
        "avg_compactions": _mean(_row_compaction_successes(row) for row in rows),
        "avg_compaction_attempts": _mean(_row_compaction_attempts(row) for row in rows),
        "summary_fallback_count": sum(_row_summary_fallback_count(row) for row in rows),
        "dispatch_bearing_runs": sum(1 for row in rows if _row_dispatch_count(row) > 0),
        "dispatch_bearing_replay_integrity_rate": _ratio(
            sum(
                1
                for row in rows
                if _row_dispatch_count(row) > 0
                and "request_replayable" in (row.get("trace_checks") or {})
                and row.get("trace_checks", {}).get("request_replayable")
            ),
            sum(
                1
                for row in rows
                if _row_dispatch_count(row) > 0
                and "request_replayable" in (row.get("trace_checks") or {})
            ),
        ),
        "avg_latency_ms": _mean(row.get("duration_ms", 0) for row in rows),
        "median_latency_ms": _percentile(durations, 0.50),
        "p95_latency_ms": _percentile(durations, 0.95),
        "overflow_observed_runs": len(overflow_rows),
        "overflow_recovered_runs": len(recovered),
        "overflow_unrecovered_runs": len(overflow_rows) - len(recovered),
        "cache_telemetry_runs": len(cache_rows),
        "pytest_cache_dirs_before_total": sum(
            int(_row_cache_telemetry(row).get("pytest_cache_dir_count_before", 0) or 0)
            for row in cache_rows
        ),
        "pytest_cache_dirs_after_total": sum(
            int(_row_cache_telemetry(row).get("pytest_cache_dir_count_after", 0) or 0)
            for row in cache_rows
        ),
        "pytest_cache_created_runs": sum(
            1
            for row in cache_rows
            if _row_cache_telemetry(row).get("pytest_cache_created")
        ),
        **usage_totals,
        "paired_tasks": len(
            {
                _block_key(row)
                for row in paired_valid_rows
                if row.get("condition") in ("A", "B", "C", "D")
            }
        ),
    }


def condition_delta(aggregate: dict, left: str = "D", right: str = "A") -> dict:
    """Paired D-vs-A delta over blocks without runner errors.

    Falls back to the full condition summaries when paired summaries are not
    present (older aggregate payloads).
    """
    paired = aggregate.get("paired_conditions") or aggregate.get("conditions") or {}
    left_summary = paired.get(left, {})
    right_summary = paired.get(right, {})
    return {
        "pass_rate_delta": float(left_summary.get("pass_rate", 0.0)) - float(right_summary.get("pass_rate", 0.0)),
        "unrecovered_overflow_delta": float(left_summary.get("unrecovered_overflow_rate", 0.0))
        - float(right_summary.get("unrecovered_overflow_rate", 0.0)),
        "critical_fact_retention_delta": float(left_summary.get("critical_fact_retention", 0.0))
        - float(right_summary.get("critical_fact_retention", 0.0)),
    }


def _quick_summary(items: list[dict]) -> dict:
    """Pass/overflow/retention rates for bootstrap samples without scanning
    per-row event lists (the point aggregates already own event scans)."""
    evaluated = [row for row in items if row.get("artifact_pass") is not None]
    overflow_rows = [row for row in items if row.get("observed_overflow")]
    return {
        "pass_rate": _ratio(
            sum(1 for row in evaluated if row.get("run_pass") is True),
            len(evaluated),
        ),
        "unrecovered_overflow_rate": _ratio(
            sum(1 for row in overflow_rows if not row.get("recovered_overflow")),
            len(overflow_rows),
        ),
        "critical_fact_retention": _ratio(
            sum(row.get("context_fact_retention", 0) for row in items),
            sum(row.get("context_required_facts", 0) for row in items),
        ),
    }


def task_clustered_bootstrap(
    rows: list[dict],
    *,
    left: str = "D",
    right: str = "A",
    seed: int = 0,
    iterations: int = 2000,
) -> dict:
    """Task-clustered bootstrap for D-vs-A deltas over paired valid rows.

    The cluster unit is a task: each bootstrap replicate resamples tasks with
    replacement and recomputes both condition summaries from the resampled
    task rows. This preserves within-task correlation while treating tasks as
    the independent sampling units for uncertainty.
    """
    paired = [row for row in _paired_valid_rows(rows) if row.get("condition") in (left, right)]
    task_rows = defaultdict(list)
    for row in paired:
        task_id = str(row.get("task_id", ""))
        if task_id:
            task_rows[task_id].append(row)
    tasks = sorted(
        task_id
        for task_id, task_items in task_rows.items()
        if any(row.get("condition") == left for row in task_items)
        and any(row.get("condition") == right for row in task_items)
    )
    if not tasks:
        return {
            "task_clustered_bootstrap": {
                "seed": int(seed),
                "iterations": int(iterations),
                "task_clusters": 0,
                "paired_rows": len(paired),
            },
            "pass_rate_delta_ci95_low": 0.0,
            "pass_rate_delta_ci95_high": 0.0,
            "unrecovered_overflow_delta_ci95_low": 0.0,
            "unrecovered_overflow_delta_ci95_high": 0.0,
            "critical_fact_retention_delta_ci95_low": 0.0,
            "critical_fact_retention_delta_ci95_high": 0.0,
        }

    rng = random.Random(int(seed))
    pass_deltas = []
    overflow_deltas = []
    retention_deltas = []
    for _ in range(int(iterations)):
        sampled = rng.choices(tasks, k=len(tasks))
        sample_rows = []
        for task_id in sampled:
            sample_rows.extend(task_rows[task_id])
        left_summary = _quick_summary([row for row in sample_rows if row.get("condition") == left])
        right_summary = _quick_summary([row for row in sample_rows if row.get("condition") == right])
        pass_deltas.append(left_summary["pass_rate"] - right_summary["pass_rate"])
        overflow_deltas.append(
            left_summary["unrecovered_overflow_rate"] - right_summary["unrecovered_overflow_rate"]
        )
        retention_deltas.append(
            left_summary["critical_fact_retention"] - right_summary["critical_fact_retention"]
        )

    return {
        "task_clustered_bootstrap": {
            "seed": int(seed),
            "iterations": int(iterations),
            "task_clusters": len(tasks),
            "paired_rows": len(paired),
        },
        "pass_rate_delta_ci95_low": _percentile(pass_deltas, 0.025),
        "pass_rate_delta_ci95_high": _percentile(pass_deltas, 0.975),
        "pass_rate_delta_bootstrap_mean": _mean(pass_deltas),
        "unrecovered_overflow_delta_ci95_low": _percentile(overflow_deltas, 0.025),
        "unrecovered_overflow_delta_ci95_high": _percentile(overflow_deltas, 0.975),
        "unrecovered_overflow_delta_bootstrap_mean": _mean(overflow_deltas),
        "critical_fact_retention_delta_ci95_low": _percentile(retention_deltas, 0.025),
        "critical_fact_retention_delta_ci95_high": _percentile(retention_deltas, 0.975),
        "critical_fact_retention_delta_bootstrap_mean": _mean(retention_deltas),
    }
