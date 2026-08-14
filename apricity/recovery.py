"""Typed recovery decisions for model, protocol, and tool-result failures."""

from dataclasses import dataclass, field
import hashlib
from pathlib import Path
import re

from .workspace import MAX_TOOL_OUTPUT, clip


FAILURE_TRANSIENT_PROVIDER = "transient_provider"
FAILURE_CONTEXT_OVERFLOW = "context_overflow"
FAILURE_PERMANENT_PROVIDER = "permanent_provider"
FAILURE_UNKNOWN_PROVIDER = "unknown_provider"
FAILURE_PROTOCOL = "protocol_error"

DECISION_RETRY_MODEL = "retry_model"
DECISION_REBUILD_CONTEXT = "rebuild_context"
DECISION_CORRECT_PROTOCOL = "correct_protocol"
DECISION_STOP = "stop"

RECOVERY_MODE_COMPATIBLE = "compatible"
RECOVERY_MODE_RESILIENT = "resilient"
RECOVERY_MODES = {RECOVERY_MODE_COMPATIBLE, RECOVERY_MODE_RESILIENT}

POLICY_PROVIDER_RETRY = "provider_retry"
POLICY_CONTEXT_RECOVERY = "context_recovery"
POLICY_PROTOCOL_CORRECTION = "protocol_correction"
POLICY_TOOL_RESULT_SPILL = "tool_result_spill"


class ProviderRequestError(RuntimeError):
    """Provider-neutral error raised by model adapters."""

    def __init__(self, message, *, kind=FAILURE_UNKNOWN_PROVIDER, status_code=None, retry_after=None):
        super().__init__(str(message))
        self.kind = str(kind)
        self.status_code = status_code
        self.retry_after = retry_after


@dataclass(frozen=True)
class Failure:
    kind: str
    message: str
    error_code: str = ""
    status_code: int | None = None
    retry_after: float | None = None


@dataclass(frozen=True)
class RunSnapshot:
    run_id: str
    phase: str
    model_attempts: int
    tool_steps: int
    recovery_actions: int
    recovery_counts: dict
    prompt_chars: int = 0
    prompt_budget: int = 0
    finalization_only: bool = False

    @property
    def remaining_recovery_budget(self):
        return max(0, 4 - int(self.recovery_actions))


@dataclass(frozen=True)
class RecoveryDecision:
    action: str
    reason: str
    policy: str = ""
    notice: str = ""
    delay_seconds: float = 0.0
    intervention: bool = False
    details: dict = field(default_factory=dict)


@dataclass(frozen=True)
class FinalizedToolResult:
    content: str
    metadata: dict
    policy: str = ""
    intervention: bool = False


@dataclass(frozen=True)
class ToolResultCandidate:
    content: str
    metadata: dict


def failure_from_exception(error):
    if isinstance(error, ProviderRequestError):
        return Failure(
            kind=error.kind,
            message=str(error),
            status_code=error.status_code,
            retry_after=error.retry_after,
        )
    return Failure(kind=FAILURE_UNKNOWN_PROVIDER, message=f"{type(error).__name__}: {error}")


def provider_http_error(provider, status_code, body="", retry_after=None):
    status_code = int(status_code)
    body = str(body or "")
    normalized = body.casefold().replace("-", "_")
    context_markers = (
        "context_length",
        "context length",
        "maximum context",
        "too many tokens",
        "prompt is too long",
    )
    if status_code in {400, 413} and any(marker in normalized for marker in context_markers):
        kind = FAILURE_CONTEXT_OVERFLOW
    elif status_code in {408, 429} or status_code >= 500:
        kind = FAILURE_TRANSIENT_PROVIDER
    else:
        kind = FAILURE_PERMANENT_PROVIDER
    return ProviderRequestError(
        f"{provider} request failed with HTTP {status_code}: {body}",
        kind=kind,
        status_code=status_code,
        retry_after=retry_after,
    )


class SpillStore:
    ID_PATTERN = re.compile(r"^[a-f0-9]{16}$")

    def __init__(self, run_dir, redactor):
        self.run_dir = Path(run_dir).resolve()
        self.root = self.run_dir / "spills"
        self.redactor = redactor

    def write(self, content):
        redacted = str(self.redactor(str(content)))
        spill_id = hashlib.sha256(redacted.encode("utf-8")).hexdigest()[:16]
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / f"{spill_id}.txt").write_text(redacted, encoding="utf-8")
        return spill_id, redacted

    def read(self, spill_id, page=1, page_size=MAX_TOOL_OUTPUT):
        spill_id = str(spill_id).strip().casefold()
        if not self.ID_PATTERN.fullmatch(spill_id):
            raise ValueError("invalid spill id")
        page = int(page)
        if page < 1 or page > 3:
            raise ValueError("spill page must be between 1 and 3")
        path = (self.root / f"{spill_id}.txt").resolve()
        if path.parent != self.root.resolve() or not path.is_file():
            raise ValueError("spill does not exist in the current run")
        text = path.read_text(encoding="utf-8", errors="replace")
        start = (page - 1) * int(page_size)
        if start >= len(text):
            return f"spill {spill_id} page {page}: (empty)"
        end = min(len(text), start + int(page_size))
        return f"spill {spill_id} page {page} chars {start + 1}-{end}/{len(text)}\n{text[start:end]}"


class RecoveryEngine:
    """Own recovery budgets and hide policy selection behind two operations."""

    def __init__(self, *, mode=RECOVERY_MODE_COMPATIBLE, backoff=(0.5, 1.0), disabled_policies=()):
        mode = str(mode).strip().casefold()
        if mode not in RECOVERY_MODES:
            raise ValueError(f"unknown recovery mode: {mode}")
        self.mode = mode
        self.backoff = tuple(float(value) for value in backoff)
        self.disabled_policies = frozenset(str(value) for value in disabled_policies)

    def handle_failure(self, failure, snapshot):
        if snapshot.finalization_only:
            return RecoveryDecision(DECISION_STOP, "finalization_only")

        if failure.kind == FAILURE_TRANSIENT_PROVIDER:
            if POLICY_PROVIDER_RETRY in self.disabled_policies:
                return RecoveryDecision(DECISION_STOP, "provider_retry_disabled", POLICY_PROVIDER_RETRY)
            used = int(snapshot.recovery_counts.get(POLICY_PROVIDER_RETRY, 0))
            if used >= 2 or (self.mode == RECOVERY_MODE_RESILIENT and snapshot.remaining_recovery_budget <= 0):
                return RecoveryDecision(DECISION_STOP, "provider_retry_exhausted", POLICY_PROVIDER_RETRY)
            delay = failure.retry_after
            if delay is None:
                delay = self.backoff[min(used, len(self.backoff) - 1)] if self.backoff else 0.0
            return RecoveryDecision(
                DECISION_RETRY_MODEL,
                "transient_provider_error",
                POLICY_PROVIDER_RETRY,
                delay_seconds=max(0.0, min(float(delay), 2.0)),
                intervention=True,
            )

        if failure.kind == FAILURE_CONTEXT_OVERFLOW and self.mode == RECOVERY_MODE_RESILIENT:
            if POLICY_CONTEXT_RECOVERY in self.disabled_policies:
                return RecoveryDecision(DECISION_STOP, "context_recovery_disabled", POLICY_CONTEXT_RECOVERY)
            used = int(snapshot.recovery_counts.get(POLICY_CONTEXT_RECOVERY, 0))
            if used >= 2 or snapshot.remaining_recovery_budget <= 0:
                return RecoveryDecision(DECISION_STOP, "context_recovery_exhausted", POLICY_CONTEXT_RECOVERY)
            return RecoveryDecision(
                DECISION_REBUILD_CONTEXT,
                "provider_context_overflow",
                POLICY_CONTEXT_RECOVERY,
                intervention=True,
                details={"minimum_reduction_ratio": 0.20},
            )

        if failure.kind == FAILURE_PROTOCOL:
            if self.mode == RECOVERY_MODE_COMPATIBLE:
                return RecoveryDecision(
                    DECISION_CORRECT_PROTOCOL,
                    "compatibility_protocol_retry",
                    notice=failure.message,
                )
            if POLICY_PROTOCOL_CORRECTION in self.disabled_policies:
                return RecoveryDecision(DECISION_STOP, "protocol_correction_disabled", POLICY_PROTOCOL_CORRECTION)
            used = int(snapshot.recovery_counts.get(POLICY_PROTOCOL_CORRECTION, 0))
            if used >= 2 or snapshot.remaining_recovery_budget <= 0:
                return RecoveryDecision(DECISION_STOP, "protocol_correction_exhausted", POLICY_PROTOCOL_CORRECTION)
            return RecoveryDecision(
                DECISION_CORRECT_PROTOCOL,
                "recoverable_protocol_error",
                POLICY_PROTOCOL_CORRECTION,
                notice=failure.message,
                intervention=True,
            )

        return RecoveryDecision(DECISION_STOP, failure.kind or "unknown_provider_error")

    def finalize_tool_result(self, candidate, snapshot, *, run_dir=None, redactor=str):
        content = str(candidate.content)
        metadata = dict(candidate.metadata or {})
        if (
            self.mode != RECOVERY_MODE_RESILIENT
            or POLICY_TOOL_RESULT_SPILL in self.disabled_policies
            or len(content) <= MAX_TOOL_OUTPUT
            or run_dir is None
        ):
            return FinalizedToolResult(content=clip(content), metadata=metadata)

        try:
            spill_id, redacted = SpillStore(run_dir, redactor).write(content)
        except Exception as exc:
            metadata.update({"spill_status": "failed", "spill_error": f"{type(exc).__name__}: {exc}"})
            return FinalizedToolResult(content=clip(redactor(content)), metadata=metadata)

        header = (
            f"[tool result spilled: id={spill_id}, raw_chars={len(content)}]\n"
            f"Use read_spill with spill_id={spill_id} and page=1..3 for bounded retrieval.\n"
        )
        visible = header + clip(redacted, max(1, MAX_TOOL_OUTPUT - len(header)))
        metadata.update(
            {
                "spill_status": "stored",
                "spill_id": spill_id,
                "raw_result_chars": len(content),
                "visible_result_chars": len(visible),
            }
        )
        return FinalizedToolResult(
            content=visible,
            metadata=metadata,
            policy=POLICY_TOOL_RESULT_SPILL,
            intervention=True,
        )
