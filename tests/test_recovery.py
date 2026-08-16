import json

from apricity import Apricity, FakeModelClient, SessionStore, WorkspaceContext
from apricity.recovery import (
    DECISION_CORRECT_PROTOCOL,
    DECISION_REBUILD_CONTEXT,
    DECISION_RETRY_MODEL,
    DECISION_STOP,
    FAILURE_CONTEXT_OVERFLOW,
    FAILURE_PROTOCOL,
    FAILURE_TRANSIENT_PROVIDER,
    Failure,
    POLICY_CONTEXT_RECOVERY,
    POLICY_PROTOCOL_CORRECTION,
    POLICY_PROVIDER_RETRY,
    ProviderRequestError,
    RecoveryEngine,
    RunSnapshot,
)
from apricity.evaluation.recovery_experiment import run_recovery_policy_experiment


def snapshot(**overrides):
    values = {
        "run_id": "run_test",
        "phase": "model_request_failed",
        "model_attempts": 1,
        "tool_steps": 0,
        "recovery_actions": 0,
        "recovery_counts": {},
    }
    values.update(overrides)
    return RunSnapshot(**values)


def build_agent(tmp_path, model_client, **kwargs):
    (tmp_path / "README.md").write_text("demo\n", encoding="utf-8")
    return Apricity(
        model_client=model_client,
        workspace=WorkspaceContext.build(tmp_path, repo_root_override=tmp_path),
        session_store=SessionStore(tmp_path / ".apricity" / "sessions"),
        approval_policy="auto",
        recovery_mode="resilient",
        recovery_backoff=(0, 0),
        **kwargs,
    )


class FaultModelClient(FakeModelClient):
    def __init__(self, outputs, failures):
        super().__init__(outputs)
        self.failures = list(failures)

    def complete(self, prompt, max_new_tokens, **kwargs):
        self.prompts.append(prompt)
        if self.failures:
            raise self.failures.pop(0)
        if not self.outputs:
            raise RuntimeError("fake model ran out of outputs")
        return self.outputs.pop(0)


def test_recovery_engine_enforces_each_policy_budget_and_global_budget():
    engine = RecoveryEngine(mode="resilient", backoff=(0, 0))

    retry = engine.handle_failure(
        Failure(FAILURE_TRANSIENT_PROVIDER, "timeout"),
        snapshot(),
    )
    exhausted = engine.handle_failure(
        Failure(FAILURE_TRANSIENT_PROVIDER, "timeout"),
        snapshot(recovery_actions=2, recovery_counts={POLICY_PROVIDER_RETRY: 2}),
    )
    context = engine.handle_failure(
        Failure(FAILURE_CONTEXT_OVERFLOW, "too long"),
        snapshot(recovery_counts={POLICY_CONTEXT_RECOVERY: 1}),
    )
    correction = engine.handle_failure(
        Failure(FAILURE_PROTOCOL, "malformed"),
        snapshot(recovery_counts={POLICY_PROTOCOL_CORRECTION: 1}),
    )
    global_stop = engine.handle_failure(
        Failure(FAILURE_PROTOCOL, "malformed"),
        snapshot(recovery_actions=4),
    )

    assert retry.action == DECISION_RETRY_MODEL
    assert exhausted.action == DECISION_STOP
    assert context.action == DECISION_REBUILD_CONTEXT
    assert correction.action == DECISION_CORRECT_PROTOCOL
    assert global_stop.action == DECISION_STOP


def test_agent_recovers_from_two_transient_provider_failures(tmp_path):
    failures = [
        ProviderRequestError("timeout", kind=FAILURE_TRANSIENT_PROVIDER),
        ProviderRequestError("429", kind=FAILURE_TRANSIENT_PROVIDER, status_code=429),
    ]
    agent = build_agent(tmp_path, FaultModelClient(["<final>Recovered.</final>"], failures))

    assert agent.ask("Finish") == "Recovered."
    assert agent.current_task_state.attempts == 3
    assert agent.current_task_state.recovery_counts == {POLICY_PROVIDER_RETRY: 2}

    trace = [
        json.loads(line)
        for line in agent.run_store.trace_path(agent.current_task_state).read_text(encoding="utf-8").splitlines()
    ]
    decisions = [event for event in trace if event.get("event") == "recovery_decided"]
    assert [event["decision"] for event in decisions] == [DECISION_RETRY_MODEL, DECISION_RETRY_MODEL]


def test_agent_rebuilds_context_after_explicit_overflow(tmp_path):
    failure = ProviderRequestError("context too long", kind=FAILURE_CONTEXT_OVERFLOW, status_code=400)
    agent = build_agent(tmp_path, FaultModelClient(["<final>Recovered context.</final>"], [failure]))
    for index in range(12):
        agent.record(
            {
                "role": "user" if index % 2 == 0 else "assistant",
                "content": f"history-{index}-" + ("x" * 700),
                "created_at": f"2026-08-14T10:{index:02d}:00+08:00",
            }
        )

    assert agent.ask("Finish after shrinking context") == "Recovered context."
    assert agent.current_task_state.recovery_counts == {POLICY_CONTEXT_RECOVERY: 1}
    assert len(agent.model_client.prompts[1]) <= len(agent.model_client.prompts[0]) * 0.8


def test_large_tool_result_is_spilled_redacted_and_retrievable(tmp_path, monkeypatch):
    monkeypatch.setenv("RECOVERY_TEST_SECRET", "secret-value-123")
    agent = build_agent(
        tmp_path,
        FakeModelClient(
            [
                '<tool>{"name":"read_file","args":{"path":"README.md","start":1,"end":1}}</tool>',
                "<final>Done.</final>",
            ]
        ),
        secret_env_names={"RECOVERY_TEST_SECRET"},
    )
    agent.tools["read_file"]["run"] = lambda args: "secret-value-123\n" + ("z" * 6000)

    assert agent.ask("Read the large result") == "Done."
    tool_event = next(item for item in agent.session["history"] if item["role"] == "tool")
    spill_id = agent._last_tool_result_metadata["spill_id"]
    spill_path = agent.current_run_dir / "spills" / f"{spill_id}.txt"

    assert "tool result spilled" in tool_event["content"]
    assert spill_path.is_file()
    assert "secret-value-123" not in spill_path.read_text(encoding="utf-8")
    assert "<redacted>" in agent.read_spill(spill_id, page=1)
    assert agent.current_task_state.recovery_actions == 1


def test_protocol_correction_stops_after_two_interventions(tmp_path):
    agent = build_agent(tmp_path, FakeModelClient(["", "", ""]))

    answer = agent.ask("Return a valid answer")

    assert answer == "Stopped by recovery policy: protocol_correction_exhausted."
    assert agent.current_task_state.stop_reason == "protocol_correction_exhausted"
    assert agent.current_task_state.recovery_counts == {POLICY_PROTOCOL_CORRECTION: 2}


def test_recovery_experiment_separates_faults_interventions_and_outcomes(tmp_path):
    result = run_recovery_policy_experiment(
        benchmark_path="benchmarks/coding_tasks.json",
        artifact_path=tmp_path / "recovery.json",
        workspace_root=tmp_path / "workspaces",
        task_ids=["readme_intro_locked"],
        variants=("compatible", "resilient"),
    )

    assert (tmp_path / "recovery.json").is_file()
    assert set(result["variants"]) == {"compatible", "resilient"}
    resilient_rows = result["variants"]["resilient"]["rows"]
    transient = next(row for row in resilient_rows if row["fault_type"] == "transient_provider")
    context = next(row for row in resilient_rows if row["fault_type"] == "context_overflow")
    spill = next(row for row in resilient_rows if row["fault_type"] == "oversized_tool_result")

    assert transient["fault_injection"]["injected"] is True
    assert transient["recovered"] is True
    assert transient["recovery_counts"][POLICY_PROVIDER_RETRY] == 1
    assert context["recovery_counts"][POLICY_CONTEXT_RECOVERY] == 1
    assert spill["fault_injection"]["events"][0]["injection_point"] == "tool_result"
