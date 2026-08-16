"""Three-layer verification for context-compaction Runs.

Every Run produces three independent results:

- ``artifact_pass``: public plus hidden task contract succeeds (the public
  verifier command is executed against the workspace copy, and the hidden
  verifier's required fact must be present in the model-visible artifacts);
- ``context_pass``: required critical facts and contradiction rules succeed;
- ``trace_pass``: event continuity, pairing, transaction, and replay
  invariants succeed.

``run_pass = artifact_pass AND context_pass AND trace_pass``.

Hidden verifiers and hidden oracles live outside the Agent-visible workspace
and are executed only by the benchmark runner. They verify only requirements
already stated in the public task contract.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from ...context.hashing import canonical_dispatch_sha256
from ...context.render import render_dispatch_prompt


def _fold_surface(events: list[dict]):
    """Fold events in seq order into the current Surface projection.

    Append adds a node; a replacement removes the inclusive span of current
    Surface nodes by seq and inserts the replacement node at the span's first
    position. This mirrors SessionLedger.surface() so pairing and replay can
    be verified over the model-visible projection rather than the raw event
    stream.
    """
    surface: list[dict] = []
    for event in events:
        op = event.get("surface_op")
        if not (isinstance(op, dict) and op.get("op") in {"replace", "remove"}):
            surface.append(event)
            continue
        start_seq = int(op.get("start", -1))
        end_seq = int(op.get("end", -1))
        start_index = next((i for i, item in enumerate(surface) if item["seq"] == start_seq), None)
        end_index = next((i for i, item in enumerate(surface) if item["seq"] == end_seq), None)
        if start_index is None or end_index is None or start_index > end_index:
            return None
        if op.get("op") == "remove":
            del surface[start_index : end_index + 1]
        else:
            surface[start_index : end_index + 1] = [event]
    return surface


def _node_stub(event: dict):
    """Wrap an event dict as the lightweight node shape the renderer reads."""
    return type("Node", (), {"seq": event["seq"], "event_type": event["type"], "data": event["data"]})()


def replay_integrity(events: list[dict]) -> bool:
    """Reconstruct every dispatched request from durable ledger events and
    verify its canonical SHA-256 matches the recorded dispatch hash.

    For each request/dispatch record we:
    1. fold all events through the dispatch seq into the Surface projection;
    2. rebuild the exact flat prompt from the header system prompt, the
       context memory snapshot, the folded Surface, and the current user
       request (via the same versioned rendering the engine uses);
    3. recompute ``canonical_dispatch_sha256`` over that envelope and compare
       it with ``canonical_request_sha256`` recorded in the dispatch event.

    Any mutation of a source event, a dropped replacement span, or a tampered
    dispatch hash makes the recomputed hash differ and the check fails.
    """
    seqs = [int(event.get("seq", -1)) for event in events]
    if seqs != list(range(len(seqs))):
        return False
    dispatches = [event for event in events if event["type"] == "request/dispatch"]
    if not dispatches:
        return False
    for dispatch in dispatches:
        header_seq = int(dispatch["data"].get("header_seq", -1))
        context_seq = int(dispatch["data"].get("context_seq", -1))
        if header_seq >= dispatch["seq"] or context_seq >= dispatch["seq"]:
            return False
        header = next((e for e in events if e["seq"] == header_seq), None)
        context = next((e for e in events if e["seq"] == context_seq), None)
        if header is None or context is None:
            return False
        system_prompt = str(header["data"].get("system_prompt", ""))
        memory_text = str(context["data"].get("memory", ""))
        surface = _fold_surface([e for e in events if e["seq"] <= dispatch["seq"]])
        if surface is None:
            return False
        # The current user request is the trailing user/message Surface node at
        # dispatch time (the engine appends it before rendering).
        current_request = ""
        for event in surface:
            if event["type"] == "user/message":
                current_request = str(event["data"].get("content", ""))
        nodes = [_node_stub(event) for event in surface]
        renderer = str(dispatch["data"].get("renderer", "replayable"))
        if renderer == "legacy":
            prompt = _rebuild_legacy_prompt(system_prompt, memory_text, nodes, current_request)
        else:
            prompt = render_dispatch_prompt(system_prompt, memory_text, nodes, current_request)
        expected = canonical_dispatch_sha256(
            provider=str(header["data"].get("provider", "")),
            model=str(header["data"].get("model", "")),
            temperature=float(header["data"].get("temperature", 0.0)),
            max_output_tokens=int(header["data"].get("max_output_tokens", 0)),
            prompt=prompt,
        )
        recorded = str(dispatch["data"].get("canonical_request_sha256", ""))
        if expected != recorded:
            return False
    return True


def _rebuild_legacy_prompt(system_prompt: str, memory_text: str, nodes, user_message: str) -> str:
    """Rebuild the legacy char-budget prompt from folded Surface nodes using
    the same versioned renderer the LegacyContextEngine dispatches."""
    from ...context.engine import _LegacyRenderer

    class _StubLedger:
        class _Surface:
            def __init__(self, nodes):
                self.nodes = nodes

        def __init__(self, nodes):
            self.surface = self._Surface(nodes)

    stub = _StubLedger(nodes)
    renderer = _LegacyRenderer(stub, system_prompt, memory_text, user_message)
    prompt, _ = renderer.render()
    return prompt


def tool_pairs_balanced(events: list[dict]) -> bool:
    """Every Surface generation must have balanced tool call/result pairing.

    Pairing is verified over the folded Surface projection: a tool/call opens
    a call, and a tool/result or pruned replacement closes it. An orphan
    result, a pruned result without an open call, or an open call left
    dangling at the end fails the invariant.
    """
    surface = _fold_surface(events)
    if surface is None:
        return False
    open_calls = set()
    for event in surface:
        event_type = event["type"]
        data = event["data"]
        if event_type == "tool/call":
            open_calls.add(str(data.get("call_id", event["seq"])))
        elif event_type in {"tool/result", "context/pruned-tool-result"}:
            call_id = str(data.get("call_id", ""))
            if call_id not in open_calls:
                return False
            open_calls.discard(call_id)
    return not open_calls


def partial_transactions_zero(events: list[dict]) -> bool:
    """A committed context/summary with a compaction_id must be closed by a
    successful compaction/end; a compaction/start must be closed by an end."""
    starts = {event["data"]["compaction_id"] for event in events if event["type"] == "compaction/start"}
    ends = {event["data"]["compaction_id"] for event in events if event["type"] == "compaction/end"}
    if not starts.issubset(ends):
        return False
    committed = {
        event["data"]["compaction_id"]
        for event in events
        if event["type"] == "context/summary" and "compaction_id" in event["data"]
    }
    successful_ends = {
        event["data"]["compaction_id"]
        for event in events
        if event["type"] == "compaction/end" and event["data"].get("success")
    }
    return committed.issubset(successful_ends)


def current_request_preserved(events: list[dict], user_request: str) -> bool:
    """The current user request must be referenced by the LAST dispatch's
    surface_event_seqs, not merely present somewhere in the history."""
    dispatches = [event for event in events if event["type"] == "request/dispatch"]
    if not dispatches:
        return False
    last = dispatches[-1]
    dispatch_seqs = set(last["data"].get("surface_event_seqs", []))
    for event in events:
        if event["type"] == "user/message" and event["data"].get("content") == user_request:
            if event["seq"] in dispatch_seqs:
                return True
    return False


def duplicate_side_effect_zero(trace_events: list[dict]) -> bool:
    """No automatically repeated successfully completed side-effecting tool
    call. An unmatched durable call on resume is uncertain, never re-run."""
    seen = set()
    for event in trace_events:
        if event.get("event") != "tool_executed" or not event.get("workspace_changed"):
            continue
        identity = (str(event.get("name", "")), json.dumps(event.get("args") or {}, sort_keys=True))
        if identity in seen:
            return False
        seen.add(identity)
    return True


def _normalized(text: str) -> str:
    return re.sub(r"\s+", " ", str(text).strip()).casefold()


def context_facts_pass(surface_text: str, facts: list[dict]) -> dict:
    """Deterministic fact/contradiction oracle over the model-visible Surface.

    A critical fact is retained if its authoritative content is visible in the
    Surface. A contradiction fails the fact only when the obsolete value is
    presented WITHOUT the authoritative value -- the dangerous compaction
    outcome where the correct event was dropped and a contradicted value
    remains. When the raw history contains both values (a historical statement
    plus the authoritative replacement), the fact still passes because the
    correct value is visible.
    """
    normalized = _normalized(surface_text)
    results = []
    for fact in facts:
        predicate = _normalized(fact.get("predicate", ""))
        contradiction = _normalized(fact.get("contradiction_predicate", ""))
        present = bool(predicate) and predicate in normalized
        contradicted = bool(contradiction) and contradiction in normalized
        # A fact is retained only when its authoritative content is visible.
        # When the obsolete value is visible without the authoritative value,
        # the fact fails; when both are visible (raw history), the fact passes.
        retained = bool(present)
        results.append(
            {
                "id": str(fact.get("id", "")),
                "fact_present": present,
                "contradicted": contradicted,
                "retained": retained,
            }
        )
    return {
        "facts": results,
        "fact_retention": sum(1 for result in results if result["retained"]),
        "required_facts": len(results),
        "pass": all(result["retained"] for result in results),
    }


def artifact_verifier_pass(
    *,
    workspace_copy_root: Path,
    task: dict,
    hidden_verifier: dict,
    public_verifier_result: dict | None = None,
    events: list[dict] | None = None,
    surface_text: str = "",
) -> dict:
    """Execute the public and hidden artifact verifiers against the workspace.

    ``public_verifier_result`` is the subprocess outcome produced by the
    runner (exit code + bounded output); when None the public verifier has
    not been executed and the check fails. Hidden checks may validate the
    declared artifact, the durable event projection, or the model-visible
    surface. The hidden verifier payload itself is never part of the workspace.
    """
    public_pass = bool(public_verifier_result and public_verifier_result.get("exit_code") == 0)
    hidden_required = str(hidden_verifier.get("required_fact") or "").strip()
    hidden_surface_required = str(hidden_verifier.get("surface_required_fact") or "").strip()
    hidden_check = str(hidden_verifier.get("check") or "").strip()
    hidden_pass = False
    artifact_rel = str(task.get("artifact_path") or "").strip()
    if hidden_required and artifact_rel:
        artifact_path = (workspace_copy_root / artifact_rel).resolve()
        try:
            artifact_path.relative_to(workspace_copy_root.resolve())
        except ValueError:
            artifact_path = None
        if artifact_path is not None and artifact_path.is_file():
            try:
                text = artifact_path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                text = ""
            hidden_pass = _normalized(hidden_required) in _normalized(text)
    elif hidden_surface_required:
        hidden_pass = _normalized(hidden_surface_required) in _normalized(surface_text)
    elif hidden_check == "pair_balance":
        hidden_pass = tool_pairs_balanced(events or [])
    artifact_pass = bool(public_pass and hidden_pass)
    return {
        "artifact_pass": artifact_pass,
        "public_pass": public_pass,
        "hidden_pass": hidden_pass,
        "public_verifier": {
            "exit_code": (public_verifier_result or {}).get("exit_code"),
            "stdout_tail": (public_verifier_result or {}).get("stdout", "")[-500:],
            "stderr_tail": (public_verifier_result or {}).get("stderr", "")[-500:],
            "cache_telemetry": (public_verifier_result or {}).get("cache_telemetry", {}),
        },
        "hidden_verifier_id": str(hidden_verifier.get("check", "")),
        "hidden_required_fact": hidden_required,
        "hidden_surface_required_fact": hidden_surface_required,
        "artifact_path": artifact_rel,
    }


def verify_run(
    *,
    events: list[dict],
    trace_events: list[dict],
    surface_text: str,
    user_request: str,
    critical_facts: list[dict],
    process_invariants: list[str],
    artifact_result: dict | None = None,
) -> dict:
    checks = {}
    if "tool_pairs_balanced" in process_invariants:
        checks["tool_pairs_balanced"] = tool_pairs_balanced(events)
    if "current_request_preserved" in process_invariants:
        checks["current_request_preserved"] = current_request_preserved(events, user_request)
    if "request_replayable" in process_invariants:
        checks["request_replayable"] = replay_integrity(events)
    if "partial_transaction_zero" in process_invariants:
        checks["partial_transaction_zero"] = partial_transactions_zero(events)
    if "duplicate_side_effect_zero" in process_invariants:
        checks["duplicate_side_effect_zero"] = duplicate_side_effect_zero(trace_events)

    context = context_facts_pass(surface_text, critical_facts)
    trace_pass = all(checks.values())
    if artifact_result is None or artifact_result.get("artifact_pass") is None:
        artifact_pass = None
        artifact_skipped = True
    else:
        artifact_pass = bool(artifact_result.get("artifact_pass"))
        artifact_skipped = bool(artifact_result.get("skipped"))
    # The artifact dimension is only evaluated when the runner executed the
    # public/hidden verifiers (paid runs); the deterministic gate records it
    # as skipped rather than pretending it passed.
    mechanics_pass = bool(context["pass"] and trace_pass)
    # A deterministic gate deliberately does not execute artifact verifiers.
    # Keep that dimension out of paid-run pass rates instead of treating a
    # mechanics-only result as a completed end-to-end pass.
    if not mechanics_pass:
        run_pass = False
    else:
        run_pass = None if artifact_skipped else bool(artifact_pass)
    return {
        "context_pass": context["pass"],
        "trace_pass": trace_pass,
        "trace_checks": checks,
        "context": context,
        "artifact_pass": artifact_pass,
        "artifact_skipped": artifact_skipped,
        "artifact": artifact_result or {},
        "mechanics_pass": mechanics_pass,
        "run_pass": run_pass,
    }
