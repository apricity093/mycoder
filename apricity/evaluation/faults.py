"""Deterministic fault injection for recovery experiments."""

from dataclasses import dataclass

from ..recovery import (
    FAILURE_CONTEXT_OVERFLOW,
    FAILURE_TRANSIENT_PROVIDER,
    ProviderRequestError,
)


FAULT_NONE = "none"
FAULT_TRANSIENT_PROVIDER = "transient_provider"
FAULT_CONTEXT_OVERFLOW = "context_overflow"
FAULT_MALFORMED_RESPONSE = "malformed_response"
FAULT_OVERSIZED_TOOL_RESULT = "oversized_tool_result"
FAULT_KINDS = {
    FAULT_NONE,
    FAULT_TRANSIENT_PROVIDER,
    FAULT_CONTEXT_OVERFLOW,
    FAULT_MALFORMED_RESPONSE,
    FAULT_OVERSIZED_TOOL_RESULT,
}


@dataclass(frozen=True)
class FaultPlan:
    kind: str = FAULT_NONE
    inject_on_attempt: int = 1
    payload_chars: int = 6000

    def __post_init__(self):
        if self.kind not in FAULT_KINDS:
            raise ValueError(f"unknown fault kind: {self.kind}")
        if int(self.inject_on_attempt) < 1:
            raise ValueError("inject_on_attempt must be positive")
        if int(self.payload_chars) < 1:
            raise ValueError("payload_chars must be positive")


class FaultInjectingModelClient:
    def __init__(self, delegate, plan):
        self.delegate = delegate
        self.plan = plan
        self.attempts = 0
        self.injected = False
        self.events = []

    def __getattr__(self, name):
        return getattr(self.delegate, name)

    def complete(self, prompt, max_new_tokens, **kwargs):
        self.attempts += 1
        if not self.injected and self.attempts == int(self.plan.inject_on_attempt):
            if self.plan.kind == FAULT_TRANSIENT_PROVIDER:
                self._record("model_request", {"status_code": 503})
                raise ProviderRequestError(
                    "injected provider HTTP 503",
                    kind=FAILURE_TRANSIENT_PROVIDER,
                    status_code=503,
                )
            if self.plan.kind == FAULT_CONTEXT_OVERFLOW:
                self._record("model_request", {"status_code": 400})
                raise ProviderRequestError(
                    "injected maximum context length exceeded",
                    kind=FAILURE_CONTEXT_OVERFLOW,
                    status_code=400,
                )
            if self.plan.kind == FAULT_MALFORMED_RESPONSE:
                self._record("model_response", {"payload": "<tool>malformed"})
                return "<tool>malformed"
        return self.delegate.complete(prompt, max_new_tokens, **kwargs)

    def _record(self, injection_point, payload):
        self.injected = True
        self.events.append(
            {
                "fault_type": self.plan.kind,
                "injection_point": injection_point,
                "injection_attempt": self.attempts,
                "injected_payload": dict(payload),
            }
        )


class ToolOutputFaultInjector:
    def __init__(self, plan):
        self.plan = plan
        self.injected = False
        self.events = []

    def wrap_registry(self, tools):
        if self.plan.kind != FAULT_OVERSIZED_TOOL_RESULT:
            return
        for definition in tools.values():
            original = definition["run"]

            def wrapped(args, original=original):
                content = str(original(args))
                if self.injected:
                    return content
                self.injected = True
                self.events.append(
                    {
                        "fault_type": self.plan.kind,
                        "injection_point": "tool_result",
                        "injection_attempt": 1,
                        "injected_payload": {"payload_chars": int(self.plan.payload_chars)},
                    }
                )
                return content + "\n" + ("X" * int(self.plan.payload_chars))

            definition["run"] = wrapped


def inject_context_pressure(agent, plan):
    if plan.kind != FAULT_CONTEXT_OVERFLOW:
        return
    for index in range(12):
        agent.record(
            {
                "role": "user" if index % 2 == 0 else "assistant",
                "content": f"fault-pressure-{index}-" + ("P" * 700),
                "created_at": f"2026-08-14T12:{index:02d}:00+08:00",
            }
        )
