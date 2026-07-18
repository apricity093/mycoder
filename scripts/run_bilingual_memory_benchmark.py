#!/usr/bin/env python3
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from apricity.evaluation.metrics import run_bilingual_memory_benchmark  # noqa: E402


def build_arg_parser():
    parser = argparse.ArgumentParser(
        description="Run Apricity's deterministic bilingual memory retrieval benchmark."
    )
    parser.add_argument(
        "--output",
        default="artifacts/bilingual-memory-retrieval-v1.json",
        help="Artifact JSON path.",
    )
    parser.add_argument("--repetitions", type=int, default=1)
    return parser


def main(argv=None):
    args = build_arg_parser().parse_args(argv)
    artifact = run_bilingual_memory_benchmark(
        artifact_path=args.output,
        repetitions=args.repetitions,
    )
    print(json.dumps(artifact["summary"], indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
