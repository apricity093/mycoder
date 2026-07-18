import os
from unittest.mock import patch

import pytest

from apricity.config import provider_env
from apricity.providers.config import resolve_runtime_config


def test_provider_env_reads_one_generic_name_and_default():
    with patch.dict(os.environ, {"APRICITY_MODEL": "configured-model"}, clear=True):
        assert provider_env("APRICITY_MODEL") == "configured-model"
        assert provider_env("APRICITY_BASE_URL", default="https://default.example") == "https://default.example"


@pytest.mark.parametrize("provider", ("openai", "anthropic", "deepseek"))
def test_runtime_config_requires_explicit_model_and_base_url(provider):
    with patch.dict(
        os.environ,
        {"APRICITY_PROVIDER": provider, "APRICITY_API_KEY": "sk-test"},
        clear=True,
    ):
        with pytest.raises(ValueError, match="APRICITY_MODEL.*APRICITY_BASE_URL"):
            resolve_runtime_config()


def test_runtime_config_uses_unified_values_for_any_provider():
    with patch.dict(
        os.environ,
        {
            "APRICITY_PROVIDER": "openai",
            "APRICITY_MODEL": "custom-model",
            "APRICITY_BASE_URL": "https://gateway.example/v1",
            "APRICITY_API_KEY": "sk-custom",
        },
        clear=True,
    ):
        config = resolve_runtime_config()

    assert config.model == "custom-model"
    assert config.base_url == "https://gateway.example/v1"
    assert config.api_key == "sk-custom"


@pytest.mark.parametrize(
    ("configured_provider", "expected_provider", "api_key"),
    (
        ("OLLAMA", "ollama", ""),
        ("OpenAI", "openai", "sk-openai"),
        ("ANTHROPIC", "anthropic", "sk-anthropic"),
        (" DeepSeek ", "deepseek", "sk-deepseek"),
    ),
)
def test_runtime_config_normalizes_provider_name(configured_provider, expected_provider, api_key):
    with patch.dict(
        os.environ,
        {
            "APRICITY_PROVIDER": configured_provider,
            "APRICITY_MODEL": "configured-model",
            "APRICITY_BASE_URL": "https://gateway.example/v1",
            "APRICITY_API_KEY": api_key,
        },
        clear=True,
    ):
        config = resolve_runtime_config()

    assert config.provider == expected_provider


@pytest.mark.parametrize(
    ("missing_name", "expected_message"),
    (
        ("APRICITY_MODEL", "APRICITY_MODEL"),
        ("APRICITY_BASE_URL", "APRICITY_BASE_URL"),
        ("APRICITY_API_KEY", "APRICITY_API_KEY is required"),
    ),
)
def test_runtime_config_reports_each_missing_remote_field(missing_name, expected_message):
    env = {
        "APRICITY_PROVIDER": "deepseek",
        "APRICITY_MODEL": "deepseek-v4-pro",
        "APRICITY_BASE_URL": "https://api.deepseek.com/anthropic",
        "APRICITY_API_KEY": "sk-test",
    }
    env.pop(missing_name)

    with patch.dict(os.environ, env, clear=True):
        with pytest.raises(ValueError, match=expected_message):
            resolve_runtime_config()


def test_ollama_does_not_require_api_key_when_other_fields_are_configured():
    with patch.dict(
        os.environ,
        {
            "APRICITY_PROVIDER": "ollama",
            "APRICITY_MODEL": "qwen3.5:4b",
            "APRICITY_BASE_URL": "http://127.0.0.1:11434",
        },
        clear=True,
    ):
        config = resolve_runtime_config()

    assert config.model == "qwen3.5:4b"
    assert config.base_url == "http://127.0.0.1:11434"
    assert config.api_key == ""


def test_remote_provider_requires_unified_api_key():
    with patch.dict(os.environ, {"APRICITY_PROVIDER": "deepseek"}, clear=True):
        with pytest.raises(ValueError, match="APRICITY_API_KEY is required"):
            resolve_runtime_config()


def test_removed_provider_specific_variable_is_rejected():
    removed_name = "_".join(("APRICITY", "OPENAI", "API", "KEY"))
    with patch.dict(
        os.environ,
        {"APRICITY_PROVIDER": "openai", removed_name: "sk-old"},
        clear=True,
    ):
        with pytest.raises(ValueError, match="Unsupported model configuration variable"):
            resolve_runtime_config()
