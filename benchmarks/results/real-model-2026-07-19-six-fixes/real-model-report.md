# Apricity Real-model Benchmark

This report measures real provider behavior. It is separate from the deterministic harness regression.

## Run

- Provider: deepseek
- Model: deepseek-v4-flash
- Tasks: 10
- Repetitions: 3
- Total runs: 30

## Results

- Pass rate: 96.67%
- Pass@1: 90.00%
- Stable pass rate: 90.00%
- Within-budget rate: 100.00%
- Verifier pass rate: 100.00%
- Average tool steps: 7.73
- Average attempts: 9.67
- Average duration: 52.04s
- Recorded total tokens: 67636

## Categories

| Category | Runs | Pass rate | Avg tool steps | Avg duration |
| --- | ---: | ---: | ---: | ---: |
| bug-fix | 12 | 100.00% | 6.25 | 35.53s |
| context-reasoning | 3 | 100.00% | 6.33 | 42.25s |
| cross-file | 6 | 100.00% | 10.17 | 72.15s |
| robustness | 3 | 100.00% | 8.33 | 69.71s |
| test-driven | 6 | 83.33% | 8.67 | 61.03s |

## Failure categories

- failure_stop_reason: 1

## Scope

These small repository tasks demonstrate end-to-end tool use and verifier-scored coding behavior. They are not a substitute for SWE-bench or production traffic evaluation.
