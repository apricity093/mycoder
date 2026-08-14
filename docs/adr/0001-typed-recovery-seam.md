---
status: accepted
---

# Use a typed recovery seam instead of a general event bus

Apricity exposes recovery through a deep `RecoveryEngine` module with two operations and three lifecycle points. A generic event bus or a Cordis-style plugin runtime would offer more extension surface, but would make ordering, safety precedence, and test contracts implicit; static typed decisions keep the small Python harness auditable while still allowing compatibility, resilient, and ablation adapters without editing `AgentLoop`.
