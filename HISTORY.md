# chore: rebrand product identity to mycoder - 2026-08-21 17:55:00 +08:00

Rename every product-facing occurrence of the legacy product name (case-insensitive) to `mycoder` across the Python package, benchmarks, tests, docs, installer scripts, Makefile, package metadata and the TUI, including file and directory names. Regenerate TUI RPC types and the distribution bundle, keep third-party dependency names such as picocolors and picomatch intact, and update the public-tree check to allow HISTORY.md.

# Update - 2026-08-21 16:45:50 +08:00

Ensure onboarding subprocess output is decoded as UTF-8 and replaces invalid byte sequences, preventing locale-dependent decoding failures.
