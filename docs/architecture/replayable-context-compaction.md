# Replayable Context Compaction Implementation

This document links the v1 implementation to its source of truth and design
records:

- Requirements: [Replayable Context Compaction PRD](../requirements/replayable-context-compaction-prd.md)
- Design decision: [Use an event-sourced model context ADR](../adr/0002-event-sourced-model-context.md)

## Scope

The v1 capability makes an append-only `SessionLedger` the sole source of
model-visible request state and puts three increasingly capable context
policies behind a small `ContextEngine.prepare()` seam:

- **A (legacy)**: the old character-budget policy, assembled through the new
  ledger and identical request instrumentation (`LegacyContextEngine`).
- **B (token-only)**: token-aware removal of old, semantically complete
  history units under a harness input budget.
- **C (tool-pruning)**: deterministic pruning of built-in text-tool results
  before whole-unit removal.
- **D (full-compaction)**: evidence-anchored model summaries that replace a
  safe old Surface range transactionally without deleting source events.

All four conditions share the same `SessionLedger`, request assembly, request
hashing, provider/model/temperature, system prompt, tools, task/checkpoint/
pressure payload, output cap, tool-step budget, recovery mode and budget, and
verifier definitions. Only the policy fields vary.

## Package layout

```text
apricity/context/
|-- types.py        ContextEngine Protocol, PreparedContext, ContextPolicy, ModelRequest
|-- ledger.py       SessionLedger v2, Surface fold, event envelope validation, tool pairing
|-- hashing.py      canonical request SHA-256 (excludes timestamps/usage/headers)
|-- token_meter.py  TokenMeter Protocol, fallback estimator, deterministic test meter, count cache
|-- pruning.py      deterministic built-in text-tool pruning (run_process/run_shell/read_file/search/list_files)
|-- compaction.py   summary schema, Evidence Anchor validation, structured summary parsing
`-- engine.py       LegacyContextEngine and ReplayableContextEngine adapters

apricity/evaluation/context_compaction/
|-- schema.py       task schema v2, task/fixture/pressure hashes, strict rejection of unknown fields
|-- pressure.py     frozen seed loading and checkpoint preparation
|-- verifiers.py    artifact/context/trace three-layer verification, fact/contradiction oracle
|-- runner.py       balanced schedule, workspace isolation, hidden-verifier isolation, persistence, resume
|-- metrics.py      pure aggregation and failure precedence
`-- report.py       deterministic Markdown rendering
```

## Invariants implemented

- **INV-001** append-only ledger with contiguous `seq` and JSON validation;
- **INV-002/003** every dispatched request is reconstructable from durable
  events and flushed through the dispatch seq before transport;
- **INV-004** the current user request is never removed, clipped, or
  summarized;
- **INV-005** a completed tool call/result is one atomic removal unit and
  pairing is re-verified after every replacement generation;
- **INV-006** pruning and compaction shadow source Surface nodes, never
  deleting or mutating their source events;
- **INV-007** a failed validation or pre-commit failure leaves the Surface and
  generation unchanged; a persistence failure aborts before the next request;
- **INV-008** every confirmed summary claim requires at least one exact,
  bounded quote from a cited source event's model-visible rendering;
- **INV-009** compaction commits only when measured reduction reaches the
  configured minimum gain;
- **INV-010** context recovery never auto-repeats a completed side-effecting
  tool call;
- **INV-011** pruning is deterministic, idempotent, and versioned;
- **INV-012** every token estimate names its meter adapter and version, and
  fallback estimates are never reported as provider-exact.

## Deterministic gate

Run the no-cost deterministic gate (48 Runs at 4096 tokens plus the 8192-token
A/D sensitivity subset) with:

```bash
D:\\anaconda3\\envs\\learn_claude\\python.exe scripts/run_context_compaction_gate.py
```

The gate uses the deterministic test token meter and injected frozen summaries,
so it makes no paid provider calls. A paid 48-Run pilot and the formal
180-Run experiment require explicit authorization before any provider request
is dispatched.

## Known limitations

- v1 pruning supports only the five built-in text tools; unknown and non-text
  results remain unchanged.
- The deterministic gate exercises request assembly, pressure, pruning, and
  compaction transactions, but does not measure end-to-end task completion
  with a real model.
- Summary factual-error and overflow-recovery metrics are structural in the
  gate; the paid pilot is the first place provider-reported usage and real
  overflow classification are measured.

## Verification-chain fixes (2026-08-15)

Code review found the first 180-Run experiment's core verification chain was
not trustworthy; those results are invalidated. The fixes:

- **Artifact verifiers execute**: the public verifier command runs as a
  subprocess against the workspace copy. Hidden checks validate the declared
  artifact, durable event projection, or model-visible Surface according to
  the task contract. `run_pass` requires `artifact_pass AND context_pass AND
  trace_pass`; the deterministic gate records the artifact dimension as
  skipped rather than faking a pass.
- **Replay integrity replays**: `replay_integrity()` folds events into the
  Surface, rebuilds the dispatch-time flat prompt with the same versioned
  renderer (`render_dispatch_prompt` for B/C/D, `_LegacyRenderer` for A),
  recomputes `canonical_dispatch_sha256`, and compares with the recorded
  dispatch hash. Tampering a source event, a replacement span, or the recorded
  hash fails the check. `current_request_preserved` requires the current
  request to be referenced by the last dispatch's `surface_event_seqs`.
- **Hashes describe what the provider semantically receives**:
  `canonical_dispatch_sha256` covers provider/model/temperature/output cap plus
  one `user` message containing the rendered flat prompt. Provider field names
  and transport controls are normalized away; every model-visible byte remains
  covered instead of hashing an undispatched system/messages/tools abstraction.
- **Durable-before-dispatch**: `SessionLedger` accepts a `persist_path`;
  `flush_dispatch()` writes the session atomically and raises on failure.
  Both Legacy A and Replayable B/C/D flush through the dispatch seq before
  the provider request is sent (INV-003).
- **Raw assistant tool-call output is recorded**: the model's raw output is
  appended as `assistant/message` before the structured `tool/call` and
  `tool/result`, so subsequent requests reconstruct the full conversation.
- **Dirty runs are never canonical**: the manifest marks `canonical: false`
  whenever the worktree is dirty; `allow_dirty` only permits the run.
- **Hidden verifier data**: placeholder `required_fact` values removed; each
  task has one consistently structured hidden verifier file.

A fresh paid pilot and 180-Run experiment from a clean commit are required
before any performance claim can be made.
