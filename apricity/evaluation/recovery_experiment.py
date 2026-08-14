"""Run deterministic compatibility-versus-resilient recovery experiments."""

from datetime import datetime, timezone
import json
from pathlib import Path
from time import perf_counter

from .evaluator import BenchmarkEvaluator, load_benchmark, summarize_rows
from .faults import (
    FAULT_CONTEXT_OVERFLOW,
    FAULT_MALFORMED_RESPONSE,
    FAULT_NONE,
    FAULT_OVERSIZED_TOOL_RESULT,
    FAULT_TRANSIENT_PROVIDER,
    FaultPlan,
)
from ..recovery import (
    POLICY_CONTEXT_RECOVERY,
    POLICY_PROTOCOL_CORRECTION,
    POLICY_PROVIDER_RETRY,
    POLICY_TOOL_RESULT_SPILL,
)


DEFAULT_FAULTS = (
    FAULT_NONE,
    FAULT_TRANSIENT_PROVIDER,
    FAULT_CONTEXT_OVERFLOW,
    FAULT_OVERSIZED_TOOL_RESULT,
    FAULT_MALFORMED_RESPONSE,
)
VARIANT_SPECS = {
    "compatible": {"mode": "compatible", "disabled_policies": ()},
    "resilient": {"mode": "resilient", "disabled_policies": ()},
    "resilient_without_provider_retry": {
        "mode": "resilient",
        "disabled_policies": (POLICY_PROVIDER_RETRY,),
    },
    "resilient_without_context_recovery": {
        "mode": "resilient",
        "disabled_policies": (POLICY_CONTEXT_RECOVERY,),
    },
    "resilient_without_tool_result_spill": {
        "mode": "resilient",
        "disabled_policies": (POLICY_TOOL_RESULT_SPILL,),
    },
    "resilient_without_protocol_correction": {
        "mode": "resilient",
        "disabled_policies": (POLICY_PROTOCOL_CORRECTION,),
    },
}


def _recovery_summary(rows):
    summary = summarize_rows(rows)
    faulted = [row for row in rows if (row.get("fault_injection") or {}).get("injected")]
    recovered = [row for row in faulted if row.get("passed")]
    no_fault = [
        row for row in rows if (row.get("fault_injection") or {}).get("planned") == FAULT_NONE
    ]
    false_interventions = [row for row in no_fault if int(row.get("recovery_actions", 0)) > 0]
    summary.update(
        {
            "faulted_runs": len(faulted),
            "recovered_runs": len(recovered),
            "recovery_rate": len(recovered) / len(faulted) if faulted else 0.0,
            "no_fault_runs": len(no_fault),
            "false_intervention_rate": (
                len(false_interventions) / len(no_fault) if no_fault else 0.0
            ),
            "duplicate_side_effect_rate": (
                sum(1 for row in rows if int(row.get("duplicate_side_effects", 0)) > 0) / len(rows)
                if rows
                else 0.0
            ),
            "avg_tool_steps": (
                sum(int(row.get("tool_steps", 0)) for row in rows) / len(rows) if rows else 0.0
            ),
            "avg_attempts": (
                sum(int(row.get("attempts", 0)) for row in rows) / len(rows) if rows else 0.0
            ),
            "avg_recovery_actions": (
                sum(int(row.get("recovery_actions", 0)) for row in rows) / len(rows)
                if rows
                else 0.0
            ),
            "avg_duration_seconds": (
                sum(float(row.get("duration_seconds", 0.0)) for row in rows) / len(rows)
                if rows
                else 0.0
            ),
            "recorded_total_tokens": sum(
                int(((row.get("report") or {}).get("prompt_metadata") or {}).get("total_tokens", 0) or 0)
                for row in rows
            ),
            "no_fault_avg_duration_seconds": (
                sum(float(row.get("duration_seconds", 0.0)) for row in no_fault) / len(no_fault)
                if no_fault
                else 0.0
            ),
        }
    )
    return summary


def _render_report(payload):
    lines = [
        "# Apricity Recovery Policy Experiment",
        "",
        f"- Captured at: {payload['captured_at']}",
        f"- Benchmark: `{payload['benchmark']['source']}`",
        f"- Tasks: {payload['benchmark']['task_count']}",
        f"- Faults: {', '.join(payload['faults'])}",
        "",
        "## Results",
        "",
        "| Variant | Pass rate | Recovery rate | False intervention | Duplicate side effects | Avg attempts | Avg duration |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for name, variant in payload["variants"].items():
        item = variant["summary"]
        lines.append(
            f"| {name} | {item['pass_rate']:.2%} | {item['recovery_rate']:.2%} | "
            f"{item['false_intervention_rate']:.2%} | {item['duplicate_side_effect_rate']:.2%} | "
            f"{item['avg_attempts']:.2f} | {item['avg_duration_seconds']:.3f}s |"
        )
    comparison = payload.get("comparison") or {}
    lines.extend(
        [
            "",
            "## Comparison",
            "",
            f"- Recovery-rate delta: {comparison.get('recovery_rate_delta', 0.0):+.2%}",
            f"- Pass-rate delta: {comparison.get('pass_rate_delta', 0.0):+.2%}",
            f"- No-fault duration overhead: {comparison.get('no_fault_duration_overhead', 0.0):+.2%}",
            "",
            "## Interpretation limits",
            "",
            "- This is a deterministic fixture experiment, not a production reliability claim.",
            "- A disabled policy is credited only when its ablation changes verifier-scored outcomes.",
            "- The current oversized-output fixtures do not require tail retrieval, so the spill ablation does not change pass rate.",
            "- Real-model confirmation is a separate, explicitly authorized run.",
        ]
    )
    return "\n".join(lines) + "\n"


def run_recovery_policy_experiment(
    *,
    benchmark_path="benchmarks/coding_tasks.json",
    artifact_path="artifacts/recovery-policy-experiment-v1.json",
    workspace_root="artifacts/recovery-policy-workspaces",
    task_ids=None,
    faults=DEFAULT_FAULTS,
    variants=tuple(VARIANT_SPECS),
):
    benchmark_path = Path(benchmark_path)
    benchmark = load_benchmark(benchmark_path)
    selected = [
        task for task in benchmark["tasks"] if task_ids is None or task["id"] in set(task_ids)
    ]
    if not selected:
        raise ValueError("recovery experiment selected no tasks")

    variant_payloads = {}
    for variant in variants:
        if variant not in VARIANT_SPECS:
            raise ValueError(f"unknown recovery experiment variant: {variant}")
        variant_spec = VARIANT_SPECS[variant]
        rows = []
        for fault_kind in faults:
            evaluator = BenchmarkEvaluator(
                benchmark_path=benchmark_path,
                workspace_root=Path(workspace_root) / variant / fault_kind,
                recovery_mode=variant_spec["mode"],
                fault_plan=FaultPlan(kind=fault_kind),
                disabled_recovery_policies=variant_spec["disabled_policies"],
            )
            for task in selected:
                started = perf_counter()
                row = evaluator.run_task(task)
                row["duration_seconds"] = round(perf_counter() - started, 6)
                row["variant"] = variant
                row["fault_type"] = fault_kind
                rows.append(row)
        variant_payloads[variant] = {
            "configuration": {
                "mode": variant_spec["mode"],
                "disabled_policies": list(variant_spec["disabled_policies"]),
            },
            "summary": _recovery_summary(rows),
            "rows": rows,
        }

    payload = {
        "schema_version": 1,
        "artifact_type": "recovery-policy-experiment-v1",
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "benchmark": {"source": str(benchmark_path), "task_count": len(selected)},
        "faults": list(faults),
        "variants": variant_payloads,
    }
    compatible = (variant_payloads.get("compatible") or {}).get("summary") or {}
    resilient = (variant_payloads.get("resilient") or {}).get("summary") or {}
    compatible_duration = float(compatible.get("no_fault_avg_duration_seconds", 0.0))
    resilient_duration = float(resilient.get("no_fault_avg_duration_seconds", 0.0))
    payload["comparison"] = {
        "recovery_rate_delta": float(resilient.get("recovery_rate", 0.0))
        - float(compatible.get("recovery_rate", 0.0)),
        "pass_rate_delta": float(resilient.get("pass_rate", 0.0))
        - float(compatible.get("pass_rate", 0.0)),
        "no_fault_duration_overhead": (
            (resilient_duration - compatible_duration) / compatible_duration
            if compatible_duration
            else 0.0
        ),
    }
    artifact_path = Path(artifact_path)
    artifact_path.parent.mkdir(parents=True, exist_ok=True)
    artifact_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    report_path = artifact_path.with_suffix(".md")
    report_path.write_text(_render_report(payload), encoding="utf-8")
    payload["artifact_path"] = str(artifact_path)
    payload["report_path"] = str(report_path)
    return payload
