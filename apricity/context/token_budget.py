"""Token accounting for Context Horizon v3.

Every count names its meter and version. Provider-reported usage is reported
separately by the evaluation runner; the control meter is never described as
provider-exact unless the provider itself supplied the number.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from .ledger import canonical_json

FALLBACK_ESTIMATOR_NAME = "fallback-estimator"
FALLBACK_ESTIMATOR_VERSION = "v1"
FALLBACK_CHARS_PER_TOKEN = 3.4
FALLBACK_TOKEN_PER_WORD = 1.25


@dataclass(frozen=True)
class ModelRequest:
    provider: str
    model: str
    temperature: float
    max_output_tokens: int
    system_prompt: str
    messages: list[dict]
    tools: list[dict]


class TokenMeter(Protocol):
    name: str
    version: str
    exact: bool

    def count_request(self, request: ModelRequest) -> int: ...

    def count_text(self, text: str) -> int: ...


class FallbackTokenEstimator:
    """Named deterministic fallback. ``exact=False`` by construction."""

    name = FALLBACK_ESTIMATOR_NAME
    version = FALLBACK_ESTIMATOR_VERSION
    exact = False

    def count_request(self, request: ModelRequest) -> int:
        total = self.count_text(request.system_prompt)
        total += sum(self.count_text(canonical_json(message)) for message in request.messages)
        total += sum(self.count_text(canonical_json(tool)) for tool in request.tools)
        return total

    def count_text(self, text: str) -> int:
        text = str(text)
        if not text:
            return 0
        cjk = sum(1 for char in text if "\u4e00" <= char <= "\u9fff")
        ascii_chars = len(text) - cjk
        words = max(1, len(text.split()))
        return max(1, int(round(cjk + ascii_chars / FALLBACK_CHARS_PER_TOKEN + words * FALLBACK_TOKEN_PER_WORD)))


class DeterministicTestMeter:
    """Deterministic meter for scripted-model runs: one token per two code points."""

    name = "deterministic-test"
    version = "v1"
    exact = True

    def count_request(self, request: ModelRequest) -> int:
        total = self.count_text(request.system_prompt)
        total += sum(self.count_text(canonical_json(message)) for message in request.messages)
        total += sum(self.count_text(canonical_json(tool)) for tool in request.tools)
        return total

    def count_text(self, text: str) -> int:
        return max(1, (len(str(text)) + 1) // 2)


@dataclass(frozen=True)
class ContextPolicy:
    """Policy flags and budget knobs. Hidden verifier data is never a field here."""

    condition_id: str = "A"
    condition_label: str = "window"
    effective_input_budget: int = 4096
    reserved_output_tokens: int = 512
    pressure_threshold: float = 0.85
    tool_pruning: bool = False
    history_compaction: bool = False
    structural_compaction: bool = False
    chunked_llm_compaction: bool = False
    tool_result_soft_limit: int = 1600
    tool_pruning_minimum_gain: float = 0.10
    retained_recent_steps: int = 2
    minimum_compaction_gain: float = 0.15
    max_compactions_per_run: int = 2
    summary_max_tokens: int = 384
    evidence_quote_max_chars: int = 240

    def __post_init__(self) -> None:
        if self.effective_input_budget <= 0:
            raise ValueError("effective_input_budget must be positive")
        if self.reserved_output_tokens < 0:
            raise ValueError("reserved_output_tokens must be non-negative")
        if not 0.0 < self.pressure_threshold <= 1.0:
            raise ValueError("pressure_threshold must be in (0, 1]")
        if self.tool_result_soft_limit <= 0:
            raise ValueError("tool_result_soft_limit must be positive")
        if not 0.0 <= self.tool_pruning_minimum_gain < 1.0:
            raise ValueError("tool_pruning_minimum_gain must be in [0, 1)")
        if self.retained_recent_steps < 0:
            raise ValueError("retained_recent_steps must be non-negative")
        if not 0.0 < self.minimum_compaction_gain < 1.0:
            raise ValueError("minimum_compaction_gain must be in (0, 1)")
        if self.max_compactions_per_run < 0:
            raise ValueError("max_compactions_per_run must be non-negative")
        if self.summary_max_tokens <= 0:
            raise ValueError("summary_max_tokens must be positive")
        if self.evidence_quote_max_chars <= 0:
            raise ValueError("evidence_quote_max_chars must be positive")
        if self.history_compaction and not self.tool_pruning:
            raise ValueError("history_compaction requires tool_pruning")
        modes = int(bool(self.structural_compaction)) + int(bool(self.chunked_llm_compaction))
        if self.history_compaction and modes != 1:
            raise ValueError("history_compaction requires exactly one of structural_compaction or chunked_llm_compaction")
        if not self.history_compaction and modes:
            raise ValueError("compaction mode flags require history_compaction")
