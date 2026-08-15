"""Evidence-anchored history compaction transaction.

Compaction summarizes a safe old Surface range and replaces that range with
one ``context/summary`` Surface node while retaining all source events in the
ledger. Every confirmed claim MUST carry at least one valid Evidence Anchor:
an exact, bounded quote found in the cited source event's model-visible
rendering.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

from .ledger import render_surface_node, sha256_text
from .types import ContextPolicy

SUMMARY_SCHEMA_VERSION = 1
CONFIRMED = "confirmed"
HYPOTHESIS = "hypothesis"
OPEN = "open"
CLAIM_STATUSES = frozenset({CONFIRMED, HYPOTHESIS, OPEN})


@dataclass(frozen=True)
class EvidenceAnchor:
    event_seq: int
    quote: str


@dataclass(frozen=True)
class Claim:
    text: str
    status: str
    sources: tuple[EvidenceAnchor, ...] = ()


@dataclass(frozen=True)
class SummaryCandidate:
    goal: Claim
    constraints: tuple[Claim, ...] = ()
    files: tuple[Claim, ...] = ()
    changes: tuple[Claim, ...] = ()
    tests: tuple[Claim, ...] = ()
    open_items: tuple[Claim, ...] = ()
    next_step: Claim | None = None

    def all_claims(self):
        claims = [self.goal, *self.constraints, *self.files, *self.changes, *self.tests, *self.open_items]
        if self.next_step is not None:
            claims.append(self.next_step)
        return claims


def parse_summary_json(raw_text: str) -> dict:
    text = str(raw_text).strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"summary output is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("summary output must be a JSON object")
    return data


def validate_summary_schema(data: dict) -> None:
    if not isinstance(data, dict):
        raise ValueError("summary must be a mapping")
    schema_version = data.get("schema_version")
    if int(schema_version or 0) != SUMMARY_SCHEMA_VERSION:
        raise ValueError(f"unsupported summary schema_version: {schema_version!r}")
    for key in ("goal", "constraints", "files", "changes", "tests", "open_items", "next_step"):
        if key not in data:
            raise ValueError(f"summary is missing required section: {key}")
    goal = data.get("goal")
    if not isinstance(goal, dict) or not str(goal.get("text", "")).strip():
        raise ValueError("summary goal must have non-empty text")
    validate_claim_object(goal, "goal")
    for key in ("constraints", "files", "changes", "tests", "open_items"):
        value = data.get(key)
        if not isinstance(value, list):
            raise ValueError(f"summary section {key} must be a list")
        for index, item in enumerate(value):
            validate_claim_object(item, f"{key}[{index}]")
    next_step = data.get("next_step")
    if next_step is not None:
        if not isinstance(next_step, dict):
            raise ValueError("summary next_step must be a mapping or null")
        validate_claim_object(next_step, "next_step")


def validate_claim_object(item, label: str) -> None:
    if not isinstance(item, dict):
        raise ValueError(f"summary claim {label} must be a mapping")
    text = str(item.get("text", "")).strip()
    if not text:
        raise ValueError(f"summary claim {label} must have non-empty text")
    status = str(item.get("status", "")).strip()
    if status not in CLAIM_STATUSES:
        raise ValueError(f"summary claim {label} has invalid status: {status!r}")
    sources = item.get("sources")
    if not isinstance(sources, list):
        raise ValueError(f"summary claim {label} sources must be a list")
    for source in sources:
        if not isinstance(source, dict):
            raise ValueError(f"summary claim {label} source must be a mapping")
        event_seq = source.get("event_seq")
        quote = source.get("quote")
        if not isinstance(event_seq, int) or not isinstance(quote, str):
            raise ValueError(f"summary claim {label} source must have integer event_seq and string quote")
    if status == CONFIRMED and not sources:
        raise ValueError(f"summary claim {label} is confirmed but has no evidence anchors")


def _newline_normalized(text: str) -> str:
    return str(text).replace("\r\n", "\n").replace("\r", "\n")


def _normalize_quote(quote: str) -> str:
    return _newline_normalized(str(quote)).strip()


def validate_evidence_anchors(data: dict, renderer, *, max_quote_chars: int) -> None:
    """Validate every Evidence Anchor against source event renderings.

    ``renderer`` maps an event seq to its model-visible rendering string, or
    returns None for a seq outside the selected range. After newline
    normalization only, each quote must be an exact, case-sensitive substring
    of the cited event's model-visible rendering.
    """
    max_quote_chars = int(max_quote_chars)
    for label in ("goal", "next_step", "constraints", "files", "changes", "tests", "open_items"):
        value = data.get(label)
        items = value if isinstance(value, list) else [value]
        for item in items:
            if not isinstance(item, dict):
                continue
            for source in item.get("sources", []):
                event_seq = int(source.get("event_seq", -1))
                quote = source.get("quote")
                if quote is None:
                    raise ValueError(f"evidence anchor in {label} has no quote")
                quote = str(quote)
                if not quote:
                    raise ValueError(f"evidence anchor in {label} has an empty quote")
                if len(quote) > max_quote_chars:
                    raise ValueError(
                        f"evidence anchor quote exceeds {max_quote_chars} chars in {label}"
                    )
                rendering = renderer(event_seq)
                if rendering is None:
                    raise ValueError(
                        f"evidence anchor cites nonexistent or out-of-range event seq {event_seq}"
                    )
                normalized_rendering = _newline_normalized(rendering)
                normalized_quote = _newline_normalized(quote).strip()
                if normalized_quote not in normalized_rendering:
                    raise ValueError(
                        f"evidence anchor quote is not an exact substring of event {event_seq} in {label}"
                    )


def _to_claim(item: dict) -> Claim:
    sources = tuple(
        EvidenceAnchor(event_seq=int(source["event_seq"]), quote=str(source["quote"]))
        for source in item.get("sources", [])
    )
    return Claim(
        text=str(item.get("text", "")).strip(),
        status=str(item.get("status", "")).strip(),
        sources=sources,
    )


def build_summary_candidate(data: dict) -> SummaryCandidate:
    validate_summary_schema(data)

    def section(key):
        value = data.get(key)
        if isinstance(value, list):
            return tuple(_to_claim(item) for item in value)
        return ()

    next_step_value = data.get("next_step")
    next_step = _to_claim(next_step_value) if isinstance(next_step_value, dict) else None
    return SummaryCandidate(
        goal=_to_claim(data["goal"]),
        constraints=section("constraints"),
        files=section("files"),
        changes=section("changes"),
        tests=section("tests"),
        open_items=section("open_items"),
        next_step=next_step,
    )


def render_summary(candidate: SummaryCandidate) -> str:
    lines = [
        "Context summary (evidence-anchored, not raw transcript):",
        f"- Goal [{candidate.goal.status}]: {candidate.goal.text}",
    ]
    for label, items in (
        ("Constraint", candidate.constraints),
        ("File", candidate.files),
        ("Change", candidate.changes),
        ("Test", candidate.tests),
        ("Open item", candidate.open_items),
    ):
        for claim in items:
            lines.append(f"- {label} [{claim.status}]: {claim.text}")
    if candidate.next_step is not None:
        lines.append(f"- Next step [{candidate.next_step.status}]: {candidate.next_step.text}")
    return "\n".join(lines)


def summary_output_sha256(raw_text: str) -> str:
    return sha256_text(str(raw_text))
