"""Run the deterministic context-compaction gate (no paid provider calls).

Executes the full 12-task x 4-condition schedule once (48 Runs) plus the
8192-token A/D sensitivity subset, verifies hidden-verifier isolation and
schedule/resume integrity, and writes a deterministic report. No model calls
are made: summaries are injected from frozen seeds.
"""

import argparse
import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from apricity.evaluation.context_compaction import ContextCompactionRunner

SENSITIVITY_TASKS = (
    "C01-early-fact",
    "C04-stale-conflict",
    "C06-repeated-compaction",
    "C08-overflow-recovery",
    "R01-centralized-timeout-default",
    "R03-stable-json-report",
)


def main():
    parser = argparse.ArgumentParser(description="Run the deterministic context-compaction gate.")
    parser.add_argument("--output-dir", default="artifacts/context-compaction-gate")
    parser.add_argument("--workspace-root", default="artifacts/context-compaction-workspaces")
    parser.add_argument("--repetitions", type=int, default=1)
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()

    out = Path(args.output_dir)
    if out.exists() and not args.resume:
        shutil.rmtree(out)

    runner = ContextCompactionRunner(
        taskset_path="benchmarks/context_compaction/tasks.v2.json",
        output_dir=args.output_dir,
        workspace_root=args.workspace_root,
        repetitions=args.repetitions,
        budgets=(4096,),
        resume=args.resume,
        allow_dirty=True,
    )
    payload = runner.run()
    agg = payload["aggregate"]
    print("deterministic gate status:", payload["status"])
    print("mechanics_pass_rate:", round(agg["mechanics_pass_rate"], 4), "runner_errors:", agg["runner_errors"])
    print("conditions:", {k: round(v["mechanics_pass_rate"], 4) for k, v in agg["conditions"].items()})
    print("report:", Path(args.output_dir) / "report.md")
    if agg["runner_errors"]:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
