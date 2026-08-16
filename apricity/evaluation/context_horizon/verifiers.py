"""Deterministic verifiers for Context Horizon v3 local runs."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from ...context import render_surface_node


def _norm(text: str) -> str:
    return str(text).casefold()


def context_verifier(task: dict[str, Any], ledger, hidden: dict[str, Any]) -> dict[str, Any]:
    """Check required facts against the current model-visible Surface.

    The policy never sees ``hidden``; only this verifier reads it.
    """
    surface_text = "\n".join(render_surface_node(node) for node in ledger.surface)
    norm_surface = _norm(surface_text)
    required = list(hidden.get("context_required") or [])
    forbidden = list(hidden.get("context_forbidden") or [])
    retained = [fact for fact in required if _norm(fact) in norm_surface]
    leaked = [fact for fact in forbidden if _norm(fact) in norm_surface]
    return {
        "task_id": task["id"],
        "required_facts": required,
        "retained_facts": retained,
        "forbidden_facts_hit": leaked,
        "fact_retention": len(retained) / len(required) if required else 1.0,
        "context_pass": bool(required) and not leaked and len(retained) == len(required),
    }


def artifact_verifier(task: dict[str, Any], workspace_root: str | Path, hidden: dict[str, Any]) -> dict[str, Any]:
    """Scan text artifacts for required/forbidden strings."""
    root = Path(workspace_root)
    text = ""
    for path in sorted(root.rglob("*")):
        if not path.is_file() or ".git" in path.parts or "__pycache__" in path.parts:
            continue
        try:
            text += "\n" + path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
    norm_text = _norm(text)
    required = list(hidden.get("artifact_required") or [])
    forbidden = list(hidden.get("artifact_forbidden") or [])
    found = [fact for fact in required if _norm(fact) in norm_text]
    leaked = [fact for fact in forbidden if _norm(fact) in norm_text]
    return {
        "task_id": task["id"],
        "artifact_required": required,
        "artifact_found": found,
        "artifact_forbidden_hit": leaked,
        "artifact_pass": bool(required) and not leaked and len(found) == len(required),
    }


def mechanics_verifier(task: dict[str, Any], ledger) -> dict[str, Any]:
    return {
        "task_id": task["id"],
        "tool_pairs_balanced": ledger.pairs_balanced(),
        "surface_generation": ledger.surface_generation,
        "event_count": len(ledger.events),
        "mechanics_pass": ledger.pairs_balanced(),
    }


def hidden_verifier_isolation(workspace_root: str | Path, hidden: dict[str, Any]) -> bool:
    """Confirm hidden verifier payload never landed in the agent workspace."""
    root = Path(workspace_root)
    secret = str(hidden.get("oracle_secret", ""))
    if not secret:
        return True
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        try:
            if secret in path.read_text(encoding="utf-8", errors="ignore"):
                return False
        except OSError:
            continue
    return True
