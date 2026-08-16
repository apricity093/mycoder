# Context Horizon v3 Experiment

- Status: draft, awaiting D1 approval
- Date: 2026-08-16
- Project: Apricity
- Supersedes: `docs/requirements/replayable-context-compaction-prd.md` (v1, deleted in D1), `docs/architecture/replayable-context-compaction.md`, `docs/adr/0002-event-sourced-model-context.md`
- Target role narrative: Agent framework / infrastructure / evaluation

This document is the single source of truth for the v3 context-policy experiment. It preserves the useful conclusions from the abandoned v1 work, pre-registers the v3 hypothesis, metrics, success criteria, stop lines, and artifact rules. When this document conflicts with an exploratory note or old v1 prose, this document wins.

## 1. v1 retrospective (preserved evidence)

v1 was implemented from commit `df0ea8b` through `8ace0d0` as the Replayable Context Compaction line. It is being deleted from the working tree because the experiment did not answer a useful question; the historical record below is kept here instead of being lost with the deleted files.

### 1.1 What was built

- `apricity/context/`: append-only `SessionLedger`, Surface fold, token meter, tool-result pruning, single-shot evidence-anchored LLM compaction, `ContextEngine.prepare()` seam.
- `apricity/evaluation/context_compaction/`: task schema v2, hidden verifiers, balanced schedule, resume provenance, metrics, report.
- `benchmarks/context_compaction/`: 8 controlled + 4 realistic tasks with pre-seeded event logs.
- `docs/requirements/replayable-context-compaction-prd.md`, `docs/architecture/replayable-context-compaction.md`, `docs/adr/0002-event-sourced-model-context.md`.

### 1.2 Last canonical result

`benchmarks/results/context-compaction-48run-clean2-2026-08-16-154838/` (48 paid runs, 12 tasks x A/B/C/D x 1 repetition, 4096-token harness budget):

| Condition | Pass rate | Critical-fact retention | Tool-pair integrity | Replay integrity |
| --- | ---: | ---: | ---: | ---: |
| A legacy | 50.00% | 100.00% | 100.00% | 100.00% |
| B token-only | 16.67% | 100.00% | 100.00% | 100.00% |
| C tool-pruning | 50.00% | 100.00% | 100.00% | 100.00% |
| D full-compaction | 50.00% | 100.00% | 100.00% | 100.00% |

- D - A pass-rate delta: **+0.00 percentage points**.
- First-overflow rate: 0.00%.
- D compaction attempts: 31, successes: 12, summary fallbacks: 18.
- Failure categories: 13 artifact_verifier_failed, 9 budget_exhausted, 6 model_stop_failure.

### 1.3 Why v1 cannot be used as a positive result

1. **Oracle leakage.** `ContextCompactionRunner._protected_predicates()` read hidden task `critical_facts` and passed them into the policy. `_protected_evidence()` then appended oracle-matched verbatim quotes into the model-visible summary. The 100% critical-fact retention in B/C/D was partly guaranteed by hidden metadata, not by the policy mechanism.
2. **No real pressure.** Average estimated input was 1,846 tokens against a 4096-token budget. No run observed a provider context overflow. Context was rarely the binding constraint.
3. **Wrong primary question.** Pass rate on 10-12 step micro-tasks is dominated by model capability and verifier strictness, not by context policy; 28 of 48 runs failed as artifact/model/budget failures unrelated to context loss.
4. **Single-shot compaction was brittle.** One JSON summary over a large range with 240-character excerpts and a 4,200-character prompt cap produced a 38.7% success rate and 18 fallbacks.
5. **Measurement hygiene issues.** Paid runs used `deterministic-test` as the control meter while labeling it `exact=true`.
6. **Not a product capability.** The production `Apricity` runtime kept using `ContextManager`; the v1 engine lived only inside the evaluation harness.

### 1.4 What is preserved

- All `benchmarks/results/context-compaction-*` directories stay in place, read-only, per AGENTS.md rule 4.
- The engineering lessons are encoded in the v3 invariants below: append-only source events, atomic Surface replacement, tool-call/result pairing, no hidden oracle in policy code, estimator identity, honest negative-result language.

## 2. v3 research question

> Under a fixed harness input budget, does deterministic structural compaction (E) or chunked evidence-anchored LLM compaction (D) extend a Run's Horizon — the number of completed tool steps before a terminal failure — compared with a bounded-window control (A), without losing earlier required facts and without violating replay or tool-pair invariants?

### 2.1 Goals

- Build one small external seam: `ContextEngine.prepare(user_message, trigger) -> PreparedContext`.
- Compare four policies behind that seam: A window, C prune, E structural, D chunked-LLM.
- Build a six-task Progressive-Horizon suite where context pressure is real and calibrated.
- Report paired horizon deltas, fact retention at matched step, provider tokens per completed step, and safety gates.
- Publish an honest negative result if the success criteria fail.

### 2.2 Non-goals

- Do NOT integrate a context policy into the production `Apricity` runtime in v3.
- Do NOT claim equal-compute or equal-token quality improvements.
- Do NOT rerun or extend the v1 48-run / 180-run experiments.
- Do NOT reproduce DeepSeek Harness's plugin architecture.
- Do NOT let hidden verifier data or critical-fact predicates enter policy code or model-visible prompts.
- Do NOT grow the new implementation beyond the line budgets in section 8.

## 3. Architecture

```
                    ┌──────────────────────────────┐
   AgentLoop/runner │  ContextEngine.prepare(...)  │  ← the only external seam
                    └──────────────────────────────┘
                                  │
                    ┌─────────────┴──────────────┐
                    │  PolicyController (A/C/E/D)│
                    └─────────────┬──────────────┘
                                  │
              ┌───────────────────┼────────────────────┐
              ▼                   ▼                    ▼
     SurfaceRangeSelector  StructuralCompactor  ChunkedSummaryCompactor
              │                   │                    │
              └───────── SessionLedger (append-only) ──┘
```

- **External seam** — `ContextEngine.prepare`. Callers never select ranges, validate summaries, or branch on experiment conditions.
- **Internal seam** — `Compactor`. It has two adapters (`StructuralCompactor`, `ChunkedSummaryCompactor`); two adapters justify the seam. It is not exported.
- **Deep modules** — range selection, token accounting, and transactional replacement stay inside the engine. Tests cross the external seam and observe behavior, not internal state.

### 3.1 Package shape

```text
apricity/context/
|-- __init__.py       # ContextEngine, ContextPolicy, PreparedContext, SessionLedger
|-- ledger.py         # append-only events, Surface fold, atomic replacement
|-- token_budget.py   # token meter identity, pressure, budget
|-- planner.py        # internal Surface unit planning (not exported)
|-- structural.py     # condition C tool-result pruning; condition E compactor lands here
|-- chunked.py        # condition D chunked LLM compaction with evidence anchors
`-- engine.py         # ContextEngine.prepare
apricity/evaluation/context_horizon/
|-- runner.py         # reuse existing evaluator workspace/provider/verifier helpers
|-- tasks.py          # task schema v1 (this experiment), pressure calibration
|-- verifiers.py      # fact, horizon, safety checks
`-- metrics.py        # pure aggregation
benchmarks/context_horizon/
|-- tasks.v1.json
|-- seeds/*.jsonl
`-- hidden/*.json
tests/fixtures/context_horizon_repo/...
scripts/run_context_horizon_experiment.py
```

Implementation status: D1 cleanup complete; D2 `SessionLedger` + conditions A/C complete; D3 condition E deterministic structural compaction complete; D4 six-task suite, hidden verifiers, and local scripted-model pressure calibration complete; D5 condition D chunked evidence-anchored LLM compaction complete; D6 paid runner with cache-aware ¥15 cost guard is ready. Paid DeepSeek runs are the only remaining step.

## 4. Conditions

| ID | Name | Behavior |
| --- | --- | --- |
| A | `window` | Control. When the candidate request crosses the pressure threshold, remove the oldest complete atomic units until under budget. No semantic compression, no LLM. |
| C | `prune` | A plus deterministic pruning of individual oversized text-tool results before whole-unit removal. |
| E | `structural` | C plus deterministic structural compaction of the old Surface prefix: keep user constraints, tool-call arguments, file changes, traceback/test summary/exit codes, latest value per key; drop repeated stdout and noise. Every retained item carries a source seq. No LLM. |
| D | `llm` | C plus chunked LLM compaction: group the old prefix into completed steps, summarize each step with up to 2,000 characters of full excerpt, validate Evidence Anchors per chunk, fall back to E for any failed chunk, then replace the prefix in one transaction. |

Hard rule: hidden verifiers and critical-fact predicates are constructed only in `apricity/evaluation/context_horizon/verifiers.py`. No predicate string crosses into `apricity/context`.

## 5. Task suite

### 5.1 Progressive-Horizon tasks

| ID | Task | What it tests |
| --- | --- | --- |
| L1 | 8-step incremental requirements chain; each step adds a requirement and all previous requirements remain tested | early facts survive a long horizon |
| L2 | 40KB process log with the root cause only in the middle; after repair, apply the same root cause to a second file | middle evidence survives compaction |
| L3 | three test-fix rounds; each round's diagnosis appears once; the final round needs the first two diagnoses | multi-round fact chain |
| L4 | an early value is superseded by a later value; the verifier requires the current value only | stale facts must not be summarized as current |
| L5 | realistic 10+ file repository, multi-file change, public pytest | realistic workload horizon |
| L6 | realistic repository plus one deterministic provider context-overflow fault | compaction plus recovery interaction |

### 5.2 Pressure calibration gate (local, scripted model, ¥0)

- Every task crosses the 85% input-budget threshold between step 5 and step 7.
- Condition A loses at least one required fact or overflows on at least 3 of 6 tasks.
- All conditions: tool-pair integrity 100%, replay integrity 100%, no duplicate side effects.
- At most two task-definition revision rounds; a failed round invalidates previous pilot conclusions.

## 6. Metrics, success criteria, stop lines

### 6.1 Primary metric

```text
horizon_gain(X, task) = completed_tool_steps(X) - completed_tool_steps(A)
```

A Run that passes counts as completing its full step budget.

### 6.2 Secondary metrics

- Fact retention at matched step: required facts still visible/citable at `min(horizon(A), horizon(X))`.
- Provider-reported input tokens per completed step, summary calls included.
- Compaction success rate and fallback rate (D only).
- Summary factual-error rate (D only): contradictory, stale-as-current, or unsupported confirmed claims.
- Safety gates: replay integrity, tool-pair integrity, duplicate side effects, partial transactions.

### 6.3 Success criteria

1. Safety gates are 100%.
2. E or D has `horizon_gain >= +2` on at least 4 of 6 tasks.
3. The winning condition's fact retention at matched step is >= A.
4. The winning condition's tokens per completed step is <= 130% of A.

### 6.4 Stop lines

- Paid-model spend cap: **¥15** for the main experiment; stop immediately at the cap and report available data.
- Runner errors: at most 2 documented reruns; original failure evidence is preserved.
- If the local calibration gate fails after 2 rounds, freeze the suite as-is and report the limitation.
- If both E and D fail the success criteria, publish a negative result with a mechanism decomposition (range selection vs summary quality vs task sensitivity).
- No post-hoc task tuning after hashes are frozen.

## 7. Experiment protocol

### 7.1 Local pilot (no paid calls)

- 6 tasks x A/C/E/D x 1 repetition = 24 scripted-model runs.
- Validate pressure calibration, safety gates, and D fallback behavior.
- Freeze task/seeds/fixture/schedule hashes only after the pilot passes.

### 7.2 Paid main experiment

- Conditions: A/C/E/D, 6 tasks, 1 repetition = 24 runs.
- Provider/model: DeepSeek, model recorded in the manifest; temperature 0 for D summary calls.
- Balanced block order by (task, repetition).
- If the remaining budget allows, add a second A/D repetition (+12 runs) before analysis.
- All results go to a new unique directory `benchmarks/results/context-horizon-YYYY-MM-DD-HHMMSS/`.
- Cost monitor runs before each run and aborts at the ¥15 cap.

### 7.3 Product-level evidence (parallel, separate)

- Run 5 existing `real_model_tasks.json` tasks through the production CLI in `resilient` mode, one repetition, capped at ¥5.
- This is independent evidence that the product still works after deleting v1 code; it is not merged into the v3 condition comparison.

## 8. Size and test budgets

The v1 rewrite grew to 11,000+ lines. v3 is capped:

| Area | Line cap |
| --- | ---: |
| `apricity/context/` total | 1,500 |
| `apricity/evaluation/context_horizon/` total | 1,350 |
| `scripts/run_context_horizon_experiment.py` | 250 |
| New tests total | 1,500 |

If a milestone would exceed its cap, stop and cut scope rather than add more machinery.

## 9. Deletion plan executed in D1 (after approval)

1. `git rm` the 70 tracked v1 files listed in `artifacts/d0-dry-run-20260816-2155/git-rm-files.txt`.
2. Edit `README.md` to remove the entire Replayable Context Compaction section.
3. Restore `CONTEXT.md` to the recovery-harness glossary; add v3 terms only after v3 implementation stabilizes.
4. Adjust `.gitignore`: remove v1 doc exceptions, keep `tests/pytest/` and local result-directory ignores; add `!docs/experiments/` and `!docs/experiments/context-horizon-v3.md`.
5. Adjust `pyproject.toml`: remove the v1 fixture from `norecursedirs`.
6. Verification gate: compileall, full pytest, ruff, CLI `--help`, and the 90-run deterministic recovery experiment.
7. One commit including the HISTORY.md entry; no separate HISTORY-only commit.

## 10. Evidence and archive rules

- `benchmarks/results/**` is read-only evidence, including all `context-compaction-*` directories. Never delete, overwrite, rename, or rewrite them.
- New experiment results only under `benchmarks/results/context-horizon-*/`.
- Development debug products only under `artifacts/<task>/<unique-run>/`.
- Canonical paid runs require a clean `git status --short`.
- Raw prompts and hidden verifier payloads never enter model-visible prompts or reports; reports keep failure summaries bounded and redacted.

## 11. Reporting language

Allowed when successful:

> Under a fixed harness input budget, the [structural|chunked-LLM] context policy extended completed tool steps by X steps relative to the window control on Y of 6 tasks, with fact retention at matched step no lower than control and tokens per completed step at Z percent of control.

Required when mixed or negative:

> The experiment observed [no stable / a partial] horizon advantage. Results describe a measured trade-off or a negative result, not a universal context-compaction gain. The failure decomposition is reported in section X.

## 12. CONTEXT.md terms to add after implementation

Only these five, in the project's glossary format:

- **Horizon**
- **Fact Retention**
- **Structural Compaction**
- **Chunked Summary**
- **Oracle Leakage**

No ADR is created for v3 while it remains an evaluation-only boundary. If a v3 policy is later integrated into the production runtime, record that decision as ADR `0002-context-policy-in-runtime.md`.
