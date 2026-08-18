#!/usr/bin/env python3
import argparse
from datetime import datetime
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apricity.evaluation.real_model_benchmark import run_real_model_benchmark  # noqa: E402
from apricity.evaluation.faults import FAULT_KINDS, FaultPlan  # noqa: E402
from apricity.providers.config import PROVIDER_CHOICES  # noqa: E402


def build_arg_parser():
    parser = argparse.ArgumentParser(
        description="Run Apricity's small verifier-scored real-model coding benchmark."
    )
    parser.add_argument("--provider", choices=PROVIDER_CHOICES, default=None)
    parser.add_argument("--benchmark-path", default="benchmarks/real_model_tasks.json")
    parser.add_argument("--output", default=None, help="Result directory. Defaults to a dated directory.")
    parser.add_argument("--workspace-root", default="artifacts/real-model-workspaces")
    parser.add_argument("--repetitions", type=int, default=3)
    parser.add_argument("--max-new-tokens", type=int, default=512)
    parser.add_argument("--request-timeout-seconds", type=int, default=300)
    parser.add_argument("--resume", action="store_true", help="Resume an interrupted artifact in --output.")
    parser.add_argument("--recovery-mode", choices=("compatible", "resilient"), default="compatible")
    stream_group = parser.add_mutually_exclusive_group()
    stream_group.add_argument("--stream", dest="stream_mode", action="store_const", const=True, default=False)
    stream_group.add_argument("--no-stream", dest="stream_mode", action="store_const", const=False, default=False)
    parser.add_argument("--fault", choices=sorted(FAULT_KINDS), default="none")
    parser.add_argument(
        "--disable-recovery-policy",
        dest="disabled_recovery_policies",
        action="append",
        default=[],
        choices=("provider_retry", "context_recovery", "protocol_correction", "tool_result_spill"),
    )
    return parser


def _print_progress(event):
    print(
        f"[{event['completed']}/{event['total']}] "
        f"{event['task_id']} r{event['repetition']} "
        f"{event['status'].upper()} {event['duration_seconds']:.2f}s",
        flush=True,
    )


def main(argv=None):
    args = build_arg_parser().parse_args(argv)
    output = args.output or f"benchmarks/results/real-model-{datetime.now():%Y-%m-%d-%H%M%S}"
    payload = run_real_model_benchmark(
        benchmark_path=args.benchmark_path,
        output_dir=output,
        workspace_root=args.workspace_root,
        provider=args.provider,
        repetitions=args.repetitions,
        max_new_tokens=args.max_new_tokens,
        request_timeout_seconds=args.request_timeout_seconds,
        progress=_print_progress,
        resume=args.resume,
        recovery_mode=args.recovery_mode,
        fault_plan=FaultPlan(kind=args.fault),
        disabled_recovery_policies=args.disabled_recovery_policies,
        stream=bool(args.stream_mode),
    )
    summary = payload["summary"]
    print(f"Results: {Path(payload['artifact_path']).resolve()}")
    print(
        f"pass_rate={summary['pass_rate']:.2%} "
        f"pass_at_1={summary['pass_at_1']:.2%} "
        f"stable_pass_rate={summary['stable_pass_rate']:.2%}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
