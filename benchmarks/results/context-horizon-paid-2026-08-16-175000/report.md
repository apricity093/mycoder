# Apricity Context Horizon Paid Experiment

- Experiment ID: context-horizon-paid-2026-08-16-175000
- Provider/model: deepseek / deepseek-v4-flash
- Commit: `2c8a966f756c67b48b601331f365f65fef21460d`
- Budget: 4096 harness tokens
- Total cost: ¥0.6147 (cap ¥15)
- Cache: hit 253312 / miss 62961 input tokens

## Conditions

| Condition | Run pass | Context pass | Artifact pass | Fact retention | Tool steps | Cost (¥) |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| A | 0/6 | 1/6 | 1/6 | 41.7% | 40 | 0.2109 |
| C | 1/6 | 6/6 | 1/6 | 100.0% | 28 | 0.1137 |
| D | 1/6 | 5/6 | 1/6 | 88.9% | 30 | 0.1165 |
| E | 3/6 | 5/6 | 3/6 | 94.4% | 39 | 0.1736 |

## Per-task run pass

| Task | A | C | D | E |
| --- | ---: | ---: | ---: | ---: |
| L1-incremental-contracts | F | P | P | P |
| L2-long-log-root-cause | F | F | F | P |
| L3-multi-round-test-fix | F | F | F | F |
| L4-stale-conflict | F | F | F | P |
| L5-realistic-multi-file | F | F | F | F |
| L6-overflow-recovery | F | F | F | F |

## Interpretation limits

- n = 1 run per (task, condition); no bootstrap intervals are claimed.
- `run_pass = context_pass AND artifact_pass AND mechanics_pass`.
- Artifact verifier checks the hidden required strings, not full product test suites.
- DeepSeek empty-content terminal responses are counted as `model_stop_failure`, not as strategy passes.
- Several `no_safe_range` rows occur after compaction could not reach the fixed budget; they retain fact integrity and fail loudly.

## Observed result

- A (window) loses early facts and passes 0/6 tasks.
- C/D/E preserve facts in 5/6 or 6/6 tasks; E passes 3/6 tasks at a lower cost than A.
- D/E did not beat A on the L3/L5/L6 artifact ceiling, which is dominated by model capability rather than context policy.
