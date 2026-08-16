"""Deterministic Markdown rendering for context-compaction experiment reports."""

from __future__ import annotations


def _pct(value):
    return f"{float(value):.2%}"


def render_report(payload: dict) -> str:
    summary = payload.get("aggregate", {})
    lines = [
        "# Apricity Replayable Context Compaction Experiment",
        "",
        f"- Experiment ID: {payload.get('experiment_id', '')}",
        f"- Status: {payload.get('status', '')}",
        f"- Task snapshot hash: `{payload.get('task_snapshot_hash', '')}`",
        f"- Schedule hash: `{payload.get('schedule_hash', '')}`",
        f"- Total runs: {summary.get('total_runs', 0)}",
        f"- Completed runs: {summary.get('completed_runs', 0)}",
        f"- Runner errors: {summary.get('runner_errors', 0)}",
        f"- Dispatch-bearing runs: {summary.get('dispatch_bearing_runs', 0)}",
        f"- Blocks with runner errors: {summary.get('blocks_with_runner_errors', 0)}",
        f"- Paired valid blocks: {summary.get('paired_valid_blocks', 0)}",
        "",
        "## Aggregate",
        "",
        f"- Pass rate (all runs): {_pct(summary.get('pass_rate', 0.0))}",
        f"- Pass rate (completed only): {_pct(summary.get('completed_pass_rate', 0.0))}",
        f"- First-overflow rate: {_pct(summary.get('first_overflow_rate', 0.0))}",
        f"- Overflow recovery rate: {_pct(summary.get('overflow_recovery_rate', 0.0))}",
        f"- Unrecovered-overflow rate: {_pct(summary.get('unrecovered_overflow_rate', 0.0))}",
        f"- Critical-fact retention rate: {_pct(summary.get('critical_fact_retention_rate', 0.0))}",
        f"- Summary factual-error rate: {_pct(summary.get('summary_factual_error_rate', 0.0))}",
        f"- Replay integrity rate (dispatch-bearing): {_pct(summary.get('dispatch_bearing_replay_integrity_rate', 0.0))}",
        f"- Average tool steps: {summary.get('avg_tool_steps', 0.0):.2f}",
        f"- Average attempts: {summary.get('avg_attempts', 0.0):.2f}",
        f"- Average input tokens: {summary.get('avg_input_tokens', 0.0):.2f}",
        f"- Average compactions per run: {summary.get('avg_compactions', 0.0):.2f}",
        f"- Total compaction attempts: {summary.get('avg_compaction_attempts', 0.0) * summary.get('total_runs', 0):.0f}",
        f"- Total summary fallbacks: {summary.get('summary_fallback_count', 0)}",
        "",
        "## Conditions",
        "",
        "| Condition | Runs | Pass rate | Unrecovered overflow | Critical-fact retention | Tool-pair integrity | Replay integrity (dispatch-bearing) |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for condition, item in (summary.get("conditions") or {}).items():
        lines.append(
            f"| {condition} | {item.get('runs', 0)} | {_pct(item.get('pass_rate', 0.0))} | "
            f"{_pct(item.get('unrecovered_overflow_rate', 0.0))} | {_pct(item.get('critical_fact_retention', 0.0))} | "
            f"{_pct(item.get('tool_pair_integrity_rate', 0.0))} | {_pct(item.get('replay_integrity_rate', 0.0))} |"
        )
    comparison = payload.get("comparison") or {}
    execution_mode = str(payload.get("execution_mode", "deterministic"))
    interpretation = (
        "- This report contains paid provider runs; provider latency and model behavior are part of the result."
        if execution_mode == "real"
        else "- This is a deterministic-gate report; no paid provider run was executed."
    )
    lines.extend(
        [
            "",
            "## D vs A comparison",
            "",
            f"- Pass-rate delta: {comparison.get('pass_rate_delta', 0.0):+.2%}",
            f"- Unrecovered-overflow delta: {comparison.get('unrecovered_overflow_delta', 0.0):+.2%}",
            f"- Critical-fact retention delta: {comparison.get('critical_fact_retention_delta', 0.0):+.2%}",
            "",
            "## Failure categories",
            "",
        ]
    )
    failure_counts = summary.get("failure_category_counts") or {}
    if failure_counts:
        for category, count in sorted(failure_counts.items()):
            lines.append(f"- {category}: {count}")
    else:
        lines.append("- None")
    d_summary = (summary.get("conditions") or {}).get("D", {})
    lines.extend(
        [
            "",
            "## Safety gates",
            "",
            f"- Tool-pair integrity (D): {_pct(d_summary.get('tool_pair_integrity_rate', 0.0))}",
            f"- Replay integrity (D): {_pct(d_summary.get('replay_integrity_rate', 0.0))}",
            f"- Controlled critical-fact retention (D): {_pct(d_summary.get('critical_fact_retention', 0.0))}",
            "",
            "## Interpretation limits",
            "",
            interpretation,
            "- 4096-token and 8192-token budgets are harness limits, not provider context windows.",
            "- Summary calls count toward D totals and are included in latency and token metrics.",
            "- Replay integrity is reported only over dispatch-bearing runs.",
        ]
    )
    return "\n".join(lines) + "\n"
