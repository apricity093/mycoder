"""Shared types for the replayable context seam.

This module is the small, stable interface surface of ``apricity.context``.
Everything a caller must know to use the context engines lives here: the
``PreparedContext`` result, the ``ContextTrigger`` literal, the engine
Protocol, and the typed preparation failure. Pressure detection, range
selection, canonical hashing, pruning, and compaction remain implementation
details and are NOT part of this interface.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Protocol


ContextTrigger = Literal["pressure", "context-overflow"]


@dataclass(frozen=True)
class PreparedContext:
    prompt: str
    metadata: dict
    canonical_request_sha256: str
    source_event_seqs: tuple[int, ...]
    surface_generation: int
    changed: bool


class ContextEngine(Protocol):
    def prepare(
        self,
        user_message: str,
        *,
        trigger: ContextTrigger = "pressure",
    ) -> PreparedContext:
        ...


class ContextPreparationError(RuntimeError):
    """A typed preparation failure with a stable error code.

    Raised only when preparation cannot produce a request and the Surface
    was left unchanged. Callers must treat this as a stop condition, never
    as a cue to re-render a partially mutated Surface.
    """

    def __init__(self, message: str, *, code: str):
        super().__init__(str(message))
        self.code = str(code)


@dataclass(frozen=True)
class ContextPolicy:
    """Frozen policy knobs shared by the replayable engines.

    Validation rejects non-positive budgets, thresholds outside ``(0, 1]``,
    negative limits, and inconsistent configurations such as history
    compaction without token budgeting.
    """

    token_budgeting: bool = False
    tool_pruning: bool = False
    history_compaction: bool = False
    effective_input_budget: int = 0
    reserved_output_tokens: int = 512
    pressure_threshold: float = 0.85
    tool_result_soft_limit: int = 800
    summary_max_tokens: int = 384
    retained_recent_steps: int = 2
    max_compactions_per_run: int = 2
    minimum_compaction_gain: float = 0.15
    tool_pruning_minimum_gain: float = 0.2
    evidence_quote_max_chars: int = 320

    def __post_init__(self) -> None:
        if self.token_budgeting and int(self.effective_input_budget) <= 0:
            raise ValueError("effective_input_budget must be positive when token budgeting is enabled")
        if not (0.0 < float(self.pressure_threshold) <= 1.0):
            raise ValueError("pressure_threshold must be in (0, 1]")
        if int(self.reserved_output_tokens) < 0:
            raise ValueError("reserved_output_tokens must not be negative")
        if int(self.tool_result_soft_limit) < 0:
            raise ValueError("tool_result_soft_limit must not be negative")
        if int(self.summary_max_tokens) <= 0:
            raise ValueError("summary_max_tokens must be positive")
        if int(self.retained_recent_steps) < 0:
            raise ValueError("retained_recent_steps must not be negative")
        if int(self.max_compactions_per_run) < 0:
            raise ValueError("max_compactions_per_run must not be negative")
        if not (0.0 <= float(self.minimum_compaction_gain) < 1.0):
            raise ValueError("minimum_compaction_gain must be in [0, 1)")
        if not (0.0 <= float(self.tool_pruning_minimum_gain) < 1.0):
            raise ValueError("tool_pruning_minimum_gain must be in [0, 1)")
        if self.history_compaction and not self.token_budgeting:
            raise ValueError("history_compaction requires token_budgeting")
        if self.tool_pruning and not self.token_budgeting:
            raise ValueError("tool_pruning requires token_budgeting")
        if int(self.evidence_quote_max_chars) <= 0:
            raise ValueError("evidence_quote_max_chars must be positive")

    @property
    def condition_id(self) -> str:
        if not self.token_budgeting:
            return "A"
        if not self.tool_pruning and not self.history_compaction:
            return "B"
        if not self.history_compaction:
            return "C"
        return "D"

    @property
    def condition_label(self) -> str:
        return {
            "A": "legacy",
            "B": "token-only",
            "C": "tool-pruning",
            "D": "full-compaction",
        }[self.condition_id]

    def as_dict(self) -> dict:
        return {
            "token_budgeting": self.token_budgeting,
            "tool_pruning": self.tool_pruning,
            "history_compaction": self.history_compaction,
            "effective_input_budget": self.effective_input_budget,
            "reserved_output_tokens": self.reserved_output_tokens,
            "pressure_threshold": self.pressure_threshold,
            "tool_result_soft_limit": self.tool_result_soft_limit,
            "summary_max_tokens": self.summary_max_tokens,
            "retained_recent_steps": self.retained_recent_steps,
            "max_compactions_per_run": self.max_compactions_per_run,
            "minimum_compaction_gain": self.minimum_compaction_gain,
            "tool_pruning_minimum_gain": self.tool_pruning_minimum_gain,
            "evidence_quote_max_chars": self.evidence_quote_max_chars,
            "condition_id": self.condition_id,
            "condition_label": self.condition_label,
        }


@dataclass(frozen=True)
class ModelRequest:
    """The canonical redacted request that will actually be dispatched.

    ``system_prompt`` is the rendered system text, ``messages`` are the
    ordered conversational messages (Surface), and ``tools`` are the ordered
    tool definitions. These are exactly the inputs used by canonical hashing.
    """

    provider: str
    model: str
    temperature: float
    max_output_tokens: int
    system_prompt: str
    messages: tuple[dict, ...] = field(default_factory=tuple)
    tools: tuple[dict, ...] = field(default_factory=tuple)
