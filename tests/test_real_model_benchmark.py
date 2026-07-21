import json

from apricity.evaluation.real_model_benchmark import run_real_model_benchmark
from apricity.providers.clients import FakeModelClient
from apricity.providers.config import RuntimeConfig


def _write_tiny_benchmark(tmp_path):
    fixture = tmp_path / "tests" / "fixtures" / "tiny_repo"
    fixture.mkdir(parents=True)
    (fixture / "sample.txt").write_text("before\n", encoding="utf-8")
    benchmark_dir = tmp_path / "benchmarks"
    benchmark_dir.mkdir()
    benchmark_path = benchmark_dir / "real.json"
    benchmark_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "tasks": [
                    {
                        "id": "tiny_patch",
                        "prompt": "Replace before with after.",
                        "fixture_repo": "tests/fixtures/tiny_repo",
                        "allowed_tools": ["patch_file"],
                        "step_budget": 3,
                        "expected_artifact": "sample.txt is patched",
                        "artifact_path": "sample.txt",
                        "verifier": [
                            "python3",
                            "-c",
                            "from pathlib import Path; assert Path('sample.txt').read_text().strip() == 'after'",
                        ],
                        "verifier_timeout_seconds": 10,
                        "category": "bug-fix",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return benchmark_path


def test_real_model_benchmark_runs_repetitions_and_writes_reports(tmp_path):
    benchmark_path = _write_tiny_benchmark(tmp_path)
    progress = []

    def factory(task, workspace):
        del task, workspace
        return FakeModelClient(
            [
                '<tool name="patch_file" path="sample.txt"><old_text>before</old_text><new_text>after</new_text></tool>',
                "<final>Done.</final>",
            ]
        )

    output_dir = tmp_path / "results"
    payload = run_real_model_benchmark(
        benchmark_path=benchmark_path,
        output_dir=output_dir,
        workspace_root=tmp_path / "workspaces",
        repetitions=2,
        model_client_factory=factory,
        provider_config=RuntimeConfig("fake", "fixture-model", "", ""),
        progress=progress.append,
    )

    assert payload["status"] == "completed"
    assert payload["summary"]["total_runs"] == 2
    assert payload["summary"]["pass_rate"] == 1.0
    assert payload["summary"]["pass_at_1"] == 1.0
    assert payload["summary"]["stable_pass_rate"] == 1.0
    assert len(progress) == 2
    assert (output_dir / "real-model-benchmark.json").exists()
    assert (output_dir / "real-model-report.md").exists()
    assert not (output_dir / "failures").exists()


def test_real_model_benchmark_records_runner_errors_without_aborting(tmp_path):
    benchmark_path = _write_tiny_benchmark(tmp_path)

    def broken_factory(task, workspace):
        del task, workspace
        raise RuntimeError("provider unavailable")

    output_dir = tmp_path / "results"
    payload = run_real_model_benchmark(
        benchmark_path=benchmark_path,
        output_dir=output_dir,
        workspace_root=tmp_path / "workspaces",
        repetitions=1,
        model_client_factory=broken_factory,
        provider_config=RuntimeConfig("fake", "fixture-model", "", ""),
    )

    assert payload["summary"]["pass_rate"] == 0.0
    assert payload["summary"]["failure_category_counts"] == {"runner_error": 1}
    failure = output_dir / "failures" / "tiny_patch-r01.json"
    assert failure.exists()
    assert "provider unavailable" in failure.read_text(encoding="utf-8")


def test_real_model_benchmark_resumes_without_repeating_completed_runs(tmp_path):
    benchmark_path = _write_tiny_benchmark(tmp_path)
    output_dir = tmp_path / "results"

    def working_factory(task, workspace):
        del task, workspace
        return FakeModelClient(
            [
                '<tool name="patch_file" path="sample.txt"><old_text>before</old_text><new_text>after</new_text></tool>',
                "<final>Done.</final>",
            ]
        )

    config = RuntimeConfig("fake", "fixture-model", "", "")
    run_real_model_benchmark(
        benchmark_path=benchmark_path,
        output_dir=output_dir,
        workspace_root=tmp_path / "workspaces",
        repetitions=1,
        model_client_factory=working_factory,
        provider_config=config,
    )

    artifact_path = output_dir / "real-model-benchmark.json"
    interrupted = json.loads(artifact_path.read_text(encoding="utf-8"))
    interrupted["status"] = "running"
    interrupted["total_runs"] = 1
    artifact_path.write_text(json.dumps(interrupted), encoding="utf-8")

    def must_not_run(task, workspace):
        del task, workspace
        raise AssertionError("completed task was repeated")

    resumed = run_real_model_benchmark(
        benchmark_path=benchmark_path,
        output_dir=output_dir,
        workspace_root=tmp_path / "workspaces",
        repetitions=1,
        model_client_factory=must_not_run,
        provider_config=config,
        resume=True,
    )

    assert resumed["status"] == "completed"
    assert resumed["summary"]["total_runs"] == 1
    assert resumed["summary"]["pass_rate"] == 1.0
