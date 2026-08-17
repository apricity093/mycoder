"""Deterministic structural compression for Context Horizon v3.

Condition C uses the per-result pruner below. Condition E additionally
compacts an old Surface prefix into one deterministic, source-cited capsule
without any model call.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .ledger import canonical_json, render_surface_node, sha256_text
from .planner import plan_units, request_user_seqs, retained_tail_seqs

PRUNING_POLICY_NAME = "structural-pruning"
PRUNING_POLICY_VERSION = "v1"
COMPACTION_POLICY_NAME = "structural-compaction"
COMPACTION_POLICY_VERSION = "v1"

SUPPORTED_TOOLS = frozenset({"run_process", "run_shell", "read_file", "search", "list_files"})

_DIAGNOSTIC_PATTERN = re.compile(
    r"(?i)(traceback|error|failed|failure|exception|assert|warning|required|must|authoritative)",
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


@dataclass(frozen=True)
class StructuralSummary:
    rendered: str
    source_event_seqs: tuple[int, ...]
    source_chars: int
    replacement_chars: int
    source_tokens: int
    replacement_tokens: int
    gain_ratio: float
    replacement_sha256: str
    policy: str = COMPACTION_POLICY_NAME
    policy_version: str = COMPACTION_POLICY_VERSION


def _bounded(text: str, limit: int) -> str:
    text = str(text)
    if len(text) <= limit:
        return text
    return text[: limit - 3] + f"...[{len(text) - limit + 3} chars omitted]"


def _result_excerpt(content: str, metadata: dict | None = None, max_chars: int = 600) -> str:
    content = str(content)
    if len(content) <= max_chars:
        return content
    lines = content.splitlines() or [""]
    selected = _selected_lines(lines, dict(metadata or {}))
    blocks: list[str] = []
    previous = -2
    for index in selected:
        if index == previous + 1 and blocks:
            blocks[-1] += f"\n{lines[index]}"
        else:
            blocks.append(lines[index])
        previous = index
    excerpt = "\n".join(blocks)
    if len(excerpt) > max_chars:
        excerpt = _bounded(excerpt, max_chars)
    return excerpt + f"\n[... {len(content) - len(excerpt)} chars omitted; source retained in ledger]"


def build_structural_summary(ledger, range_seqs: list[int], *, meter=None) -> StructuralSummary | None:
    """Build a deterministic, source-cited capsule for one old Surface range.

    No model is called and no hidden predicate is consulted. User messages and
    diagnostic tool-result lines are retained; repetitive assistant prose and
    redundant output are dropped. Gain is token-based when a meter is supplied,
    matching the token-based gain used by chunked LLM compaction.
    """
    selected = set(int(seq) for seq in range_seqs)
    nodes = [node for node in ledger.surface if node.seq in selected]
    if not nodes:
        return None

    lines = ["Structural context summary (deterministic, source-cited):"]
    for node in nodes:
        seq = node.seq
        if node.event_type == "user/message":
            lines.append(f"- [seq {seq}] User requirement/instruction: {_bounded(str(node.data.get('content', '')), 600)}")
        elif node.event_type == "tool/call":
            args = _bounded(canonical_json(node.data.get("args", {})), 300)
            lines.append(f"- [seq {seq}] Tool call {node.data.get('name', '')}: {args}")
        elif node.event_type in {"tool/result", "context/pruned-tool-result"}:
            name = node.data.get("tool_name") or node.data.get("name", "")
            call_id = node.data.get("call_id", "")
            content = str(node.data.get("content", ""))
            metadata = dict(node.data.get("metadata") or {})
            if node.event_type == "context/pruned-tool-result":
                original_event = ledger.event_by_seq(node.seq)
                original_seqs = original_event.source_event_seqs
                if original_seqs:
                    original = ledger.event_by_seq(original_seqs[0])
                    content = str(original.data.get("content", ""))
                    metadata = dict(original.data.get("metadata") or {})
            excerpt = _result_excerpt(content, metadata)
            lines.append(f"- [seq {seq}] Tool result {name} (call {call_id}): {excerpt}")
        elif node.event_type == "context/summary":
            lines.append(f"- [seq {seq}] Prior summary: {_bounded(str(node.data.get('rendered', '')), 900)}")
        # assistant/message is intentionally omitted: tool calls already carry
        # their structured intent, and old prose is the least stable signal.

    rendered = "\n".join(lines)
    source_chars = sum(len(render_surface_node(node)) for node in nodes)
    if not rendered:
        return None
    if meter is None and len(rendered) >= source_chars:
        return None
    source_tokens = (
        sum(int(meter.count_text(render_surface_node(node))) for node in nodes)
        if meter is not None
        else max(1, source_chars)
    )
    replacement_tokens = int(meter.count_text(rendered)) if meter is not None else len(rendered)
    gain = 1.0 - (replacement_tokens / source_tokens if source_tokens else 0.0)
    return StructuralSummary(
        rendered=rendered,
        source_event_seqs=tuple(sorted(selected)),
        source_chars=source_chars,
        replacement_chars=len(rendered),
        source_tokens=source_tokens,
        replacement_tokens=replacement_tokens,
        gain_ratio=gain,
        replacement_sha256=sha256_text(rendered),
    )


def select_structural_range(units: list[list[int]], retained_tail: set[int], request_seqs: set[int]) -> list[int]:
    """Select the contiguous old prefix before the retained tail/current request."""
    protected = set(retained_tail) | set(request_seqs)
    prefix_units: list[list[int]] = []
    for unit in units:
        if set(unit) & protected:
            break
        prefix_units.append(unit)
    if not prefix_units:
        return []
    return [seq for unit in prefix_units for seq in unit]


def compact_structural_prefix(
    *,
    ledger,
    meter,
    policy,
    user_message: str,
    compactions_used: int,
) -> bool:
    """Commit one deterministic structural compaction transaction, or return False."""
    if compactions_used >= int(policy.max_compactions_per_run):
        return False
    units = plan_units(ledger.surface)
    retained_tail = retained_tail_seqs(ledger.surface, units, int(policy.retained_recent_steps))
    request_seqs = request_user_seqs(ledger.surface, user_message)
    range_seqs = select_structural_range(units, retained_tail, request_seqs)
    if not range_seqs:
        return False
    summary = build_structural_summary(ledger, range_seqs, meter=meter)
    if summary is None:
        return False
    source_tokens = summary.source_tokens
    replacement_tokens = summary.replacement_tokens
    if summary.gain_ratio < float(policy.minimum_compaction_gain):
        return False

    ledger.append(
        "compaction/start",
        {
            "compaction_id": f"structural-{ledger.surface_generation}",
            "policy": COMPACTION_POLICY_NAME,
            "policy_version": COMPACTION_POLICY_VERSION,
            "selected_surface_seqs": list(range_seqs),
            "source_token_count": source_tokens,
            "starting_surface_generation": ledger.surface_generation,
        },
    )
    ledger.append(
        "context/summary",
        {
            "kind": "structural",
            "rendered": summary.rendered,
            "policy": COMPACTION_POLICY_NAME,
            "policy_version": COMPACTION_POLICY_VERSION,
            "source_token_count": source_tokens,
            "replacement_token_count": replacement_tokens,
            "gain_ratio": round(summary.gain_ratio, 6),
            "replacement_sha256": summary.replacement_sha256,
        },
        surface_op={"op": "replace", "start": range_seqs[0], "end": range_seqs[-1]},
        source_event_seqs=summary.source_event_seqs,
    )
    ledger.append(
        "compaction/end",
        {
            "compaction_id": f"structural-{ledger.surface_generation - 1}",
            "success": True,
            "terminal_phase": "committed",
            "policy": COMPACTION_POLICY_NAME,
            "policy_version": COMPACTION_POLICY_VERSION,
            "token_gain": source_tokens - replacement_tokens,
            "gain_ratio": round(summary.gain_ratio, 6),
        },
    )
    return True
