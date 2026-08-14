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
