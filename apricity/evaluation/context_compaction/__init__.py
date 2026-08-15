"""Replayable context compaction evaluation package."""

from .metrics import aggregate_rows, condition_delta
from .report import render_report
from .runner import (
    MAIN_BUDGET,
    SENSITIVITY_BUDGET,
    ContextCompactionRunner,
    balanced_condition_order,
    build_schedule,
    condition_policies,
)
from .schema import TASK_SCHEMA_VERSION, canonical_hash, load_taskset
from .verifiers import verify_run

__all__ = [
    "MAIN_BUDGET",
    "SENSITIVITY_BUDGET",
    "TASK_SCHEMA_VERSION",
    "ContextCompactionRunner",
    "aggregate_rows",
    "balanced_condition_order",
    "build_schedule",
    "canonical_hash",
    "condition_delta",
    "condition_policies",
    "load_taskset",
    "render_report",
    "verify_run",
]
