"""Internal Surface retention planner.

These helpers decide which current Surface units may be removed and which
must be retained. They are private to ``apricity.context`` and are not
exported through ``apricity.context``.
"""

from __future__ import annotations

from .ledger import SurfaceNode, render_surface_node


def plan_units(surface: tuple[SurfaceNode, ...]) -> list[list[int]]:
    """Split the current Surface into atomic removal units.

    A completed tool call/result pair is one unit; each user or assistant
    message and each replacement node is its own unit.
    """
    units: list[list[int]] = []
    current: list[int] = []
    nodes = {node.seq: node for node in surface}
    for node in surface:
        if node.event_type == "tool/call":
            if current:
                units.append(current)
            current = [node.seq]
            continue
        if node.event_type in {"tool/result", "context/pruned-tool-result"} and current:
            call_node = nodes.get(current[0])
            if (
                call_node is not None
                and call_node.event_type == "tool/call"
                and call_node.data.get("call_id") == node.data.get("call_id")
            ):
                current.append(node.seq)
                units.append(current)
                current = []
                continue
        if node.event_type in {"context/pruned-tool-result", "context/summary"}:
            if current:
                units.append(current)
            units.append([node.seq])
            current = []
            continue
        if node.event_type == "user/message":
            if current:
                units.append(current)
            current = [node.seq]
            continue
        current.append(node.seq)
    if current:
        units.append(current)
    return units


def retained_tail_seqs(surface: tuple[SurfaceNode, ...], units: list[list[int]], steps: int) -> set[int]:
    if steps <= 0 or not units:
        return set()
    tail: set[int] = set()
    for unit in units[-steps:]:
        tail.update(unit)
    return tail


def request_user_seqs(surface: tuple[SurfaceNode, ...], user_message: str) -> set[int]:
    content = str(user_message)
    return {
        node.seq
        for node in surface
        if node.event_type == "user/message" and node.data.get("content") == content
    }


def is_removal_notice(surface: tuple[SurfaceNode, ...], unit: list[int]) -> bool:
    if len(unit) != 1:
        return False
    node = next((item for item in surface if item.seq == unit[0]), None)
    return bool(node and node.event_type == "context/summary" and node.data.get("kind") == "removal")


def remove_old_units(*, ledger, meter, policy, user_message: str, tokens: int) -> bool:
    """Remove the oldest safe atomic units until the request fits the budget."""
    units = plan_units(ledger.surface)
    if not units:
        return False
    retained_tail = retained_tail_seqs(ledger.surface, units, int(policy.retained_recent_steps))
    request_seqs = request_user_seqs(ledger.surface, user_message)
    removable = [
        unit
        for unit in units
        if not (set(unit) & retained_tail)
        and not (set(unit) & request_seqs)
        and not is_removal_notice(ledger.surface, unit)
    ]
    if not removable:
        return False
    target = int(policy.effective_input_budget * policy.pressure_threshold)
    new_tokens = tokens
    removed: list[list[int]] = []
    nodes = {node.seq: node for node in ledger.surface}
    for unit in removable:
        unit_tokens = sum(int(meter.count_text(render_surface_node(nodes[seq]))) for seq in unit if seq in nodes)
        new_tokens -= unit_tokens
        removed.append(unit)
        if new_tokens < target:
            break
    if not removed:
        return False
    for unit in removed:
        ledger.append(
            "context/summary",
            {
                "rendered": "[context-summary] older history units removed deterministically under token pressure.",
                "kind": "removal",
                "removed_unit_seqs": sorted(unit),
            },
            surface_op={"op": "replace", "start": unit[0], "end": unit[-1]},
            source_event_seqs=tuple(sorted(unit)),
        )
    return True
