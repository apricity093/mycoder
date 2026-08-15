"""Shared, versioned rendering from folded Surface nodes to dispatch prompts.

Both the context engines (which produce requests) and the verifier (which
reconstructs requests from durable ledger events) MUST use these functions so
a recomputed canonical hash matches the dispatched hash byte-for-byte. The
provider adapters receive the flat ``prompt`` and wrap it as a single
``user`` message, so replay verification operates on that flat prompt.
"""

from __future__ import annotations

import json

from .ledger import render_surface_node


def surface_to_messages(nodes, user_message=None) -> list[dict]:
    """Convert folded Surface nodes plus an optional trailing user request into
    the ordered message list the engine renders. ``user_message`` of None means
    no trailing user message is appended (surface-only rendering)."""
    messages = []
    for node in nodes:
        if node.event_type == "user/message":
            messages.append({"role": "user", "content": node.data.get("content", "")})
        elif node.event_type == "assistant/message":
            messages.append({"role": "assistant", "content": node.data.get("content", "")})
        elif node.event_type in {"tool/call", "tool/result"}:
            data = node.data
            if node.event_type == "tool/call":
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            f"[tool call {data.get('tool_name', '')} call_id={data.get('call_id', '')}] "
                            f"{json.dumps(data.get('args', {}), sort_keys=True, ensure_ascii=True)}"
                        ),
                    }
                )
            else:
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            f"[tool result {data.get('tool_name', '')} call_id={data.get('call_id', '')}]\n"
                            f"{data.get('content', '')}"
                        ),
                    }
                )
        elif node.event_type in {"context/pruned-tool-result", "context/summary"}:
            messages.append(
                {
                    "role": "user",
                    "content": render_surface_node(node),
                }
            )
    if user_message is not None:
        messages.append({"role": "user", "content": str(user_message)})
    return messages


def render_dispatch_prompt(
    system_prompt: str,
    memory_text: str,
    nodes,
    user_message: str,
) -> str:
    """Render the exact flat prompt the provider adapter receives."""
    messages = surface_to_messages(nodes, user_message)
    parts = [str(system_prompt), "Memory:", str(memory_text)]
    for message in messages:
        parts.append(f"[{message['role']}] {message['content']}")
    return "\n\n".join(parts).strip()
