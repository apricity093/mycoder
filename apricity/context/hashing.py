"""Canonical request hashing shared by all context engines.

The canonical hash must describe the EXACT redacted semantic request the
provider semantically receives. The provider adapters serialize the engine's flat
``prompt`` as a single ``user`` message plus sampling parameters; the dispatch
hash therefore covers that transport envelope (provider, model, temperature,
output cap, and the single user message containing the rendered prompt), not
an abstract system/messages/tools structure the provider never sees.
Timestamps, run IDs, transport headers, retry attempt numbers, and returned
usage are excluded by construction.
"""

from __future__ import annotations

import hashlib

from .ledger import canonical_json
from .types import ModelRequest


def canonical_request_sha256(request: ModelRequest) -> str:
    """Stable canonical hash of the structured request envelope.

    Retained for engine-internal token estimation parity and unit tests, but
    the dispatched-request hash used for replay is ``canonical_dispatch_sha256``
    over the normalized flat-prompt dispatch envelope.
    """
    payload = {
        "provider": request.provider,
        "model": request.model,
        "temperature": request.temperature,
        "max_output_tokens": request.max_output_tokens,
        "system_prompt": request.system_prompt,
        "messages": [dict(message) for message in request.messages],
        "tools": [dict(tool) for tool in request.tools],
    }
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def canonical_dispatch_sha256(
    *,
    provider: str,
    model: str,
    temperature: float,
    max_output_tokens: int,
    prompt: str,
) -> str:
    """Hash of the normalized semantic envelope dispatched to the provider.

    ``prompt`` is the flattened, redacted text the provider adapter receives;
    the adapter wraps it as one ``user`` message with the configured sampling
    parameters. Provider-specific field names and transport controls are
    normalized away, while every model-visible byte remains covered so replay
    integrity can recompute the same semantic request from durable state.
    """
    payload = {
        "provider": str(provider),
        "model": str(model),
        "temperature": float(temperature),
        "max_output_tokens": int(max_output_tokens),
        "messages": [{"role": "user", "content": str(prompt)}],
    }
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()
