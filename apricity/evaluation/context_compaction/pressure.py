"""Frozen seed/checkpoint preparation for the context-compaction benchmark.

Controlled tasks use frozen, pre-seeded event logs; realistic tasks fork from
a common investigation checkpoint after an identical investigation phase. The
seed preparation is deterministic and independent of the experiment condition.
"""

from __future__ import annotations

import json
from pathlib import Path

from ...context import SessionLedger, render_surface_node
from ...context.ledger import SURFACE_PRODUCING_TYPES


def load_seed_events(seed_path: Path) -> list[dict]:
    """Load a frozen .jsonl seed event log into a list of event dicts."""
    events = []
    for line in Path(seed_path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        event = json.loads(line)
        event_type = event["type"]
        if event_type not in SURFACE_PRODUCING_TYPES:
            continue
        events.append(
            {
                "type": event_type,
                "data": event["data"],
                "source_event_seqs": tuple(event.get("source_event_seqs", ())),
            }
        )
    return events


def prepare_ledger_from_seed(seed_path: Path, *, session_id: str, workspace_root: str) -> SessionLedger:
    ledger = SessionLedger(
        session_id=session_id,
        workspace_root=str(workspace_root),
        created_at="2026-08-14T00:00:00+00:00",
    )
    for event in load_seed_events(seed_path):
        ledger.append(event["type"], dict(event["data"]))
    return ledger


def seed_surface_text(seed_path: Path) -> str:
    events = load_seed_events(seed_path)
    return "\n\n".join(
        render_surface_node(type("N", (), {"seq": i, "event_type": e["type"], "data": e["data"]})())
        for i, e in enumerate(events)
    )


def task_fixture_relative(task: dict) -> str:
    return str(task["fixture_repo"]).strip()
