"""Canonical model-provider configuration."""

from dataclasses import dataclass
import os
import re

from ..config import provider_env


DEFAULT_PROVIDER = "deepseek"
PROVIDER_CHOICES = ("ollama", "openai", "anthropic", "deepseek")


def normalize_provider_name(value):
    """Return the canonical, case-insensitive provider name."""
    return str(value).strip().lower()


@dataclass(frozen=True)
class ProviderDefaults:
    model: str
    base_url: str
    requires_api_key: bool = True


@dataclass(frozen=True)
class RuntimeConfig:
    provider: str
    model: str
    base_url: str
    api_key: str


PROVIDER_DEFAULTS = {
    "ollama": ProviderDefaults(
        model="qwen3.5:4b",
        base_url="http://127.0.0.1:11434",
        requires_api_key=False,
    ),
    "openai": ProviderDefaults(
        model="gpt-5.4",
        base_url="https://api.openai.com/v1",
    ),
    "anthropic": ProviderDefaults(
        model="claude-sonnet-4-6",
        base_url="https://api.anthropic.com/v1",
    ),
    "deepseek": ProviderDefaults(
        model="deepseek-v4-pro",
        base_url="https://api.deepseek.com/anthropic",
    ),
}

REMOVED_PROVIDER_ENV_PATTERN = re.compile(
    r"^(?:PICO_)?(?:OPENAI|ANTHROPIC|DEEPSEEK)_"
    r"(?:API_BASE|API_KEY|MODEL|API_TOKEN|AUTH_TOKEN)$"
)


def removed_provider_env_names(env=None):
    env = os.environ if env is None else env
    return sorted(
        name
        for name, value in env.items()
        if value and REMOVED_PROVIDER_ENV_PATTERN.match(str(name).upper())
    )


def validate_unified_provider_env(env=None):
    removed_names = removed_provider_env_names(env=env)
    if removed_names:
        names = ", ".join(removed_names)
        raise ValueError(
            f"Unsupported model configuration variable(s): {names}. "
            "Use only PICO_PROVIDER, PICO_MODEL, PICO_BASE_URL, and PICO_API_KEY."
        )


def resolve_runtime_config(provider=None, model=None, base_url=None):
    validate_unified_provider_env()
    selected_provider = normalize_provider_name(
        provider or provider_env("PICO_PROVIDER", default=DEFAULT_PROVIDER)
    )
    if selected_provider not in PROVIDER_DEFAULTS:
        choices = ", ".join(PROVIDER_CHOICES)
        raise ValueError(
            f"unknown provider: {selected_provider}. expected one of: {choices}"
        )

    defaults = PROVIDER_DEFAULTS[selected_provider]
    selected_model = str(
        model if model is not None else provider_env("PICO_MODEL")
    ).strip()
    selected_base_url = str(
        base_url if base_url is not None else provider_env("PICO_BASE_URL")
    ).strip()
    api_key = str(provider_env("PICO_API_KEY")).strip()

    missing_fields = []
    if not selected_model:
        missing_fields.append("PICO_MODEL (or --model)")
    if not selected_base_url:
        missing_fields.append("PICO_BASE_URL (or --base-url)")
    missing_api_key = defaults.requires_api_key and not api_key
    if missing_api_key:
        missing_fields.append("PICO_API_KEY")

    if missing_fields:
        if missing_api_key:
            message = f"PICO_API_KEY is required for provider {selected_provider}."
            other_missing = [
                field for field in missing_fields if field != "PICO_API_KEY"
            ]
            if other_missing:
                message += " Also missing: " + ", ".join(other_missing) + "."
            raise ValueError(message)
        raise ValueError(
            f"Missing required configuration for provider {selected_provider}: "
            + ", ".join(missing_fields)
            + "."
        )

    return RuntimeConfig(
        provider=selected_provider,
        model=selected_model,
        base_url=selected_base_url,
        api_key=api_key,
    )
