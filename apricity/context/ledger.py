"""Lean append-only event ledger for Context Horizon v3.

The ledger keeps the two ideas worth preserving from the deleted v1 line:
source events are immutable and the model-visible Surface is a projection that
can shadow ranges without deleting sources. Everything else is deliberately
smaller than v1.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

APPEND_FORM = "append"
SESSION_SCHEMA_VERSION = 1
EVENT_SCHEMA_VERSION = 1

SURFACE_PRODUCING_TYPES = frozenset(
    {
        "user/message",
        "assistant/message",
        "tool/call",
        "tool/result",
        "context/pruned-tool-result",
        "context/summary",
    }
)


class LedgerError(ValueError):
    """Raised when an event cannot be accepted or a saved ledger is invalid."""


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_text(text: str) -> str:
    return hashlib.sha256(str(text).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class SurfaceNode:
    seq: int
    event_type: str
    data: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {"seq": self.seq, "event_type": self.event_type, "data": dict(self.data)}


@dataclass(frozen=True)
class Event:
    seq: int
    type: str
    data: dict[str, Any]
    surface_op: str | dict[str, Any] | None = None
    source_event_seqs: tuple[int, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": EVENT_SCHEMA_VERSION,
            "seq": self.seq,
            "type": self.type,
            "data": dict(self.data),
            "surface_op": APPEND_FORM if self.surface_op == APPEND_FORM else self.surface_op,
            "source_event_seqs": list(self.source_event_seqs),
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "Event":
        if int(payload.get("schema_version", 0)) != EVENT_SCHEMA_VERSION:
            raise LedgerError(f"unsupported event schema_version: {payload.get('schema_version')!r}")
        op = payload.get("surface_op")
        if op == APPEND_FORM:
            surface_op: str | dict[str, Any] | None = APPEND_FORM
        elif isinstance(op, dict):
            surface_op = dict(op)
        elif op is None:
            surface_op = None
        else:
            raise LedgerError(f"invalid surface_op: {op!r}")
        sources = tuple(sorted(set(int(item) for item in (payload.get("source_event_seqs") or []))))
        return cls(
            seq=int(payload["seq"]),
            type=str(payload["type"]),
            data=dict(payload.get("data") or {}),
            surface_op=surface_op,
            source_event_seqs=sources,
        )


def _copy_data(data: dict[str, Any]) -> dict[str, Any]:
    return json.loads(json.dumps(data, ensure_ascii=False, default=str))


def render_surface_node(node: SurfaceNode) -> str:
    data = node.data
    if node.event_type == "user/message":
        return f"[user] {data.get('content', '')}"
    if node.event_type == "assistant/message":
        return f"[assistant] {data.get('content', '')}"
    if node.event_type == "tool/call":
        args = data.get("args", {})
        return f"[tool-call {data.get('name', '')}] {canonical_json(args)}"
    if node.event_type in {"tool/result", "context/pruned-tool-result"}:
        return f"[tool-result {data.get('tool_name') or data.get('name', '')}] {data.get('content', '')}"
    if node.event_type == "context/summary":
        return str(data.get("rendered", ""))
    return canonical_json(data)


class SessionLedger:
    """Append-only event log plus its current Surface projection."""

    def __init__(
        self,
        *,
        session_id: str,
        workspace_root: str,
        created_at: str | None = None,
        persist_path: str | Path | None = None,
    ):
        self.session_id = str(session_id)
        self.workspace_root = str(workspace_root)
        self.created_at = str(created_at or "")
        self.persist_path = Path(persist_path) if persist_path is not None else None
        self._events: list[Event] = []
        self._surface: list[SurfaceNode] = []
        self.surface_generation = 0

    # ------------------------------------------------------------------
    # Event acceptance
    # ------------------------------------------------------------------

    def append(
        self,
        event_type: str,
        data: dict[str, Any],
        *,
        surface_op: str | dict[str, Any] | None = None,
        source_event_seqs: tuple[int, ...] | list[int] = (),
    ) -> Event:
        if surface_op is None and event_type in SURFACE_PRODUCING_TYPES:
            surface_op = APPEND_FORM
        seq = len(self._events)
        frozen_data = _copy_data(data)
        sources = tuple(sorted(set(int(item) for item in source_event_seqs)))
        event = Event(seq=seq, type=str(event_type), data=frozen_data, surface_op=surface_op, source_event_seqs=sources)
        self._accept(event)
        self._events.append(event)
        return event

    def _accept(self, event: Event) -> None:
        if event.seq != len(self._events):
            raise LedgerError(f"event seq {event.seq} is not contiguous; expected {len(self._events)}")
        for source in event.source_event_seqs:
            if source < 0 or source >= len(self._events):
                raise LedgerError(f"source event seq {source} does not exist at append time")
        op = event.surface_op
        if op is None:
            return
        if op == APPEND_FORM:
            if event.type not in SURFACE_PRODUCING_TYPES:
                raise LedgerError(f"{event.type} is not surface-producing")
            self._surface.append(SurfaceNode(seq=event.seq, event_type=event.type, data=event.data))
            return
        if isinstance(op, dict) and op.get("op") == "replace":
            start = int(op.get("start", -1))
            end = int(op.get("end", -1))
            start_index = next((index for index, node in enumerate(self._surface) if node.seq == start), None)
            end_index = next((index for index, node in enumerate(self._surface) if node.seq == end), None)
            if start_index is None or end_index is None or start_index > end_index:
                raise LedgerError(f"invalid replacement span ({start}, {end})")
            if event.type not in SURFACE_PRODUCING_TYPES:
                raise LedgerError(f"{event.type} is not surface-producing")
            if not event.source_event_seqs:
                raise LedgerError("replacement events must cite source_event_seqs")
            replacement = SurfaceNode(seq=event.seq, event_type=event.type, data=event.data)
            self._surface = self._surface[:start_index] + [replacement] + self._surface[end_index + 1 :]
            self.surface_generation += 1
            return
        raise LedgerError(f"invalid surface_op: {op!r}")

    # ------------------------------------------------------------------
    # Projections and helpers
    # ------------------------------------------------------------------

    @property
    def events(self) -> tuple[Event, ...]:
        return tuple(self._events)

    @property
    def surface(self) -> tuple[SurfaceNode, ...]:
        return tuple(self._surface)

    def event_by_seq(self, seq: int) -> Event:
        if seq < 0 or seq >= len(self._events):
            raise LedgerError(f"event seq {seq} does not exist")
        return self._events[seq]

    def surface_event_seqs(self) -> list[int]:
        return [node.seq for node in self._surface]

    def count_surface_tokens(self, meter) -> int:
        return sum(int(meter.count_text(render_surface_node(node))) for node in self._surface)

    def pairs_balanced(self) -> bool:
        calls: dict[str, int] = {}
        results: dict[str, int] = {}
        for node in self._surface:
            call_id = str(node.data.get("call_id", ""))
            if not call_id:
                continue
            if node.event_type == "tool/call":
                calls[call_id] = calls.get(call_id, 0) + 1
            elif node.event_type in {"tool/result", "context/pruned-tool-result"}:
                results[call_id] = results.get(call_id, 0) + 1
        return all(calls.get(call_id, 0) == results.get(call_id, 0) == 1 for call_id in set(calls) | set(results))

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def save(self, path: str | Path | None = None) -> Path:
        target = Path(path) if path is not None else self.persist_path
        if target is None:
            raise LedgerError("no persist path configured")
        target = Path(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema_version": SESSION_SCHEMA_VERSION,
            "id": self.session_id,
            "created_at": self.created_at,
            "workspace_root": self.workspace_root,
            "surface_generation": self.surface_generation,
            "events": [event.to_dict() for event in self._events],
        }
        tmp = target.with_name(target.name + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(str(tmp), str(target))
        return target

    def flush_dispatch(self) -> None:
        if self.persist_path is not None:
            self.save()

    @classmethod
    def load(cls, path: str | Path) -> "SessionLedger":
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        if int(payload.get("schema_version", 0)) != SESSION_SCHEMA_VERSION:
            raise LedgerError(f"unsupported session schema_version: {payload.get('schema_version')!r}")
        ledger = cls(
            session_id=str(payload.get("id", "")),
            workspace_root=str(payload.get("workspace_root", "")),
            created_at=str(payload.get("created_at", "")),
            persist_path=Path(path),
        )
        ledger.surface_generation = 0
        ledger._events = []
        ledger._surface = []
        for raw in payload.get("events", []):
            event = Event.from_dict(raw)
            ledger._accept(event)
            ledger._events.append(event)
        return ledger
