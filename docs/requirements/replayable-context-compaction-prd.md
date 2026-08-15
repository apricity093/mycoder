# Replayable Context Compaction PRD

## 0. Document control

- Status: frozen v1 scope, ready for implementation planning
- Date: 2026-08-14
- Project: Apricity
- Requirement owner: repository owner
- Target capability: replayable, token-aware context compaction for repository coding-agent Runs
- Primary implementation language: Python 3.10+
- Default development environment: `learn_claude`
- Normative language: `MUST`, `MUST NOT`, `SHOULD`, and `MAY` are requirements keywords

This document is the implementation source of truth for the v1 capability and its evaluation. An implementing model MUST read this document completely before editing code. When this document conflicts with an exploratory note, chat transcript, or existing benchmark prose, this document wins. Existing safety behavior remains authoritative unless this document explicitly replaces it.

## 1. Executive summary

Apricity currently assembles prompts with fixed character budgets and section-level clipping. The v1 change replaces the session's mutable history as the source of model context with an append-only `SessionLedger`, adds a small `ContextEngine.prepare()` seam, and implements three increasingly capable context policies behind that seam:

1. token-aware removal of old, semantically complete history units;
2. deterministic pruning of built-in text-tool results;
3. model-generated, evidence-anchored summaries that replace old surface ranges without deleting their source events.

All model-visible request inputs MUST be reconstructable from the ledger. The capability is evaluated with four paired conditions: legacy character policy, token-only, tool pruning, and full compaction. The evaluation uses eight controlled tasks and four realistic tasks, hidden verifiers, a 4096-token main experiment, and an 8192-token sensitivity experiment.

The intended claim is a system-level quality/cost trade-off. The project MUST NOT claim that compaction improves model intelligence or produces gains at equal inference compute.

## 2. Current state and motivation

### 2.1 Existing behavior

The current `ContextManager`:

- uses a fixed total character budget;
- allocates character budgets to prefix, memory, relevant memory, and history;
- clips and compresses sections in a fixed order;
- protects the current request from clipping;
- tightens dynamic section budgets after a context-overflow recovery decision;
- writes prompt metadata for reports and traces.

The current runtime stores conversation history in `session["history"]`. A tool history item combines tool name, arguments, and result content. This structure is useful for a small agent, but it is not an append-only model of what the provider saw and cannot express a replayable range replacement.

### 2.2 Problems to solve

The v1 capability addresses these specific failures:

- character counts do not reliably predict model tokens, especially for mixed Chinese, English, code, and JSON;
- an oversized tool result can crowd out more valuable task evidence;
- clipping does not express semantic units or durable provenance;
- old and new facts can be merged into an internally contradictory summary;
- a failed compaction can leave no durable explanation of what was attempted;
- a request cannot be reconstructed when system prompt, tool schemas, memory, checkpoint, or visible history changes;
- an evaluation that only checks the final file cannot detect lost facts, orphan tool interactions, or replay mismatches.

### 2.3 Research hypothesis

Under the same provider, model, task, tool set, output-token allowance, and harness-enforced input budget, token-aware budgeting plus deterministic text-tool pruning plus replayable history summarization will improve long-context task success or materially reduce unrecovered context overflows without violating safety invariants and with explicitly measured additional cost.

## 3. Goals and non-goals

### 3.1 Goals

The implementation MUST:

- make an append-only ledger the sole source of model-visible request state;
- reconstruct every dispatched model request from durable ledger events;
- use a model-aware token meter when available and a named fallback estimator otherwise;
- preserve the current user request and a configurable recent tail;
- treat tool calls and results as paired semantic units;
- prune supported text-tool results deterministically before summarizing history;
- summarize only a safe, old surface range and retain exact provenance;
- require evidence anchors for every confirmed summary claim;
- commit surface replacements transactionally;
- integrate confirmed provider overflow with the existing typed recovery decision;
- expose enough structured evidence to evaluate correctness, replay, safety, cost, and latency;
- retain an honest legacy policy condition on the new ledger for causal comparison.

### 3.2 Non-goals

The v1 implementation MUST NOT:

- preserve or migrate abandoned pre-v2 session files;
- reproduce DeepSeek Harness's complete plugin architecture;
- redesign the long-term memory retrieval algorithm;
- add a vector database or embeddings;
- redesign all tool execution as a generic plugin pipeline;
- compress arbitrary binary, image, audio, or unknown tool results;
- use a second LLM as the authoritative factual judge for primary metrics;
- claim SWE-bench, production reliability, or universal large-context performance;
- silently alter tasks, pressure payloads, thresholds, or verifiers after formal experiment hashes are frozen.

## 4. Canonical language

The implementation and documentation MUST use these terms consistently.

### 4.1 Run

One bounded attempt to satisfy one benchmark or user request, including model requests, tool calls, recovery actions, compactions, and one terminal outcome.

### 4.2 Session Event

One immutable, JSON-serializable fact appended to a `SessionLedger`. A Session Event has a contiguous `seq`, a type, a timestamp, and typed data. It is never edited or deleted after acceptance.

### 4.3 Surface

The ordered projection of Session Events that supplies conversational messages to model requests. A replacement can shadow older Surface nodes without deleting their source events.

### 4.4 Request Header

The durable snapshot of model configuration, rendered system prompt, and assembled tool schemas used by model requests until superseded by a later header.

### 4.5 Request Context

The durable snapshot of dynamic model-visible context outside conversational history, including memory and checkpoint text, used until superseded.

### 4.6 Pressure

A pre-request condition in which the estimated request input reaches the configured fraction of the effective input budget. Pressure is not a provider error.

### 4.7 Context Overflow

A typed provider failure confirming that a request exceeded the provider's context capacity. It is distinct from locally predicted Pressure.

### 4.8 Tool Result Pruning

A deterministic Surface replacement that reduces one supported text-tool result while preserving its tool identity, call pairing, relevant structural evidence, and provenance.

### 4.9 History Compaction

A transaction that summarizes a safe old Surface range and replaces that range with one evidence-anchored summary Surface node while retaining all source events in the ledger.

### 4.10 Evidence Anchor

An exact, bounded quote from a cited source event that grounds one confirmed summary claim. A source event ID without a valid quote is not an Evidence Anchor.

### 4.11 Replay Integrity

The property that a request reconstructed from durable ledger state has the same canonical SHA-256 as the request actually dispatched to the provider.

### 4.12 Compaction Progress

A committed change that advances `surface_generation` and reduces the measured tokens of the affected request state by at least the configured minimum gain.

## 5. Scope decisions frozen for v1

The following decisions are final for v1:

- old sessions are abandoned; no legacy-session migration is required;
- every experiment condition uses the new `SessionLedger` and identical request assembly;
- condition A preserves the old character-budget policy, not the old storage implementation;
- condition D uses the same provider and model as the main Agent with temperature zero;
- all summary calls count toward tokens, latency, failures, and total system cost;
- every confirmed claim requires at least one valid Evidence Anchor;
- controlled tasks use frozen, pre-seeded event logs;
- realistic tasks fork from a frozen checkpoint after a common investigation phase;
- hidden verifiers remain outside the Agent-visible workspace;
- main-condition Runs are interleaved in balanced blocks;
- v1 pruning supports only built-in text tools;
- the main experiment disables durable memory and relevant-memory retrieval to isolate context policy;
- runtime support for memory remains, and memory/context snapshots still participate in request replay outside the main experiment.

## 6. System invariants

These are hard correctness gates, not optimization targets.

### INV-001 Append-only ledger

Accepted Session Events MUST be immutable. `seq` MUST equal the zero-based append position and MUST remain contiguous.

### INV-002 Model-visible reconstruction

Every byte of semantic request content sent to a model MUST be derivable from durable Session Events plus versioned canonical rendering code. Provider authentication headers and transport-only metadata are excluded.

### INV-003 Durable-before-dispatch

The `request/dispatch` event and all events it references MUST be durably flushed before the provider request is sent.

### INV-004 Current request preservation

The current user request MUST NOT be removed, clipped, summarized, or rewritten by context policy.

### INV-005 Tool pairing

A current Surface MUST NOT contain an orphan `tool/result`, a `tool/call` whose completed result was accidentally removed, or a history-compaction boundary that splits a completed call/result pair.

### INV-006 Source retention

Pruning and compaction MUST shadow source Surface nodes, not delete or mutate their source events.

### INV-007 Atomic replacement

A failed validation or pre-commit failure MUST leave the Surface and `surface_generation` unchanged. A persistence failure after an in-memory commit MUST terminate the Run before another model request.

### INV-008 Evidence-grounded confirmation

Every summary claim marked `confirmed` MUST cite existing source events and at least one exact quote found in the cited model-visible source rendering.

### INV-009 Measurable progress

History Compaction MUST NOT commit unless measured reduction for the selected range is at least `minimum_compaction_gain`, default 15 percent.

### INV-010 No duplicate side effects

Context recovery MUST NOT automatically repeat a successfully completed side-effecting tool call. An unmatched durable call on resume MUST be treated as an uncertain outcome, not silently re-executed.

### INV-011 Deterministic pruning

The same tool result, metadata, pruning policy version, and token budget MUST produce the same replacement content and hash.

### INV-012 Explicit estimator identity

Every estimated token count MUST name the token-meter adapter and version. Fallback estimates MUST NOT be reported as provider-exact counts.

## 7. High-level architecture

### 7.1 External seam

`AgentLoop` MUST depend on one context interface:

```python
from dataclasses import dataclass
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
```

The interface includes these behavioral rules:

- `prepare()` MAY append Pressure, pruning, compaction, header, context, and dispatch events;
- on success, the returned prompt and hash describe exactly the next provider request;
- on a normal no-op, `changed` is false;
- an expected inability to compact safely is represented by typed metadata or a typed `ContextPreparationError`, never by a partially mutated Surface;
- callers MUST NOT separately clip or summarize the returned prompt.

### 7.2 Adapters

Two adapters MUST satisfy the seam:

- `LegacyContextEngine`: new ledger and request assembly, legacy character-budget policy;
- `ReplayableContextEngine`: configurable token budgeting, pruning, and compaction.

The AgentLoop MUST NOT branch on experiment condition, pruning status, summary validity, token threshold, or selected Surface range.

### 7.3 Recommended package shape

```text
apricity/context/
|-- __init__.py
|-- types.py
|-- engine.py
|-- legacy.py
|-- ledger.py
|-- token_meter.py
|-- pruning.py
|-- compaction.py
`-- summarizer.py
```

Only these names SHOULD be exported from `apricity.context`:

- `ContextEngine`
- `LegacyContextEngine`
- `ReplayableContextEngine`
- `ContextPolicy`
- `PreparedContext`
- `ContextPreparationError`
- `SessionLedger`

Pressure detection, range selection, summary validation, and canonical hashing are implementation details and MUST NOT become public interfaces merely for tests.

## 8. SessionLedger model

### 8.1 Session schema

New sessions MUST use schema version 2:

```json
{
  "schema_version": 2,
  "id": "session-id",
  "created_at": "RFC-3339 timestamp",
  "workspace_root": "absolute path at runtime",
  "events": [],
  "memory": {},
  "checkpoints": {},
  "runtime_identity": {},
  "resume_state": {}
}
```

`events` is canonical for model-visible state. `memory` and checkpoint structures MAY remain optimized runtime state, but any rendered value sent to a model MUST first appear in a `request/context` snapshot.

### 8.2 Event envelope

```json
{
  "schema_version": 1,
  "seq": 17,
  "time": "RFC-3339 timestamp",
  "type": "tool/result",
  "data": {},
  "surface_op": "append",
  "source_event_seqs": [15, 16]
}
```

Rules:

- `surface_op` is present only for Surface-producing event types;
- append form is the string `append`;
- replacement form is `{ "op": "replace", "start": 3, "end": 11 }` where start/end identify current Surface nodes by seq and the interval is interpreted by current Surface position;
- `source_event_seqs` is sorted by source Surface order, contains no duplicates, and cites the complete known source set;
- all nested data MUST be lossless JSON after redaction;
- event acceptance MUST deep-copy or freeze borrowed data.

### 8.3 Required event types

Conversation and tools:

- `turn/start`, `turn/end`
- `step/start`, `step/end`
- `user/message`
- `assistant/message`
- `tool/call`
- `tool/result`

Request reconstruction:

- `request/header`
- `request/context`
- `request/dispatch`

Context policy:

- `context/pressure`
- `context/pruned-tool-result`
- `compaction/start`
- `context/summary`
- `compaction/end`

Recovery and diagnostics MAY remain trace-only if they are not model-visible and cannot change request reconstruction. Any fact that changes the Surface or request envelope MUST be a Session Event, not only a trace record.

### 8.4 Surface-producing types

The following types produce ordered model history:

- `user/message`
- `assistant/message`
- `tool/call`
- `tool/result`
- `context/pruned-tool-result`
- `context/summary`

`context/pruned-tool-result` MUST replace exactly one current `tool/result` or earlier pruned replacement for the same `call_id`.

`context/summary` MUST replace a safe contiguous range by Surface position. Its sources MUST include every shadowed Surface node.

### 8.5 Surface replay

`SessionLedger.surface()` MUST fold events in seq order:

1. append adds one Surface node;
2. replace validates that both declared endpoints are current nodes;
3. replace removes the current positional span inclusively;
4. replace inserts the replacement node at the removed span's first position;
5. each successful replacement increments `surface_generation`;
6. invalid replacement MUST reject event acceptance.

The Surface node sequence MAY become numerically non-monotonic after replacements. Code MUST use Surface position, not numeric seq ranges, to select replacement spans.

## 9. Request reconstruction

### 9.1 Request header

`request/header` MUST contain a complete snapshot, not a delta:

```json
{
  "provider": "deepseek",
  "model": "deepseek-v4-flash",
  "temperature": 0,
  "max_output_tokens": 512,
  "system_prompt": "rendered text",
  "tools": [],
  "system_prompt_sha256": "...",
  "tool_schema_sha256": "..."
}
```

A new header MUST be appended on the first request and whenever any semantic field changes. Otherwise `request/dispatch` references the latest header seq.

### 9.2 Request context

`request/context` MUST contain the complete current dynamic snapshot:

```json
{
  "memory": "rendered memory text or empty",
  "checkpoint": "rendered checkpoint text or empty",
  "workspace_context": "dynamic injected context or empty",
  "content_sha256": "..."
}
```

A new context event is appended only when the canonical snapshot changes. In the primary experiment, memory and relevant-memory retrieval are disabled, but an explicit empty snapshot is still reconstructable.

### 9.3 Dispatch record

Immediately before each provider call, append:

```json
{
  "header_seq": 4,
  "context_seq": 5,
  "surface_generation": 2,
  "surface_event_seqs": [0, 1, 7, 12, 19],
  "canonical_request_sha256": "...",
  "token_meter": {
    "name": "...",
    "version": "...",
    "exact": true
  },
  "estimated_input_tokens": 3910,
  "trigger": "pressure"
}
```

The ledger MUST flush through the dispatch seq before transport begins. Provider-reported usage is recorded later with the assistant result or Run report and MUST NOT change the pre-dispatch request hash.

### 9.4 Canonical hashing

Canonical request hashing MUST:

- encode UTF-8;
- sort mapping keys;
- preserve ordered arrays;
- use stable JSON separators without insignificant whitespace;
- include provider, model, sampling parameters, output cap, system prompt, messages, and tools;
- exclude timestamps, run IDs, transport headers, retry attempt number, and returned usage;
- hash the exact redacted semantic request sent to the provider.

Redaction MUST occur before ledger acceptance and before dispatch rendering. The provider MUST never receive an unredacted variant when the ledger contains only a redacted variant.

## 10. Token accounting

### 10.1 TokenMeter seam

The implementation MUST inject a token-meter adapter:

```python
class TokenMeter(Protocol):
    name: str
    version: str
    exact: bool

    def count_request(self, request: ModelRequest) -> int:
        ...

    def count_text(self, text: str) -> int:
        ...
```

At least these adapters are required:

- configured model tokenizer when available;
- named deterministic fallback estimator;
- deterministic test meter.

### 10.2 Effective input budget

For B/C/D:

```text
effective_input_budget = configured_experiment_budget
```

For ordinary non-experiment runtime, the default SHOULD be:

```text
route_context_window
- reserved_output_tokens
- safety_margin_tokens
```

The experiment's configured 4096 or 8192 input budget is a harness limit and MUST NOT be presented as the provider's actual maximum context window.

### 10.3 Pressure threshold

Pressure is present when:

```text
estimated_input_tokens >= effective_input_budget * pressure_threshold
```

Default `pressure_threshold` is 0.85.

### 10.4 Counting cache

Per-event token measurements SHOULD be cached by:

```text
sha256(canonical model-visible event rendering)
+ token meter name
+ token meter version
```

The cache MUST NOT change correctness. A hash miss recalculates. A token-meter version change invalidates prior counts.

## 11. Experiment conditions and policies

### 11.1 Shared runtime

All conditions MUST share:

- SessionLedger and event schema;
- prompt rendering;
- request hashing;
- provider/model/temperature;
- system prompt and tools;
- task/checkpoint/pressure payload;
- output cap and tool-step budget;
- recovery mode and recovery budget;
- verifier definitions;
- workspace initial state.

Only the policy fields below may vary.

### 11.2 Condition A: Legacy

```json
{
  "token_budgeting": false,
  "tool_pruning": false,
  "history_compaction": false
}
```

Requirements:

- preserve current character-budget allocation and reduction order;
- preserve current clipping and spill behavior;
- assemble through the new ledger;
- emit the same request/header, request/context, and request/dispatch instrumentation as other groups;
- pass legacy parity tests before formal evaluation.

### 11.3 Condition B: Token-only

```json
{
  "token_budgeting": true,
  "tool_pruning": false,
  "history_compaction": false
}
```

Requirements:

- measure the complete candidate request;
- never clip an individual message to meet history pressure;
- remove the oldest safe atomic Surface units until below target;
- preserve the current request and configured recent tail;
- treat a completed tool call/result pair as one removal unit;
- record removed unit seqs in metadata without creating a summary replacement;
- return a typed no-safe-reduction outcome when retained units alone exceed budget.

B intentionally tests token-aware selection without semantic compression.

### 11.4 Condition C: Tool-pruning

```json
{
  "token_budgeting": true,
  "tool_pruning": true,
  "history_compaction": false
}
```

Requirements:

- run deterministic supported-tool pruning before removing whole history units;
- remeasure after every committed pruning pass;
- fall back to B behavior if still pressured;
- never call a model for pruning;
- make repeated pruning idempotent for the same policy and budget.

### 11.5 Condition D: Full-compaction

```json
{
  "token_budgeting": true,
  "tool_pruning": true,
  "history_compaction": true
}
```

Requirements:

- execute C behavior first;
- if pressure remains, select the oldest safe compaction range;
- call the same configured provider/model with temperature zero;
- validate summary structure, citations, quotes, range stability, and token gain;
- commit a `context/summary` replacement transaction;
- remeasure the resulting request;
- fall back to safe whole-unit removal only when policy explicitly permits and records the fallback;
- count summary usage and latency in the Run.

## 12. Tool Result Pruning

### 12.1 Supported tools

v1 supports text results from built-in:

- `run_process`
- `run_shell`
- `read_file`
- `search`
- `list_files`

Unknown tools and non-text results MUST remain unchanged.

### 12.2 Common retention requirements

Every pruned replacement MUST retain:

- tool name;
- `call_id`;
- success/failure identity and exit code when present;
- original content SHA-256;
- original and replacement token counts;
- pruning policy name/version;
- retained ranges or structured retention reasons;
- a bounded notice that content was pruned and how to identify the source event.

### 12.3 Process and shell results

When structured metadata exists, pruning MUST treat stdout, stderr, and exit code separately. Retention priority is:

1. non-zero exit status and failure summary;
2. traceback/error/failed-test blocks with bounded surrounding lines;
3. file paths and line-number diagnostics associated with retained errors;
4. final test summary;
5. bounded stdout/stderr head and tail windows.

The implementation MUST NOT rely only on English words. It SHOULD support configurable diagnostic patterns and common pytest/unittest formats, but patterns MUST remain deterministic and versioned.

### 12.4 File and search results

`read_file`, `search`, and `list_files` pruning MUST preserve:

- rendered path identity;
- line numbers when originally present;
- complete retained lines, not arbitrary byte slices;
- the first and last bounded windows;
- matching regions identified by validated `retention_hints`.

### 12.5 Retention hints

Built-in tools MAY attach internal `retention_hints` metadata. The pruning implementation MUST validate all ranges against actual content and MUST ignore invalid hints rather than indexing outside content.

### 12.6 Unicode and redaction

Text slicing MUST operate on Unicode code points and MUST NOT split surrogate pairs. Secret redaction MUST run before pruning. Pruning metadata MUST NOT preserve unredacted secret fragments.

### 12.7 No-op and gain rules

Pruning returns no replacement when:

- content is at or below the soft limit;
- the tool/result type is unsupported;
- the replacement is not smaller;
- required structural content cannot be preserved;
- policy gain is below the configured tool-pruning minimum.

## 13. History Compaction

### 13.1 Trigger order

For D, the exact order is:

```text
assemble candidate request
-> measure
-> if no Pressure, dispatch
-> prune supported oversized tool results
-> remeasure
-> if no Pressure, dispatch
-> select safe old Surface range
-> summarize and validate
-> commit replacement
-> remeasure
-> dispatch or return typed no-progress outcome
```

Confirmed Context Overflow uses the same sequence but MAY select a larger safe old range than ordinary Pressure. It MUST still preserve hard retained units and minimum evidence rules.

### 13.2 Retained tail

The default retained tail is the two most recent complete steps plus the current request. Configuration MAY increase it. A compaction range MUST NOT include an open step, current request, or active tool call.

### 13.3 Safe range requirements

A range is safe only if:

- both endpoints exist in the current Surface snapshot;
- it lies completely before the retained tail;
- it does not begin with an orphan tool result;
- it does not end after a call but before its result;
- it contains no current request;
- its events remain unchanged at commit time;
- it has enough measured content to satisfy minimum gain after summary.

### 13.4 Summary request

The summarizer MUST receive:

- selected events in Surface order;
- stable seq identifiers;
- event roles and tool identities;
- a strict output schema;
- maximum summary output tokens;
- instructions to distinguish confirmed facts, hypotheses, and open work;
- instructions never to claim an edit/test/action without cited evidence;
- an explicit cancellation signal.

The summary request MUST NOT include benchmark hidden-oracle metadata or experiment-condition labels.

### 13.5 Structured summary schema

The summary candidate MUST validate against this conceptual schema:

```json
{
  "schema_version": 1,
  "goal": {
    "text": "string",
    "status": "confirmed|hypothesis|open",
    "sources": [{"event_seq": 1, "quote": "exact quote"}]
  },
  "constraints": [],
  "files": [],
  "changes": [],
  "tests": [],
  "open_items": [],
  "next_step": {
    "text": "string",
    "status": "confirmed|hypothesis|open",
    "sources": []
  }
}
```

Each claim object MUST contain:

- non-empty `text`;
- `status` in the fixed enum;
- a list of source objects;
- at least one valid Evidence Anchor when status is `confirmed`;
- no source outside the selected range.

`hypothesis` and `open` MAY omit quotes but MUST NOT be rendered as established facts.

### 13.6 Evidence-anchor validation

For every Evidence Anchor:

- `event_seq` MUST identify an event in the selected source range;
- `quote` MUST be non-empty and bounded by configured maximum length;
- after newline normalization only, `quote` MUST be an exact substring of that event's model-visible rendering;
- matching MUST remain case-sensitive;
- ellipsis or paraphrase MUST NOT count as an exact quote;
- an anchor MUST NOT cite hidden metadata excluded from model-visible rendering.

This structural validation does not prove semantic entailment. Controlled-task oracles measure semantic factual correctness separately.

### 13.7 Transaction state machine

One compaction has these states:

```text
planned
-> started
-> summarized
-> validated
-> committed
-> closed
```

Failure transitions end in `closed_failed`. There is no retry inside the same transaction identity.

Required sequence:

1. snapshot `surface_generation` and selected seqs;
2. append and flush `compaction/start`;
3. call summarizer;
4. validate schema and Evidence Anchors;
5. render candidate summary and calculate gain;
6. revalidate generation and exact selected span;
7. append `context/summary` with replace operation;
8. append `compaction/end` success record;
9. flush committed events;
10. only then prepare the next provider dispatch.

An expected failure MUST append `compaction/end` with error details when `compaction/start` was durably appended.

### 13.8 Compaction events

`compaction/start` data MUST include:

- unique `compaction_id`;
- trigger;
- selected endpoint seqs;
- complete selected Surface seqs;
- source token count;
- starting surface generation;
- summarizer provider/model and output cap.

`context/summary` data MUST include:

- `compaction_id`;
- validated structured summary;
- rendered model-visible summary;
- selected source seqs;
- source/replacement token counts;
- summary request/response usage when available;
- raw summary output SHA-256;
- validation result and policy version.

`compaction/end` data MUST include:

- `compaction_id`;
- success boolean;
- terminal phase;
- typed error code/message when failed;
- ending surface generation;
- committed replacement seq when successful;
- token gain and gain ratio.

### 13.9 Compaction error codes

At minimum:

- `no_safe_range`
- `summary_transport_failed`
- `summary_cancelled`
- `summary_schema_invalid`
- `summary_evidence_invalid`
- `summary_no_gain`
- `surface_changed`
- `commit_failed`
- `persistence_failed`
- `tokenizer_unavailable`
- `context_recovery_no_progress`

Errors MUST remain distinct from final Run failure categories.

## 14. Cancellation, crashes, and uncertain side effects

### 14.1 Cancellation

Cancellation MUST be forwarded to summary model calls. Cancellation before replacement commit leaves Surface unchanged. Cancellation after durable replacement commit closes the transaction and stops before another model dispatch.

### 14.2 Crash recovery

On session load:

- unmatched `compaction/start` before the latest durable replacement is a failed prior attempt and MUST NOT lock the new process forever;
- a committed `context/summary` without a closing end record MUST be recognized as a committed replacement requiring a synthetic recovery/diagnostic close, not rolled back by deleting events;
- invalid event continuity or replacement replay MUST fail session loading loudly;
- unmatched tool calls MUST be surfaced as uncertain outcomes.

### 14.3 Tool execution durability

The tool call event MUST be durable before executing the tool. The result event MUST be durable immediately after result finalization. On resume, an unmatched side-effecting call MUST NOT be executed automatically.

## 15. Integration with AgentLoop and RecoveryEngine

### 15.1 AgentLoop changes allowed

AgentLoop MAY:

- call `context_engine.prepare(user_message, trigger="pressure")` before a model request;
- call it with `trigger="context-overflow"` after a typed recovery decision;
- persist returned metadata;
- dispatch the returned prompt/request;
- stop on typed preparation failures.

AgentLoop MUST NOT:

- inspect policy booleans;
- select pruning or compaction ranges;
- validate summary claims;
- reduce section budgets directly;
- commit Surface replacements;
- recalculate compaction token gain.

### 15.2 RecoveryEngine ownership

RecoveryEngine continues to own whether a typed provider failure permits context rebuilding within the recovery budget. ContextEngine owns how context is rebuilt.

Context recovery is allowed only when:

- provider failure is classified as context overflow;
- recovery policy returns `rebuild_context`;
- remaining recovery budget is positive;
- the new preparation produces Compaction Progress or another measurable token reduction.

No-progress recovery MUST stop with `context_recovery_no_progress`.

### 15.3 Feature configuration

Recommended runtime configuration:

```python
@dataclass(frozen=True)
class ContextPolicy:
    token_budgeting: bool
    tool_pruning: bool
    history_compaction: bool
    effective_input_budget: int
    reserved_output_tokens: int = 512
    pressure_threshold: float = 0.85
    tool_result_soft_limit: int = 800
    summary_max_tokens: int = 384
    retained_recent_steps: int = 2
    max_compactions_per_run: int = 2
    minimum_compaction_gain: float = 0.15
```

Validation MUST reject non-positive budgets, thresholds outside `(0, 1]`, negative limits, and inconsistent configurations such as history compaction enabled without token budgeting.

## 16. Security, redaction, and artifact policy

### 16.1 Secrets

- API keys and authentication headers MUST never enter Session Events, traces, reports, or benchmark artifacts.
- Existing configured secret-name and secret-shaped-value detection MUST apply before event acceptance.
- A redacted value sent to the model MUST be the same redacted value stored for replay.
- Evidence Anchors MUST NOT preserve pre-redaction content.

### 16.2 Paths

Run artifacts SHOULD store workspace-relative paths. Environment-specific absolute paths MAY appear in local session headers but MUST be normalized out of canonical benchmark comparisons.

### 16.3 Raw prompts and raw summaries

Formal reports MUST NOT embed full prompts. Local Run artifacts MAY retain redacted raw summary output when needed for audit. The manifest MUST state whether raw output retention was enabled.

## 17. Evaluation design

### 17.1 Evaluation layers

Every Run produces three independent results:

```text
run_pass = artifact_pass AND context_pass AND trace_pass
```

- `artifact_pass`: public plus hidden task contract succeeds;
- `context_pass`: required facts and contradiction rules succeed;
- `trace_pass`: event continuity, pairing, transaction, and replay invariants succeed.

### 17.2 Task mix

The main task set contains:

- 8 controlled tasks with frozen pre-seeded ledgers and deterministic fact oracles;
- 4 realistic tasks forked from a common investigation checkpoint.

No task may expose condition labels, hidden fact-oracle metadata, or hidden verifier code to the Agent.

### 17.3 Controlled task requirements

Each controlled task definition MUST include:

- stable task ID and category;
- fixture and fixture-tree hash;
- frozen seed event log and hash;
- pressure-profile metadata hidden from the Agent;
- public user request;
- allowed tools and step budget;
- critical fact predicates;
- contradiction predicates;
- artifact verifier;
- process invariants;
- expected pressure/compaction trigger behavior per condition.

Required controlled tasks:

#### C01 early-fact

- critical contract appears near the oldest retained history;
- later investigation adds irrelevant but realistic repository evidence;
- verifier checks the contract is implemented and remains visible or correctly summarized.

#### C02 middle-evidence

- a large process result contains the only root-cause diagnostic in its middle;
- head and tail contain plausible distractors;
- C/D SHOULD preserve the diagnostic using structured retention;
- context verifier rejects retention of only the distractors.

#### C03 tail-evidence

- the decisive failed-test expectation appears in the final bounded portion of a long result;
- verifier checks tail retention and correct repair.

#### C04 stale-conflict

- an early event contains an obsolete value;
- a later authoritative event contains the replacement value;
- a summary containing both as current facts fails context verification;
- only the latest authoritative fact may be confirmed.

#### C05 tool-pair-boundary

- Pressure occurs adjacent to completed tool call/result pairs;
- selected ranges tempt an off-by-one boundary error;
- trace verifier requires balanced pairing after every replacement generation.

#### C06 repeated-compaction

- the Run requires two successful compactions;
- facts from the first compacted range remain necessary after the second;
- replay and source chains must remain valid through both generations.

#### C07 oversized-result-selection

- at least three large tool results exist;
- only one contains relevant evidence;
- pruning/removal metadata must show deterministic prioritization;
- artifact and context verifiers ensure relevant evidence survives.

#### C08 overflow-recovery

- the first eligible request receives one deterministic context-overflow fault;
- recovery may rebuild context once;
- the Run must complete without repeated side effects;
- trace verifier distinguishes injected overflow, recovery action, and final stop reason.

### 17.4 Realistic tasks

The realistic tasks extend existing contracts:

- `centralized_timeout_default`
- `public_user_label`
- `stable_json_report`
- `workspace_relative_path_guard`

Each task has two phases:

1. a common, frozen investigation phase produces the same durable checkpoint for A/B/C/D;
2. each condition forks that checkpoint and independently completes the task.

The forked seed log, workspace snapshot, and checkpoint hash MUST match across conditions. Divergence after the fork is expected and is part of the treatment effect.

### 17.5 Public and hidden verification

Public checks:

- live in the Agent workspace;
- may be read and executed by the Agent;
- help normal coding workflow.

Hidden checks:

- live outside the Agent-visible workspace;
- are executed only by the benchmark runner;
- verify only requirements already stated in the public task contract;
- MUST NOT introduce undisclosed product behavior;
- produce a bounded, redacted failure summary in artifacts.

`artifact_pass` requires both public and hidden checks.

## 18. Benchmark task schema v2

Conceptual task definition:

```json
{
  "schema_version": 2,
  "id": "C01-early-fact",
  "suite": "context-compaction",
  "task_style": "controlled",
  "category": "early-fact",
  "prompt": "public task request",
  "fixture_repo": "tests/fixtures/context_compaction_repo",
  "seed_event_log": "benchmarks/context_compaction/seeds/C01.jsonl",
  "allowed_tools": ["read_file", "search", "patch_file", "run_process"],
  "step_budget": 12,
  "pressure_profile": {
    "target_input_tokens": 4096,
    "critical_fact_position": "early",
    "large_tool_result_position": "middle"
  },
  "critical_facts": [
    {
      "id": "timeout-source",
      "predicate": "named deterministic predicate",
      "contradiction_predicate": "named deterministic predicate"
    }
  ],
  "public_verifier": ["python", "-m", "pytest", "-q", "public_tests/..."],
  "hidden_verifier_id": "context/C01",
  "process_invariants": [
    "tool_pairs_balanced",
    "current_request_preserved",
    "request_replayable"
  ]
}
```

Task schema validation MUST reject unknown condition-specific fields. The task definition MUST NOT select A/B/C/D behavior.

## 19. Experiment protocol

### 19.1 Main experiment

- Tasks: all 12
- Conditions: A, B, C, D
- Repetitions: 3
- Effective input budget: 4096 tokens
- Reserved output: 512 tokens
- Expected Run count: `12 * 4 * 3 = 144`

### 19.2 Sensitivity experiment

- Tasks: 6 tasks selected and frozen before the main formal run, stratified as 4 controlled and 2 realistic
- Conditions: A and D
- Repetitions: 3
- Effective input budget: 8192 tokens
- Expected Run count: `6 * 2 * 3 = 36`

Total formal Runs: 180, excluding deterministic gates and invalidated pilots.

### 19.3 Pilot

Before freezing formal hashes:

- run all 12 tasks under A/B/C/D once: 48 Runs;
- verify that at least 80 percent of tasks trigger intended Pressure in B/C/D;
- allow fixes only for fixture defects, invalid oracles, inaccessible hidden-verifier setup, and failure to create the declared pressure profile;
- do not remove or rewrite a task merely because the model fails it;
- after any permitted change, invalidate old pilot conclusions and freeze new hashes.

### 19.4 Balanced scheduling

Formal Runs MUST be scheduled in `(task_id, repetition, budget)` blocks. Conditions inside a block MUST use a deterministic balanced order derived from the frozen manifest.

Requirements:

- conditions are approximately balanced across first/second/third/fourth positions over all tasks;
- sensitivity A/D order alternates;
- one condition failure does not skip sibling conditions;
- all blocks record request time, configured model, provider-returned model identity when available, and cache telemetry;
- the complete schedule and schedule-generation version are stored before execution.

### 19.5 Model settings

- configured provider: DeepSeek unless the frozen experiment config says otherwise;
- configured current candidate model: `deepseek-v4-flash`;
- temperature: 0;
- main Agent and summary model: same provider and model;
- output limits fixed by config;
- model alias mutability is acknowledged; timestamps and returned identifiers must be retained.

## 20. Metrics

### 20.1 Primary outcome

```text
end_to_end_pass_rate = count(run_pass) / count(all completed Runs)
primary_delta = pass_rate(D) - pass_rate(A)
```

### 20.2 Context overflow metrics

Report separately:

- first-overflow rate;
- overflow recovery rate;
- unrecovered-overflow rate.

A recovered overflow remains counted as an observed overflow.

### 20.3 Critical fact retention

Controlled tasks only:

```text
critical_fact_retention_rate = retained required facts / required facts
```

A fact is retained if either:

- its authoritative source Surface node remains visible; or
- a visible summary contains the correct claim with valid source anchors.

If a visible summary presents a contradicted obsolete value as current, the fact fails even when the correct value also appears.

### 20.4 Summary factual error

Controlled-task error claims include:

- missing/nonexistent source;
- invalid quote;
- claim contradicted by deterministic oracle;
- hypothesis labeled confirmed;
- obsolete fact retained as current;
- edit/test/action claimed without supporting event.

```text
summary_factual_error_rate = erroneous claims / all summary claims
```

Realistic summaries receive structural and anchor validation automatically. Human audit MAY be reported separately but MUST NOT replace primary deterministic metrics.

### 20.5 Integrity metrics

- tool-pair integrity rate;
- replay integrity rate;
- current-request preservation rate;
- duplicate-side-effect rate;
- partial-compaction-transaction count;
- unnecessary-intervention rate.

### 20.6 Cost and latency

Report:

- main-request input/output tokens;
- summary input/output tokens;
- total tokens;
- cached tokens and cache availability;
- cache-hit rate and cached-token ratio;
- average, median, and P95 Run latency;
- average compactions per Run;
- tokens removed and summary tokens added;
- total tokens per successful Run.

Summary calls are included in D totals.

### 20.7 Intervention definitions

An intervention is unnecessary if:

- it starts below the configured threshold without an overflow trigger;
- it produces gain below policy minimum;
- it repeats on the same Surface generation and request hash;
- it cannot enable a new request and no typed reason justifies the attempt.

## 21. Failure taxonomy

Each failed Run has exactly one primary failure category plus optional secondary tags. Primary precedence:

1. `runner_error`
2. `replay_mismatch`
3. `tool_pair_broken`
4. `compaction_commit_failed`
5. `summary_invalid`
6. `critical_fact_lost`
7. `context_overflow_unrecovered`
8. `budget_exhausted`
9. `artifact_verifier_failed`
10. `model_stop_failure`

Infrastructure failures MUST remain distinguishable from model failures. A `runner_error` invalidates that paired observation and triggers a rerun only after the runner defect is fixed and documented; it MUST NOT be silently counted as a strategy failure.

## 22. Success criteria

### 22.1 Mandatory safety gates for D

- tool-pair integrity: 100 percent;
- replay integrity: 100 percent;
- current-request preservation: 100 percent;
- duplicate side effects: zero;
- partial transaction commits: zero;
- controlled critical-fact retention: at least 90 percent;
- controlled summary factual-error rate: at most 5 percent.

### 22.2 Effect paths

D succeeds only if mandatory gates pass and either path succeeds.

Path 1:

```text
pass_rate(D) - pass_rate(A) >= 10 percentage points
```

Path 2:

```text
relative unrecovered-overflow reduction >= 50 percent
AND pass_rate(D) - pass_rate(A) >= -5 percentage points
AND critical_fact_retention(D) >= critical_fact_retention(A)
```

### 22.3 Cost interpretation

Target cost bounds:

- D total tokens per successful Run <= 115 percent of A;
- D P50 latency <= 125 percent of A.

Exceeding a cost target does not erase a quality result, but the report MUST describe it as a trade-off and MUST NOT claim unconditional improvement.

### 22.4 Statistical reporting

- pair by `(task_id, repetition, budget)`;
- report absolute and relative changes;
- use task-clustered bootstrap 95 percent intervals for pass-rate differences;
- report mean, median, and P95 for tokens/latency;
- stratify controlled vs realistic and pressure category;
- if an interval crosses zero, state that the observed improvement is not established as stable;
- do not use significance language unsupported by the small task sample.

## 23. Experiment artifacts

### 23.1 Directory layout

```text
benchmarks/results/context-compaction-YYYY-MM-DD-HHMMSS/
|-- manifest.json
|-- experiment-config.json
|-- tasks.snapshot.json
|-- schedule.json
|-- aggregate.json
|-- report.md
|-- runs/
|   |-- A-legacy/<task-id>/r01/
|   |   |-- report.json
|   |   |-- events.jsonl
|   |   |-- requests.jsonl
|   |   `-- verifier.json
|   |-- B-token-only/
|   |-- C-tool-pruning/
|   `-- D-full-compaction/
`-- failures/
```

### 23.2 Manifest

Must contain:

- schema/artifact version;
- experiment ID and timestamps;
- commit SHA, branch, and dirty status;
- task/config/fixture/pressure/schedule hashes;
- Python/platform/Apricity versions;
- tokenizer and pruning-policy identities;
- canonical/noncanonical status.

Official formal Runs SHOULD require a clean worktree. An explicit override MAY permit a dirty run, but its manifest is `noncanonical` and it MUST NOT be merged into canonical aggregates.

### 23.3 Per-Run report

Must contain identity, condition, repetition, budget, status, primary failure, three verifier results, stop reason, tool/model/compaction counts, token/cache/latency metrics, and hashes of event/request/verifier artifacts.

### 23.4 Resume

`--resume` MUST reject any mismatch in:

- provider/model;
- task snapshot hash;
- fixture-tree hash;
- pressure payload hash;
- experiment config hash;
- schedule hash;
- repetitions or budget;
- code commit for canonical runs.

Completed `(task, condition, repetition, budget)` keys are skipped only after their artifact hashes validate.

## 24. Evaluation implementation shape

Recommended package:

```text
apricity/evaluation/context_compaction/
|-- schema.py
|-- pressure.py
|-- verifiers.py
|-- runner.py
|-- metrics.py
`-- report.py
```

Responsibilities:

- `schema.py`: strict input/artifact validation;
- `pressure.py`: frozen seed/checkpoint preparation;
- `verifiers.py`: artifact/context/trace verification;
- `runner.py`: balanced schedule, workspace isolation, persistence, resume;
- `metrics.py`: pure aggregation from completed Run records;
- `report.py`: deterministic Markdown rendering.

The existing evaluator MAY provide workspace copying, provider creation, tool budgets, and subprocess verifier execution. It MUST NOT become responsible for pressure construction, Surface replay, compaction implementation, and report prose simultaneously.

## 25. Test strategy and phase gates

### Phase 0: baseline freeze

Required:

- current deterministic suite passes;
- task and fixture hashes captured;
- normalized prompts from existing manager match `LegacyContextEngine` on representative sessions;
- current overflow and spill behavior has characterization tests.

No B/C/D work proceeds until parity passes.

### Phase 1: ledger and replay

Interface tests cover contiguous append, immutability, JSON rejection, persistence/restart, Surface replacements, invalid endpoints, non-monotonic Surface seq order, tool pairing, header/context changes, canonical hashing, and request replay.

Exit: replay integrity 100 percent on deterministic scenarios.

### Phase 2: token-only

Tests cover threshold/no-threshold, output reserve, current request, retained tail, mixed-language content, tool schema cost, exact/fallback meter identity, count cache, no-safe-range, overflow trigger, and repeated no-op preparation.

Exit: generated candidate requests respect configured budget or return a typed impossibility.

### Phase 3: pruning

Tests cover small no-op, head/tail, middle diagnostics, multiple errors, path/line retention, Unicode, non-text no-op, repeated idempotence, redaction, fallback deletion, gain rules, and pairing.

Exit: deterministic hashes and controlled C02/C03 oracle pass.

### Phase 4: compaction

Tests cover no safe range, valid summary, invalid schema, invalid evidence, unsupported confirmation, no gain, timeout, cancellation, generation conflict, persistence failure, two compactions, stale conflicts, and repeated no-progress prevention.

Exit: zero partial commits, replay 100 percent, deterministic oracle factual error within gate.

### Phase 5: recovery integration

Tests cover one overflow rebuild, progress-required retry, budget exhaustion, no duplicate tool side effects, distinct summary/provider failures, compatible/resilient behavior, and policy ablations.

Exit: existing recovery experiment does not regress.

### Phase 6: evaluator

Tests cover strict schema, hidden verifier isolation, hash mismatch resume rejection, partial artifact handling, paired scheduling, independent verifier booleans, failure precedence, deterministic aggregation/reporting, and secret scanning.

Exit: every report number traces to immutable Run records.

## 26. Implementation milestones

### M1 Measurement foundation

- SessionLedger v2
- Surface fold and invariants
- request header/context/dispatch
- canonical request replay
- Legacy parity

### M2 Token policy

- TokenMeter adapters
- ContextEngine seam
- A and B conditions
- pressure metadata

### M3 Compression policies

- built-in text-tool pruning
- evidence-anchored summary adapter
- compaction transaction
- C and D conditions

### M4 Evaluation

- task schema v2
- controlled seeds and realistic checkpoints
- hidden verifier runner
- three-layer verification
- balanced schedule and resume

### M5 Evidence and presentation

- deterministic gate
- 48-Run pilot
- frozen 180-Run formal experiment
- aggregate JSON and report
- README update only after results exist
- HISTORY update only after the corresponding remote push, following repository instructions

## 27. Definition of Done

The v1 capability is done only when:

- all functional requirements and invariants have tests;
- A/B/C/D share SessionLedger and request assembly;
- Legacy parity is documented;
- all model requests in deterministic tests replay to identical hashes;
- pruning is deterministic and scope-limited to supported text tools;
- compaction claims enforce Evidence Anchors;
- compaction failures cannot partially change Surface;
- context overflow recovery requires measurable progress;
- hidden verifiers are inaccessible to Agent tools;
- task, fixture, pressure, config, and schedule hashes are frozen;
- the pilot validates pressure construction;
- formal results contain all 180 expected Runs or explicitly remain incomplete;
- report language follows the success and cost rules in this PRD;
- the existing recovery, security, checkpoint, provider, and public-contract tests do not regress;
- implementation documentation links to this PRD and the event-sourcing ADR.

## 28. Prohibited shortcuts

An implementing model MUST NOT:

- treat trace JSONL as the source of model-visible history while continuing to render from mutable history;
- log only request hashes without preserving reconstructable request inputs;
- mutate or delete source events during pruning/compaction;
- split a completed call/result pair to make a token target;
- call the summary model without counting its cost;
- accept `source_event_seqs` as factual proof without Evidence Anchors;
- use the hidden oracle or hidden verifier in any model prompt;
- tune tasks after inspecting formal A/B/C/D outcomes;
- mark runner failures as model failures;
- compare A on the old runtime against D on the new runtime;
- claim equal-compute improvement;
- silently fall back from exact tokenizer to estimator;
- update README claims before canonical experiment artifacts exist.

## 29. Known risks and mitigations

### Risk: event-source rewrite expands scope

Mitigation: old sessions are explicitly abandoned; only schema v2 is implemented. Keep the AgentLoop seam small and phase-gate replay before compression.

### Risk: tokenizer differs from provider accounting

Mitigation: record estimator identity and provider usage separately; use provider usage for retrospective reporting, not pre-dispatch control.

### Risk: summary hallucination

Mitigation: structured statuses, exact Evidence Anchors, deterministic controlled oracles, source retention, and explicit factual-error metric.

### Risk: artificial 4096-token advantage

Mitigation: label it as a harness budget and run the frozen 8192 A/D sensitivity experiment.

### Risk: provider drift and cache order

Mitigation: interleaved balanced blocks, timestamps, returned model identity, cache telemetry, and frozen schedule.

### Risk: extra D compute

Mitigation: include all summary costs and frame conclusions as system quality/cost trade-offs.

### Risk: benchmark leakage

Mitigation: external hidden verifiers and hidden fact metadata inaccessible to Agent tools.

### Risk: task overfitting

Mitigation: mixed controlled/realistic suite, fixed pilot rules, frozen hashes, and no post-formal task selection.

### Risk: supported-tool claims become too broad

Mitigation: v1 explicitly supports only built-in text tools and reports unknown/non-text results as unpruned.

## 30. Expected final report claims

The final report MUST choose language based on observed gates.

Allowed when successful:

> Under fixed 4096-token harness budgets, Apricity's replayable full-compaction policy changed end-to-end pass rate by X percentage points relative to its legacy character policy, with Y percent change in unrecovered overflows and Z percent change in total tokens per successful Run. The 8192-token sensitivity subset showed [result].

Required when evidence is mixed:

> The experiment observed a quality or overflow improvement, but the task-clustered interval crossed zero and/or cost targets were exceeded. Results demonstrate an instrumented trade-off, not a stable universal advantage.

Required when safety gates fail:

> The full-compaction policy did not satisfy its replay, pairing, provenance, or transaction safety requirements and is not considered successful regardless of task pass rate.

## 31. Implementation handoff checklist for another model

Before coding, the implementing model MUST produce a plan that maps edits to M1-M5 and identifies existing user changes in the worktree. It MUST begin with M1 and MUST NOT implement summarization before replay invariants pass.

For each milestone, the implementing model MUST:

1. cite the requirement and invariant IDs it implements;
2. list files to create or modify;
3. add tests at the public module interface;
4. run focused tests with the `learn_claude` environment;
5. run the relevant existing regression suite;
6. report any deliberate deviation from this PRD before proceeding;
7. preserve unrelated worktree changes;
8. avoid README claims until formal artifacts exist.

If a requirement cannot be implemented without changing a frozen scope decision, the implementing model MUST stop and request a decision rather than silently reinterpret the requirement.
