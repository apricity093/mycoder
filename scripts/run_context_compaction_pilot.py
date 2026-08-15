"""Run the 48-Run paid pilot for replayable context compaction.

12 tasks x 4 conditions x 1 repetition at the 4096-token harness budget, with
the real DeepSeek provider at temperature 0. A/B/C/D share the SessionLedger
runtime and identical request assembly; condition D also calls the same model
for evidence-anchored summaries. Summary calls count toward tokens, latency,
and failures. No API key is logged; it is read only from the environment.
"""

import argparse
import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from apricity.config import load_project_env
from apricity.evaluation.context_compaction import ContextCompactionRunner, MAIN_BUDGET
from apricity.providers.factory import create_model_client, load_provider_config


def build_model_client_factory():
    repo_root = Path.cwd().resolve()
    load_project_env(repo_root)
    config = load_provider_config(provider="deepseek", start=repo_root)
    provider = config.provider
    model = config.model

    def factory():
        return create_model_client(config, temperature=0.0, timeout=300)

    return factory, provider, model


def main():
    parser = argparse.ArgumentParser(description="Run the 48-Run paid pilot.")
    parser.add_argument("--output-dir", default=None)
    parser.add_argument("--workspace-root", default="artifacts/context-compaction-pilot-workspaces")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument(
        "--tasks",
        default=None,
        help="comma-separated task ids for a small smoke subset; omit for all 12 tasks",
    )
    parser.add_argument(
        "--verify-artifacts",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="execute public and hidden artifact verifiers (default: enabled)",
    )
    args = parser.parse_args()

    output_dir = Path(args.output_dir or f"benchmarks/results/context-compaction-{datetime.now(timezone.utc).strftime('%Y-%m-%d-%H%M%S')}")
    task_ids = tuple(item.strip() for item in (args.tasks or "").split(",") if item.strip())
    factory, provider, model = build_model_client_factory()

    print(f"pilot provider/model: {provider}/{model}")
    print(f"output dir: {output_dir}")

    if output_dir.exists() and not args.resume:
        shutil.rmtree(output_dir)

    runner = ContextCompactionRunner(
        taskset_path="benchmarks/context_compaction/tasks.v2.json",
        output_dir=str(output_dir),
        workspace_root=args.workspace_root,
        repetitions=1,
        budgets=(MAIN_BUDGET,),
        model_client_factory=factory,
        resume=args.resume,
        real=True,
        verify_artifacts=args.verify_artifacts,
        task_ids=task_ids,
    )
    payload = runner.run()
    agg = payload["aggregate"]
    print("\n=== PILOT COMPLETE ===")
    print("status:", payload["status"])
    print("pass_rate:", round(agg["pass_rate"], 4))
    print("runner_errors:", agg["runner_errors"])
    print("conditions:", {k: round(v["pass_rate"], 4) for k, v in agg["conditions"].items()})
    print("report:", output_dir / "report.md")
    return 0 if not agg["runner_errors"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
