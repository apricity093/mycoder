"""Deterministic Markdown rendering for context-compaction experiment reports."""

from __future__ import annotations


def _pct(value):
    return f"{float(value or 0.0):.2%}"


def _num(value, digits=2):
    return f"{float(value or 0.0):.{digits}f}"


def _int(value):
    return f"{int(value or 0):,}"


def _delta(value):
    return f"{float(value or 0.0):+.2%}"


def _artifact_hash_section(payload: dict, lines: list[str]) -> None:
    hashes = payload.get("completed_artifact_hashes") or {}
    lines.extend(["", "## Artifact hashes", ""])
    if not isinstance(hashes, dict) or not hashes:
        lines.append("- None (no completed-run artifact hashes recorded)")
        return
    artifact_names = ("report.json", "events.jsonl", "requests.jsonl", "trace.jsonl", "verifier.json")
    for run_name in sorted(hashes):
        run_hashes = hashes.get(run_name) or {}
        lines.append(f"- `{run_name}`")
        for artifact_name in artifact_names:
            digest = str(run_hashes.get(artifact_name) or "")
            lines.append(f"  - {artifact_name}: `{digest}`")


def _token_totals_section(summary: dict, lines: list[str]) -> None:
    lines.extend(
        [
            "",
            "## Token totals",
            "",
            "| Meter | Calls | Input tokens | Output tokens | Total tokens |",
            "| --- | ---: | ---: | ---: | ---: |",
            f"| Main loop usage | {_int(summary.get('main_usage_calls', 0))} | "
            f"{_int(summary.get('main_usage_input_tokens', 0))} | "
            f"{_int(summary.get('main_usage_output_tokens', 0))} | "
            f"{_int(summary.get('main_usage_total_tokens', 0))} |",
            f"| Summary-call usage | {_int(summary.get('summary_usage_calls', 0))} | "
            f"{_int(summary.get('summary_usage_input_tokens', 0))} | "
            f"{_int(summary.get('summary_usage_output_tokens', 0))} | "
            f"{_int(summary.get('summary_usage_total_tokens', 0))} |",
            f"| Combined | {_int(summary.get('total_usage_calls', 0))} | "
            f"{_int(summary.get('total_usage_input_tokens', 0))} | "
            f"{_int(summary.get('total_usage_output_tokens', 0))} | "
            f"{_int(summary.get('total_usage_tokens', 0))} |",
        ]
    )


def _cache_telemetry_section(summary: dict, lines: list[str]) -> None:
    lines.extend(
        [
            "",
            "## Cache telemetry",
            "",
            f"- Public-verifier runs with telemetry: {summary.get('cache_telemetry_runs', 0)}",
            f"- Pytest cache dirs before verifier (total): {summary.get('pytest_cache_dirs_before_total', 0)}",
            f"- Pytest cache dirs after verifier (total): {summary.get('pytest_cache_dirs_after_total', 0)}",
            f"- Public-verifier runs that created a new pytest cache: {summary.get('pytest_cache_created_runs', 0)}",
        ]
    )
    if summary.get("pytest_cache_created_runs", 0):
        lines.append("- ⚠️ At least one public verifier created a pytest cache directory.")
    else:
        lines.append("- No public verifier created a pytest cache directory.")


def _comparison_section(payload: dict, summary: dict, lines: list[str]) -> None:
    comparison = payload.get("comparison") or {}
    bootstrap = comparison.get("task_clustered_bootstrap") or {}
    lines.extend(
        [
            "",
            "## D vs A comparison",
            "",
            f"- Pass-rate delta: {_delta(comparison.get('pass_rate_delta', 0.0))}",
            f"- Unrecovered-overflow delta: {_delta(comparison.get('unrecovered_overflow_delta', 0.0))}",
            f"- Critical-fact retention delta: {_delta(comparison.get('critical_fact_retention_delta', 0.0))}",
        ]
    )
    if isinstance(bootstrap, dict):
        lines.extend(
            [
                f"- Task-clustered bootstrap: {bootstrap.get('iterations', 0)} iterations, "
                f"seed {bootstrap.get('seed', 0)}, {bootstrap.get('task_clusters', 0)} task clusters, "
                f"{bootstrap.get('paired_rows', 0)} paired rows",
                f"- Pass-rate delta 95% CI: "
                f"[{_delta(comparison.get('pass_rate_delta_ci95_low', 0.0))}, "
                f"{_delta(comparison.get('pass_rate_delta_ci95_high', 0.0))}]",
                f"- Unrecovered-overflow delta 95% CI: "
                f"[{_delta(comparison.get('unrecovered_overflow_delta_ci95_low', 0.0))}, "
                f"{_delta(comparison.get('unrecovered_overflow_delta_ci95_high', 0.0))}]",
                f"- Critical-fact retention delta 95% CI: "
                f"[{_delta(comparison.get('critical_fact_retention_delta_ci95_low', 0.0))}, "
                f"{_delta(comparison.get('critical_fact_retention_delta_ci95_high', 0.0))}]",
            ]
        )
    lines.extend(["", "## Failure categories", ""])
    failure_counts = summary.get("failure_category_counts") or {}
    if failure_counts:
        for category, count in sorted(failure_counts.items()):
            lines.append(f"- {category}: {count}")
    else:
        lines.append("- None")


def render_report(payload: dict) -> str:
    summary = payload.get("aggregate", {})
    lines = [
        "# Apricity Replayable Context Compaction Experiment",
        "",
        f"- Experiment ID: {payload.get('experiment_id', '')}",
        f"- Status: {payload.get('status', '')}",
        f"- Version: {payload.get('version', '')}",
        f"- Provider: {payload.get('provider', '')}",
        f"- Model: {payload.get('model', '')}",
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
        f"- Overflow observed runs: {summary.get('overflow_observed_runs', 0)}",
        f"- Overflow recovered runs: {summary.get('overflow_recovered_runs', 0)}",
        f"- Overflow unrecovered runs: {summary.get('overflow_unrecovered_runs', 0)}",
        f"- Overflow recovery rate: {_pct(summary.get('overflow_recovery_rate', 0.0))}",
        f"- Unrecovered-overflow rate: {_pct(summary.get('unrecovered_overflow_rate', 0.0))}",
        f"- Critical-fact retention rate: {_pct(summary.get('critical_fact_retention_rate', 0.0))}",
        f"- Summary factual-error rate: {_pct(summary.get('summary_factual_error_rate', 0.0))}",
        f"- Replay integrity rate (dispatch-bearing): {_pct(summary.get('dispatch_bearing_replay_integrity_rate', 0.0))}",
        f"- Average tool steps: {_num(summary.get('avg_tool_steps', 0.0))}",
        f"- Average attempts: {_num(summary.get('avg_attempts', 0.0))}",
        f"- Average input tokens: {_num(summary.get('avg_input_tokens', 0.0))}",
        f"- Average compactions per run: {_num(summary.get('avg_compactions', 0.0))}",
        f"- Total compaction attempts: {summary.get('avg_compaction_attempts', 0.0) * summary.get('total_runs', 0):.0f}",
        f"- Total summary fallbacks: {summary.get('summary_fallback_count', 0)}",
        f"- Average latency (ms): {_num(summary.get('avg_latency_ms', 0.0))}",
        f"- Median latency (ms): {_num(summary.get('median_latency_ms', 0.0))}",
        f"- P95 latency (ms): {_num(summary.get('p95_latency_ms', 0.0))}",
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

    _artifact_hash_section(payload, lines)
    _token_totals_section(summary, lines)
    _cache_telemetry_section(summary, lines)
    _comparison_section(payload, summary, lines)

    d_summary = (summary.get("conditions") or {}).get("D", {})
    execution_mode = str(payload.get("execution_mode", "deterministic"))
    interpretation = (
        "- This report contains paid provider runs; provider latency and model behavior are part of the result."
        if execution_mode == "real"
        else "- This is a deterministic-gate report; no paid provider run was executed."
    )
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
