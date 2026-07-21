import json
import os
from unittest.mock import patch

import pytest

from apricity.run_store import RunStore
from apricity.task_state import STOP_REASON_FINAL_ANSWER_RETURNED, TaskState


def test_run_store_creates_run_directory_and_state_file(tmp_path):
    store = RunStore(tmp_path / ".apricity" / "runs")
    state = TaskState.create(run_id="run_001", task_id="task_001", user_request="Inspect the repo.")

    run_dir = store.start_run(state)

    assert run_dir == store.run_dir(state.run_id)
    assert run_dir.exists()
    persisted = json.loads((run_dir / "task_state.json").read_text(encoding="utf-8"))
    assert persisted["task_id"] == "task_001"
    assert persisted["run_id"] == "run_001"
    assert persisted["user_request"] == "Inspect the repo."


def test_run_store_appends_trace_jsonl(tmp_path):
    store = RunStore(tmp_path / ".apricity" / "runs")
    state = TaskState.create(run_id="run_002", task_id="task_002", user_request="Trace the run.")
    store.start_run(state)

    store.append_trace(state, {"event": "run_started", "created_at": "2026-04-07T00:00:00+00:00"})
    store.append_trace(
        state.run_id,
        {
            "event": "prompt_built",
            "created_at": "2026-04-07T00:00:01+00:00",
            "prompt_metadata": {"prompt_chars": 128, "secret_env_count": 1},
        },
    )
    store.append_trace(state.run_id, {"event": "run_finished", "created_at": "2026-04-07T00:00:02+00:00"})

    lines = (store.trace_path(state.run_id)).read_text(encoding="utf-8").splitlines()
    assert len(lines) == 3
    assert json.loads(lines[0])["event"] == "run_started"
    assert json.loads(lines[1])["event"] == "prompt_built"
    assert json.loads(lines[2])["event"] == "run_finished"


def test_run_store_writes_report_json(tmp_path):
    store = RunStore(tmp_path / ".apricity" / "runs")
    state = TaskState.create(run_id="run_003", task_id="task_003", user_request="Report the run.")
    store.start_run(state)
    state.finish_success("Done.")

    store.write_task_state(state)
    store.write_report(state, {"task_state": state.to_dict(), "stop_reason": state.stop_reason})

    report = json.loads(store.report_path(state.run_id).read_text(encoding="utf-8"))
    assert report["stop_reason"] == STOP_REASON_FINAL_ANSWER_RETURNED
    assert report["task_state"]["final_answer"] == "Done."


def test_run_store_tolerates_missing_final_report(tmp_path):
    store = RunStore(tmp_path / ".apricity" / "runs")
    state = TaskState.create(run_id="run_004", task_id="task_004", user_request="Crash before finalize.")

    store.start_run(state)
    store.append_trace(state, {"event": "run_started"})

    assert store.trace_path(state.run_id).exists()
    assert not store.report_path(state.run_id).exists()


def test_run_store_normalizes_relative_root_to_absolute_path(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    store = RunStore("relative-runs")

    assert store.root == (tmp_path / "relative-runs").resolve()
    assert store.root.is_absolute()


def test_atomic_json_write_retries_transient_windows_permission_error(tmp_path):
    store = RunStore(tmp_path / "runs")
    path = store.root / "state.json"
    real_replace = os.replace
    attempts = []

    def flaky_replace(source, target):
        attempts.append((source, target))
        if len(attempts) < 3:
            raise PermissionError(5, "temporarily locked")
        real_replace(source, target)

    with patch("apricity.run_store.os.replace", side_effect=flaky_replace), patch(
        "apricity.run_store.time.sleep"
    ) as sleep:
        store._write_json_atomic(path, {"status": "ok"})

    assert json.loads(path.read_text(encoding="utf-8")) == {"status": "ok"}
    assert len(attempts) == 3
    assert all(source.is_absolute() and target.is_absolute() for source, target in attempts)
    assert [call.args[0] for call in sleep.call_args_list] == [0.01, 0.02]
    assert list(store.root.glob("state.json.*.tmp")) == []


def test_atomic_json_write_cleans_temp_file_after_persistent_permission_error(tmp_path):
    store = RunStore(tmp_path / "runs")
    path = store.root / "state.json"

    with patch(
        "apricity.run_store.os.replace",
        side_effect=PermissionError(5, "persistently locked"),
    ), patch("apricity.run_store.time.sleep"):
        with pytest.raises(PermissionError, match="failed after 5 attempts"):
            store._write_json_atomic(path, {"status": "blocked"})

    assert not path.exists()
    assert list(store.root.glob("state.json.*.tmp")) == []
