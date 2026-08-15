"""Real-model summarizer adapter for history compaction.

The summarizer uses the same provider/model as the main Agent with
temperature zero, receives the selected events in Surface order with stable
seq identifiers and roles, and is constrained to a strict structured summary
schema. It never receives hidden-oracle metadata or experiment-condition
labels. All summary calls count toward tokens, latency, failures, and total
system cost.
"""

from __future__ import annotations

import json
import time
from typing import Callable

from .compaction import parse_summary_json, validate_summary_schema

SCHEMA_EXAMPLE = {
    "schema_version": 1,
    "goal": {
        "text": "string",
        "status": "confirmed|hypothesis|open",
        "sources": [{"event_seq": 1, "quote": "exact quote from the event text"}],
    },
    "constraints": [],
    "files": [],
    "changes": [],
    "tests": [],
    "open_items": [],
    "next_step": {"text": "string", "status": "confirmed|hypothesis|open", "sources": []},
}


SUMMARY_PROMPT_MAX_CHARS = 4200
SUMMARY_EVENT_EXCERPT_CHARS = 240


def _excerpt(text: str, limit: int) -> str:
    text = str(text)
    if len(text) <= limit:
        return text
    return text[:limit] + f"... [{len(text) - limit} chars omitted]"


def build_summary_request(events: list[dict], renderings: dict, max_tokens: int) -> str:
    """Render the summary request prompt for a selected Surface range.

    The prompt is deliberately bounded: each event is presented as a bounded
    excerpt with its stable seq identifier, and the whole prompt is capped so
    the model has room to answer within its output budget. Facts are never
    quoted from hidden metadata; only the model-visible excerpts are sent.
    """
    lines = [
        "You are summarizing an OLD segment of a coding-agent session. The rest of the",
        "session continues with this summary, so it must be accurate and grounded.",
        "",
        "Produce ONLY one JSON object matching this exact schema:",
        json.dumps(SCHEMA_EXAMPLE, indent=2),
        "",
        "Rules:",
        "- status MUST be one of: confirmed | hypothesis | open.",
        "- A confirmed claim MUST cite at least one source with integer event_seq and an",
        "  exact quote that appears verbatim (case-sensitive) in the cited event excerpt.",
        "- hypothesis and open claims MAY omit sources but MUST NOT be presented as",
        "  established facts.",
        "- Never claim an edit, test, or action happened without a cited event showing it.",
        "- Quotes must be exact substrings of the cited event excerpt; paraphrase or",
        "  ellipsis is not allowed.",
        "- Do not invent events, paths, or results that are not in the excerpts below.",
        "- Every entry in constraints, files, changes, tests, and open_items MUST be a",
        "  JSON object with text, status, and sources fields (same shape as goal).",
        "- Return valid JSON only, no prose before or after.",
        "",
        "Selected events (in chronological order, bounded excerpts):",
    ]
    for seq, rendering in renderings.items():
        lines.append(f"[seq {seq}] {_excerpt(rendering, SUMMARY_EVENT_EXCERPT_CHARS)}")
    lines.append("")
    lines.append(f"Output at most {int(max_tokens)} tokens. Return the JSON object only.")
    prompt = "\n".join(lines)
    if len(prompt) > SUMMARY_PROMPT_MAX_CHARS:
        # The full prompt must stay bounded: truncate the event excerpt body
        # from the middle, keeping the schema and rules intact.
        head = prompt[: SUMMARY_PROMPT_MAX_CHARS // 2]
        tail = prompt[len(prompt) - SUMMARY_PROMPT_MAX_CHARS // 2 :]
        prompt = head + "\n...[intermediate event excerpts omitted]...\n" + tail
    return prompt


class SummaryUsage:
    """Accumulates summary-call cost/latency counters for one Run."""

    def __init__(self):
        self.calls = 0
        self.input_tokens = 0
        self.output_tokens = 0
        self.total_tokens = 0
        self.latency_ms = 0
        self.failures = 0

    def as_dict(self) -> dict:
        return {
            "summary_calls": self.calls,
            "summary_input_tokens": self.input_tokens,
            "summary_output_tokens": self.output_tokens,
            "summary_total_tokens": self.total_tokens,
            "summary_latency_ms": self.latency_ms,
            "summary_failures": self.failures,
        }


class RealModelSummarizer:
    """Summarizer adapter that calls the configured provider/model at temp 0."""

    def __init__(self, model_client, usage: SummaryUsage | None = None):
        self.model_client = model_client
        self.usage = usage or SummaryUsage()

    def __call__(self, events: list[dict], renderings: dict, max_tokens: int) -> str:
        prompt = build_summary_request(events, renderings, max_tokens)
        # The configured summary cap is the requested JSON size, but the
        # transport call needs room for provider reasoning before content.
        request_max = max(int(max_tokens), 2048)
        started = time.monotonic()
        try:
            raw = self.model_client.complete(prompt, request_max)
        except Exception as exc:
            self.usage.failures += 1
            raise
        finally:
            self.usage.latency_ms += int((time.monotonic() - started) * 1000)
        self.usage.calls += 1
        metadata = dict(getattr(self.model_client, "last_completion_metadata", {}) or {})
        self.usage.input_tokens += int(metadata.get("input_tokens", 0) or 0)
        self.usage.output_tokens += int(metadata.get("output_tokens", 0) or 0)
        self.usage.total_tokens += int(metadata.get("total_tokens", 0) or 0)
        # Validate structure before returning so compaction sees a clean error.
        data = parse_summary_json(raw)
        validate_summary_schema(data)
        return raw


def build_summarizer_factory(model_client_factory: Callable, provider: str, model: str):
    """Build a summarizer factory bound to a fresh temp-0 client per Run."""

    def factory(task, ledger):
        del task, ledger
        client = model_client_factory()
        usage = SummaryUsage()
        return RealModelSummarizer(client, usage), usage

    return factory
