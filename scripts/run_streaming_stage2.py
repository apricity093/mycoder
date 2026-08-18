#!/usr/bin/env python3
"""PRD Stage 2: DeepSeek streaming vs --no-stream small matrix.

Runs the five-task Stage 2 subset once in each condition, then writes one
comparison artifact and report into a new unique benchmark result directory.
"""

import argparse
from datetime import datetime
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apricity.evaluation.faults import FaultPlan  # noqa: E402
from apricity.evaluation.real_model_benchmark import run_real_model_benchmark  # noqa: E402
from apricity.providers.config import PROVIDER_CHOICES  # noqa: E402


def _mean(values):
    values = [float(value) for value in values if value is not None]
    return sum(values) / len(values) if values else 0.0


def _ratio(numerator, denominator):
    return numerator / denominator if denominator else 0.0


def _condition_block(artifact, label):
    summary = artifact["summary"]
    rows = artifact["rows"]
    return {
        "label": label,
        "artifact": str(artifact["artifact_path"]),
        "runs": len(rows),
        "pass_rate": summary["pass_rate"],
        "avg_duration_seconds": summary["avg_duration_seconds"],
        "avg_ttft_ms": summary["avg_ttft_ms"],
        "avg_model_response_ms": summary["avg_model_response_ms"],
        "provider_error_rate": summary["provider_error_rate"],
        "partial_visible_rate": summary["partial_visible_rate"],
        "cancelled_runs": summary["cancelled_runs"],
        "avg_tool_steps": summary["avg_tool_steps"],
        "duplicate_side_effect_rate": summary["duplicate_side_effect_rate"],
        "input_tokens": summary["input_tokens"],
        "output_tokens": summary["output_tokens"],
        "total_tokens": summary["total_tokens"],
        "cached_tokens": summary["cached_tokens"],
        "cache_miss_tokens": summary["cache_miss_tokens"],
        "cache_telemetry_run_count": summary["cache_telemetry_run_count"],
    }


def _render_report(payload):
    stream = payload["conditions"]["streaming"]
    no_stream = payload["conditions"]["no-stream"]
    lines = [
        "# Apricity Streaming Stage 2 Matrix",
        "",
        "PRD Stage 2 first-wave check: default streaming vs `--no-stream` on the same five tasks.",
        "",
        "## Run",
        "",
        f"- Provider: {payload['provider']}",
        f"- Model: {payload['model']}",
        f"- Commit: {payload['commit_sha']}",
        "- Tasks: 5 x 1 repetition per condition",
        "",
        "## Results",
        "",
        "| Metric | streaming | no-stream |",
        "| --- | ---: | ---: |",
        f"| Pass rate | {stream['pass_rate']:.2%} | {no_stream['pass_rate']:.2%} |",
        f"| Avg duration (s) | {stream['avg_duration_seconds']:.2f} | {no_stream['avg_duration_seconds']:.2f} |",
        f"| Avg TTFT / model response (ms) | {stream['avg_ttft_ms']:.1f} | {no_stream['avg_model_response_ms']:.1f} |",
        f"| Provider error rate | {stream['provider_error_rate']:.2%} | {no_stream['provider_error_rate']:.2%} |",
        f"| Partial-visible rate | {stream['partial_visible_rate']:.2%} | {no_stream['partial_visible_rate']:.2%} |",
        f"| Cancelled runs | {stream['cancelled_runs']} | {no_stream['cancelled_runs']} |",
        f"| Avg tool steps | {stream['avg_tool_steps']:.2f} | {no_stream['avg_tool_steps']:.2f} |",
        f"| Duplicate side-effect rate | {stream['duplicate_side_effect_rate']:.2%} | {no_stream['duplicate_side_effect_rate']:.2%} |",
        f"| Input tokens | {stream['input_tokens']} | {no_stream['input_tokens']} |",
        f"| Output tokens | {stream['output_tokens']} | {no_stream['output_tokens']} |",
        f"| Cached tokens | {stream['cached_tokens']} | {no_stream['cached_tokens']} |",
        f"| Cache-miss tokens | {stream['cache_miss_tokens']} | {no_stream['cache_miss_tokens']} |",
        "",
        "## Scope",
        "",
        "This is a small paid-provider first wave, not a production latency claim. Full acceptance targets and rollback rules live in `docs/requirements/streaming-output-prd.md`.",
    ]
    return "\n".join(lines) + "\n"


def build_arg_parser():
    parser = argparse.ArgumentParser(
        description="Run the PRD Stage 2 streaming matrix (streaming vs --no-stream)."
    )
    parser.add_argument("--provider", choices=PROVIDER_CHOICES, default=None)
    parser.add_argument(
        "--benchmark-path",
        default="benchmarks/streaming_stage2_tasks.json",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="Result directory. Defaults to benchmarks/results/streaming-stage2-<timestamp>/.",
    )
    parser.add_argument(
        "--workspace-root",
        default=None,
        help="Workspace copy root. Defaults to artifacts/streaming-stage2-<timestamp>-workspaces/.",
    )
    parser.add_argument(
        "--from-existing",
        default=None,
        help="Reuse already-completed run artifacts in this directory instead of calling the provider.",
    )
    parser.add_argument("--repetitions", type=int, default=1)
    parser.add_argument("--max-new-tokens", type=int, default=512)
    parser.add_argument("--request-timeout-seconds", type=int, default=300)
    parser.add_argument("--recovery-mode", choices=("compatible", "resilient"), default="compatible")
    return parser


def main(argv=None):
    args = build_arg_parser().parse_args(argv)
    timestamp = datetime.now().strftime("%Y-%m-%d-%H%M%S")
    output_dir = Path(args.output or f"benchmarks/results/streaming-stage2-{timestamp}")
    workspace_root = Path(
        args.workspace_root or f"artifacts/streaming-stage2-{timestamp}-workspaces"
    )

    conditions = [
        ("streaming", True),
        ("no-stream", False),
    ]
    artifacts = {}
    reused_existing = False
    if args.from_existing:
        output_dir = Path(args.from_existing)
        reused_existing = True
        for label, _stream in conditions:
            artifact_path = output_dir / label / "real-model-benchmark.json"
            artifacts[label] = json.loads(artifact_path.read_text(encoding="utf-8"))
    else:
        for label, stream in conditions:
            condition_dir = output_dir / label
            payload = run_real_model_benchmark(
                benchmark_path=args.benchmark_path,
                output_dir=condition_dir,
                workspace_root=workspace_root / label,
                provider=args.provider,
                repetitions=args.repetitions,
                max_new_tokens=args.max_new_tokens,
                request_timeout_seconds=args.request_timeout_seconds,
                recovery_mode=args.recovery_mode,
                fault_plan=FaultPlan(kind="none"),
                stream=stream,
            )
            artifacts[label] = payload

    summary = {
        "schema_version": 1,
        "artifact_type": "streaming-stage2-v1",
        "captured_at": datetime.now().isoformat(),
        "provider": artifacts["streaming"]["provider"]["name"],
        "model": artifacts["streaming"]["provider"]["model"],
        "commit_sha": artifacts["streaming"]["runtime"]["commit_sha"],
        "conditions": {
            label: _condition_block(artifacts[label], label)
            for label in ("streaming", "no-stream")
        },
    }
    if reused_existing:
        summary["reused_original_artifacts"] = True
        summary["runner_error_recovery"] = (
            "v1 report renderer raised KeyError('no_stream') after all model runs "
            "completed; original run artifacts were preserved and reused to finish "
            "the summary/report without calling the provider again."
        )
    stream_block = summary["conditions"]["streaming"]
    no_stream_block = summary["conditions"]["no-stream"]
    summary["comparison"] = {
        "pass_rate_delta": stream_block["pass_rate"] - no_stream_block["pass_rate"],
        "duration_ratio": _ratio(
            stream_block["avg_duration_seconds"], no_stream_block["avg_duration_seconds"]
        ),
        "ttft_vs_no_stream_model_response_ratio": _ratio(
            stream_block["avg_ttft_ms"], no_stream_block["avg_model_response_ms"]
        ),
        "provider_error_rate_delta": (
            stream_block["provider_error_rate"] - no_stream_block["provider_error_rate"]
        ),
        "duplicate_side_effect_rate_delta": (
            stream_block["duplicate_side_effect_rate"]
            - no_stream_block["duplicate_side_effect_rate"]
        ),
        "cached_token_delta": stream_block["cached_tokens"] - no_stream_block["cached_tokens"],
        "output_token_delta": stream_block["output_tokens"] - no_stream_block["output_tokens"],
    }

    output_dir.mkdir(parents=True, exist_ok=True)
    summary_path = output_dir / "streaming-stage2-summary.json"
    summary_path.write_text(
        json.dumps(summary, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    report_path = output_dir / "streaming-stage2-report.md"
    report_path.write_text(_render_report(summary), encoding="utf-8")

    print(f"Results: {output_dir.resolve()}")
    print(
        f"streaming pass={stream_block['pass_rate']:.2%} "
        f"avg_ttft={stream_block['avg_ttft_ms']:.1f}ms "
        f"avg_duration={stream_block['avg_duration_seconds']:.2f}s"
    )
    print(
        f"no-stream pass={no_stream_block['pass_rate']:.2%} "
        f"avg_model_response={no_stream_block['avg_model_response_ms']:.1f}ms "
        f"avg_duration={no_stream_block['avg_duration_seconds']:.2f}s"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
