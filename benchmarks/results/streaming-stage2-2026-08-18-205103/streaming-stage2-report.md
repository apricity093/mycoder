# Apricity Streaming Stage 2 Matrix

PRD Stage 2 first-wave check: default streaming vs `--no-stream` on the same five tasks.

## Run

- Provider: deepseek
- Model: deepseek-v4-flash
- Commit: 9e85b00ad134ba5be0307ad7e8978505706b776a
- Tasks: 5 x 1 repetition per condition

## Results

| Metric | streaming | no-stream |
| --- | ---: | ---: |
| Pass rate | 80.00% | 80.00% |
| Avg duration (s) | 34.29 | 36.41 |
| Avg TTFT / model response (ms) | 1897.8 | 27118.4 |
| Provider error rate | 0.00% | 0.00% |
| Partial-visible rate | 0.00% | 0.00% |
| Cancelled runs | 0 | 0 |
| Avg tool steps | 8.80 | 8.60 |
| Duplicate side-effect rate | 0.00% | 0.00% |
| Input tokens | 9955 | 10196 |
| Output tokens | 1061 | 798 |
| Cached tokens | 5504 | 5120 |
| Cache-miss tokens | 4451 | 5076 |

## Scope

This is a small paid-provider first wave, not a production latency claim. Full acceptance targets and rollback rules live in `docs/requirements/streaming-output-prd.md`.
