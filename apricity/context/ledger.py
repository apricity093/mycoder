"""Replayable SessionLedger and Surface fold.

The ledger is the sole durable source of model-visible request state. It is
append-only: accepted events are never edited or deleted. The Surface is an
ordered projection of selected events; a replacement can shadow older Surface
nodes without deleting their source events.

Canonical language: a *Session Event* is one immutable, JSON-serializable
fact with a contiguous ``seq``. The *Surface* is the ordered projection that
supplies conversational messages. A *replace* operation removes an inclusive
span of current Surface nodes and inserts the replacement node at the span's
first position.
"""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass, field
from pathlib import Path

from .types import ContextPreparationError
from ..workspace import now

SESSION_SCHEMA_VERSION = 2
EVENT_SCHEMA_VERSION = 1

# Event types that produce ordered model history (Surface nodes).
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

# Non-Surface types that are still valid, typed session facts.
KNOWN_EVENT_TYPES = frozenset(
    {
        *SURFACE_PRODUCING_TYPES,
        "turn/start",
        "turn/end",
        "step/start",
        "step/end",
        "request/header",
        "request/context",
        "request/dispatch",
        "context/pressure",
        "compaction/start",
        "compaction/end",
    }
)

# Replacement forms accepted by Surface folding. The string "append" is the
# plain append form; a mapping is the structured replace form.
APPEND_FORM = "append"


def canonical_json(value) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=True, separators=(",", ":"))


def sha256_text(text: str) -> str:
    import hashlib

    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class SurfaceNode:
    seq: int
    event_type: str
    data: dict
    source_event_seqs: tuple[int, ...] = ()


@dataclass
class SurfaceSnapshot:
    nodes: list = field(default_factory=list)
    generation: int = 0

    @property
    def seqs(self) -> list[int]:
        return [node.seq for node in self.nodes]

    def node_by_seq(self, seq: int) -> SurfaceNode | None:
        for node in self.nodes:
            if node.seq == seq:
                return node
        return None

    def node_index_by_seq(self, seq: int) -> int:
        for index, node in enumerate(self.nodes):
            if node.seq == seq:
                return index
        return -1


class SessionLedger:
    """Append-only durable event store with a replayed Surface projection.

    The ledger is the single source of truth: every model-visible fact must
    be derivable from accepted events plus versioned rendering code. Events
    accepted here are deep-copied and never mutated afterwards. Invalid
    events -- non-contiguous seq, non-JSON data, or an invalid replacement --
    are rejected before acceptance.
    """

    def __init__(
        self,
        session_id: str,
        workspace_root: str,
        *,
        created_at: str | None = None,
        events: list | None = None,
        memory: dict | None = None,
        checkpoints: dict | None = None,
        runtime_identity: dict | None = None,
        resume_state: dict | None = None,
        persist_path: str | None = None,
    ):
        self.session_id = str(session_id)
        self.workspace_root = str(workspace_root)
        self.created_at = created_at or now()
        self._events: list[dict] = []
        self._surface = SurfaceSnapshot()
        self._memory: dict = dict(memory or {})
        self._checkpoints: dict = dict(checkpoints or {})
        self._runtime_identity: dict = dict(runtime_identity or {})
        self._resume_state: dict = dict(resume_state or {})
        self._compaction_generation = 0
        self.persist_path = Path(persist_path) if persist_path else None
        if events:
            for event in events:
                self._accept(event, validate_seq=True)
        self._validate_surface_integrity()

    # ------------------------------------------------------------------
    # Public state accessors
    # ------------------------------------------------------------------

    @property
    def seq(self) -> int:
        return len(self._events)

    @property
    def surface(self) -> SurfaceSnapshot:
        return self._surface

    @property
    def surface_generation(self) -> int:
        return self._surface.generation

    @property
    def memory(self) -> dict:
        return copy.deepcopy(self._memory)

    @property
    def checkpoints(self) -> dict:
        return copy.deepcopy(self._checkpoints)

    @property
    def runtime_identity(self) -> dict:
        return copy.deepcopy(self._runtime_identity)

    @property
    def resume_state(self) -> dict:
        return copy.deepcopy(self._resume_state)

    def events(self) -> list[dict]:
        return copy.deepcopy(self._events)

    def event_by_seq(self, seq: int) -> dict | None:
        seq = int(seq)
        if seq < 0 or seq >= len(self._events):
            return None
        return copy.deepcopy(self._events[seq])

    def surface_events(self) -> list[dict]:
        """Return the model-visible Surface nodes as full event records."""
        result = []
        for node in self._surface.nodes:
            result.append(self._event_record(node.seq))
        return result

    def surface_event_seqs(self) -> tuple[int, ...]:
        return tuple(self._surface.seqs)

    def model_visible_event(self, seq: int) -> str | None:
        """Return the model-visible rendering for one event, or None."""
        node = self._surface.node_by_seq(int(seq))
        if node is None:
            return None
        return render_surface_node(node)

    def surface_rendering(self, seqs: tuple[int, ...] | None = None) -> str:
        """Render the Surface (or a selected subset) as conversational text."""
        if seqs is None:
            nodes = self._surface.nodes
        else:
            wanted = set(seqs)
            nodes = [node for node in self._surface.nodes if node.seq in wanted]
        return "\n\n".join(render_surface_node(node) for node in nodes)

    def count_surface_tokens(self, meter) -> int:
        return sum(meter.count_text(render_surface_node(node)) for node in self._surface.nodes)

    # ------------------------------------------------------------------
    # Event acceptance
    # ------------------------------------------------------------------

    def append(
        self,
        event_type: str,
        data: dict,
        *,
        source_event_seqs: tuple[int, ...] = (),
        surface_op=APPEND_FORM,
    ) -> int:
        """Append one immutable event and return its seq.

        ``data`` must be JSON-serializable and is deep-copied before
        acceptance. ``surface_op`` is either the string ``"append"`` or a
        ``{"op": "replace", "start": seq, "end": seq}`` mapping whose
        endpoints identify current Surface nodes.
        """
        event = {
            "schema_version": EVENT_SCHEMA_VERSION,
            "seq": self.seq,
            "time": now(),
            "type": event_type,
            "data": copy.deepcopy(data),
            "surface_op": copy.deepcopy(surface_op),
            "source_event_seqs": sorted(int(seq) for seq in source_event_seqs),
        }
        return self._accept(event, validate_seq=True)

    def _accept(self, event: dict, *, validate_seq: bool) -> int:
        self._validate_event_envelope(event, validate_seq=validate_seq)
        event = copy.deepcopy(event)
        seq = int(event["seq"])
        if len(self._events) != seq:
            raise ContextPreparationError(
                f"ledger seq discontinuity: expected {len(self._events)}, got {seq}",
                code="ledger_seq_discontinuity",
            )
        op = event["surface_op"]
        if op == APPEND_FORM:
            if event["type"] in SURFACE_PRODUCING_TYPES:
                self._surface.nodes.append(
                    SurfaceNode(
                        seq=seq,
                        event_type=event["type"],
                        data=copy.deepcopy(event["data"]),
                        source_event_seqs=tuple(int(value) for value in event["source_event_seqs"]),
                    )
                )
        elif isinstance(op, dict) and op.get("op") == "replace":
            self._apply_replacement(event, op)
        elif isinstance(op, dict) and op.get("op") == "remove":
            self._apply_removal(event, op)
        else:
            raise ContextPreparationError(
                f"invalid surface_op for event {seq}: {op!r}",
                code="invalid_surface_op",
            )
        self._events.append(event)
        return seq

    def _apply_removal(self, event: dict, op: dict) -> None:
        """Remove an inclusive span of current Surface nodes without inserting
        a replacement (condition B whole-unit removal)."""
        start_seq = int(op.get("start", -1))
        end_seq = int(op.get("end", -1))
        start_index = self._surface.node_index_by_seq(start_seq)
        end_index = self._surface.node_index_by_seq(end_seq)
        if start_index == -1 or end_index == -1 or start_index > end_index:
            raise ContextPreparationError(
                f"invalid removal span: start={start_seq} end={end_seq}",
                code="invalid_replacement_span",
            )
        del self._surface.nodes[start_index : end_index + 1]
        self._surface.generation += 1

    def _apply_replacement(self, event: dict, op: dict) -> None:
        start_seq = int(op.get("start", -1))
        end_seq = int(op.get("end", -1))
        start_index = self._surface.node_index_by_seq(start_seq)
        end_index = self._surface.node_index_by_seq(end_seq)
        if start_index == -1 or end_index == -1 or start_index > end_index:
            raise ContextPreparationError(
                f"invalid replacement span: start={start_seq} end={end_seq}",
                code="invalid_replacement_span",
            )
        replacement = SurfaceNode(
            seq=int(event["seq"]),
            event_type=event["type"],
            data=copy.deepcopy(event["data"]),
            source_event_seqs=tuple(int(value) for value in event["source_event_seqs"]),
        )
        self._surface.nodes[start_index : end_index + 1] = [replacement]
        self._surface.generation += 1

    # ------------------------------------------------------------------
    # Event envelope validation
    # ------------------------------------------------------------------

    def _validate_event_envelope(self, event: dict, *, validate_seq: bool) -> None:
        if not isinstance(event, dict):
            raise ContextPreparationError("event must be a mapping", code="invalid_event")
        event_type = event.get("type")
        if event_type not in KNOWN_EVENT_TYPES:
            raise ContextPreparationError(f"unknown event type: {event_type!r}", code="unknown_event_type")
        if int(event.get("schema_version", 0)) != EVENT_SCHEMA_VERSION:
            raise ContextPreparationError("unsupported event schema_version", code="invalid_event_schema")
        seq = event.get("seq")
        if not isinstance(seq, int) or seq < 0:
            raise ContextPreparationError(f"invalid event seq: {seq!r}", code="invalid_event_seq")
        if validate_seq and seq != self.seq:
            raise ContextPreparationError(
                f"non-contiguous seq: expected {self.seq}, got {seq}",
                code="ledger_seq_discontinuity",
            )
        data = event.get("data")
        try:
            canonical_json(data)
        except (TypeError, ValueError) as exc:
            raise ContextPreparationError(
                f"event data is not lossless JSON: {exc}", code="invalid_event_json"
            ) from exc
        surface_op = event.get("surface_op")
        if surface_op != APPEND_FORM and not (
            isinstance(surface_op, dict)
            and surface_op.get("op") in {"replace", "remove"}
            and isinstance(surface_op.get("start"), int)
            and isinstance(surface_op.get("end"), int)
        ):
            raise ContextPreparationError(
                f"invalid surface_op for event {seq}", code="invalid_surface_op"
            )
        if event["type"] not in SURFACE_PRODUCING_TYPES and surface_op != APPEND_FORM:
            raise ContextPreparationError(
                f"non-surface event {event_type} must use append form", code="invalid_surface_op"
            )
        source_seqs = event.get("source_event_seqs", [])
        if not isinstance(source_seqs, list) or any(not isinstance(value, int) for value in source_seqs):
            raise ContextPreparationError(
                f"invalid source_event_seqs for event {seq}", code="invalid_source_seqs"
            )
        if len(set(source_seqs)) != len(source_seqs):
            raise ContextPreparationError(
                f"duplicate source_event_seqs for event {seq}", code="invalid_source_seqs"
            )
        if list(source_seqs) != sorted(source_seqs):
            raise ContextPreparationError(
                f"source_event_seqs must be sorted for event {seq}", code="invalid_source_seqs"
            )
        if event_type in {"context/pruned-tool-result", "context/summary"} and isinstance(
            surface_op, dict
        ):
            declared = set(source_seqs)
            span = set(self._span_seqs(surface_op))
            missing = span - declared
            if missing:
                raise ContextPreparationError(
                    f"replacement event {seq} does not cite every shadowed node: {sorted(missing)}",
                    code="incomplete_sources",
                )
    def _span_seqs(self, op: dict) -> list[int]:
        start_index = self._surface.node_index_by_seq(int(op["start"]))
        end_index = self._surface.node_index_by_seq(int(op["end"]))
        return [node.seq for node in self._surface.nodes[start_index : end_index + 1]]

    def _validate_surface_integrity(self) -> None:
        """Enforce tool pairing invariants over the current Surface."""
        open_calls = {}
        for node in self._surface.nodes:
            data = node.data
            if node.event_type == "tool/call":
                open_calls[str(data.get("call_id", node.seq))] = node
            elif node.event_type == "tool/result":
                call_id = str(data.get("call_id", ""))
                if call_id not in open_calls:
                    raise ContextPreparationError(
                        f"orphan tool/result at seq {node.seq}", code="orphan_tool_result"
                    )
                del open_calls[call_id]
            elif node.event_type == "context/pruned-tool-result":
                # A pruned replacement must shadow exactly one current
                # tool/result or earlier pruned replacement for the same call.
                call_id = str(data.get("call_id", ""))
                if call_id not in open_calls:
                    raise ContextPreparationError(
                        f"pruned result without an open call at seq {node.seq}",
                        code="orphan_tool_result",
                    )
        if open_calls:
            raise ContextPreparationError(
                f"surface has open tool calls without results: {sorted(open_calls)}",
                code="open_tool_call",
            )

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def to_session_dict(self) -> dict:
        return {
            "schema_version": SESSION_SCHEMA_VERSION,
            "id": self.session_id,
            "created_at": self.created_at,
            "workspace_root": self.workspace_root,
            "events": copy.deepcopy(self._events),
            "memory": copy.deepcopy(self._memory),
            "checkpoints": copy.deepcopy(self._checkpoints),
            "runtime_identity": copy.deepcopy(self._runtime_identity),
            "resume_state": copy.deepcopy(self._resume_state),
        }

    @classmethod
    def from_session_dict(cls, session: dict) -> "SessionLedger":
        if int(session.get("schema_version", 1)) != SESSION_SCHEMA_VERSION:
            raise ContextPreparationError(
                f"unsupported session schema version: {session.get('schema_version')}",
                code="unsupported_session_schema",
            )
        return cls(
            session_id=str(session.get("id", "")),
            workspace_root=str(session.get("workspace_root", "")),
            created_at=str(session.get("created_at", "")),
            events=session.get("events") or [],
            memory=session.get("memory") or {},
            checkpoints=session.get("checkpoints") or {},
            runtime_identity=session.get("runtime_identity") or {},
            resume_state=session.get("resume_state") or {},
        )

    def save(self, path: Path) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        text = json.dumps(self.to_session_dict(), indent=2, sort_keys=True, ensure_ascii=False)
        temp_path = path.with_suffix(path.suffix + ".tmp")
        temp_path.write_text(text, encoding="utf-8")
        import os

        os.replace(temp_path, path)

    def flush_dispatch(self) -> None:
        """Durably flush events through the latest dispatch seq.

        When a ``persist_path`` is configured, the full session dict is
        written atomically; a persistence failure raises so the Run stops
        before another model request (INV-003). Without a persist path this
        is a no-op (in-memory ledger, e.g. unit tests).
        """
        if self.persist_path is None:
            return None
        self.save(self.persist_path)
        return self.persist_path


def render_surface_node(node: SurfaceNode) -> str:
    """Render one Surface node into model-visible conversational text."""
    data = node.data
    if node.event_type == "user/message":
        return f"[user] {data.get('content', '')}"
    if node.event_type == "assistant/message":
        return f"[assistant] {data.get('content', '')}"
    if node.event_type == "tool/call":
        import json as _json

        return (
            f"[tool:{data.get('tool_name', '')}] call_id={data.get('call_id', '')} "
            f"args={_json.dumps(data.get('args', {}), sort_keys=True, ensure_ascii=True)}"
        )
    if node.event_type == "tool/result":
        content = data.get("content", "")
        head = data.get("content_sha256", "")
        return (
            f"[tool-result:{data.get('tool_name', '')}] call_id={data.get('call_id', '')} "
            f"content_sha256={head}\n{content}"
        )
    if node.event_type == "context/pruned-tool-result":
        return (
            f"[pruned-tool-result:{data.get('tool_name', '')}] call_id={data.get('call_id', '')} "
            f"original_sha256={data.get('original_content_sha256', '')}\n{data.get('replacement', '')}"
        )
    if node.event_type == "context/summary":
        return f"[context-summary] generation={data.get('generation', '')}\n{data.get('rendered', '')}"
    return f"[{node.event_type}] {canonical_json(data)}"
