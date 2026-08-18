# Apricity Real-model Benchmark

This report measures real provider behavior. It is separate from the deterministic harness regression.

## Run

- Provider: deepseek
- Model: deepseek-v4-flash
- Tasks: 5
- Repetitions: 1
- Total runs: 5

## Results

- Pass rate: 80.00%
- Pass@1: 80.00%
- Stable pass rate: 80.00%
- Within-budget rate: 100.00%
- Verifier pass rate: 80.00%
- Average tool steps: 8.80
- Average attempts: 10.60
- Average duration: 34.29s
- Recorded total tokens: 11016

## Categories

| Category | Runs | Pass rate | Avg tool steps | Avg duration |
| --- | ---: | ---: | ---: | ---: |
| bug-fix | 4 | 100.00% | 8.00 | 28.62s |
| cross-file | 1 | 0.00% | 12.00 | 56.97s |

## Failure categories

- verifier_failed: 1

## Scope

These small repository tasks demonstrate end-to-end tool use and verifier-scored coding behavior. They are not a substitute for SWE-bench or production traffic evaluation.
