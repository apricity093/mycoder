# Resilient Harness Architecture

## Runtime flow

```text
tool returns raw result
  -> RecoveryEngine.finalize_tool_result
  -> bounded result or redacted spill reference
  -> history and trace

model request fails
  -> provider-neutral Failure
  -> RecoveryEngine.handle_failure
  -> retry model, rebuild context, or stop

model response is rejected
  -> protocol Failure
  -> RecoveryEngine.handle_failure
  -> correction notice or stop
```

`AgentLoop` applies decisions but does not select policies. `ToolExecutor` observes raw content before clipping. Provider adapters perform one transport attempt and raise `ProviderRequestError`; the harness owns the retry budget.

## Modes

- `compatible`: preserves the pre-experiment control behavior behind the seam.
- `resilient`: enforces global and per-policy budgets and enables spill/context recovery.
- ablation: resilient mode with one statically disabled policy.

Policies are statically assembled. They cannot call the model, execute tools, mutate approval decisions, or access the whole runtime object.

## Evidence

`task_state.json` and `report.json` include model attempts, recovery actions, recovery counts, mode, and disabled policies. `trace.jsonl` records `recovery_decided`, `recovery_intervened`, and `context_rebuilt` events. Experiment rows keep `fault_injection`, `recovered`, `stop_reason`, and `duplicate_side_effects` as separate fields.

Run deterministic ablations with:

```powershell
D:\anaconda3\envs\learn_claude\python.exe scripts\run_recovery_policy_experiment.py `
  --task-id readme_intro_locked `
  --task-id sample_beta_locked `
  --task-id sample_gamma_locked
```

Run an explicitly authorized real-model condition with:

```powershell
D:\anaconda3\envs\learn_claude\python.exe scripts\run_real_model_benchmark.py `
  --provider deepseek `
  --recovery-mode resilient `
  --fault transient_provider `
  --repetitions 3
```
