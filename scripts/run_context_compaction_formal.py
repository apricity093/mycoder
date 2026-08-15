"""Run the 180-Run paid formal experiment for replayable context compaction.

Main experiment: 12 tasks x A/B/C/D x 3 repetitions at the 4096-token budget
(144 Runs). Sensitivity experiment: 6 frozen tasks x A/D x 3 repetitions at
the 8192-token budget (36 Runs). Balanced scheduling, immutable hashes, and
strict resume validation are enforced by ContextCompactionRunner.
"""

import argparse
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from apricity.config import load_project_env
from apricity.evaluation.context_compaction import MAIN_BUDGET, SENSITIVITY_BUDGET, ContextCompactionRunner
from apricity.providers.factory import create_model_client, load_provider_config

SENSITIVITY_TASKS = (
    "C01-early-fact",
    "C04-stale-conflict",
    "C06-repeated-compaction",
    "C08-overflow-recovery",
    "R01-centralized-timeout-default",
    "R03-stable-json-report",
)


def build_model_client_factory():
    repo_root = Path.cwd().resolve()
    load_project_env(repo_root)
    config = load_provider_config(provider="deepseek", start=repo_root)

    def factory():
        return create_model_client(config, temperature=0.0, timeout=300)

    return factory, config.provider, config.model


def main():
    parser = argparse.ArgumentParser(description="Run the 180-Run paid formal experiment.")
    parser.add_argument("--output-dir", default=None)
    parser.add_argument("--workspace-root", default="artifacts/context-compaction-formal-workspaces")
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()

    output_dir = Path(
        args.output_dir
        or f"benchmarks/results/context-compaction-formal-{datetime.now(timezone.utc).strftime('%Y-%m-%d-%H%M%S')}"
    )
    factory, provider, model = build_model_client_factory()

    print(f"formal provider/model: {provider}/{model}")
    print(f"output dir: {output_dir}")

    if output_dir.exists() and not args.resume:
        shutil.rmtree(output_dir)

    runner = ContextCompactionRunner(
        taskset_path="benchmarks/context_compaction/tasks.v2.json",
        output_dir=str(output_dir),
        workspace_root=args.workspace_root,
        repetitions=3,
        budgets=(MAIN_BUDGET, SENSITIVITY_BUDGET),
        sensitivity_tasks=SENSITIVITY_TASKS,
        model_client_factory=factory,
        resume=args.resume,
        real=True,
        verify_artifacts=True,
        allow_dirty=False,
    )
    payload = runner.run()
    agg = payload["aggregate"]
    print("\n=== FORMAL EXPERIMENT COMPLETE ===")
    print("status:", payload["status"])
    print("pass_rate:", round(agg["pass_rate"], 4))
    print("runner_errors:", agg["runner_errors"])
    print("conditions:", {k: round(v["pass_rate"], 4) for k, v in agg["conditions"].items()})
    print("report:", output_dir / "report.md")
    return 0 if not agg["runner_errors"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
