# Apricity Review Pack

Reviewers and interviewers can start here. It maps the three project stories to the code and evidence that prove them, with a reproducible path under ten minutes.

## 30-second pitch

Apricity is a small local coding-agent harness built around three explicit seams: a typed recovery engine that owns retry/context/protocol budgets, a bounded production context manager, and an experimental context-policy seam (`ContextEngine.prepare`) evaluated with deterministic and paid evidence. The project deliberately separates "the product works" from "the experiment claims something", and publishes negative results when a hypothesis fails.

## 10-minute path

```powershell
cd D:\Pythonprojects\yan_1\agent\mypico
D:\anaconda3\envs\learn_claude\python.exe -m pytest tests -q
D:\anaconda3\envs\learn_claude\python.exe scripts\run_recovery_policy_experiment.py `
  --task-id readme_intro_locked `
  --task-id sample_beta_locked `
  --task-id sample_gamma_locked `
  --output artifacts\review-recovery.json `
  --workspace-root artifacts\review-recovery-workspaces
D:\anaconda3\envs\learn_claude\python.exe scripts\run_context_horizon_experiment.py pilot
```

Then read:

1. `benchmarks/results/recovery-policy-2026-08-14/recovery-policy-experiment-v1.md`
2. `benchmarks/results/context-horizon-paid-2026-08-16-175000/report.md`
3. `docs/experiments/context-horizon-v3.md`

## Architecture map

### Production control flow

| Module | Responsibility |
| --- | --- |
| `apricity.cli` | config loading, provider selection, approval mode, one-shot/REPL entry |
| `apricity.runtime.Apricity` | session shape, tool allowlist, prompt assembly, run artifacts |
| `apricity.agent_loop.AgentLoop` | applies lifecycle decisions but does not choose policy |
| `apricity.recovery.RecoveryEngine` | typed Failure → retry_model / rebuild_context / correct_protocol / stop |
| `apricity.context_manager.ContextManager` | bounded production prompt from prefix, memory, history, current request |
| `apricity.tools` + `apricity.tool_executor` | explicit tool allowlist and side-effect execution |
| `apricity.run_store` | per-run `task_state.json`, `trace.jsonl`, `report.json` |

### Recovery seam

- Provider adapters perform one transport attempt and raise `ProviderRequestError`.
- `AgentLoop` asks `RecoveryEngine.handle_failure` and applies the returned decision.
- Recovery budget, disabled policies, and actions are written into run artifacts.
- Evidence: `benchmarks/results/recovery-policy-2026-08-14/`.

### Context Horizon experiment seam

- `apricity.context.engine.ContextEngine.prepare` is the only external interface.
- `apricity.context.ledger.SessionLedger` keeps append-only source events; replacements shadow Surface ranges without deleting sources.
- `apricity.context.planner` plans atomic removal units and retained tails.
- `apricity.context.structural` implements condition C pruning and condition E deterministic compaction.
- `apricity.context.chunked` implements condition D chunked evidence-anchored LLM compaction.
- `apricity.evaluation.context_horizon` owns tasks, hidden verifiers, local calibration, paid runner, metrics.
- Hard rule: hidden verifier predicates never enter policy code or model-visible prompts.

## Evidence index

| Claim | Evidence |
| --- | --- |
| Recovery improves controlled fault recovery by 25pp | `benchmarks/results/recovery-policy-2026-08-14/recovery-policy-experiment-v1.md` (90 deterministic runs) |
| Product works on real-model micro-tasks | `benchmarks/results/real-model-2026-08-13-full/` |
| Window policy loses facts under pressure; E/D preserve them | `benchmarks/results/context-horizon-calibration-2026-08-16-162322/` |
| Paid DeepSeek: A 0/6 vs E 3/6 run-pass, E cheaper | `benchmarks/results/context-horizon-paid-2026-08-16-175000/report.md` |
| Test suite is green | `215 passed, 1 skipped` at commit `5953409` |

## Interview questions and where the answer lives

### 1. How do you separate policy from mechanism?

`RecoveryEngine` returns typed decisions; `AgentLoop` applies them (`apricity/recovery.py`, `apricity/agent_loop.py`). Provider adapters never own retry. In the context experiment, `ContextEngine.prepare` hides A/C/E/D policy from callers (`apricity/context/engine.py`).

### 2. Why append-only events instead of mutable history?

Because compaction must shadow old Surface ranges without losing source evidence, and replay/hash checks need durable sources. See `apricity/context/ledger.py` and the v1 retrospective in `docs/experiments/context-horizon-v3.md`.

### 3. How do you prove fact retention is not oracle leakage?

Hidden verifier predicates live only in `benchmarks/context_horizon/hidden/`, loaded by `apricity/evaluation/context_horizon/tasks.py`; `apricity/context` has no import path to them. The v1 oracle-leakage bug is documented in the experiment doc.

### 4. Why does E beat A, and why is D not better than E?

A removes old units and loses early facts. E deterministically keeps requirement/diagnostic lines with source seqs. D adds LLM chunks and fallback, but the paid run shows it does not beat E on this small suite; the report states that honestly.

### 5. What is your stop-loss discipline?

Every experiment has pre-registered metrics and stop lines in `docs/experiments/context-horizon-v3.md`; the paid run stopped by cost guard (¥15 cap) and writes immutable per-run evidence.

## Honest limitations

- Real-model micro-tasks are not SWE-bench.
- Paid context results are n=1 per cell; no bootstrap significance.
- Artifact verifiers check hidden required strings, not full product test suites.
- `ContextEngine` is an experiment seam, not yet wired into the production AgentLoop.
