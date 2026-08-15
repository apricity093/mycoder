"""Public surface of the replayable context package.

Only these names are exported:

- ``ContextEngine``
- ``LegacyContextEngine``
- ``ReplayableContextEngine``
- ``ContextPolicy``
- ``PreparedContext``
- ``ContextPreparationError``
- ``SessionLedger``

Pressure detection, range selection, summary validation, and canonical
hashing are implementation details and MUST NOT become public interfaces
merely for tests.
"""

from __future__ import annotations

from .compaction import (
    Claim,
    EvidenceAnchor,
    SummaryCandidate,
    build_summary_candidate,
    parse_summary_json,
    render_summary,
    validate_evidence_anchors,
    validate_summary_schema,
)
from .engine import LegacyContextEngine, ReplayableContextEngine
from .ledger import (
    EVENT_SCHEMA_VERSION,
    SESSION_SCHEMA_VERSION,
    SURFACE_PRODUCING_TYPES,
    APPEND_FORM,
    SessionLedger,
    render_surface_node,
)
from .pruning import PRUNING_POLICY_NAME, PRUNING_POLICY_VERSION, prune_tool_result
from .token_meter import (
    CountingCache,
    DeterministicTestMeter,
    FallbackTokenEstimator,
)
from .types import (
    ContextEngine,
    ContextPolicy,
    ContextPreparationError,
    ContextTrigger,
    ModelRequest,
    PreparedContext,
)

__all__ = [
    "APPEND_FORM",
    "Claim",
    "ContextEngine",
    "ContextPolicy",
    "ContextPreparationError",
    "ContextTrigger",
    "CountingCache",
    "DeterministicTestMeter",
    "EVENT_SCHEMA_VERSION",
    "EvidenceAnchor",
    "FallbackTokenEstimator",
    "LegacyContextEngine",
    "ModelRequest",
    "PRUNING_POLICY_NAME",
    "PRUNING_POLICY_VERSION",
    "PreparedContext",
    "ReplayableContextEngine",
    "SESSION_SCHEMA_VERSION",
    "SURFACE_PRODUCING_TYPES",
    "SessionLedger",
    "SummaryCandidate",
    "build_summary_candidate",
    "parse_summary_json",
    "prune_tool_result",
    "render_summary",
    "render_surface_node",
    "validate_evidence_anchors",
    "validate_summary_schema",
]
