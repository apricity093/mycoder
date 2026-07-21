"""Small, repeatable real-model benchmark behind one evaluation interface."""

from collections import Counter, defaultdict
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import subprocess
from time import perf_counter

from .evaluator import BenchmarkEvaluator, load_benchmark
from ..providers.config import RuntimeConfig
from ..providers.factory import create_model_client, load_provider_config


DEFAULT_REAL_MODEL_BENCHMARK_PATH = Path("benchmarks/real_model_tasks.json")


def _ratio(numerator, denominator):
    return numerator / denominator if denominator else 0.0


def _mean(values):
    values = list(values)
    return sum(values) / len(values) if values else 0.0


def _git_value(repo_root, *args):
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=True,
            timeout=5,
        )
        return result.stdout.strip()
    except Exception:
        return ""


def _prompt_usage(row):
    metadata = (row.get("report") or {}).get("prompt_metadata") or {}
    return {
        "input_tokens": int(metadata.get("input_tokens", 0) or 0),
        "output_tokens": int(metadata.get("output_tokens", 0) or 0),
        "total_tokens": int(metadata.get("total_tokens", 0) or 0),
        "cached_tokens": int(metadata.get("cached_tokens", 0) or 0),
        "cache_telemetry_available": metadata.get("cache_telemetry_available") is True,
    }


def _summarize(rows, tasks, repetitions):
    passed_rows = [row for row in rows if row.get("passed")]
    category_rows = defaultdict(list)
    task_rows = defaultdict(list)
    for row in rows:
        category_rows[str(row.get("category", "unknown"))].append(row)
        task_rows[str(row.get("id", "unknown"))].append(row)

    failure_counts = Counter(
        str(row.get("failure_category") or "unknown")
        for row in rows
        if not row.get("passed")
    )
    usage_rows = [_prompt_usage(row) for row in rows]
    cache_rows = [usage for usage in usage_rows if usage["cache_telemetry_available"]]
    first_rows = [min(items, key=lambda item: int(item["repetition"])) for items in task_rows.values()]
    stable_passes = sum(
        1
        for task in tasks
        if len(task_rows[task["id"]]) == repetitions
        and all(row.get("passed") for row in task_rows[task["id"]])
    )

    def category_summary(items):
        return {
            "runs": len(items),
            "passed": sum(1 for row in items if row.get("passed")),
            "pass_rate": _ratio(sum(1 for row in items if row.get("passed")), len(items)),
            "avg_tool_steps": _mean(int(row.get("tool_steps", 0)) for row in items),
            "avg_duration_seconds": _mean(float(row.get("duration_seconds", 0.0)) for row in items),
        }

    return {
        "task_count": len(tasks),
        "repetitions": repetitions,
        "total_runs": len(rows),
        "passed_runs": len(passed_rows),
        "failed_runs": len(rows) - len(passed_rows),
        "pass_rate": _ratio(len(passed_rows), len(rows)),
        "pass_at_1": _ratio(sum(1 for row in first_rows if row.get("passed")), len(first_rows)),
        "stable_pass_rate": _ratio(stable_passes, len(tasks)),
        "within_budget_rate": _ratio(sum(1 for row in rows if row.get("within_budget")), len(rows)),
        "verifier_pass_rate": _ratio(sum(1 for row in rows if row.get("verifier_passed")), len(rows)),
        "avg_tool_steps": _mean(int(row.get("tool_steps", 0)) for row in rows),
        "avg_attempts": _mean(int(row.get("attempts", 0)) for row in rows),
        "avg_duration_seconds": _mean(float(row.get("duration_seconds", 0.0)) for row in rows),
        "input_tokens": sum(usage["input_tokens"] for usage in usage_rows),
        "output_tokens": sum(usage["output_tokens"] for usage in usage_rows),
        "total_tokens": sum(usage["total_tokens"] for usage in usage_rows),
        "cached_tokens": sum(usage["cached_tokens"] for usage in cache_rows),
        "cache_telemetry_run_count": len(cache_rows),
        "failure_category_counts": dict(sorted(failure_counts.items())),
        "categories": {
            category: category_summary(items)
            for category, items in sorted(category_rows.items())
        },
    }


def _write_json(path, payload):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _render_report(payload):
    summary = payload["summary"]
    lines = [
        "# Apricity Real-model Benchmark",
        "",
        "This report measures real provider behavior. It is separate from the deterministic harness regression.",
        "",
        "## Run",
        "",
        f"- Provider: {payload['provider']['name']}",
        f"- Model: {payload['provider']['model']}",
        f"- Tasks: {summary['task_count']}",
        f"- Repetitions: {summary['repetitions']}",
        f"- Total runs: {summary['total_runs']}",
        "",
        "## Results",
        "",
        f"- Pass rate: {summary['pass_rate']:.2%}",
        f"- Pass@1: {summary['pass_at_1']:.2%}",
        f"- Stable pass rate: {summary['stable_pass_rate']:.2%}",
        f"- Within-budget rate: {summary['within_budget_rate']:.2%}",
        f"- Verifier pass rate: {summary['verifier_pass_rate']:.2%}",
        f"- Average tool steps: {summary['avg_tool_steps']:.2f}",
        f"- Average attempts: {summary['avg_attempts']:.2f}",
        f"- Average duration: {summary['avg_duration_seconds']:.2f}s",
        f"- Recorded total tokens: {summary['total_tokens']}",
        "",
        "## Categories",
        "",
        "| Category | Runs | Pass rate | Avg tool steps | Avg duration |",
        "| --- | ---: | ---: | ---: | ---: |",
    ]
    for category, item in summary["categories"].items():
        lines.append(
            f"| {category} | {item['runs']} | {item['pass_rate']:.2%} | "
            f"{item['avg_tool_steps']:.2f} | {item['avg_duration_seconds']:.2f}s |"
        )
    lines.extend(
        [
            "",
            "## Failure categories",
            "",
        ]
    )
    if summary["failure_category_counts"]:
        for category, count in summary["failure_category_counts"].items():
            lines.append(f"- {category}: {count}")
    else:
        lines.append("- None")
    lines.extend(
        [
            "",
            "## Scope",
            "",
            "These small repository tasks demonstrate end-to-end tool use and verifier-scored coding behavior. They are not a substitute for SWE-bench or production traffic evaluation.",
        ]
    )
    return "\n".join(lines) + "\n"


def run_real_model_benchmark(
    *,
    benchmark_path=DEFAULT_REAL_MODEL_BENCHMARK_PATH,
    output_dir="benchmarks/results/real-model",
    workspace_root="artifacts/real-model-workspaces",
    provider=None,
    repetitions=3,
    max_new_tokens=512,
    request_timeout_seconds=300,
    model_client_factory=None,
    provider_config=None,
    progress=None,
    resume=False,
):
    """Run all tasks repeatedly, persist artifacts, and return one aggregate result.

    ``model_client_factory`` is the internal test seam. Production callers normally
    provide only a provider name and let this module construct fresh adapters.
    """
    benchmark_path = Path(benchmark_path)
    output_dir = Path(output_dir)
    workspace_root = Path(workspace_root)
    repetitions = int(repetitions)
    if repetitions < 1:
        raise ValueError("repetitions must be positive")

    benchmark = load_benchmark(benchmark_path)
    repo_root = benchmark_path.resolve().parent.parent
    if model_client_factory is None:
        config = provider_config or load_provider_config(provider=provider, start=repo_root)

        def model_client_factory(task, workspace):
            del task, workspace
            return create_model_client(
                config,
                temperature=0.0,
                timeout=int(request_timeout_seconds),
            )
    else:
        config = provider_config or RuntimeConfig(
            provider=str(provider or "injected"),
            model="injected-model-client",
            base_url="",
            api_key="",
        )

    artifact_path = output_dir / "real-model-benchmark.json"
    report_path = output_dir / "real-model-report.md"
    failures_root = output_dir / "failures"
    total_runs = len(benchmark["tasks"]) * repetitions
    benchmark_sha256 = hashlib.sha256(benchmark_path.read_bytes()).hexdigest()
    rows = []
    captured_at = datetime.now(timezone.utc).isoformat()
    if resume and artifact_path.exists():
        existing = json.loads(artifact_path.read_text(encoding="utf-8"))
        existing_provider = existing.get("provider") or {}
        existing_benchmark = existing.get("benchmark") or {}
        if existing_provider.get("name") != config.provider or existing_provider.get("model") != config.model:
            raise ValueError("cannot resume: provider or model differs from the existing artifact")
        if existing_benchmark.get("sha256") != benchmark_sha256:
            raise ValueError("cannot resume: benchmark content differs from the existing artifact")
        if int(existing.get("total_runs", total_runs)) != total_runs:
            raise ValueError("cannot resume: repetition count differs from the existing artifact")
        rows = list(existing.get("rows") or [])
        captured_at = str(existing.get("captured_at") or captured_at)

    base_payload = {
        "schema_version": 1,
        "artifact_type": "real-model-benchmark-v1",
        "status": "running",
        "captured_at": captured_at,
        "provider": {
            "name": config.provider,
            "model": config.model,
            "base_url": config.base_url,
        },
        "benchmark": {
            "source": str(benchmark_path),
            "sha256": benchmark_sha256,
            "task_count": len(benchmark["tasks"]),
        },
        "runtime": {
            "commit_sha": _git_value(repo_root, "rev-parse", "HEAD"),
            "branch": _git_value(repo_root, "branch", "--show-current"),
            "dirty": bool(_git_value(repo_root, "status", "--porcelain")),
        },
    }

    completed_keys = {
        (str(row.get("id")), int(row.get("repetition", 0)))
        for row in rows
    }
    completed = len(rows)
    for repetition in range(1, repetitions + 1):
        repetition_root = workspace_root / config.provider / f"repetition-{repetition:02d}"
        evaluator = BenchmarkEvaluator(
            benchmark_path=benchmark_path,
            artifact_path=output_dir / f"unused-repetition-{repetition:02d}.json",
            workspace_root=repetition_root,
            model_name=config.provider,
            model_version=config.model,
            max_new_tokens=int(max_new_tokens),
            model_client_factory=model_client_factory,
        )
        for task in benchmark["tasks"]:
            if (task["id"], repetition) in completed_keys:
                continue
            started = perf_counter()
            try:
                row = evaluator.run_task(task)
            except Exception as exc:
                row = {
                    "id": task["id"],
                    "category": task["category"],
                    "status": "fail",
                    "passed": False,
                    "within_budget": False,
                    "verifier_passed": False,
                    "tool_steps": 0,
                    "attempts": 0,
                    "failure_category": "runner_error",
                    "error": f"{type(exc).__name__}: {exc}",
                    "report": {},
                }
            row["repetition"] = repetition
            row["duration_seconds"] = round(perf_counter() - started, 6)
            row["workspace_root"] = str(repetition_root.resolve())
            rows.append(row)
            completed += 1

            if not row.get("passed"):
                _write_json(
                    failures_root / f"{task['id']}-r{repetition:02d}.json",
                    row,
                )
            partial = {
                **base_payload,
                "completed_runs": completed,
                "total_runs": total_runs,
                "rows": rows,
            }
            _write_json(artifact_path, partial)
            if progress is not None:
                progress(
                    {
                        "completed": completed,
                        "total": total_runs,
                        "task_id": task["id"],
                        "repetition": repetition,
                        "status": row["status"],
                        "duration_seconds": row["duration_seconds"],
                    }
                )

    payload = {
        **base_payload,
        "status": "completed",
        "completed_at": datetime.now(timezone.utc).isoformat(),
        "summary": _summarize(rows, benchmark["tasks"], repetitions),
        "rows": rows,
    }
    _write_json(artifact_path, payload)
    output_dir.mkdir(parents=True, exist_ok=True)
    report_path.write_text(_render_report(payload), encoding="utf-8")
    payload["artifact_path"] = str(artifact_path)
    payload["report_path"] = str(report_path)
    return payload
