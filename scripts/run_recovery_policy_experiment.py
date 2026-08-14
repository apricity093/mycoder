import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apricity.evaluation.recovery_experiment import run_recovery_policy_experiment  # noqa: E402


def main(argv=None):
    parser = argparse.ArgumentParser(description="Run deterministic recovery-policy ablations.")
    parser.add_argument("--benchmark", default="benchmarks/coding_tasks.json")
    parser.add_argument("--output", default="artifacts/recovery-policy-experiment-v1.json")
    parser.add_argument("--workspace-root", default="artifacts/recovery-policy-workspaces")
    parser.add_argument("--task-id", action="append", default=None)
    args = parser.parse_args(argv)

    result = run_recovery_policy_experiment(
        benchmark_path=args.benchmark,
        artifact_path=args.output,
        workspace_root=args.workspace_root,
        task_ids=args.task_id,
    )
    summaries = {
        name: payload["summary"]
        for name, payload in result["variants"].items()
    }
    print(
        json.dumps(
            {
                "artifact_path": result["artifact_path"],
                "report_path": result["report_path"],
                "comparison": result["comparison"],
                "variants": summaries,
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
