"""ContextEngine external seam for Context Horizon v3.

Conditions A (window) and C (prune) are implemented here. Conditions E and D
will be added behind the same ``prepare`` interface; callers never learn which
policy is active from this module's signature.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any

from .ledger import SessionLedger, canonical_json, render_surface_node
from .planner import is_removal_notice, plan_units, request_user_seqs, retained_tail_seqs
from .structural import PRUNING_POLICY_NAME, PRUNING_POLICY_VERSION, prune_tool_result
from .token_budget import ContextPolicy, FallbackTokenEstimator, ModelRequest, TokenMeter


class ContextPreparationError(Exception):
    def __init__(self, message: str, *, code: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class PreparedContext:
    prompt: str
    metadata: dict[str, Any]
    canonical_request_sha256: str
    source_event_seqs: tuple[int, ...]
    surface_generation: int
    changed: bool


class ContextEngine:
    """Assemble the next model request under one context policy.

    The interface is deliberately small: callers pass the current user message
    and an optional trigger, and receive the exact prompt to dispatch. Range
    selection, pruning, and token accounting stay behind this seam.
    """

    def __init__(
        self,
        *,
        provider: str,
        model: str,
        system_prompt: str,
        tools: list[dict[str, Any]],
        memory_text: str = "",
        temperature: float = 0.0,
        max_output_tokens: int = 512,
        meter: TokenMeter | None = None,
        policy: ContextPolicy | None = None,
        ledger: SessionLedger | None = None,
        summarizer=None,
    ):
        self.provider = str(provider)
        self.model = str(model)
        self.system_prompt = str(system_prompt)
        self.tools = [dict(tool) for tool in tools]
        self.memory_text = str(memory_text)
        self.temperature = float(temperature)
        self.max_output_tokens = int(max_output_tokens)
        self.meter = meter or FallbackTokenEstimator()
        self.policy = policy or ContextPolicy()
        self.ledger = ledger or SessionLedger(
            session_id="context-horizon-session",
            workspace_root=".",
            created_at="2026-08-16T00:00:00+00:00",
        )
        self.summarizer = summarizer
        self._compactions_used = 0

    @property
    def condition_id(self) -> str:
        return self.policy.condition_id

    # ------------------------------------------------------------------
    # Request assembly
    # ------------------------------------------------------------------

    def _prompt_text(self) -> str:
        parts = [self.system_prompt, "Memory:", self.memory_text]
        parts.extend(render_surface_node(node) for node in self.ledger.surface)
        return "\n\n".join(part for part in parts if str(part).strip()).strip()

    def _build_request(self) -> ModelRequest:
        prompt = self._prompt_text()
        return ModelRequest(
            provider=self.provider,
            model=self.model,
            temperature=self.temperature,
            max_output_tokens=self.max_output_tokens,
            system_prompt="",
            messages=[{"role": "user", "content": prompt}],
            tools=list(self.tools),
        )

    def _dispatch_hash(self) -> str:
        request = self._build_request()
        payload = {
            "provider": request.provider,
            "model": request.model,
            "temperature": request.temperature,
            "max_output_tokens": request.max_output_tokens,
            "messages": request.messages,
            "tools": request.tools,
        }
        return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()

    def _pressure_active(self, request: ModelRequest, tokens: int) -> bool:
        budget = int(self.policy.effective_input_budget)
        return tokens >= int(budget * self.policy.pressure_threshold)

    # ------------------------------------------------------------------
    # Surface mutation policies
    # ------------------------------------------------------------------

    def _ensure_current_request(self, user_message: str) -> None:
        content = str(user_message)
        for node in self.ledger.surface:
            if node.event_type == "user/message" and node.data.get("content") == content:
                return
        self.ledger.append("user/message", {"content": content})

    def _prune_oversized_results(self) -> bool:
        changed = False
        for node in self.ledger.surface:
            if node.event_type != "tool/result":
                continue
            data = node.data
            if data.get("metadata", {}).get("pruned"):
                continue
            pruned = prune_tool_result(
                tool_name=str(data.get("tool_name") or data.get("name", "")),
                content=str(data.get("content", "")),
                metadata=dict(data.get("metadata") or {}),
                soft_limit=int(self.policy.tool_result_soft_limit),
                minimum_gain=float(self.policy.tool_pruning_minimum_gain),
            )
            if pruned is None:
                continue
            self.ledger.append(
                "context/pruned-tool-result",
                {
                    "tool_name": data.get("tool_name") or data.get("name", ""),
                    "call_id": data.get("call_id", ""),
                    "content": pruned.replacement,
                    "metadata": {
                        "pruned": True,
                        "original_chars": pruned.original_chars,
                        "retained_chars": pruned.retained_chars,
                        "original_sha256": pruned.original_sha256,
                        "policy": PRUNING_POLICY_NAME,
                        "policy_version": PRUNING_POLICY_VERSION,
                    },
                },
                surface_op={"op": "replace", "start": node.seq, "end": node.seq},
                source_event_seqs=(node.seq,),
            )
            changed = True
        return changed

    def _remove_old_units(self, user_message: str, request: ModelRequest, tokens: int) -> bool:
        units = plan_units(self.ledger.surface)
        if not units:
            return False
        retained_tail = retained_tail_seqs(self.ledger.surface, units, int(self.policy.retained_recent_steps))
        request_seqs = request_user_seqs(self.ledger.surface, user_message)
        removable = [
            unit
            for unit in units
            if not (set(unit) & retained_tail)
            and not (set(unit) & request_seqs)
            and not is_removal_notice(self.ledger.surface, unit)
        ]
        if not removable:
            return False
        target = int(self.policy.effective_input_budget * self.policy.pressure_threshold)
        new_tokens = tokens
        removed: list[list[int]] = []
        nodes = {node.seq: node for node in self.ledger.surface}
        for unit in removable:
            unit_tokens = sum(int(self.meter.count_text(render_surface_node(nodes[seq]))) for seq in unit if seq in nodes)
            new_tokens -= unit_tokens
            removed.append(unit)
            if new_tokens < target:
                break
        if not removed:
            return False
        for unit in removed:
            self.ledger.append(
                "context/summary",
                {
                    "rendered": "[context-summary] older history units removed deterministically under token pressure.",
                    "kind": "removal",
                    "removed_unit_seqs": sorted(unit),
                },
                surface_op={"op": "replace", "start": unit[0], "end": unit[-1]},
                source_event_seqs=tuple(sorted(unit)),
            )
        return True

    # ------------------------------------------------------------------
    # External seam
    # ------------------------------------------------------------------

    def prepare(self, user_message: str, *, trigger: str = "pressure") -> PreparedContext:
        user_message = str(user_message)
        self._ensure_current_request(user_message)
        request = self._build_request()
        tokens = int(self.meter.count_request(request))
        pressure = self._pressure_active(request, tokens)
        initial_pressure = pressure
        changed = False

        if pressure and self.policy.tool_pruning:
            if self._prune_oversized_results():
                request = self._build_request()
                tokens = int(self.meter.count_request(request))
                pressure = self._pressure_active(request, tokens)
                changed = True

        guard = 0
        while pressure and guard < 50:
            guard += 1
            if not self._remove_old_units(user_message, request, tokens):
                break
            request = self._build_request()
            tokens = int(self.meter.count_request(request))
            pressure = self._pressure_active(request, tokens)
            changed = True

        self.ledger.append(
            "context/pressure",
            {
                "pressure": bool(pressure),
                "initial_pressure": bool(initial_pressure),
                "estimated_input_tokens": tokens,
                "effective_input_budget": int(self.policy.effective_input_budget),
                "pressure_threshold": float(self.policy.pressure_threshold),
                "trigger": str(trigger),
                "changed": bool(changed),
            },
        )
        if pressure:
            raise ContextPreparationError(
                f"no_safe_range: retained units alone exceed the effective input budget ({tokens} tokens)",
                code="no_safe_range",
            )

        surface_seqs = self.ledger.surface_event_seqs()
        digest = self._dispatch_hash()
        self.ledger.append(
            "request/dispatch",
            {
                "canonical_request_sha256": digest,
                "surface_generation": self.ledger.surface_generation,
                "surface_event_seqs": list(surface_seqs),
                "estimated_input_tokens": int(self.meter.count_request(self._build_request())),
                "token_meter": {
                    "name": self.meter.name,
                    "version": self.meter.version,
                    "exact": bool(self.meter.exact),
                },
                "trigger": str(trigger),
            },
        )
        self.ledger.flush_dispatch()

        return PreparedContext(
            prompt=self._prompt_text(),
            metadata={
                "canonical_request_sha256": digest,
                "source_event_seqs": list(surface_seqs),
                "surface_generation": self.ledger.surface_generation,
                "condition": self.condition_id,
                "condition_label": self.policy.condition_label,
                "estimated_input_tokens": tokens,
                "pressure": bool(initial_pressure),
                "pressure_threshold": self.policy.pressure_threshold,
                "effective_input_budget": int(self.policy.effective_input_budget),
                "token_meter": {
                    "name": self.meter.name,
                    "version": self.meter.version,
                    "exact": bool(self.meter.exact),
                },
                "tool_pairs_balanced": self.ledger.pairs_balanced(),
            },
            canonical_request_sha256=digest,
            source_event_seqs=tuple(surface_seqs),
            surface_generation=self.ledger.surface_generation,
            changed=changed,
        )


__all__ = ["ContextEngine", "ContextPreparationError", "PreparedContext"]
