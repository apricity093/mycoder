# Data Provenance

- Captured: 2026-08-14
- Runtime: deterministic scripted model; no external provider calls
- Tasks: `readme_intro_locked`, `sample_beta_locked`, `sample_gamma_locked`
- Conditions: no fault, transient provider error, context overflow, oversized tool result, malformed response
- Variants: compatibility, resilient, and four single-policy ablations
- Total runs: 90

Command:

```powershell
D:\anaconda3\envs\learn_claude\python.exe scripts\run_recovery_policy_experiment.py `
  --task-id readme_intro_locked `
  --task-id sample_beta_locked `
  --task-id sample_gamma_locked `
  --output artifacts\recovery-policy-experiment-v1.json `
  --workspace-root artifacts\recovery-policy-workspaces
```

The fixture experiment validates deterministic control-flow causality. It is not a real-model or production reliability result. The current oversized-output tasks do not require tail retrieval, so the spill ablation does not change verifier pass rate.
