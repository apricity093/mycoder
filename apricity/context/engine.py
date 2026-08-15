"""Context engine adapters behind the ContextEngine seam.

Two adapters satisfy the seam:

- ``LegacyContextEngine``: new ledger and request assembly, legacy
  character-budget policy (condition A);
- ``ReplayableContextEngine``: configurable token budgeting, pruning, and
  compaction (conditions B/C/D).

Both adapters own the ledger, emit request/header, request/context, and
request/dispatch events, and return a ``PreparedContext`` whose prompt and
hash describe exactly the next provider request.
"""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from typing import Any

from ..security import redact_text
from ..workspace import now
from .compaction import (
    build_summary_candidate,
    parse_summary_json,
    render_summary,
    validate_evidence_anchors,
    validate_summary_schema,
)
from .hashing import canonical_request_sha256
from .ledger import SessionLedger, render_surface_node
from .pruning import prune_tool_result
from .token_meter import CountingCache, DeterministicTestMeter, FallbackTokenEstimator, TokenMeter
from .types import ContextPolicy, ContextPreparationError, ModelRequest, PreparedContext

APPEND = "append"
TAIL_WINDOW = 6

# Legacy character budgets preserved from the old ContextManager policy so
# condition A keeps the exact old allocation and reduction order.
LEGACY_TOTAL_BUDGET = 12000
LEGACY_SECTION_BUDGETS = {
    "prefix": 3600,
    "memory": 1600,
    "relevant_memory": 1200,
    "history": 5200,
}
LEGACY_SECTION_FLOORS = {
    "prefix": 1200,
    "memory": 400,
    "relevant_memory": 300,
    "history": 1500,
}
LEGACY_REDUCTION_ORDER = ("relevant_memory", "history", "memory", "prefix")
CURRENT_REQUEST_SECTION = "current_request"

# Token-policy atomic unit: keep the most recent N complete steps plus the
# current request. A completed tool call/result pair is one removal unit.
TOKEN_UNIT_MESSAGE_TYPES = frozenset(
    {"user/message", "assistant/message", "tool/call", "tool/result", "context/summary"}
)


def _tail_clip(text: str, limit: int) -> str:
    text = str(text)
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    if limit <= 3:
        return text[:limit]
    return text[: limit - 3] + "..."


class _LegacyRenderer:
    """Renders the old char-budget prompt sections from ledger events.

    The legacy policy renders history with the same compression and clipping
    the old ``ContextManager`` used, but reads its events from the ledger
    instead of the mutable ``session["history"]`` list. The current request is
    appended verbatim and is never clipped.
    """

    def __init__(self, ledger: SessionLedger, prefix: str, memory_text: str, user_message: str):
        self.ledger = ledger
        self.prefix = str(prefix)
        self.memory_text = str(memory_text)
        self.user_message = str(user_message)

    def _history_items(self) -> list[dict]:
        items = []
        for node in self.ledger.surface.nodes:
            data = node.data
            if node.event_type == "user/message":
                items.append({"role": "user", "content": data.get("content", "")})
            elif node.event_type == "assistant/message":
                items.append({"role": "assistant", "content": data.get("content", "")})
            elif node.event_type == "tool/call":
                items.append(
                    {
                        "role": "tool",
                        "name": data.get("tool_name", ""),
                        "args": data.get("args", {}),
                        "content": "",
                    }
                )
            elif node.event_type == "tool/result":
                if items and items[-1]["role"] == "tool" and not items[-1]["content"]:
                    items[-1]["content"] = data.get("content", "")
                else:
                    items.append({"role": "tool", "name": "", "args": {}, "content": data.get("content", "")})
        return items

    def _raw_history_text(self, history):
        if not history:
            return "Transcript:\n- empty"
        lines = []
        for item in history:
            if item["role"] == "tool":
                args_json = json.dumps(item["args"], sort_keys=True, ensure_ascii=True)
                lines.append(f"[tool:{item['name']}] {args_json}")
                lines.append(str(item["content"]))
            else:
                lines.append(f"[{item['role']}] {item['content']}")
        return "\n".join(["Transcript:", *lines])

    def _render_history_item(self, item, line_limit):
        if item["role"] == "tool":
            prefix = f"[tool:{item['name']}] {json.dumps(item['args'], sort_keys=True, ensure_ascii=True)}"
            return [prefix, _tail_clip(item["content"], max(20, line_limit))]
        return [f"[{item['role']}] {_tail_clip(item['content'], line_limit)}"]

    def render_sections(self, budgets, reduction_enabled=True):
        section_texts = {
            "prefix": self.prefix,
            "memory": self.memory_text,
            "relevant_memory": "Relevant memory:\n- none",
            "history": "",
            CURRENT_REQUEST_SECTION: f"Current user request:\n{self.user_message}",
        }
        history = self._history_items()
        history_raw = self._raw_history_text(history)

        def render_history(budget):
            if not history:
                return "Transcript:\n- empty"
            recent_window = 6
            recent_start = max(0, len(history) - recent_window)
            entries = []
            seen_older_reads = set()
            for index, item in enumerate(history):
                recent = index >= recent_start
                if recent:
                    entries.append({"recent": True, "lines": self._render_history_item(item, 900)})
                    continue
                if item["role"] == "tool" and item["name"] == "read_file":
                    path = str(item["args"].get("path", "")).strip()
                    if path in seen_older_reads:
                        continue
                    seen_older_reads.add(path)
                    entries.append({"recent": False, "lines": [f"{path} -> (older read)"]})
                    continue
                if item["role"] == "tool":
                    entries.append({"recent": False, "lines": self._render_history_item(item, 60)})
                    continue
                entries.append({"recent": False, "lines": self._render_history_item(item, 60)})
            rendered_entries = []
            for entry in reversed(entries):
                candidate = list(entry["lines"]) + rendered_entries
                candidate_rendered = "\n".join(["Transcript:", *candidate])
                if len(candidate_rendered) <= budget:
                    rendered_entries = candidate
                    continue
                if entry["recent"]:
                    available = budget - len("Transcript:")
                    if rendered_entries:
                        available -= sum(len(line) + 1 for line in rendered_entries)
                    available = max(20, available - 1)
                    clipped = [_tail_clip(line, available) for line in entry["lines"]]
                    candidate = clipped + rendered_entries
                    if len("\n".join(["Transcript:", *candidate])) <= budget:
                        rendered_entries = candidate
                else:
                    clipped = [_tail_clip(line, 20) for line in entry["lines"]]
                    candidate = clipped + rendered_entries
                    if len("\n".join(["Transcript:", *candidate])) <= budget:
                        rendered_entries = candidate
            return "\n".join(["Transcript:", *rendered_entries])

        rendered = {}
        for section, budget in budgets.items():
            if section == CURRENT_REQUEST_SECTION:
                rendered[section] = section_texts[section]
            elif section == "history":
                rendered[section] = render_history(int(budget))
            else:
                rendered[section] = _tail_clip(section_texts[section], int(budget))
        rendered[CURRENT_REQUEST_SECTION] = section_texts[CURRENT_REQUEST_SECTION]
        return rendered

    def render(self, *, reduction_enabled=True):
        budgets = dict(LEGACY_SECTION_BUDGETS)
        rendered = self.render_sections(budgets, reduction_enabled=reduction_enabled)
        prompt = "\n\n".join(
            [
                rendered["prefix"],
                rendered["memory"],
                rendered["relevant_memory"],
                rendered["history"],
                rendered[CURRENT_REQUEST_SECTION],
            ]
        ).strip()
        reduction_log = []
        if reduction_enabled:
            while len(prompt) > LEGACY_TOTAL_BUDGET:
                overflow = len(prompt) - LEGACY_TOTAL_BUDGET
                reduced = False
                for section in LEGACY_REDUCTION_ORDER:
                    floor = LEGACY_SECTION_FLOORS.get(section, 0)
                    current = int(budgets.get(section, 0))
                    if current <= floor:
                        continue
                    new_budget = max(floor, current - overflow)
                    if new_budget >= current:
                        continue
                    reduction_log.append(
                        {
                            "section": section,
                            "before_chars": current,
                            "after_chars": new_budget,
                            "overflow_chars": overflow,
                        }
                    )
                    budgets[section] = new_budget
                    rendered = self.render_sections(budgets, reduction_enabled=reduction_enabled)
                    prompt = "\n\n".join(
                        [
                            rendered["prefix"],
                            rendered["memory"],
                            rendered["relevant_memory"],
                            rendered["history"],
                            rendered[CURRENT_REQUEST_SECTION],
                        ]
                    ).strip()
                    reduced = True
                    break
                if not reduced:
                    break
        return prompt, {
            "prompt_chars": len(prompt),
            "prompt_budget_chars": LEGACY_TOTAL_BUDGET,
            "prompt_over_budget": len(prompt) > LEGACY_TOTAL_BUDGET,
            "budget_reductions": reduction_log,
            "reduction_order": list(LEGACY_REDUCTION_ORDER),
            "section_budgets": {section: int(budgets.get(section, 0)) for section in budgets},
            "legacy_policy": "v1-char-budget",
        }


class LegacyContextEngine:
    """Condition A: old character-budget policy on the new ledger."""

    condition = "A"

    def __init__(
        self,
        *,
        ledger: SessionLedger,
        provider: str,
        model: str,
        temperature: float,
        max_output_tokens: int,
        system_prompt: str,
        tools: list,
        memory_text: str,
        redactor=None,
        user_message: str = "",
        seed_surface: list | None = None,
    ):
        self.ledger = ledger
        self.provider = provider
        self.model = model
        self.temperature = temperature
        self.max_output_tokens = int(max_output_tokens)
        self.system_prompt = str(system_prompt)
        self.tools = list(tools)
        self.memory_text = str(memory_text)
        self.redactor = redactor or (lambda text: text)
        self.user_message = user_message
        self._seeded = False
        self._header_seq: int | None = None
        self._context_seq: int | None = None
        self._last_header_sig: str | None = None
        self._last_context_sig: str | None = None
        if seed_surface:
            self._seed_surface(seed_surface)

    def _seed_surface(self, events: list) -> None:
        for event in events:
            self.ledger.append(
                event["type"],
                copy.deepcopy(event["data"]),
                source_event_seqs=tuple(event.get("source_event_seqs", ())),
            )
        self._seeded = True

    def _sync_request_header(self) -> None:
        signature = canonical_request_sha256(
            ModelRequest(
                provider=self.provider,
                model=self.model,
                temperature=self.temperature,
                max_output_tokens=self.max_output_tokens,
                system_prompt=self.system_prompt,
                messages=(),
                tools=tuple(self.tools),
            )
        )
        if self._last_header_sig == signature and self._header_seq is not None:
            return
        self.ledger.append(
            "request/header",
            {
                "provider": self.provider,
                "model": self.model,
                "temperature": self.temperature,
                "max_output_tokens": self.max_output_tokens,
                "system_prompt": self.system_prompt,
                "system_prompt_sha256": self._sha256(self.system_prompt),
                "tools": self.tools,
                "tool_schema_sha256": self._sha256(json.dumps(self.tools, sort_keys=True)),
            },
        )
        self._header_seq = self.ledger.seq - 1
        self._last_header_sig = signature

    def _sync_request_context(self) -> None:
        signature = canonical_request_sha256(
            ModelRequest(
                provider=self.provider,
                model=self.model,
                temperature=self.temperature,
                max_output_tokens=self.max_output_tokens,
                system_prompt="",
                messages=({"role": "user", "content": self.memory_text},),
                tools=(),
            )
        )
        if self._last_context_sig == signature and self._context_seq is not None:
            return
        self.ledger.append(
            "request/context",
            {
                "memory": self.memory_text,
                "checkpoint": "",
                "workspace_context": "",
                "content_sha256": self._sha256(self.memory_text),
            },
        )
        self._context_seq = self.ledger.seq - 1
        self._last_context_sig = signature

    def _messages_from_surface(self) -> list[dict]:
        messages = []
        for node in self.ledger.surface.nodes:
            if node.event_type == "user/message":
                messages.append({"role": "user", "content": node.data.get("content", "")})
            elif node.event_type == "assistant/message":
                messages.append({"role": "assistant", "content": node.data.get("content", "")})
            elif node.event_type in {"tool/call", "tool/result"}:
                data = node.data
                if node.event_type == "tool/call":
                    messages.append(
                        {
                            "role": "user",
                            "content": (
                                f"[tool call {data.get('tool_name', '')} call_id={data.get('call_id', '')}] "
                                f"{json.dumps(data.get('args', {}), sort_keys=True, ensure_ascii=True)}"
                            ),
                        }
                    )
                else:
                    messages.append(
                        {
                            "role": "user",
                            "content": (
                                f"[tool result {data.get('tool_name', '')} call_id={data.get('call_id', '')}]\n"
                                f"{data.get('content', '')}"
                            ),
                        }
                    )
            elif node.event_type in {"context/pruned-tool-result", "context/summary"}:
                messages.append(
                    {
                        "role": "user",
                        "content": render_surface_node(node),
                    }
                )
        return messages

    def prepare(self, user_message: str, *, trigger: str = "pressure") -> PreparedContext:
        user_message = str(user_message)
        self.user_message = user_message
        # Append the current user message as a durable Surface event before
        # rendering, so the request is reconstructable from the ledger. Skip
        # when a user/message with this content is already current in the
        # Surface, so repeated prepares for the same turn never duplicate the
        # request (INV-004).
        if not any(
            node.event_type == "user/message" and node.data.get("content") == user_message
            for node in self.ledger.surface.nodes
        ):
            self.ledger.append("user/message", {"content": user_message})
        renderer = _LegacyRenderer(self.ledger, self.system_prompt, self.memory_text, user_message)
        prompt, metadata = renderer.render()
        # The legacy prompt is the exact flat text the provider receives, so
        # the dispatch hash covers that envelope.
        from .hashing import canonical_dispatch_sha256

        digest = canonical_dispatch_sha256(
            provider=self.provider,
            model=self.model,
            temperature=self.temperature,
            max_output_tokens=self.max_output_tokens,
            prompt=prompt,
        )
        surface_seqs = self.ledger.surface_event_seqs()
        self._sync_request_header()
        self._sync_request_context()
        header_seq = self._header_seq
        context_seq = self._context_seq
        self.ledger.append(
            "request/dispatch",
            {
                "header_seq": header_seq,
                "context_seq": context_seq,
                "surface_generation": self.ledger.surface_generation,
                "surface_event_seqs": list(surface_seqs),
                "canonical_request_sha256": digest,
                "renderer": "legacy",
                "token_meter": {"name": "legacy", "version": "v1", "exact": False},
                "estimated_input_tokens": len(prompt),
                "trigger": str(trigger),
            },
        )
        # Durable-before-dispatch (INV-003): flush through the dispatch seq
        # before the provider request is sent.
        self.ledger.flush_dispatch()
        metadata.update(
            {
                "canonical_request_sha256": digest,
                "source_event_seqs": list(surface_seqs),
                "surface_generation": self.ledger.surface_generation,
                "condition": self.condition,
                "ledger_seq": self.ledger.seq,
            }
        )
        return PreparedContext(
            prompt=prompt,
            metadata=metadata,
            canonical_request_sha256=digest,
            source_event_seqs=tuple(surface_seqs),
            surface_generation=self.ledger.surface_generation,
            changed=True,
        )

    def _dispatch_hash(self, user_message: str) -> str:
        """Canonical hash of the exact flat prompt envelope dispatched to the
        provider (a single user message containing the rendered prompt)."""
        from .hashing import canonical_dispatch_sha256
        from .render import render_dispatch_prompt

        prompt = render_dispatch_prompt(
            self.system_prompt,
            self.memory_text,
            self.ledger.surface.nodes,
            user_message,
        )
        return canonical_dispatch_sha256(
            provider=self.provider,
            model=self.model,
            temperature=self.temperature,
            max_output_tokens=self.max_output_tokens,
            prompt=prompt,
        )

    @staticmethod
    def _sha256(text: str) -> str:
        import hashlib

        return hashlib.sha256(str(text).encode("utf-8")).hexdigest()


class ReplayableContextEngine:
    """Conditions B/C/D: token budgeting, deterministic pruning, compaction."""

    condition = "replayable"

    def __init__(
        self,
        *,
        ledger: SessionLedger,
        policy: ContextPolicy,
        provider: str,
        model: str,
        temperature: float,
        max_output_tokens: int,
        system_prompt: str,
        tools: list,
        memory_text: str,
        meter: TokenMeter,
        redactor=None,
        summarizer=None,
        seed_surface: list | None = None,
        protected_predicates: tuple[str, ...] = (),
    ):
        self.ledger = ledger
        self.policy = policy
        self.provider = provider
        self.model = model
        self.temperature = temperature
        self.max_output_tokens = int(max_output_tokens)
        self.system_prompt = str(system_prompt)
        self.tools = list(tools)
        self.memory_text = str(memory_text)
        self.meter = meter
        self.redactor = redactor or (lambda text: text)
        self.summarizer = summarizer
        self._cache = CountingCache(meter=meter)
        self._seeded = False
        self._compactions_used = 0
        self._last_prepare_signature: str | None = None
        self._header_seq: int | None = None
        self._context_seq: int | None = None
        self._last_header_sig: str | None = None
        self._last_context_sig: str | None = None
        self._protected_predicates = tuple(
            str(value).strip() for value in protected_predicates if str(value).strip()
        )
        if seed_surface:
            self._seed_surface(seed_surface)

    def _seed_surface(self, events: list) -> None:
        for event in events:
            self.ledger.append(
                event["type"],
                copy.deepcopy(event["data"]),
                source_event_seqs=tuple(event.get("source_event_seqs", ())),
            )
        self._seeded = True

    def _sync_request_header(self) -> None:
        signature = canonical_request_sha256(
            ModelRequest(
                provider=self.provider,
                model=self.model,
                temperature=self.temperature,
                max_output_tokens=self.max_output_tokens,
                system_prompt=self.system_prompt,
                messages=(),
                tools=tuple(self.tools),
            )
        )
        if self._last_header_sig == signature and self._header_seq is not None:
            return
        self.ledger.append(
            "request/header",
            {
                "provider": self.provider,
                "model": self.model,
                "temperature": self.temperature,
                "max_output_tokens": self.max_output_tokens,
                "system_prompt": self.system_prompt,
                "system_prompt_sha256": self._sha256(self.system_prompt),
                "tools": self.tools,
                "tool_schema_sha256": self._sha256(json.dumps(self.tools, sort_keys=True)),
            },
        )
        self._header_seq = self.ledger.seq - 1
        self._last_header_sig = signature

    def _sync_request_context(self) -> None:
        signature = canonical_request_sha256(
            ModelRequest(
                provider=self.provider,
                model=self.model,
                temperature=self.temperature,
                max_output_tokens=self.max_output_tokens,
                system_prompt="",
                messages=({"role": "user", "content": self.memory_text},),
                tools=(),
            )
        )
        if self._last_context_sig == signature and self._context_seq is not None:
            return
        self.ledger.append(
            "request/context",
            {
                "memory": self.memory_text,
                "checkpoint": "",
                "workspace_context": "",
                "content_sha256": self._sha256(self.memory_text),
            },
        )
        self._context_seq = self.ledger.seq - 1
        self._last_context_sig = signature

    @property
    def condition_id(self) -> str:
        return self.policy.condition_id

    def _messages_from_surface(self) -> list[dict]:
        from .render import surface_to_messages

        return surface_to_messages(self.ledger.surface.nodes)

    def _build_request(self, user_message: str) -> ModelRequest:
        from .render import surface_to_messages

        messages = surface_to_messages(self.ledger.surface.nodes, user_message)
        return ModelRequest(
            provider=self.provider,
            model=self.model,
            temperature=self.temperature,
            max_output_tokens=self.max_output_tokens,
            system_prompt=self.system_prompt,
            messages=tuple(messages),
            tools=tuple(self.tools),
        )

    def _render_prompt(self, request: ModelRequest) -> str:
        parts = [request.system_prompt, "Memory:", self.memory_text]
        for message in request.messages:
            parts.append(f"[{message['role']}] {message['content']}")
        return "\n\n".join(parts).strip()

    def _dispatch_hash(self, user_message: str) -> str:
        """Canonical hash of the exact flat prompt envelope dispatched to the
        provider (a single user message containing the rendered prompt)."""
        from .hashing import canonical_dispatch_sha256
        from .render import render_dispatch_prompt

        prompt = render_dispatch_prompt(
            self.system_prompt,
            self.memory_text,
            self.ledger.surface.nodes,
            user_message,
        )
        return canonical_dispatch_sha256(
            provider=self.provider,
            model=self.model,
            temperature=self.temperature,
            max_output_tokens=self.max_output_tokens,
            prompt=prompt,
        )

    def _effective_budget(self) -> int:
        return int(self.policy.effective_input_budget)

    def _pressure_active(self, request: ModelRequest, tokens: int) -> bool:
        if not self.policy.token_budgeting:
            return False
        budget = self._effective_budget()
        return tokens >= int(budget * self.policy.pressure_threshold)

    def prepare(self, user_message: str, *, trigger: str = "pressure") -> PreparedContext:
        user_message = str(user_message)
        if not any(
            node.event_type == "user/message" and node.data.get("content") == user_message
            for node in self.ledger.surface.nodes
        ):
            self.ledger.append("user/message", {"content": user_message})

        if not self.policy.token_budgeting:
            # Condition A fallback path (should not normally route here).
            request = self._build_request(user_message)
            return self._finalize(request, user_message, trigger)

        request = self._build_request(user_message)
        tokens = self.meter.count_request(request)
        initial_pressure = self._pressure_active(request, tokens)
        pressure = initial_pressure
        changed = False
        events: list[str] = []

        if pressure and self.policy.tool_pruning:
            pruned = self._prune_oversized_results()
            if pruned:
                events.append("context/pruned-tool-result")
                request = self._build_request(user_message)
                tokens = self.meter.count_request(request)
                pressure = self._pressure_active(request, tokens)
                changed = True

        if pressure and self.policy.history_compaction and self._compactions_used < self.policy.max_compactions_per_run:
            if self._compact(user_message, trigger):
                request = self._build_request(user_message)
                tokens = self.meter.count_request(request)
                pressure = self._pressure_active(request, tokens)
                changed = True

        if pressure:
            removed = self._remove_old_units(user_message, request, tokens)
            if removed:
                request = self._build_request(user_message)
                tokens = self.meter.count_request(request)
                pressure = self._pressure_active(request, tokens)
                changed = True
            # The bounded removal notice re-adds a small number of tokens, so
            # a single pass may land just above the target. Keep removing
            # until the request fits or no safe unit remains.
            guard = 0
            while pressure and guard < 50:
                guard += 1
                again = self._remove_old_units(user_message, request, tokens)
                if not again:
                    break
                request = self._build_request(user_message)
                tokens = self.meter.count_request(request)
                pressure = self._pressure_active(request, tokens)
                changed = True

        self.ledger.append(
            "context/pressure",
            {
                "pressure": bool(pressure) or bool(initial_pressure),
                "estimated_input_tokens": tokens,
                "initial_pressure": bool(initial_pressure),
                "trigger": str(trigger),
                "changed": changed,
            },
        )
        if pressure:
            raise ContextPreparationError(
                f"no_safe_range: retained units alone exceed the effective input budget ({tokens} tokens)",
                code="no_safe_range",
            )
        return self._finalize(request, user_message, trigger, pressure=bool(initial_pressure))

    def _finalize_metadata(
        self, request: ModelRequest, user_message: str, trigger: str, *, pressure: bool = False
    ) -> dict:
        digest = self._dispatch_hash(user_message)
        surface_seqs = self.ledger.surface_event_seqs()
        self._sync_request_header()
        self._sync_request_context()
        self.ledger.append(
            "request/dispatch",
            {
                "header_seq": self._header_seq,
                "context_seq": self._context_seq,
                "surface_generation": self.ledger.surface_generation,
                "surface_event_seqs": list(surface_seqs),
                "canonical_request_sha256": digest,
                "renderer": "replayable",
                "token_meter": {
                    "name": self.meter.name,
                    "version": self.meter.version,
                    "exact": bool(self.meter.exact),
                },
                "estimated_input_tokens": self.meter.count_request(request),
                "trigger": str(trigger),
            },
        )
        # Durable-before-dispatch: the ledger MUST be flushed through the
        # dispatch seq before the provider request is sent. A persistence
        # failure here must stop the Run before another model request.
        self.ledger.flush_dispatch()
        return {
            "canonical_request_sha256": digest,
            "source_event_seqs": list(surface_seqs),
            "surface_generation": self.ledger.surface_generation,
            "condition": self.condition_id,
            "condition_label": self.policy.condition_label,
            "ledger_seq": self.ledger.seq,
            "estimated_input_tokens": self.meter.count_request(request),
            "pressure": bool(pressure),
            "pressure_threshold": self.policy.pressure_threshold,
            "effective_input_budget": self._effective_budget(),
        }

    def _finalize(self, request: ModelRequest, user_message: str, trigger: str, *, pressure: bool = False) -> PreparedContext:
        metadata = self._finalize_metadata(request, user_message, trigger, pressure=pressure)
        return PreparedContext(
            prompt=self._render_prompt(request),
            metadata=metadata,
            canonical_request_sha256=metadata["canonical_request_sha256"],
            source_event_seqs=tuple(metadata["source_event_seqs"]),
            surface_generation=metadata["surface_generation"],
            changed=bool(pressure),
        )

    def _prune_oversized_results(self) -> bool:
        pruned_any = False
        for node in list(self.ledger.surface.nodes):
            if node.event_type not in {"tool/result", "context/pruned-tool-result"}:
                continue
            data = node.data
            tool_name = data.get("tool_name", "")
            content = data.get("content") or data.get("replacement") or ""
            metadata = {
                "call_id": data.get("call_id", ""),
                **data.get("metadata", {}),
            }
            payload = prune_tool_result(
                tool_name=tool_name,
                content=content,
                metadata=metadata,
                soft_limit=self.policy.tool_result_soft_limit,
                minimum_gain=self.policy.tool_pruning_minimum_gain,
            )
            if payload is None:
                continue
            start_seq = node.seq
            end_seq = node.seq
            self.ledger.append(
                "context/pruned-tool-result",
                payload,
                surface_op={"op": "replace", "start": start_seq, "end": end_seq},
                source_event_seqs=(node.seq,),
            )
            pruned_any = True
        return pruned_any

    def _retained_unit_plan(self) -> list[list[int]]:
        """Split the Surface into atomic removal units in Surface order.

        A completed tool call/result pair is one atomic unit; a single
        user/message or assistant/message is its own unit; a pruned result or
        summary replacement is its own unit. The current request must be
        preserved, so it is reported separately by the caller.
        """
        nodes = self.ledger.surface.nodes
        units: list[list[int]] = []
        current: list[int] = []
        for index, node in enumerate(nodes):
            if node.event_type == "tool/call":
                if current:
                    units.append(current)
                current = [node.seq]
                continue
            if node.event_type in {"tool/result", "context/pruned-tool-result"} and current:
                # A completed call/result pair must stay one atomic unit. A
                # pruned result has a newer event seq than its source result,
                # so pairing must follow Surface order rather than numeric seq
                # adjacency.
                previous_node = nodes[index - 1] if index else None
                if (
                    previous_node is not None
                    and previous_node.event_type == "tool/call"
                    and current == [previous_node.seq]
                ):
                    current.append(node.seq)
                    units.append(current)
                    current = []
                    continue
                if current:
                    units.append(current)
                current = [node.seq]
                continue
            if node.event_type in {"context/pruned-tool-result", "context/summary"}:
                if current:
                    units.append(current)
                # Replacements occupy their original Surface position while
                # carrying a newer ledger seq. Keep them as standalone units
                # so a later node cannot create a numerically reversed span.
                units.append([node.seq])
                current = []
                continue
            if node.event_type == "user/message":
                # The current user request is preserved by the caller; a
                # seeded or historical user/message is a removable unit.
                if current:
                    units.append(current)
                current = [node.seq]
                continue
            current.append(node.seq)
        if current:
            units.append(current)
        return units

    def _protected_seq_set(self) -> set[int]:
        """Seq set of authoritative fact-bearing Surface nodes that whole-unit
        removal must never drop. Only tool results, pruned results, and
        summaries are protected; plain user/assistant messages are removable
        even when they mention a predicate, as long as the authoritative
        source survives."""
        if not self._protected_predicates:
            return set()
        protected = set()
        for node in self.ledger.surface.nodes:
            if node.event_type not in {"tool/result", "context/pruned-tool-result", "context/summary"}:
                continue
            rendered = render_surface_node(node)
            if any(pred in rendered for pred in self._protected_predicates):
                protected.add(node.seq)
        return protected

    def _unit_is_removal_notice(self, unit: list[int]) -> bool:
        """A removal-notice summary node must never be removed itself."""
        seqs = set(unit)
        return len(seqs) == 1 and any(
            node.event_type == "context/summary" and node.data.get("kind") == "removal"
            for node in self.ledger.surface.nodes
            if node.seq in seqs
        )

    def _unit_pairs_balanced_after_removal(self, unit: list[int], removed_so_far: set[int]) -> bool:
        """A removable unit must not orphan a call or its result. A tool/call
        unit is removable only when its completed result is also being removed
        (or already removed); a result unit only when its call is removed."""
        unit_set = set(unit)
        nodes = {node.seq: node for node in self.ledger.surface.nodes}
        for seq in unit:
            node = nodes.get(seq)
            if node is None:
                continue
            if node.event_type == "tool/call":
                call_id = node.data.get("call_id", "")
                result_seq = next(
                    (
                        n.seq
                        for n in self.ledger.surface.nodes
                        if n.event_type in {"tool/result", "context/pruned-tool-result"}
                        and n.data.get("call_id") == call_id
                    ),
                    None,
                )
                if result_seq is not None and result_seq not in removed_so_far and result_seq not in unit_set:
                    return False
            elif node.event_type in {"tool/result", "context/pruned-tool-result"}:
                call_id = node.data.get("call_id", "")
                call_seq = next(
                    (
                        n.seq
                        for n in self.ledger.surface.nodes
                        if n.event_type == "tool/call" and n.data.get("call_id") == call_id
                    ),
                    None,
                )
                if call_seq is not None and call_seq not in removed_so_far and call_seq not in unit_set:
                    return False
        return True

    def _remove_old_units(self, user_message: str, request: ModelRequest, tokens: int) -> bool:
        units = self._retained_unit_plan()
        if not units:
            return False
        # Retained tail: the most recent N complete steps plus the current
        # request. The current request is the trailing user/message node.
        request_seqs = set(self._request_user_seqs(user_message))
        retained_tail = self._retained_tail_seqs()
        protected = self._protected_seq_set()
        removable = [
            unit
            for unit in units
            if not (set(unit) & retained_tail)
            and not (set(unit) & request_seqs)
            and not (set(unit) & protected)
            and not self._unit_is_removal_notice(unit)
        ]
        if not removable:
            return False
        removed_seqs = set()
        new_tokens = tokens
        target = int(self.policy.effective_input_budget * self.policy.pressure_threshold)
        # Each removed unit is its own atomic span. Remove units (oldest first)
        # until the estimated request is under the pressure target; a removed
        # unit contributes nothing back to the request. Units that would orphan
        # a call or its result are skipped.
        for unit in removable:
            if not self._unit_pairs_balanced_after_removal(unit, removed_seqs):
                continue
            unit_tokens = sum(
                self._cache.count_text(render_surface_node(node))
                for node in self.ledger.surface.nodes
                if node.seq in unit
            )
            new_tokens -= unit_tokens
            removed_seqs.update(unit)
            if new_tokens < target:
                break
        if not removed_seqs:
            return False
        # Shadow each removed unit with a bounded removal notice Surface node,
        # one atomic span per unit, so the replacement never splits a pair.
        for unit in removable:
            unit_set = set(unit)
            if not (unit_set & removed_seqs):
                continue
            # ``unit`` is in Surface order, which can differ from event-seq
            # order after a replacement. Surface endpoints drive the span;
            # source citations remain sorted for the ledger envelope.
            start_seq = unit[0]
            end_seq = unit[-1]
            unit_source_seqs = sorted(unit)
            self.ledger.append(
                "context/summary",
                {
                    "rendered": "[context-summary] older history units removed deterministically under token pressure.",
                    "removed_unit_seqs": unit_source_seqs,
                    "removed": True,
                    "kind": "removal",
                },
                surface_op={"op": "replace", "start": start_seq, "end": end_seq},
                source_event_seqs=tuple(unit_source_seqs),
            )
        return True

    def _request_user_seqs(self, user_message: str) -> set[int]:
        result = set()
        for node in self.ledger.surface.nodes:
            if node.event_type == "user/message" and node.data.get("content") == user_message:
                result.add(node.seq)
        return result

    def _retained_tail_seqs(self) -> set[int]:
        # Retained tail is the most recent N complete steps. Fall back to the
        # trailing atomic unit when the ledger has fewer steps.
        if not self.policy.retained_recent_steps:
            return set()
        units = self._retained_unit_plan()
        if not units:
            return set()
        tail = set()
        for unit in units[-int(self.policy.retained_recent_steps):]:
            tail.update(unit)
        return tail

    def _compact(self, user_message: str, trigger: str) -> bool:
        if self.summarizer is None:
            return False
        range_seqs = self._select_compaction_range(user_message)
        if not range_seqs:
            return False
        start_seq = range_seqs[0]
        end_seq = range_seqs[-1]
        source_tokens = sum(
            self._cache.count_text(render_surface_node(node))
            for node in self.ledger.surface.nodes
            if node.seq in set(range_seqs)
        )
        compaction_id = f"compaction-{self.ledger.seq}"
        self.ledger.append(
            "compaction/start",
            {
                "compaction_id": compaction_id,
                "trigger": str(trigger),
                "selected_start_seq": start_seq,
                "selected_end_seq": end_seq,
                "selected_surface_seqs": list(range_seqs),
                "source_token_count": source_tokens,
                "starting_surface_generation": self.ledger.surface_generation,
                "summarizer_provider": self.provider,
                "summarizer_model": self.model,
                "output_cap": self.policy.summary_max_tokens,
            },
        )
        raw_output = None
        try:
            raw_output = self.summarizer(
                events=[self.ledger.event_by_seq(seq) for seq in range_seqs],
                renderings={
                    node.seq: render_surface_node(node)
                    for node in self.ledger.surface.nodes
                    if node.seq in set(range_seqs)
                },
                max_tokens=self.policy.summary_max_tokens,
            )
        except Exception:
            self.ledger.append(
                "compaction/end",
                {
                    "compaction_id": compaction_id,
                    "success": False,
                    "terminal_phase": "summarized",
                    "error_code": "summary_transport_failed",
                    "error_message": "summarizer call failed",
                    "ending_surface_generation": self.ledger.surface_generation,
                },
            )
            return False
        # The summary validation renderer must cite only events inside the
        # selected range, per the Evidence Anchor rules.
        def range_renderer(seq):
            if seq not in set(range_seqs):
                return None
            return self.ledger.model_visible_event(int(seq))

        try:
            data = parse_summary_json(raw_output)
            validate_summary_schema(data)
            validate_evidence_anchors(
                data,
                renderer=range_renderer,
                max_quote_chars=self.policy.evidence_quote_max_chars,
            )
            candidate = build_summary_candidate(data)
        except ValueError:
            self.ledger.append(
                "compaction/end",
                {
                    "compaction_id": compaction_id,
                    "success": False,
                    "terminal_phase": "validated",
                    "error_code": "summary_schema_invalid",
                    "error_message": "summary failed schema or evidence validation",
                    "ending_surface_generation": self.ledger.surface_generation,
                },
            )
            return False
        rendered = render_summary(candidate)
        rendered_tokens = self._cache.count_text(rendered)
        gain_ratio = 1.0 - (rendered_tokens / source_tokens if source_tokens else 1.0)
        if gain_ratio < self.policy.minimum_compaction_gain:
            self.ledger.append(
                "compaction/end",
                {
                    "compaction_id": compaction_id,
                    "success": False,
                    "terminal_phase": "validated",
                    "error_code": "summary_no_gain",
                    "error_message": f"gain {gain_ratio:.3f} below minimum {self.policy.minimum_compaction_gain}",
                    "ending_surface_generation": self.ledger.surface_generation,
                },
            )
            return False
        self.ledger.append(
            "context/summary",
            {
                "compaction_id": compaction_id,
                "rendered": rendered,
                "selected_surface_seqs": list(range_seqs),
                "selected_source_seqs": sorted(range_seqs),
                "source_token_count": source_tokens,
                "replacement_token_count": rendered_tokens,
                "gain_ratio": round(gain_ratio, 6),
                "summary_schema_version": 1,
                "generation": self.ledger.surface_generation,
            },
            surface_op={"op": "replace", "start": start_seq, "end": end_seq},
            source_event_seqs=tuple(sorted(range_seqs)),
        )
        self.ledger.append(
            "compaction/end",
            {
                "compaction_id": compaction_id,
                "success": True,
                "terminal_phase": "committed",
                "ending_surface_generation": self.ledger.surface_generation,
                "committed_replacement_seq": self.ledger.seq - 1,
                "token_gain": source_tokens - rendered_tokens,
                "gain_ratio": round(gain_ratio, 6),
            },
        )
        self._compactions_used += 1
        return True

    def _select_compaction_range(self, user_message: str | None = None) -> list[int]:
        """Select a contiguous safe old Surface range before the retained tail.

        The range must lie completely before the retained tail, must not
        contain the current request or a protected critical fact, and must
        not split a completed call/result pair. It is returned in contiguous
        Surface order so the replacement span matches exactly.
        """
        nodes = self.ledger.surface.nodes
        if not nodes:
            return []
        retained_tail = self._retained_tail_seqs()
        excluded = set(retained_tail)
        excluded.update(self._protected_seq_set())
        if user_message is not None:
            excluded.update(self._request_user_seqs(str(user_message)))
        prefix_index = 0
        while prefix_index < len(nodes) and nodes[prefix_index].seq not in excluded:
            prefix_index += 1
        prefix = nodes[:prefix_index]
        if not prefix:
            return []
        result = [node.seq for node in prefix]
        # Never end the range after a tool/call whose result is outside it.
        while result:
            last = prefix[len(result) - 1]
            if last.event_type != "tool/call":
                break
            call_id = last.data.get("call_id", "")
            has_result = any(
                node.event_type == "tool/result" and node.data.get("call_id") == call_id
                for node in prefix
            )
            if has_result:
                break
            result.pop()
        return result

    @staticmethod
    def _sha256(text: str) -> str:
        import hashlib

        return hashlib.sha256(str(text).encode("utf-8")).hexdigest()
