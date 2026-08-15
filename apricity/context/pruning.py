"""Deterministic pruning of built-in text-tool results.

v1 supports only built-in text tools: ``run_process``, ``run_shell``,
``read_file``, ``search``, and ``list_files``. Unknown tools and non-text
results MUST remain unchanged. Pruning is deterministic, idempotent, and
scope-limited; it shadows a source Surface node with a smaller replacement
without deleting the source event.
"""

from __future__ import annotations

import hashlib
import re

SUPPORTED_TOOLS = frozenset({"run_process", "run_shell", "read_file", "search", "list_files"})
PRUNING_POLICY_NAME = "deterministic-text-tool-pruning"
PRUNING_POLICY_VERSION = "v1"

HEAD_WINDOW = 12
TAIL_WINDOW = 12
CONTEXT_LINES = 3
MAX_ERROR_BLOCKS = 6
MAX_PATH_LINES = 8

# Diagnostic markers with bounded context lines around matches. Patterns are
# deliberately versioned and deterministic; they are not a language model.
DIAGNOSTIC_PATTERNS = (
    re.compile(r"Traceback \(most recent call last\)"),
    re.compile(r"^\s*(FAILED|FAIL|ERROR|PASSED|PASS|SKIPPED)\b", re.MULTILINE),
    re.compile(r"^\s*(?:E\s+|assert\s)"),  # pytest assertion failure lines
    re.compile(r"(?i)(error|exception|failed|failure)"),
)


def sha256_hex(text: str) -> str:
    return hashlib.sha256(str(text).encode("utf-8")).hexdigest()


def _split_codepoints(text: str) -> list[str]:
    # Operate on Unicode code points so slicing never splits surrogate pairs.
    return list(str(text))


def _codepoint_window(lines: list[str], start: int, end: int) -> list[str]:
    return [line for line in lines[start:end]]


def _bounded(lines: list[str], limit: int) -> list[str]:
    return lines[:limit]


def _head_tail(lines: list[str]) -> list[str]:
    head = _bounded(lines, HEAD_WINDOW)
    tail = _bounded(list(reversed(lines)), TAIL_WINDOW)
    tail = list(reversed(tail))
    if len(lines) <= HEAD_WINDOW + TAIL_WINDOW:
        return lines
    return [*head, f"... [{len(lines) - HEAD_WINDOW - TAIL_WINDOW} lines omitted] ...", *tail]


def _diagnostic_blocks(lines: list[str]) -> list[str]:
    kept: list[str] = []
    marked: list[int] = set()
    for index, line in enumerate(lines):
        if any(pattern.search(line) for pattern in DIAGNOSTIC_PATTERNS):
            for offset in range(-CONTEXT_LINES, CONTEXT_LINES + 1):
                marked.add(index + offset)
    for index, line in enumerate(lines):
        if index in marked:
            kept.append(line)
    return _bounded(kept, MAX_ERROR_BLOCKS * (2 * CONTEXT_LINES + 1))


def _path_lines(lines: list[str]) -> list[str]:
    kept = [line for line in lines if re.search(r"(?:[A-Za-z]:)?[\\/][\w.\-/\\]+(?:\.\w+)?(?::\d+)?", line)]
    return _bounded(kept, MAX_PATH_LINES)


def _final_summary(lines: list[str]) -> list[str]:
    kept = [
        line
        for line in lines
        if re.search(
            r"(?i)(passed|failed|error|summary|\d+ passed|\d+ failed|\d+ error)",
            line,
        )
    ]
    return _bounded(kept, 6)


def _retain_for_tool(tool_name: str, lines: list[str], hints: dict) -> list[str]:
    if tool_name in {"run_process", "run_shell"}:
        kept = []
        kept.extend(_diagnostic_blocks(lines))
        kept.extend(_path_lines(lines))
        kept.extend(_final_summary(lines))
        kept.extend(_head_tail(lines))
        return _dedupe_preserve_order(kept)
    if tool_name in {"read_file", "search", "list_files"}:
        kept = []
        kept.extend(_hint_lines(lines, hints))
        kept.extend(_head_tail(lines))
        return _dedupe_preserve_order(kept)
    return lines


def _hint_lines(lines: list[str], hints) -> list[str]:
    hints = hints or {}
    raw_hints = hints.get("retention_lines") or hints.get("lines") or []
    kept: list[str] = []
    for raw in raw_hints:
        try:
            start = int(raw)
        except (TypeError, ValueError):
            continue
        if start < 1 or start > len(lines):
            continue
        # Validate the range against actual content; invalid hints are ignored.
        if start <= len(lines):
            kept.append(lines[start - 1])
    return kept


def _dedupe_preserve_order(items):
    seen = set()
    result = []
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        result.append(item)
    return result


def prune_tool_result(
    *,
    tool_name: str,
    content: str,
    metadata: dict,
    soft_limit: int,
    minimum_gain: float,
) -> dict | None:
    """Return a pruned replacement payload, or None when pruning does not apply.

    The returned payload carries the tool identity, original hash, token
    counts, pruning policy identity, and a bounded notice identifying the
    source event. It does NOT mutate the ledger.
    """
    tool_name = str(tool_name)
    content = str(content or "")
    if tool_name not in SUPPORTED_TOOLS:
        return None
    if not content:
        return None
    if len(content) <= max(1, int(soft_limit)):
        return None

    lines = content.splitlines()
    if not lines:
        return None
    # Validate retention hints against actual content: ignore invalid ranges
    # rather than indexing outside the content.
    retained = _retain_for_tool(tool_name, lines, metadata or {})
    if not retained:
        return None
    # When the whole content is one long line (e.g. dense Unicode), fall back
    # to a bounded head/tail window so pruning can still produce a smaller
    # replacement without splitting a character. Slicing operates on Unicode
    # code points, so surrogate pairs are never split.
    if len(retained) == 1 and len(retained[0]) == len(content):
        soft = max(1, int(soft_limit))
        head = content[:soft]
        tail = content[len(content) - soft :]
        retained = [head, f"... [{len(content) - 2 * soft} chars omitted] ...", tail]
    replacement = "\n".join(retained)
    original_chars = len(content)
    replacement_chars = len(replacement)
    if replacement_chars >= original_chars:
        return None
    gain_ratio = 1.0 - (replacement_chars / original_chars if original_chars else 1.0)
    if gain_ratio < float(minimum_gain):
        return None

    return {
        "tool_name": tool_name,
        "call_id": str((metadata or {}).get("call_id", "")),
        "replacement": replacement,
        "original_content_sha256": sha256_hex(content),
        "replacement_content_sha256": sha256_hex(replacement),
        "original_chars": original_chars,
        "replacement_chars": replacement_chars,
        "gain_ratio": round(gain_ratio, 6),
        "pruning_policy_name": PRUNING_POLICY_NAME,
        "pruning_policy_version": PRUNING_POLICY_VERSION,
        "retained_line_count": len(retained),
        "original_line_count": len(lines),
        "retention": "diagnostic_blocks+paths+summary+head_tail"
        if tool_name in {"run_process", "run_shell"}
        else "hint_lines+head_tail",
        "pruned_notice": (
            f"[pruned-tool-result: {tool_name}] content was deterministically reduced "
            f"({original_chars} -> {replacement_chars} chars); source event retains the original."
        ),
    }
