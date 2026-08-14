# Apricity Resilient Harness PRD

## Product question

Can typed lifecycle recovery policies improve a coding agent's recovery success under controlled faults without regressing normal tasks or safety invariants?

## Goals

- Move recovery selection out of `AgentLoop` and provider adapters.
- Support provider retry, context recovery, tool-result spill, and protocol correction.
- Attribute each injected fault, policy intervention, and terminal outcome separately.
- Run compatibility, resilient, and single-policy ablation experiments.
- Preserve verifier success, bounded work, redaction, and approval precedence.

## Non-goals

- A general event bus, plugin marketplace, hot reload, or Cordis reimplementation.
- Retrying writes whose side effects are uncertain.
- Subagent failover, transactional recovery, or production reliability claims.

## Required behavior

| Policy | Trigger | Budget | Terminal condition |
| --- | --- | ---: | --- |
| Provider retry | timeout, 408, 429, 5xx, connection failure | 2 | `provider_retry_exhausted` |
| Context recovery | explicit context overflow | 2 | exhausted or less than 20% progress |
| Tool-result spill | raw result over 4000 characters | 3 readable pages | spill failure falls back to redacted clipping |
| Protocol correction | malformed response or recoverable rejected call | 2 | `protocol_correction_exhausted` |

The global resilient-mode budget is four Recovery Actions. Final-only mode cannot recover. Approval denials and security decisions cannot be overridden.

## Lifecycle seam

`RecoveryEngine` exposes:

```text
finalize_tool_result(candidate, run_snapshot) -> finalized result
handle_failure(failure, run_snapshot) -> recovery decision
```

The only lifecycle points are `tool_result_ready`, `model_request_failed`, and `model_response_rejected`.

## Experiment

- G1: compatibility mode behind the new seam.
- G2: all resilient policies enabled.
- G2-X: one resilient policy disabled per variant.
- F1: transient provider error.
- F2: provider context overflow.
- F3: oversized tool result.
- F4: malformed model response or rejected protocol call.

Primary metrics are recovery rate and verifier pass rate. Guardrails are normal-task pass-rate regression at most 3 percentage points, no-fault overhead at most 10%, false-intervention rate below 5%, and duplicate-side-effect rate exactly zero.

## Evidence limits

Deterministic fixture results validate control-flow causality, not production reliability. Real-model confirmation requires explicit API execution and must report model, task set, repetitions, token use, and fault plan. A policy whose ablation does not change verifier outcomes must not be credited with a pass-rate improvement.
