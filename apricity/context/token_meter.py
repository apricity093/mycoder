"""TokenMeter seam and adapters.

A TokenMeter measures the tokens of a full canonical model request and of
individual texts. Every estimate MUST name the adapter and version, and
fallback estimates MUST NOT be reported as provider-exact counts. The count
cache keys on the canonical rendering hash plus the meter identity, and a
meter version change invalidates prior counts.
"""

from __future__ import annotations

import hashlib
from typing import Protocol

from .hashing import canonical_request_sha256
from .ledger import canonical_json
from .types import ModelRequest

FALLBACK_ESTIMATOR_NAME = "fallback-estimator"
FALLBACK_ESTIMATOR_VERSION = "v1"
FALLBACK_CHARS_PER_TOKEN = 3.4
FALLBACK_TOKEN_PER_WORD = 1.25


class TokenMeter(Protocol):
    name: str
    version: str
    exact: bool

    def count_request(self, request: ModelRequest) -> int: ...

    def count_text(self, text: str) -> int: ...


class FallbackTokenEstimator:
    """Named deterministic fallback estimator.

    Used only when a configured model tokenizer is unavailable. It is
    explicitly labeled ``exact=False`` so consumers can distinguish a fallback
    estimate from provider-exact counts.
    """

    name = FALLBACK_ESTIMATOR_NAME
    version = FALLBACK_ESTIMATOR_VERSION
    exact = False

    def count_request(self, request: ModelRequest) -> int:
        total = self.count_text(request.system_prompt)
        for message in request.messages:
            total += self.count_text(canonical_json(message))
        for tool in request.tools:
            total += self.count_text(canonical_json(tool))
        return total

    def count_text(self, text: str) -> int:
        text = str(text)
        if not text:
            return 0
        cjk_count = sum(1 for char in text if "\u4e00" <= char <= "\u9fff")
        ascii_chars = len(text) - cjk_count
        word_count = max(1, len(text.split()))
        ascii_tokens = ascii_chars / FALLBACK_CHARS_PER_TOKEN
        return max(1, int(round(cjk_count + ascii_tokens + word_count * FALLBACK_TOKEN_PER_WORD)))


class DeterministicTestMeter:
    """Deterministic meter for tests: one token per two code points."""

    name = "deterministic-test"
    version = "v1"
    exact = True

    def count_request(self, request: ModelRequest) -> int:
        total = self.count_text(request.system_prompt)
        for message in request.messages:
            total += self.count_text(canonical_json(message))
        for tool in request.tools:
            total += self.count_text(canonical_json(tool))
        return total

    def count_text(self, text: str) -> int:
        return max(1, (len(str(text)) + 1) // 2)


class CountingCache:
    """Per-event token count cache.

    The cache MUST NOT change correctness: a hash miss recalculates, and a
    token-meter version change invalidates prior counts.
    """

    def __init__(self, meter: TokenMeter | None = None):
        self.meter = meter
        self._counts: dict[str, int] = {}

    def bind(self, meter: TokenMeter) -> "CountingCache":
        return CountingCache(meter=meter)

    def _key(self, text: str) -> str:
        meter = self.meter
        if meter is None:
            return "unbound"
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        return f"{digest}:{meter.name}:{meter.version}"
    def count_text(self, text: str) -> int:
        if self.meter is None:
            raise ValueError("counting cache is not bound to a meter")
        key = self._key(text)
        cached = self._counts.get(key)
        if cached is not None:
            return cached
        value = int(self.meter.count_text(str(text)))
        self._counts[key] = value
        return value

    def count_request(self, request: ModelRequest) -> int:
        if self.meter is None:
            raise ValueError("counting cache is not bound to a meter")
        return int(self.meter.count_request(request))

    def clear(self) -> None:
        self._counts.clear()


def count_request_tokens(request: ModelRequest, meter: TokenMeter) -> int:
    return int(meter.count_request(request))
