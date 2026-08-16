"""Deterministic structural compression for Context Horizon v3.

This module starts as the tool-result pruner for condition C. The prefix-level
structural compactor for condition E will be added behind the same internal
seam later without changing the engine's external interface.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .ledger import sha256_text

PRUNING_POLICY_NAME = "structural-pruning"
PRUNING_POLICY_VERSION = "v1"

SUPPORTED_TOOLS = frozenset({"run_process", "run_shell", "read_file", "search", "list_files"})

_DIAGNOSTIC_PATTERN = re.compile(
    r"(?i)(traceback|error|failed|failure|exception|assert|warning)",
)
_PATH_LINE_PATTERN = re.compile(r"(?:^|\s)(?:\w:[\\/])?[A-Za-z0-9_./\\-]+\.(?:py|json|md|txt)(?::\d+)?")

HEAD_LINES = 6
TAIL_LINES = 6


@dataclass(frozen=True)
class PrunedResult:
    replacement: str
    original_chars: int
    retained_chars: int
    original_sha256: str
    policy: str = PRUNING_POLICY_NAME
    policy_version: str = PRUNING_POLICY_VERSION


def _validate_hints(content: str, metadata: dict) -> list[tuple[int, int]]:
    lines = content.splitlines()
    hints = []
    for hint in metadata.get("retention_hints", []) or []:
        if not isinstance(hint, dict):
            continue
        try:
            start = int(hint.get("start", 1)) - 1
            end = int(hint.get("end", start + 1))
        except (TypeError, ValueError):
            continue
        if 0 <= start < len(lines) and start <= end <= len(lines):
            hints.append((start, end))
    return hints


def _selected_lines(lines: list[str], metadata: dict) -> list[int]:
    selected: set[int] = set()
    for index in range(min(HEAD_LINES, len(lines))):
        selected.add(index)
    for index in range(max(0, len(lines) - TAIL_LINES), len(lines)):
        selected.add(index)
    for start, end in _validate_hints("\n".join(lines), metadata):
        selected.update(range(start, end))
    for index, line in enumerate(lines):
        if _DIAGNOSTIC_PATTERN.search(line) or _PATH_LINE_PATTERN.search(line):
            selected.add(index)
    return sorted(selected)


def prune_tool_result(
    *,
    tool_name: str,
    content: str,
    metadata: dict | None = None,
    soft_limit: int = 1600,
    minimum_gain: float = 0.10,
) -> PrunedResult | None:
    """Deterministically prune one oversized text-tool result.

    Returns ``None`` when the result is unsupported, already small enough, or
    the replacement would not reach the configured gain.
    """
    metadata = dict(metadata or {})
    if tool_name not in SUPPORTED_TOOLS:
        return None
    content = str(content)
    if len(content) <= soft_limit:
        return None

    lines = content.splitlines() or [""]
    selected = _selected_lines(lines, metadata)
    retained_blocks: list[str] = []
    previous = -2
    for index in selected:
        if index == previous + 1 and retained_blocks:
            retained_blocks[-1] += f"\n{lines[index]}"
        else:
            retained_blocks.append(lines[index])
        previous = index

    status = metadata.get("status", "")
    exit_code = metadata.get("exit_code")
    header_lines = [
        f"[pruned {tool_name} result]",
        f"original_chars={len(content)}",
        f"original_sha256={sha256_text(content)}",
    ]
    if status != "":
        header_lines.append(f"status={status}")
    if exit_code is not None:
        header_lines.append(f"exit_code={exit_code}")
    retained_text = "\n".join(retained_blocks)
    replacement = "\n".join(
        [
            *header_lines,
            "retained:",
            retained_text,
            f"[... {len(content) - len(retained_text)} chars omitted; source retained in ledger]",
        ]
    )
    if len(replacement) >= len(content):
        return None
    gain = 1.0 - (len(replacement) / len(content))
    if gain < minimum_gain:
        return None
    return PrunedResult(
        replacement=replacement,
        original_chars=len(content),
        retained_chars=len(replacement),
        original_sha256=sha256_text(content),
    )
