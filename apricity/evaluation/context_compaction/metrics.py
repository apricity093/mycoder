"""Deterministic aggregation and failure precedence for context-compaction Runs.

Pure functions: every number in the aggregate traces to immutable Run records.
The primary failure category follows the PRD precedence, and a runner_error
invalidates the paired observation instead of being counted as a strategy
failure.
"""

from __future__ import annotations

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


def _primary_failure(row: dict) -> str:
    """Choose the primary failure by PRD precedence, then optional secondary
    tags. A runner_error always outranks strategy failures."""
    if row.get("runner_error"):
        return "runner_error"
    for category in FAILURE_PRECEDENCE:
        if row.get(f"fail_{category}") or row.get("primary_failure") == category:
            return category
    stop_reason = str(row.get("stop_reason") or "")
    if stop_reason.startswith("preparation_failed:"):
        return stop_reason.split(":", 1)[1]
    if stop_reason == "model_stop_failure":
        return "model_stop_failure"
    if stop_reason:
        return stop_reason
    return row.get("primary_failure") or "unknown"


def aggregate_rows(rows: list[dict]) -> dict:
    rows = list(rows)
    evaluated = [row for row in rows if row.get("artifact_pass") is not None]
    passed = [row for row in evaluated if row.get("run_pass") is True]
    completed = [row for row in rows if not row.get("runner_error")]
    # Runs whose artifact dimension was skipped (deterministic gate) still
    # count toward the context/trace mechanics pass rate.
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

    def condition_summary(items):
        evaluated_items = [row for row in items if row.get("artifact_pass") is not None]
        return {
            "runs": len(items),
            "evaluated_runs": len(evaluated_items),
            "passed": sum(1 for row in evaluated_items if row.get("run_pass") is True),
            "pass_rate": _ratio(sum(1 for row in evaluated_items if row.get("run_pass") is True), len(evaluated_items)),
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
                sum(1 for row in items if row.get("trace_checks", {}).get("request_replayable")),
                len(items),
            ),
        }

    return {
        "total_runs": len(rows),
        "completed_runs": len(completed),
        "runner_errors": len(rows) - len(completed),
        "passed_runs": len(passed),
        "pass_rate": _ratio(len(passed), len(evaluated)),
        "completed_pass_rate": _ratio(len(passed), len([row for row in completed if row.get("artifact_pass") is not None])),
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
            condition: condition_summary(items)
            for condition, items in sorted(condition_rows.items())
        },
        "avg_tool_steps": _mean(row.get("tool_steps", 0) for row in rows),
        "avg_attempts": _mean(row.get("attempts", 0) for row in rows),
        "avg_input_tokens": _mean(row.get("estimated_input_tokens", 0) for row in rows),
        "avg_compactions": _mean(row.get("compaction_count", 0) for row in rows),
        "avg_latency_ms": _mean(row.get("duration_ms", 0) for row in rows),
        "paired_tasks": len(
            {
                (row.get("task_id"), row.get("repetition"), row.get("budget"))
                for row in rows
                if row.get("condition") in ("A", "B", "C", "D")
            }
        ),
    }


def condition_delta(aggregate: dict, left: str = "D", right: str = "A") -> dict:
    left_summary = aggregate.get("conditions", {}).get(left, {})
    right_summary = aggregate.get("conditions", {}).get(right, {})
    return {
        "pass_rate_delta": float(left_summary.get("pass_rate", 0.0)) - float(right_summary.get("pass_rate", 0.0)),
        "unrecovered_overflow_delta": float(left_summary.get("unrecovered_overflow_rate", 0.0))
        - float(right_summary.get("unrecovered_overflow_rate", 0.0)),
        "critical_fact_retention_delta": float(left_summary.get("critical_fact_retention", 0.0))
        - float(right_summary.get("critical_fact_retention", 0.0)),
    }
