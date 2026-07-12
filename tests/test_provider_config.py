import os
from unittest.mock import patch

import pytest

from pico.config import provider_env
from pico.providers.config import resolve_runtime_config


def test_provider_env_reads_one_generic_name_and_default():
    with patch.dict(os.environ, {"PICO_MODEL": "configured-model"}, clear=True):
        assert provider_env("PICO_MODEL") == "configured-model"
        assert provider_env("PICO_BASE_URL", default="https://default.example") == "https://default.example"


@pytest.mark.parametrize("provider", ("openai", "anthropic", "deepseek"))
def test_runtime_config_requires_explicit_model_and_base_url(provider):
    with patch.dict(
        os.environ,
        {"PICO_PROVIDER": provider, "PICO_API_KEY": "sk-test"},
        clear=True,
    ):
        with pytest.raises(ValueError, match="PICO_MODEL.*PICO_BASE_URL"):
            resolve_runtime_config()


def test_runtime_config_uses_unified_values_for_any_provider():
    with patch.dict(
        os.environ,
        {
            "PICO_PROVIDER": "openai",
            "PICO_MODEL": "custom-model",
            "PICO_BASE_URL": "https://gateway.example/v1",
            "PICO_API_KEY": "sk-custom",
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
            "PICO_PROVIDER": configured_provider,
            "PICO_MODEL": "configured-model",
            "PICO_BASE_URL": "https://gateway.example/v1",
            "PICO_API_KEY": api_key,
        },
        clear=True,
    ):
        config = resolve_runtime_config()

    assert config.provider == expected_provider


@pytest.mark.parametrize(
    ("missing_name", "expected_message"),
    (
        ("PICO_MODEL", "PICO_MODEL"),
        ("PICO_BASE_URL", "PICO_BASE_URL"),
        ("PICO_API_KEY", "PICO_API_KEY is required"),
    ),
)
def test_runtime_config_reports_each_missing_remote_field(missing_name, expected_message):
    env = {
        "PICO_PROVIDER": "deepseek",
        "PICO_MODEL": "deepseek-v4-pro",
        "PICO_BASE_URL": "https://api.deepseek.com/anthropic",
        "PICO_API_KEY": "sk-test",
    }
    env.pop(missing_name)

    with patch.dict(os.environ, env, clear=True):
        with pytest.raises(ValueError, match=expected_message):
            resolve_runtime_config()


def test_ollama_does_not_require_api_key_when_other_fields_are_configured():
    with patch.dict(
        os.environ,
        {
            "PICO_PROVIDER": "ollama",
            "PICO_MODEL": "qwen3.5:4b",
            "PICO_BASE_URL": "http://127.0.0.1:11434",
        },
        clear=True,
    ):
        config = resolve_runtime_config()

    assert config.model == "qwen3.5:4b"
    assert config.base_url == "http://127.0.0.1:11434"
    assert config.api_key == ""


def test_remote_provider_requires_unified_api_key():
    with patch.dict(os.environ, {"PICO_PROVIDER": "deepseek"}, clear=True):
        with pytest.raises(ValueError, match="PICO_API_KEY is required"):
            resolve_runtime_config()


def test_removed_provider_specific_variable_is_rejected():
    removed_name = "_".join(("PICO", "OPENAI", "API", "KEY"))
    with patch.dict(
        os.environ,
        {"PICO_PROVIDER": "openai", removed_name: "sk-old"},
        clear=True,
    ):
        with pytest.raises(ValueError, match="Unsupported model configuration variable"):
            resolve_runtime_config()
