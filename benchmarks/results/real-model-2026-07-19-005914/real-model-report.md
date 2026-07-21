# Apricity Real-model Benchmark

This report measures real provider behavior. It is separate from the deterministic harness regression.

## Run

- Provider: deepseek
- Model: deepseek-v4-flash
- Tasks: 10
- Repetitions: 3
- Total runs: 30

## Results

- Pass rate: 43.33%
- Pass@1: 40.00%
- Stable pass rate: 20.00%
- Within-budget rate: 46.67%
- Verifier pass rate: 46.67%
- Average tool steps: 3.53
- Average attempts: 4.00
- Average duration: 26.85s
- Recorded total tokens: 29143

## Categories

| Category | Runs | Pass rate | Avg tool steps | Avg duration |
| --- | ---: | ---: | ---: | ---: |
| bug-fix | 12 | 58.33% | 4.00 | 24.49s |
| context-reasoning | 3 | 100.00% | 6.67 | 28.24s |
| cross-file | 6 | 50.00% | 6.33 | 35.17s |
| robustness | 3 | 0.00% | 0.00 | 24.32s |
| test-driven | 6 | 0.00% | 0.00 | 23.83s |

## Failure categories

- failure_stop_reason: 1
- runner_error: 16

## Scope

These small repository tasks demonstrate end-to-end tool use and verifier-scored coding behavior. They are not a substitute for SWE-bench or production traffic evaluation.
