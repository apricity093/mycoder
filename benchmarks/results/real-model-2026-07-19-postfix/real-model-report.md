# Apricity Real-model Benchmark

This report measures real provider behavior. It is separate from the deterministic harness regression.

## Run

- Provider: deepseek
- Model: deepseek-v4-flash
- Tasks: 10
- Repetitions: 3
- Total runs: 30

## Results

- Pass rate: 53.33%
- Pass@1: 50.00%
- Stable pass rate: 30.00%
- Within-budget rate: 86.67%
- Verifier pass rate: 80.00%
- Average tool steps: 7.27
- Average attempts: 8.03
- Average duration: 46.74s
- Recorded total tokens: 58284

## Categories

| Category | Runs | Pass rate | Avg tool steps | Avg duration |
| --- | ---: | ---: | ---: | ---: |
| bug-fix | 12 | 41.67% | 7.58 | 46.87s |
| context-reasoning | 3 | 100.00% | 6.33 | 31.56s |
| cross-file | 6 | 33.33% | 6.50 | 46.26s |
| robustness | 3 | 100.00% | 7.67 | 57.06s |
| test-driven | 6 | 50.00% | 7.67 | 49.41s |

## Failure categories

- failure_stop_reason: 8
- runner_error: 4
- verifier_failed: 2

## Scope

These small repository tasks demonstrate end-to-end tool use and verifier-scored coding behavior. They are not a substitute for SWE-bench or production traffic evaluation.
