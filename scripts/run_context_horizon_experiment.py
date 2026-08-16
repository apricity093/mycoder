"""Context Horizon v3 experiment entrypoint.

Commands:
- freeze-seeds: regenerate deterministic seed JSONL files and hashes.
- pilot: run the local scripted-model pressure calibration (A/C/E/D, no API).
- formal: run the paid DeepSeek main experiment (A/C/E/D x 6 tasks).
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from apricity.evaluation.context_horizon import (
    MAIN_BUDGET,
    CostGuard,
    ModelSummarizer,
    build_paid_schedule,
    condition_policies,
    freeze_seed_files,
    load_hidden_verifier,
    load_taskset,
    run_local_pilot,
    run_paid_run,
    summarize_rows,
)
from apricity.evaluation.context_horizon.runner import ScriptedChunkSummarizer, write_pilot_artifacts
from apricity.providers.factory import create_model_client, load_provider_config


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


def _git_state(repo: Path) -> tuple[str, bool]:
    commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True).stdout.strip()
    dirty = bool(subprocess.run(["git", "status", "--porcelain"], cwd=repo, capture_output=True, text=True).stdout.strip())
    return commit, dirty


def cmd_formal(args) -> int:
    repo = _repo_root()
    taskset = load_taskset(args.tasks, repo_root=repo)
    hidden_root = Path(args.hidden)
    commit, dirty = _git_state(repo)
    if dirty and not args.allow_dirty:
        print("formal run requires a clean worktree; commit changes or pass --allow-dirty for a noncanonical run")
        return 2
    provider_config = load_provider_config(provider=args.provider, start=repo)
    provider = provider_config.provider
    model = provider_config.model

    def client_factory():
        return create_model_client(provider_config, temperature=0.0)

    def summarizer_factory():
        return ModelSummarizer(
            create_model_client(provider_config, temperature=0.0),
            {"input_tokens": 0, "output_tokens": 0, "cached_tokens": 0, "cache_miss_tokens": 0, "calls": 0},
        )

    cost_guard = CostGuard(max_cost_cny=float(args.max_cost))

    output_dir = Path(args.output) if args.output else repo / "benchmarks" / "results" / f"context-horizon-paid-{_now()}"
    output_dir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "schema_version": 1,
        "experiment_id": output_dir.name,
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "commit_sha": commit,
        "dirty": dirty,
        "provider": provider,
        "model": model,
        "budget": args.budget,
        "conditions": list(args.condition or ["A", "C", "E", "D"]),
        "task_snapshot_hash": taskset["task_snapshot_hash"],
        "pricing": {
            "input_cache_miss_cny_per_1m": 1.4,
            "input_cache_hit_cny_per_1m": 0.05,
            "output_cny_per_1m": 4.5,
        },
        "max_cost_cny": args.max_cost,
    }
    schedule = build_paid_schedule(taskset["tasks"], manifest["conditions"])
    if args.task:
        wanted = set(args.task)
        schedule = [block for block in schedule if block["task_id"] in wanted]
    rows = []
    for block in schedule:
        if cost_guard.remaining_cny <= 0:
            print("cost cap reached; stopping")
            break
        task = next(item for item in taskset["tasks"] if item["id"] == block["task_id"])
        hidden = load_hidden_verifier(task, hidden_root)
        workspace = output_dir / "workspaces" / f"{task['id']}-{block['condition']}"
        row = run_paid_run(
            task=task,
            condition=block["condition"],
            fixture_root=taskset["fixture_repo_abs"],
            workspace_root=workspace,
            hidden=hidden,
            budget=args.budget,
            client_factory=client_factory,
            summarizer_factory=summarizer_factory,
            cost_guard=cost_guard,
            max_new_tokens=args.max_new_tokens,
            provider=provider,
            model=model,
        )
        rows.append(row)
        print(
            f"{row['task_id']} {row['condition']}: stop={row['stop_reason']} context={row['context']['context_pass']} "
            f"artifact={row['artifact']['artifact_pass']} cost={row['cost_guard_snapshot']['spent_cny']:.4f} "
            f"error={row.get('model_error', '')[:80]}"
        )
    manifest["completed_at"] = datetime.now(timezone.utc).isoformat()
    manifest["rows"] = rows
    manifest["cost_totals"] = {
        "spent_cny": cost_guard.spent_cny,
        "input_tokens": cost_guard.input_tokens,
        "output_tokens": cost_guard.output_tokens,
        "cached_tokens": cost_guard.cached_tokens,
        "cache_miss_tokens": cost_guard.cache_miss_tokens,
    }
    (output_dir / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    summary = summarize_rows(rows)
    (output_dir / "paid-summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"output": str(output_dir), "cost": manifest["cost_totals"], "summary": summary}, ensure_ascii=False, indent=2))
    return 0


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
    formal = sub.add_parser("formal")
    formal.add_argument("--condition", action="append", choices=sorted(condition_policies(MAIN_BUDGET)))
    formal.add_argument("--task", action="append")
    formal.add_argument("--budget", type=int, default=MAIN_BUDGET)
    formal.add_argument("--output", default=None)
    formal.add_argument("--provider", default=None)
    formal.add_argument("--max-cost", type=float, default=15.0)
    formal.add_argument("--max-new-tokens", type=int, default=1024)
    formal.add_argument("--allow-dirty", action="store_true")
    formal.set_defaults(func=cmd_formal)
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
