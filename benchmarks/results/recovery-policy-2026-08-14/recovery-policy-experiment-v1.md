# Apricity Recovery Policy Experiment

- Captured at: 2026-08-14T04:01:05.787311+00:00
- Benchmark: `benchmarks\coding_tasks.json`
- Tasks: 3
- Faults: none, transient_provider, context_overflow, oversized_tool_result, malformed_response

## Results

| Variant | Pass rate | Recovery rate | False intervention | Duplicate side effects | Avg attempts | Avg duration |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| compatible | 80.00% | 75.00% | 0.00% | 0.00% | 2.20 | 2.316s |
| resilient | 100.00% | 100.00% | 0.00% | 0.00% | 2.60 | 2.581s |
| resilient_without_provider_retry | 80.00% | 75.00% | 0.00% | 0.00% | 2.20 | 2.607s |
| resilient_without_context_recovery | 80.00% | 75.00% | 0.00% | 0.00% | 2.20 | 2.407s |
| resilient_without_tool_result_spill | 100.00% | 100.00% | 0.00% | 0.00% | 2.60 | 2.703s |
| resilient_without_protocol_correction | 80.00% | 75.00% | 0.00% | 0.00% | 2.20 | 2.463s |

## Comparison

- Recovery-rate delta: +25.00%
- Pass-rate delta: +20.00%
- No-fault duration overhead: -9.30%

## Interpretation limits

- This is a deterministic fixture experiment, not a production reliability claim.
- A disabled policy is credited only when its ablation changes verifier-scored outcomes.
- The current oversized-output fixtures do not require tail retrieval, so the spill ablation does not change pass rate.
- Real-model confirmation is a separate, explicitly authorized run.
