import os
from unittest.mock import patch

from apricity.evaluation.metrics import (
    _client_from_profile,
    _provider_profile,
    _provider_summary_from_artifact,
    run_bilingual_memory_benchmark,
    run_context_ablation_v2,
    run_memory_ablation_v2,
    run_provider_experiments,
    run_recovery_ablation_v2,
    write_benchmark_core_report,
)
from apricity import AnthropicCompatibleModelClient, DeepSeekModelClient


def test_provider_summary_excludes_runs_without_cache_telemetry():
    payload = {
        "rows": [
            {
                "tool_steps": 1,
                "attempts": 1,
                "report": {
                    "prompt_metadata": {
                        "cache_telemetry_available": True,
                        "cached_tokens": 75,
                        "input_tokens": 100,
                        "cache_hit": True,
                    }
                },
            },
            {
                "tool_steps": 1,
                "attempts": 1,
                "report": {
                    "prompt_metadata": {
                        "cache_telemetry_available": False,
                        "cached_tokens": 0,
                        "input_tokens": 100,
                        "cache_hit": False,
                    }
                },
            },
        ],
        "summary": {"total_tasks": 2, "pass_rate": 1.0},
    }

    summary = _provider_summary_from_artifact(payload)

    assert summary["cache_telemetry_run_count"] == 1
    assert summary["cache_hit_rate"] == 1.0
    assert summary["cached_token_ratio"] == 0.75


def test_provider_experiment_client_selects_deepseek_transport_from_base_url():
    native = _client_from_profile(
        {
            "provider": "deepseek",
            "model": "deepseek-v4-pro",
            "base_url": "https://api.deepseek.com",
            "api_key": "sk-test",
        },
        temperature=0.0,
        timeout=30,
    )
    anthropic_compatible = _client_from_profile(
        {
            "provider": "deepseek",
            "model": "deepseek-v4-pro",
            "base_url": "https://api.deepseek.com/anthropic",
            "api_key": "sk-test",
        },
        temperature=0.0,
        timeout=30,
    )

    assert isinstance(native, DeepSeekModelClient)
    assert isinstance(anthropic_compatible, AnthropicCompatibleModelClient)


def test_run_context_ablation_v2_writes_expected_artifact(tmp_path):
    artifact_path = tmp_path / "artifacts" / "context-ablation-v2.json"

    artifact = run_context_ablation_v2(
        artifact_path=artifact_path,
        repetitions=1,
    )

    assert artifact_path.exists()
    assert artifact["artifact_type"] == "context-ablation-v2"
    assert artifact["config_count"] == 12
    assert len(artifact["configs"]) == 12
    assert "current_request_preserved_rate" in artifact["summary"]


def test_provider_profile_loads_unified_project_env(tmp_path, monkeypatch, isolated_project_env):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text(
        "\n".join(
            [
                "APRICITY_PROVIDER=deepseek",
                "APRICITY_API_KEY=sk-project-deepseek",
                "APRICITY_MODEL=deepseek-v4-pro",
                "APRICITY_BASE_URL=https://api.deepseek.com/anthropic",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    with patch.dict(
        os.environ,
        {},
        clear=True,
    ):
        profile = _provider_profile("deepseek")

    assert profile["status"] == "ready"
    assert profile["api_key"] == "sk-project-deepseek"
    assert profile["model"] == "deepseek-v4-pro"
    assert profile["base_url"] == "https://api.deepseek.com/anthropic"


def test_provider_profile_uses_unified_openai_config(tmp_path, monkeypatch, isolated_project_env):
    monkeypatch.chdir(tmp_path)

    with patch.dict(
        os.environ,
        {
            "APRICITY_PROVIDER": "openai",
            "APRICITY_MODEL": "gpt-5.4",
            "APRICITY_BASE_URL": "https://api.openai.com/v1",
            "APRICITY_API_KEY": "sk-openai",
        },
        clear=True,
    ):
        profile = _provider_profile()

    assert profile["status"] == "ready"
    assert profile["api_key"] == "sk-openai"
    assert profile["provider"] == "openai"
    assert profile["model"] == "gpt-5.4"


def test_provider_experiments_runs_only_the_active_provider(tmp_path, monkeypatch, isolated_project_env):
    monkeypatch.chdir(tmp_path)
    fake_payload = {
        "rows": [],
        "summary": {"total_tasks": 0, "pass_rate": 0.0},
    }

    with patch.dict(
        os.environ,
        {
            "APRICITY_PROVIDER": "openai",
            "APRICITY_MODEL": "gpt-5.4",
            "APRICITY_BASE_URL": "https://api.openai.com/v1",
            "APRICITY_API_KEY": "sk-openai",
        },
        clear=True,
    ), patch("apricity.evaluation.metrics.run_fixed_benchmark", return_value=fake_payload) as runner:
        result = run_provider_experiments(
            benchmark_path=tmp_path / "benchmark.json",
            workspace_root=tmp_path / "workspaces",
            artifact_root=tmp_path / "artifacts",
        )

    assert len(result["providers"]) == 1
    assert result["providers"][0]["provider"] == "openai"
    assert runner.call_args.kwargs["model_name"] == "openai"
    assert runner.call_args.kwargs["workspace_root"] == tmp_path / "workspaces" / "openai"


def test_run_memory_ablation_v2_writes_expected_artifact(tmp_path):
    artifact_path = tmp_path / "artifacts" / "memory-ablation-v2.json"

    artifact = run_memory_ablation_v2(
        artifact_path=artifact_path,
        repetitions=1,
    )

    assert artifact_path.exists()
    assert artifact["artifact_type"] == "memory-ablation-v2"
    assert artifact["task_count"] == 12
    assert set(artifact["variants"]) == {"memory_on", "memory_off", "memory_irrelevant"}
    assert "memory_hit_rate" in artifact["variants"]["memory_on"]


def test_run_bilingual_memory_benchmark_covers_positive_and_negative_cases(tmp_path):
    artifact_path = tmp_path / "artifacts" / "bilingual-memory-retrieval-v1.json"

    artifact = run_bilingual_memory_benchmark(artifact_path=artifact_path, repetitions=1)

    assert artifact_path.exists()
    assert artifact["artifact_type"] == "bilingual-memory-retrieval-v1"
    assert artifact["case_count"] == 6
    assert artifact["summary"] == {
        "pass_rate": 1.0,
        "positive_recall_rate": 1.0,
        "negative_false_positive_rate": 0.0,
    }
    assert {row["language_pair"] for row in artifact["rows"]} >= {"zh-zh", "en-zh", "zh-en", "mixed"}


def test_run_recovery_ablation_v2_writes_expected_artifact(tmp_path):
    artifact_path = tmp_path / "artifacts" / "recovery-ablation-v2.json"

    artifact = run_recovery_ablation_v2(
        artifact_path=artifact_path,
        repetitions=1,
    )

    assert artifact_path.exists()
    assert artifact["artifact_type"] == "recovery-ablation-v2"
    assert artifact["task_count"] == 10
    assert set(artifact["variants"]) == {"resume_enabled", "resume_disabled"}
    assert set(artifact["variants"]["resume_enabled"]["summary"]) >= {
        "resume_success_rate",
        "stale_reanchor_rate",
        "workspace_drift_detection_rate",
        "resume_false_accept_rate",
    }


def test_write_benchmark_core_report_marks_resume_safe_metrics(tmp_path):
    run_context_ablation_v2(tmp_path / "artifacts" / "context-ablation-v2.json", repetitions=1)
    run_memory_ablation_v2(tmp_path / "artifacts" / "memory-ablation-v2.json", repetitions=1)
    run_recovery_ablation_v2(tmp_path / "artifacts" / "recovery-ablation-v2.json", repetitions=1)
    harness_artifact_path = tmp_path / "artifacts" / "harness-regression-v2.json"
    harness_artifact_path.write_text(
        '{"summary":{"total_tasks":12,"pass_rate":1.0,"within_budget_rate":1.0,"verifier_pass_rate":1.0},"failure_category_counts":{}}',
        encoding="utf-8",
    )

    report_path = tmp_path / "docs" / "metrics" / "apricity-benchmark-core-report.md"
    report_text = write_benchmark_core_report(
        report_path=report_path,
        harness_artifact_path=harness_artifact_path,
        context_artifact_path=tmp_path / "artifacts" / "context-ablation-v2.json",
        memory_artifact_path=tmp_path / "artifacts" / "memory-ablation-v2.json",
        recovery_artifact_path=tmp_path / "artifacts" / "recovery-ablation-v2.json",
    )

    assert report_path.exists()
    assert "可以安全写进简历的指标" in report_text
    assert "只适合放文档/面试展开的指标" in report_text
    assert "resume_success_rate" in report_text
    assert "memory_hit_rate" in report_text
