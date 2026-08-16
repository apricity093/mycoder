"""Public surface of the lean Context Horizon v3 package.

Only the names below are exported. Range selection, pruning internals, and
canonical hashing stay implementation details behind ``ContextEngine``.
"""

from __future__ import annotations

from .engine import ContextEngine, ContextPreparationError, PreparedContext
from .ledger import (
    EVENT_SCHEMA_VERSION,
    SESSION_SCHEMA_VERSION,
    SURFACE_PRODUCING_TYPES,
    LedgerError,
    SessionLedger,
    canonical_json,
    render_surface_node,
    sha256_text,
)
from .structural import PRUNING_POLICY_NAME, PRUNING_POLICY_VERSION, PrunedResult, prune_tool_result
from .token_budget import (
    FALLBACK_ESTIMATOR_NAME,
    FALLBACK_ESTIMATOR_VERSION,
    ContextPolicy,
    DeterministicTestMeter,
    FallbackTokenEstimator,
    ModelRequest,
    TokenMeter,
)

__all__ = [
    "ContextEngine",
    "ContextPolicy",
    "ContextPreparationError",
    "DeterministicTestMeter",
    "EVENT_SCHEMA_VERSION",
    "FALLBACK_ESTIMATOR_NAME",
    "FALLBACK_ESTIMATOR_VERSION",
    "FallbackTokenEstimator",
    "LedgerError",
    "ModelRequest",
    "PRUNING_POLICY_NAME",
    "PRUNING_POLICY_VERSION",
    "PreparedContext",
    "PrunedResult",
    "SESSION_SCHEMA_VERSION",
    "SURFACE_PRODUCING_TYPES",
    "SessionLedger",
    "TokenMeter",
    "canonical_json",
    "prune_tool_result",
    "render_surface_node",
    "sha256_text",
]
