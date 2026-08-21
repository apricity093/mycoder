"""Pin the absence of centralized OpenTelemetry tracing / exporter.

mycoder has no OTEL dependency and no `mycoder/**` module imports opentelemetry.
Adding an OTEL exporter dependency (or an import) later must break this test so
the decision is revisited deliberately.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import mycoder


def test_opentelemetry_is_not_a_dependency():
    assert importlib.util.find_spec("opentelemetry") is None


def test_no_mycoder_module_imports_opentelemetry():
    root = Path(mycoder.__file__).resolve().parent
    offenders = [
        str(path.relative_to(root))
        for path in root.rglob("*.py")
        if "opentelemetry" in path.read_text(encoding="utf-8")
    ]
    assert offenders == []
