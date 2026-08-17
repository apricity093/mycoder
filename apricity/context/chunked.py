"""Chunked evidence-anchored LLM compaction for Context Horizon condition D.

Each completed step in the old Surface prefix is summarized separately with
bounded full excerpts; every confirmed claim must carry an exact quote from
that chunk. A failed chunk falls back to deterministic structural compaction
for the same source range. The whole prefix is then replaced in one
transaction.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from .ledger import render_surface_node, sha256_text
from .planner import plan_units, request_user_seqs, retained_tail_seqs
from .structural import build_structural_summary, select_structural_range

CHUNKED_POLICY_NAME = "chunked-llm-compaction"
CHUNKED_POLICY_VERSION = "v1"
CHUNK_SCHEMA_VERSION = 1
CHUNK_MAX_PROMPT_CHARS = 2000
CHUNK_EXCERPT_CHARS = 600
STATUSES = frozenset({"confirmed", "hypothesis", "open"})


class SummaryValidationError(ValueError):
    pass


@dataclass(frozen=True)
class ChunkedSummary:
    rendered: str
    source_event_seqs: tuple[int, ...]
    source_chars: int
    replacement_chars: int
    gain_ratio: float
    replacement_sha256: str
    fallback_chunks: tuple[int, ...] = ()


SCHEMA_EXAMPLE = {
    "schema_version": 1,
    "facts": [
        {
            "text": "the timeout default must be 120",
            "status": "confirmed",
            "sources": [{"event_seq": 3, "quote": "exact quote from event 3"}],
        }
    ],
    "open_items": ["still need to run the tail test"],
}


def build_summary_request(events: list[dict[str, Any]], renderings: dict[int, str], max_tokens: int) -> str:
    lines = [
        "You are summarizing ONE completed step of a coding-agent session.",
        "Produce ONLY one JSON object matching this schema:",
        json.dumps(SCHEMA_EXAMPLE, indent=2),
        "",
        "Rules:",
        "- status MUST be confirmed, hypothesis, or open.",
        "- A confirmed fact MUST cite at least one source with integer event_seq and an",
        "  exact case-sensitive quote from the cited event rendering.",
        "- Never claim an edit, test, or action happened without a cited event showing it.",
        "- Quotes must be exact substrings; paraphrase or ellipsis is not allowed.",
        "",
        "Selected events (bounded excerpts):",
    ]
    used = sum(len(line) + 1 for line in lines)
    for seq, rendering in renderings.items():
        excerpt = str(rendering)
        if len(excerpt) > CHUNK_EXCERPT_CHARS:
            excerpt = excerpt[:CHUNK_EXCERPT_CHARS] + f"...[{len(excerpt) - CHUNK_EXCERPT_CHARS} chars omitted]"
        line = f"[seq {seq}] {excerpt}"
        if used + len(line) + 1 > CHUNK_MAX_PROMPT_CHARS:
            lines.append(f"[... {len(renderings) - list(renderings).index(seq)} later events omitted ...]")
            break
        lines.append(line)
        used += len(line) + 1
    lines.append("")
    lines.append(f"Output at most {int(max_tokens)} tokens. Return the JSON object only.")
    return "\n".join(lines)


def parse_summary_json(raw_text: str) -> dict[str, Any]:
    text = str(raw_text).strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise SummaryValidationError(f"summary output is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise SummaryValidationError("summary output must be a JSON object")
    return data


def validate_summary_schema(data: dict[str, Any], renderings: dict[int, str]) -> None:
    if int(data.get("schema_version", 0)) != CHUNK_SCHEMA_VERSION:
        raise SummaryValidationError(f"unsupported summary schema_version: {data.get('schema_version')!r}")
    facts = data.get("facts")
    open_items = data.get("open_items")
    if not isinstance(facts, list):
        raise SummaryValidationError("summary facts must be a list")
    if not isinstance(open_items, list):
        raise SummaryValidationError("summary open_items must be a list")
    if not facts and not open_items:
        raise SummaryValidationError("summary must contain at least one fact or open item")
    for index, fact in enumerate(facts):
        if not isinstance(fact, dict):
            raise SummaryValidationError(f"fact {index} must be a mapping")
        text = str(fact.get("text", "")).strip()
        status = str(fact.get("status", "")).strip()
        sources = fact.get("sources")
        if not text:
            raise SummaryValidationError(f"fact {index} has empty text")
        if status not in STATUSES:
            raise SummaryValidationError(f"fact {index} has invalid status: {status!r}")
        if not isinstance(sources, list):
            raise SummaryValidationError(f"fact {index} sources must be a list")
        if status == "confirmed" and not sources:
            raise SummaryValidationError(f"confirmed fact {index} has no sources")
        for source in sources:
            if not isinstance(source, dict):
                raise SummaryValidationError(f"fact {index} source must be a mapping")
            seq = int(source.get("event_seq", -1))
            quote = str(source.get("quote", ""))
            if seq not in renderings:
                raise SummaryValidationError(f"fact {index} cites unknown event seq {seq}")
            if not quote or quote not in str(renderings[seq]):
                raise SummaryValidationError(f"fact {index} quote is not an exact substring of event {seq}")


def render_summary(data: dict[str, Any]) -> str:
    lines = ["Chunk summary (evidence-anchored, not raw transcript):"]
    for fact in data.get("facts", []):
        lines.append(f"- Fact [{fact.get('status', 'open')}]: {fact.get('text', '')}")
        for source in fact.get("sources", []):
            lines.append(f"  - evidence [seq {source.get('event_seq')}]: {source.get('quote', '')}")
    for item in data.get("open_items", []):
        lines.append(f"- Open: {item}")
    return "\n".join(lines)


def _group_steps(units: list[list[int]], surface_nodes: dict[int, Any]) -> list[list[list[int]]]:
    steps: list[list[list[int]]] = []
    current: list[list[int]] = []
    for unit in units:
        starts_step = any(surface_nodes[seq].event_type == "user/message" for seq in unit if seq in surface_nodes)
        if starts_step and current:
            steps.append(current)
            current = []
        current.append(unit)
    if current:
        steps.append(current)
    return steps


def compact_chunked_prefix(
    *,
    ledger,
    meter,
    policy,
    user_message: str,
    compactions_used: int,
    summarizer,
) -> bool:
    """Commit one chunked LLM compaction transaction, or return False."""
    if summarizer is None:
        return False
    if compactions_used >= int(policy.max_compactions_per_run):
        return False
    units = plan_units(ledger.surface)
    retained_tail = retained_tail_seqs(ledger.surface, units, int(policy.retained_recent_steps))
    request_seqs = request_user_seqs(ledger.surface, user_message)
    range_seqs = select_structural_range(units, retained_tail, request_seqs)
    if not range_seqs:
        return False

    selected = set(range_seqs)
    surface_nodes = {node.seq: node for node in ledger.surface if node.seq in selected}
    range_units = [unit for unit in units if unit and unit[0] in selected]
    steps = _group_steps(range_units, surface_nodes)
    if not steps:
        return False

    rendered_chunks: list[str] = []
    fallback_chunks: list[int] = []
    for index, step_units in enumerate(steps):
        chunk_seqs = [seq for unit in step_units for seq in unit]
        renderings = {seq: render_surface_node(surface_nodes[seq]) for seq in chunk_seqs}
        events = [ledger.event_by_seq(seq).to_dict() for seq in chunk_seqs]
        try:
            raw = summarizer(events=events, renderings=renderings, max_tokens=int(policy.summary_max_tokens))
            data = parse_summary_json(raw)
            validate_summary_schema(data, renderings)
            rendered_chunks.append(render_summary(data))
        except Exception:
            fallback = build_structural_summary(ledger, chunk_seqs, meter=meter)
            if fallback is None:
                return False
            rendered_chunks.append(fallback.rendered)
            fallback_chunks.append(index)

    rendered = (
        "Chunked context summary (LLM chunks with deterministic fallback):\n"
        + "\n".join(rendered_chunks)
    )
    source_chars = sum(len(render_surface_node(node)) for node in surface_nodes.values())
    replacement_tokens = int(meter.count_text(rendered))
    source_tokens = sum(int(meter.count_text(render_surface_node(node))) for node in surface_nodes.values())
    gain = 1.0 - (replacement_tokens / source_tokens if source_tokens else 1.0)
    if gain < float(policy.minimum_compaction_gain):
        return False

    summary = ChunkedSummary(
        rendered=rendered,
        source_event_seqs=tuple(sorted(selected)),
        source_chars=source_chars,
        replacement_chars=len(rendered),
        gain_ratio=gain,
        replacement_sha256=sha256_text(rendered),
        fallback_chunks=tuple(fallback_chunks),
    )
    ledger.append(
        "compaction/start",
        {
            "compaction_id": f"chunked-{ledger.surface_generation}",
            "policy": CHUNKED_POLICY_NAME,
            "policy_version": CHUNKED_POLICY_VERSION,
            "selected_surface_seqs": list(range_seqs),
            "source_token_count": source_tokens,
            "starting_surface_generation": ledger.surface_generation,
        },
    )
    ledger.append(
        "context/summary",
        {
            "kind": "chunked-llm",
            "rendered": summary.rendered,
            "policy": CHUNKED_POLICY_NAME,
            "policy_version": CHUNKED_POLICY_VERSION,
            "source_token_count": source_tokens,
            "replacement_token_count": replacement_tokens,
            "gain_ratio": round(summary.gain_ratio, 6),
            "replacement_sha256": summary.replacement_sha256,
            "fallback_chunks": list(summary.fallback_chunks),
            "summary_schema_version": CHUNK_SCHEMA_VERSION,
        },
        surface_op={"op": "replace", "start": range_seqs[0], "end": range_seqs[-1]},
        source_event_seqs=summary.source_event_seqs,
    )
    ledger.append(
        "compaction/end",
        {
            "compaction_id": f"chunked-{ledger.surface_generation - 1}",
            "success": True,
            "terminal_phase": "committed",
            "policy": CHUNKED_POLICY_NAME,
            "policy_version": CHUNKED_POLICY_VERSION,
            "token_gain": source_tokens - replacement_tokens,
            "gain_ratio": round(summary.gain_ratio, 6),
        },
    )
    return True
