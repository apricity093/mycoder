"""Context Horizon v3 experiment entrypoint.

Commands:
- freeze-seeds: regenerate deterministic seed JSONL files and hashes.
- pilot: run the local scripted-model pressure calibration (A/C/E, no API).
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from apricity.evaluation.context_horizon import (
    MAIN_BUDGET,
    condition_policies,
    freeze_seed_files,
    load_hidden_verifier,
    load_taskset,
    run_local_pilot,
    summarize_rows,
)
from apricity.evaluation.context_horizon.runner import ScriptedChunkSummarizer, write_pilot_artifacts


def _repo_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d-%H%M%S")


def cmd_freeze_seeds(args) -> int:
    repo = _repo_root()
    taskset = load_taskset(args.tasks, repo_root=repo)
    hashes = freeze_seed_files(taskset, args.seeds)
    (Path(args.seeds) / "hashes.json").write_text(json.dumps(hashes, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"froze {len(hashes)} seed files in {args.seeds}")
    return 0


def cmd_pilot(args) -> int:
    repo = _repo_root()
    taskset = load_taskset(args.tasks, repo_root=repo)
    hidden_root = Path(args.hidden)
    output_dir = Path(args.output) if args.output else repo / "benchmarks" / "results" / f"context-horizon-calibration-{_now()}"
    output_dir.mkdir(parents=True, exist_ok=True)
    conditions = tuple(args.condition or ["A", "C", "E", "D"])
    summarizer = ScriptedChunkSummarizer()
    rows = []
    for task in taskset["tasks"]:
        hidden = load_hidden_verifier(task, hidden_root)
        for condition in conditions:
            workspace = output_dir / "workspaces" / f"{task['id']}-{condition}"
            row = run_local_pilot(
                task=task,
                condition=condition,
                fixture_root=taskset["fixture_repo_abs"],
                workspace_root=workspace,
                hidden=hidden,
                budget=args.budget,
                summarizer=summarizer,
            )
            rows.append(row)
            print(f"{task['id']} {condition}: pressure={row['pressure_crossed']} no_safe_range={row['no_safe_range']} context={row['context']['context_pass']}")
    write_pilot_artifacts(output_dir=output_dir, rows=rows, taskset=taskset)
    summary = summarize_rows(rows)
    (output_dir / "pilot-summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"output": str(output_dir), "gate": summary["calibration_gate_pass"], "summary": summary}, ensure_ascii=False, indent=2))
    return 0 if summary["calibration_gate_pass"] else 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tasks", default=str(_repo_root() / "benchmarks" / "context_horizon" / "tasks.v1.json"))
    parser.add_argument("--seeds", default=str(_repo_root() / "benchmarks" / "context_horizon" / "seeds"))
    parser.add_argument("--hidden", default=str(_repo_root() / "benchmarks" / "context_horizon" / "hidden"))
    sub = parser.add_subparsers(dest="command", required=True)
    freeze = sub.add_parser("freeze-seeds")
    freeze.set_defaults(func=cmd_freeze_seeds)
    pilot = sub.add_parser("pilot")
    pilot.add_argument("--condition", action="append", choices=sorted(condition_policies(MAIN_BUDGET)))
    pilot.add_argument("--budget", type=int, default=MAIN_BUDGET)
    pilot.add_argument("--output", default=None)
    pilot.set_defaults(func=cmd_pilot)
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
