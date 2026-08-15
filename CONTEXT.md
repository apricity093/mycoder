# Apricity Harness

Apricity Harness describes one repository-scoped coding-agent run and the evidence used to evaluate whether it completed safely and reliably.

## Language

**Run**:
One bounded attempt to satisfy a user request, including model requests, tool calls, recovery actions, and a terminal outcome.
_Avoid_: Job, execution session

**Failure**:
A typed fact observed at a lifecycle point that a recovery policy may handle.
_Avoid_: Exception, final failure

**Recovery Action**:
One recorded policy intervention intended to let a faulted Run continue within its budget.
_Avoid_: Retry, workaround

**Fault Injection**:
A deterministic experiment condition that introduces a Failure at a declared lifecycle point.
_Avoid_: Failure category, stop reason

**Recovery Success**:
A faulted Run that still passes its verifier and finishes within budget.
_Avoid_: Successful retry

**Stop Reason**:
The terminal reason a Run ended, independent of any earlier Fault Injection.
_Avoid_: Fault type, error category

**Spill**:
A redacted, run-local artifact containing a large tool result whose model-visible form is bounded and retrievable by page.
_Avoid_: Truncation, cache

**Session Event**:
One immutable fact in the append-only record from which a Session's model-visible state is derived.
_Avoid_: History item, trace entry

**Surface**:
The ordered projection of Session Events that supplies conversational history to a model request; newer events may shadow older Surface nodes without deleting their sources.
_Avoid_: History list, transcript

**Request Header**:
The durable snapshot of model configuration, system prompt, and tool schemas that defines the stable envelope of a model request.
_Avoid_: Prompt prefix, provider config

**Request Context**:
The durable snapshot of dynamic model-visible state outside the conversational Surface, such as memory and checkpoint text.
_Avoid_: Memory, prompt section

**Pressure**:
A locally predicted condition in which a candidate request reaches the configured fraction of its harness input budget.
_Avoid_: Context Overflow, token limit error

**Context Overflow**:
A typed provider Failure confirming that a model request exceeded provider context capacity.
_Avoid_: Pressure, long prompt

**Tool Result Pruning**:
A deterministic replacement that reduces one supported text-tool result while preserving its identity, pairing, relevant evidence, and source.
_Avoid_: Truncation, summarization

**History Compaction**:
A recorded transaction that summarizes a safe old Surface range and replaces it for future model requests without deleting its source events.
_Avoid_: Clipping, cleanup

**Evidence Anchor**:
An exact bounded quote from a cited Session Event that grounds one confirmed summary claim.
_Avoid_: Citation, event ID

**Replay Integrity**:
The property that a model request reconstructed from durable Session Events has the same canonical request hash as the dispatched request.
_Avoid_: Reproducibility, trace completeness

**Compaction Progress**:
A committed Surface change that reaches the configured minimum token reduction and can enable a materially smaller request.
_Avoid_: Compaction attempt, Recovery Action
