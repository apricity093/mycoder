"""M4 tests: context-compaction evaluation schema, verifiers, scheduling, resume."""

import json
from pathlib import Path

import pytest

from apricity.evaluation.context_compaction import (
    ContextCompactionRunner,
    balanced_condition_order,
    build_schedule,
    canonical_hash,
    condition_policies,
    load_taskset,
    verify_run,
)


def test_load_taskset_v2_requires_8_controlled_and_4_realistic():
    repo_root = Path(".").resolve()
    taskset = load_taskset(
        Path("benchmarks/context_compaction/tasks.v2.json"),
        repo_root=repo_root,
        seed_root=repo_root / "benchmarks" / "context_compaction" / "seeds",
    )

    assert taskset["schema_version"] == 2
    assert len(taskset["tasks"]) == 12
    assert sum(1 for task in taskset["tasks"] if task["task_style"] == "controlled") == 8
    assert sum(1 for task in taskset["tasks"] if task["task_style"] == "realistic") == 4
    assert taskset["task_snapshot_hash"].startswith("sha256:")


def test_load_taskset_rejects_unknown_condition_field(tmp_path):
    payload = json.loads(
        Path("benchmarks/context_compaction/tasks.v2.json").read_text(encoding="utf-8")
    )
    payload["tasks"][0]["condition"] = "D"
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="unknown keys"):
        load_taskset(
            bad,
            repo_root=Path(".").resolve(),
            seed_root=Path("benchmarks/context_compaction/seeds"),
        )


def test_condition_policies_validate_configuration():
    policies = condition_policies(4096)

    assert policies["A"].condition_id == "A"
    assert policies["B"].condition_id == "B"
    assert policies["C"].condition_id == "C"
    assert policies["D"].condition_id == "D"
    assert policies["D"].history_compaction is True
    assert policies["B"].token_budgeting is True


def test_balanced_schedule_produces_144_main_blocks():
    taskset = load_taskset(
        Path("benchmarks/context_compaction/tasks.v2.json"),
        repo_root=Path(".").resolve(),
        seed_root=Path("benchmarks/context_compaction/seeds"),
    )
    blocks, schedule_hash = build_schedule(taskset["tasks"], repetitions=3)

    assert len(blocks) == 144
    assert schedule_hash.startswith("sha256:")
    # Conditions approximately balanced across first/second/third/fourth
    # positions.
    positions = {"A": 0, "B": 0, "C": 0, "D": 0}
    for index, block in enumerate(blocks):
        if index % 4 == 0:
            positions[block["condition"]] += 1
    assert max(positions.values()) - min(positions.values()) <= 1


def test_sensitivity_schedule_adds_36_blocks():
    taskset = load_taskset(
        Path("benchmarks/context_compaction/tasks.v2.json"),
        repo_root=Path(".").resolve(),
        seed_root=Path("benchmarks/context_compaction/seeds"),
    )
    sens = ("C01-early-fact", "C04-stale-conflict", "C06-repeated-compaction", "C08-overflow-recovery", "R01-centralized-timeout-default", "R03-stable-json-report")
    blocks, _ = build_schedule(taskset["tasks"], repetitions=3, budgets=(4096, 8192), sensitivity_tasks=sens)

    assert len(blocks) == 180


def test_verify_run_three_layer_pass_and_failure_precedence():
    from apricity.context.hashing import canonical_dispatch_sha256
    from apricity.context.render import render_dispatch_prompt

    def node(event):
        return type("Node", (), {"seq": event["seq"], "event_type": event["type"], "data": event["data"]})()

    events = [
        {"seq": 0, "type": "user/message", "data": {"content": "task"}, "surface_op": "append"},
        {
            "seq": 1,
            "type": "tool/call",
            "data": {"tool_name": "run_process", "call_id": "c1", "args": {}},
            "surface_op": "append",
        },
        {
            "seq": 2,
            "type": "tool/result",
            "data": {"tool_name": "run_process", "call_id": "c1", "content": "evidence here"},
            "surface_op": "append",
        },
        {
            "seq": 3,
            "type": "request/header",
            "data": {
                "provider": "deepseek",
                "model": "deepseek-v4-flash",
                "temperature": 0.0,
                "max_output_tokens": 512,
                "system_prompt": "SYSTEM",
            },
            "surface_op": "append",
        },
        {
            "seq": 4,
            "type": "request/context",
            "data": {"memory": "Memory:\n- task: -"},
            "surface_op": "append",
        },
        {
            "seq": 5,
            "type": "request/dispatch",
            "data": {
                "header_seq": 3,
                "context_seq": 4,
                "surface_generation": 0,
                "surface_event_seqs": [0, 1, 2],
                "canonical_request_sha256": "",
            },
            "surface_op": "append",
        },
    ]
    surface = [node(e) for e in events[:3]]
    prompt = render_dispatch_prompt("SYSTEM", "Memory:\n- task: -", surface, "task")
    digest = canonical_dispatch_sha256(
        provider="deepseek", model="deepseek-v4-flash", temperature=0.0,
        max_output_tokens=512, prompt=prompt,
    )
    events[5]["data"]["canonical_request_sha256"] = digest

    result = verify_run(
        events=events,
        trace_events=[],
        surface_text="[user] task\n[tool] evidence here",
        user_request="task",
        critical_facts=[{"id": "f", "predicate": "evidence here", "contradiction_predicate": "missing"}],
        process_invariants=["tool_pairs_balanced", "current_request_preserved", "request_replayable"],
        artifact_result={"artifact_pass": True, "skipped": False},
    )

    assert result["artifact_pass"] is True
    assert result["context_pass"] is True
    assert result["trace_pass"] is True
    assert result["run_pass"] is True


def test_verify_run_artifact_failure_blocks_run_pass():
    from apricity.context.hashing import canonical_dispatch_sha256
    from apricity.context.render import render_dispatch_prompt

    def node(event):
        return type("Node", (), {"seq": event["seq"], "event_type": event["type"], "data": event["data"]})()

    events = [
        {"seq": 0, "type": "user/message", "data": {"content": "task"}, "surface_op": "append"},
        {
            "seq": 1,
            "type": "request/header",
            "data": {
                "provider": "deepseek",
                "model": "deepseek-v4-flash",
                "temperature": 0.0,
                "max_output_tokens": 512,
                "system_prompt": "SYSTEM",
            },
            "surface_op": "append",
        },
        {
            "seq": 2,
            "type": "request/context",
            "data": {"memory": "Memory:\n- task: -"},
            "surface_op": "append",
        },
        {
            "seq": 3,
            "type": "request/dispatch",
            "data": {
                "header_seq": 1,
                "context_seq": 2,
                "surface_generation": 0,
                "surface_event_seqs": [0],
                "canonical_request_sha256": "",
            },
            "surface_op": "append",
        },
    ]
    prompt = render_dispatch_prompt("SYSTEM", "Memory:\n- task: -", [node(events[0])], "task")
    digest = canonical_dispatch_sha256(
        provider="deepseek", model="deepseek-v4-flash", temperature=0.0,
        max_output_tokens=512, prompt=prompt,
    )
    events[3]["data"]["canonical_request_sha256"] = digest

    result = verify_run(
        events=events,
        trace_events=[],
        surface_text="[user] task",
        user_request="task",
        critical_facts=[{"id": "f", "predicate": "task", "contradiction_predicate": ""}],
        process_invariants=["tool_pairs_balanced", "current_request_preserved", "request_replayable"],
        artifact_result={"artifact_pass": False, "skipped": False},
    )

    assert result["artifact_pass"] is False
    assert result["context_pass"] is True
    assert result["trace_pass"] is True
    assert result["run_pass"] is False


def test_verify_run_skipped_artifact_dimension_does_not_fail_mechanics():
    events = [
        {"seq": 0, "type": "user/message", "data": {"content": "task"}, "surface_op": "append"},
    ]
    result = verify_run(
        events=events,
        trace_events=[],
        surface_text="[user] task",
        user_request="task",
        critical_facts=[{"id": "f", "predicate": "task", "contradiction_predicate": ""}],
        process_invariants=["tool_pairs_balanced"],
        artifact_result={"artifact_pass": None, "skipped": True},
    )

    assert result["artifact_pass"] is None
    assert result["artifact_skipped"] is True
    assert result["mechanics_pass"] is True
    assert result["run_pass"] is None


def test_verify_run_detects_orphan_tool_result():
    events = [
        {
            "seq": 0,
            "type": "tool/result",
            "data": {"tool_name": "run_process", "call_id": "orphan", "content": "x"},
            "surface_op": "append",
        }
    ]
    result = verify_run(
        events=events,
        trace_events=[],
        surface_text="[tool] x",
        user_request="task",
        critical_facts=[{"id": "f", "predicate": "x", "contradiction_predicate": "y"}],
        process_invariants=["tool_pairs_balanced"],
    )

    assert result["trace_pass"] is False
    assert result["run_pass"] is False


def test_verify_run_contradiction_without_authoritative_fails():
    events = [
        {
            "seq": 0,
            "type": "user/message",
            "data": {"content": "deployment key is red"},
            "surface_op": "append",
        }
    ]
    result = verify_run(
        events=events,
        trace_events=[],
        surface_text="[user] deployment key is red",
        user_request="task",
        critical_facts=[
            {"id": "color", "predicate": "deployment key color: blue", "contradiction_predicate": "deployment key is red"}
        ],
        process_invariants=[],
    )

    assert result["context_pass"] is False
    assert result["run_pass"] is False


def test_hidden_verifier_never_lands_in_workspace(tmp_path):
    repo_root = Path(".").resolve()
    hidden_root = repo_root / "benchmarks" / "context_compaction" / "hidden"
    runner = ContextCompactionRunner(
        taskset_path="benchmarks/context_compaction/tasks.v2.json",
        output_dir=tmp_path / "out",
        workspace_root=tmp_path / "ws",
        repetitions=1,
    )
    task = runner.tasks_by_id["C01-early-fact"]

    ws = tmp_path / "ws" / "copy"
    ws.mkdir(parents=True)
    (ws / "leaked.txt").write_text(
        json.dumps(runner.hidden_verifier(task), sort_keys=True), encoding="utf-8"
    )

    assert runner.hidden_verifier_isolated(task, ws) is False


def test_hidden_verifier_isolated_when_clean(tmp_path):
    repo_root = Path(".").resolve()
    runner = ContextCompactionRunner(
        taskset_path="benchmarks/context_compaction/tasks.v2.json",
        output_dir=tmp_path / "out",
        workspace_root=tmp_path / "ws",
        repetitions=1,
    )
    task = runner.tasks_by_id["C01-early-fact"]

    ws = tmp_path / "ws" / "clean"
    ws.mkdir(parents=True)
    (ws / "README.md").write_text("clean file\n", encoding="utf-8")

    assert runner.hidden_verifier_isolated(task, ws) is True


def test_resume_rejects_hash_mismatch(tmp_path):
    repo_root = Path(".").resolve()
    runner = ContextCompactionRunner(
        taskset_path="benchmarks/context_compaction/tasks.v2.json",
        output_dir=tmp_path / "out",
        workspace_root=tmp_path / "ws",
        repetitions=1,
    )
    runner.output_dir.mkdir(parents=True, exist_ok=True)
    (runner.output_dir / "manifest.json").write_text(
        json.dumps(
            {
                "task_snapshot_hash": "sha256:stale",
                "schedule_hash": "sha256:stale",
                "config_hash": "sha256:stale",
                "repetitions": 2,
                "budgets": [2048],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="cannot resume"):
        runner.validate_resume()


def test_deterministic_report_regeneration_is_stable(tmp_path):
    from apricity.evaluation.context_compaction import render_report

    payload = {
        "experiment_id": "context-compaction-2026-08-14-000000",
        "status": "completed",
        "task_snapshot_hash": "sha256:abc",
        "schedule_hash": "sha256:def",
        "aggregate": {
            "total_runs": 4,
            "completed_runs": 4,
            "runner_errors": 0,
            "pass_rate": 1.0,
            "completed_pass_rate": 1.0,
            "first_overflow_rate": 0.0,
            "overflow_recovery_rate": 0.0,
            "unrecovered_overflow_rate": 0.0,
            "critical_fact_retention_rate": 1.0,
            "summary_factual_error_rate": 0.0,
            "avg_tool_steps": 1.0,
            "avg_attempts": 1.0,
            "avg_input_tokens": 500.0,
            "avg_compactions": 0.0,
            "avg_latency_ms": 100.0,
            "failure_category_counts": {},
            "conditions": {
                "A": {
                    "runs": 1,
                    "pass_rate": 1.0,
                    "unrecovered_overflow_rate": 0.0,
                    "critical_fact_retention": 1.0,
                    "tool_pair_integrity_rate": 1.0,
                    "replay_integrity_rate": 1.0,
                },
                "D": {
                    "runs": 1,
                    "pass_rate": 1.0,
                    "unrecovered_overflow_rate": 0.0,
                    "critical_fact_retention": 1.0,
                    "tool_pair_integrity_rate": 1.0,
                    "replay_integrity_rate": 1.0,
                },
            },
        },
        "comparison": {"pass_rate_delta": 0.0, "unrecovered_overflow_delta": 0.0, "critical_fact_retention_delta": 0.0},
    }

    first = render_report(payload)
    second = render_report(payload)

    assert first == second
    assert "D vs A comparison" in first
    assert "Safety gates" in first


def _make_dispatch_events():
    """A minimal valid ledger with one dispatched request."""
    return [
        {"seq": 0, "type": "user/message", "data": {"content": "task"}, "surface_op": "append"},
        {
            "seq": 1,
            "type": "request/header",
            "data": {
                "provider": "deepseek",
                "model": "deepseek-v4-flash",
                "temperature": 0.0,
                "max_output_tokens": 512,
                "system_prompt": "SYSTEM",
            },
            "surface_op": "append",
        },
        {
            "seq": 2,
            "type": "request/context",
            "data": {"memory": "Memory:\n- task: -"},
            "surface_op": "append",
        },
        {
            "seq": 3,
            "type": "request/dispatch",
            "data": {
                "header_seq": 1,
                "context_seq": 2,
                "surface_generation": 0,
                "surface_event_seqs": [0],
                "canonical_request_sha256": "",
            },
            "surface_op": "append",
        },
    ]


def test_replay_integrity_accepts_consistent_dispatch():
    from apricity.context.hashing import canonical_dispatch_sha256
    from apricity.context.render import render_dispatch_prompt

    events = _make_dispatch_events()
    prompt = render_dispatch_prompt("SYSTEM", "Memory:\n- task: -", [_node(events[0])], "task")
    digest = canonical_dispatch_sha256(
        provider="deepseek", model="deepseek-v4-flash", temperature=0.0,
        max_output_tokens=512, prompt=prompt,
    )
    events[3]["data"]["canonical_request_sha256"] = digest

    from apricity.evaluation.context_compaction.verifiers import replay_integrity
    assert replay_integrity(events) is True


def test_replay_integrity_rejects_tampered_dispatch_hash():
    events = _make_dispatch_events()
    events[3]["data"]["canonical_request_sha256"] = "deadbeef" * 8

    from apricity.evaluation.context_compaction.verifiers import replay_integrity
    assert replay_integrity(events) is False


def test_replay_integrity_rejects_tampered_surface_event():
    from apricity.context.hashing import canonical_dispatch_sha256
    from apricity.context.render import render_dispatch_prompt

    events = _make_dispatch_events()
    prompt = render_dispatch_prompt("SYSTEM", "Memory:\n- task: -", [_node(events[0])], "task")
    digest = canonical_dispatch_sha256(
        provider="deepseek", model="deepseek-v4-flash", temperature=0.0,
        max_output_tokens=512, prompt=prompt,
    )
    events[3]["data"]["canonical_request_sha256"] = digest
    # Mutate the source user message AFTER dispatch: replay must now differ.
    events[0]["data"]["content"] = "tampered"

    from apricity.evaluation.context_compaction.verifiers import replay_integrity
    assert replay_integrity(events) is False


def test_replay_integrity_rejects_non_contiguous_seq():
    events = _make_dispatch_events()
    events[1]["seq"] = 99

    from apricity.evaluation.context_compaction.verifiers import replay_integrity
    assert replay_integrity(events) is False


def test_current_request_must_be_in_last_dispatch_surface():
    from apricity.evaluation.context_compaction.verifiers import current_request_preserved

    events = _make_dispatch_events()
    events[3]["data"]["surface_event_seqs"] = []  # current request dropped
    assert current_request_preserved(events, "task") is False

    events2 = _make_dispatch_events()
    assert current_request_preserved(events2, "task") is True


def _node(event):
    return type("Node", (), {"seq": event["seq"], "event_type": event["type"], "data": event["data"]})()


def test_artifact_verifier_requires_public_and_hidden_success(tmp_path):
    from apricity.evaluation.context_compaction.verifiers import artifact_verifier_pass

    ws = tmp_path / "repo"
    ws.mkdir()
    (ws / "centralized_timeout_default.py").write_text("DEFAULT_TIMEOUT = 120\n", encoding="utf-8")
    task = {"artifact_path": "centralized_timeout_default.py"}
    hidden = {"check": "timeout_source", "required_fact": "DEFAULT_TIMEOUT = 120"}

    ok = artifact_verifier_pass(
        workspace_copy_root=ws,
        task=task,
        hidden_verifier=hidden,
        public_verifier_result={"exit_code": 0, "stdout": "", "stderr": ""},
    )
    assert ok["artifact_pass"] is True

    public_fail = artifact_verifier_pass(
        workspace_copy_root=ws,
        task=task,
        hidden_verifier=hidden,
        public_verifier_result={"exit_code": 1, "stdout": "", "stderr": "failed"},
    )
    assert public_fail["artifact_pass"] is False
    assert public_fail["public_pass"] is False

    (ws / "centralized_timeout_default.py").write_text("DEFAULT_TIMEOUT = 30\n", encoding="utf-8")
    hidden_fail = artifact_verifier_pass(
        workspace_copy_root=ws,
        task=task,
        hidden_verifier=hidden,
        public_verifier_result={"exit_code": 0, "stdout": "", "stderr": ""},
    )
    assert hidden_fail["artifact_pass"] is False
    assert hidden_fail["hidden_pass"] is False


def test_artifact_verifier_rejects_required_fact_outside_artifact(tmp_path):
    from apricity.evaluation.context_compaction.verifiers import artifact_verifier_pass

    ws = tmp_path / "repo"
    ws.mkdir()
    # Fact present in a different file, but not in the declared artifact.
    (ws / "notes.txt").write_text("DEFAULT_TIMEOUT = 120\n", encoding="utf-8")
    (ws / "centralized_timeout_default.py").write_text("DEFAULT_TIMEOUT = 30\n", encoding="utf-8")
    task = {"artifact_path": "centralized_timeout_default.py"}
    hidden = {"check": "timeout_source", "required_fact": "DEFAULT_TIMEOUT = 120"}

    result = artifact_verifier_pass(
        workspace_copy_root=ws,
        task=task,
        hidden_verifier=hidden,
        public_verifier_result={"exit_code": 0, "stdout": "", "stderr": ""},
    )
    assert result["hidden_pass"] is False
    assert result["artifact_pass"] is False


def test_deepseek_wire_payload_maps_to_dispatch_hash_independently(monkeypatch):
    """Capture the adapter payload instead of reusing an engine renderer."""
    import hashlib
    import urllib.request

    from apricity.context.hashing import canonical_dispatch_sha256
    from apricity.providers.clients import DeepSeekModelClient

    captured = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return json.dumps(
                {
                    "choices": [{"message": {"content": "done"}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 7, "completion_tokens": 1, "total_tokens": 8},
                }
            ).encode("utf-8")

    def fake_urlopen(request, timeout):
        captured["url"] = request.full_url
        captured["timeout"] = timeout
        captured["payload"] = json.loads(request.data.decode("utf-8"))
        return Response()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    client = DeepSeekModelClient(
        model="deepseek-v4-flash",
        base_url="https://api.deepseek.com",
        api_key="test-key",
        temperature=0.0,
        timeout=17,
    )

    assert client.complete("SYSTEM\n\n[user] inspect", 512) == "done"
    wire = captured["payload"]
    assert wire == {
        "model": "deepseek-v4-flash",
        "messages": [{"role": "user", "content": "SYSTEM\n\n[user] inspect"}],
        "max_tokens": 512,
        "stream": False,
        "temperature": 0.0,
    }

    # Normalize the captured provider payload into the documented semantic
    # dispatch envelope without calling any engine or replay renderer.
    semantic = {
        "provider": "deepseek",
        "model": wire["model"],
        "temperature": wire["temperature"],
        "max_output_tokens": wire["max_tokens"],
        "messages": wire["messages"],
    }
    encoded = json.dumps(
        semantic, sort_keys=True, ensure_ascii=True, separators=(",", ":")
    ).encode("utf-8")
    independently_computed = hashlib.sha256(encoded).hexdigest()
    assert canonical_dispatch_sha256(
        provider="deepseek",
        model="deepseek-v4-flash",
        temperature=0.0,
        max_output_tokens=512,
        prompt="SYSTEM\n\n[user] inspect",
    ) == independently_computed


def test_skipped_artifacts_are_excluded_from_end_to_end_pass_rate():
    from apricity.evaluation.context_compaction.metrics import aggregate_rows

    row = {
        "condition": "A",
        "runner_error": False,
        "artifact_pass": None,
        "run_pass": None,
        "mechanics_pass": True,
    }
    aggregate = aggregate_rows([row])

    assert aggregate["artifact_evaluated_runs"] == 0
    assert aggregate["passed_runs"] == 0
    assert aggregate["pass_rate"] == 0.0
    assert aggregate["mechanics_pass_rate"] == 1.0
    assert aggregate["conditions"]["A"]["evaluated_runs"] == 0
    assert aggregate["conditions"]["A"]["pass_rate"] == 0.0
    assert aggregate["conditions"]["A"]["mechanics_pass_rate"] == 1.0
    assert aggregate["failure_category_counts"] == {}


def test_public_verifier_timeout_fails_closed(tmp_path, monkeypatch):
    import subprocess

    runner = ContextCompactionRunner(
        taskset_path="benchmarks/context_compaction/tasks.v2.json",
        output_dir=tmp_path / "out",
        workspace_root="tests/fixtures/context_compaction_repo",
    )

    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(cmd=args[0], timeout=1, output="partial", stderr="late")

    monkeypatch.setattr(subprocess, "run", timeout)
    result = runner.run_public_verifier(
        {"public_verifier": ["python", "-c", "print('never')"], "verifier_timeout_seconds": 1},
        tmp_path,
    )

    assert result["exit_code"] is None
    assert "timed out" in result["stderr"]


def test_hidden_artifact_path_escape_fails_closed(tmp_path):
    from apricity.evaluation.context_compaction.verifiers import artifact_verifier_pass

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (tmp_path / "outside.txt").write_text("protected fact", encoding="utf-8")
    result = artifact_verifier_pass(
        workspace_copy_root=workspace,
        task={"artifact_path": "../outside.txt"},
        hidden_verifier={"check": "escape", "required_fact": "protected fact"},
        public_verifier_result={"exit_code": 0, "stdout": "", "stderr": ""},
    )

    assert result["public_pass"] is True
    assert result["hidden_pass"] is False
    assert result["artifact_pass"] is False


def test_clean_worktree_is_enforced_and_dirty_override_is_noncanonical(tmp_path, monkeypatch):
    def make_runner(allow_dirty):
        return ContextCompactionRunner(
            taskset_path="benchmarks/context_compaction/tasks.v2.json",
            output_dir=tmp_path / ("dirty" if allow_dirty else "blocked"),
            workspace_root="tests/fixtures/context_compaction_repo",
            allow_dirty=allow_dirty,
        )

    blocked = make_runner(False)
    monkeypatch.setattr(
        blocked,
        "_git_value",
        lambda *args: " M README.md" if args == ("status", "--porcelain") else "test",
    )
    with pytest.raises(RuntimeError, match="requires a clean worktree"):
        blocked.run()

    diagnostic = make_runner(True)
    diagnostic.blocks = []
    monkeypatch.setattr(
        diagnostic,
        "_git_value",
        lambda *args: " M README.md" if args == ("status", "--porcelain") else "test",
    )
    payload = diagnostic.run()
    assert payload["dirty"] is True
    assert payload["canonical"] is False
    assert payload["canonical_override_requested"] is True
