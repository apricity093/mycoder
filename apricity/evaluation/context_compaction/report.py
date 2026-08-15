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
        f"- Average tool steps: {summary.get('avg_tool_steps', 0.0):.2f}",
        f"- Average attempts: {summary.get('avg_attempts', 0.0):.2f}",
        f"- Average input tokens: {summary.get('avg_input_tokens', 0.0):.2f}",
        f"- Average compactions per run: {summary.get('avg_compactions', 0.0):.2f}",
        "",
        "## Conditions",
        "",
        "| Condition | Runs | Pass rate | Unrecovered overflow | Critical-fact retention | Tool-pair integrity | Replay integrity |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for condition, item in (summary.get("conditions") or {}).items():
        lines.append(
            f"| {condition} | {item['runs']} | {_pct(item['pass_rate'])} | "
            f"{_pct(item['unrecovered_overflow_rate'])} | {_pct(item['critical_fact_retention'])} | "
            f"{_pct(item['tool_pair_integrity_rate'])} | {_pct(item['replay_integrity_rate'])} |"
        )
    comparison = payload.get("comparison") or {}
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
    lines.extend(
        [
            "",
            "## Safety gates",
            "",
            f"- Tool-pair integrity (D): {_pct((summary.get('conditions', {}) or {}).get('D', {}).get('tool_pair_integrity_rate', 0.0))}",
            f"- Replay integrity (D): {_pct((summary.get('conditions', {}) or {}).get('D', {}).get('replay_integrity_rate', 0.0))}",
            f"- Controlled critical-fact retention (D): {_pct((summary.get('conditions', {}) or {}).get('D', {}).get('critical_fact_retention', 0.0))}",
            "",
            "## Interpretation limits",
            "",
            "- This is a deterministic-gate report when no paid provider run has been authorized.",
            "- 4096-token and 8192-token budgets are harness limits, not provider context windows.",
            "- Summary calls count toward D totals and are included in latency and token metrics.",
        ]
    )
    return "\n".join(lines) + "\n"
