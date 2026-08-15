---
status: accepted
---

# Use an event-sourced model context

Apricity will make an append-only Session Event ledger the sole source of model-visible request state, and will derive the current Surface, request headers, dynamic request context, pruning replacements, and history summaries from that ledger. This was chosen over retaining mutable history plus experiment-only traces because replayable compaction requires source retention and atomic replacement, and all A/B/C/D conditions must share the same runtime representation to keep the experiment attributable to context policy rather than storage differences. Abandoned pre-v2 sessions will not be migrated.

## Consequences

Every dispatched request must be reconstructable from durable events, source events cannot be edited or deleted, and a persistence failure after a context replacement must stop the Run before another model request. The implementation cost is higher than a trace sidecar, but replay, audit, crash diagnosis, and policy comparison become enforceable invariants rather than report conventions.
