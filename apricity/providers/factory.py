"""Provider construction seam shared by CLI and evaluation modules."""

from pathlib import Path

from ..config import load_project_env
from .clients import (
    AnthropicCompatibleModelClient,
    DeepSeekModelClient,
    OllamaModelClient,
    OpenAICompatibleModelClient,
    is_deepseek_anthropic_base_url,
)
from .config import RuntimeConfig, resolve_runtime_config


def load_provider_config(provider=None, *, start=None):
    """Load project-local environment values and return validated provider config."""
    load_project_env(Path(start or Path.cwd()))
    return resolve_runtime_config(provider=provider)


def create_model_client(config: RuntimeConfig, *, temperature=0.0, timeout=300):
    """Create a fresh model-client adapter for a validated runtime configuration."""
    if config.provider == "openai":
        return OpenAICompatibleModelClient(
            model=config.model,
            base_url=config.base_url,
            api_key=config.api_key,
            temperature=temperature,
            timeout=timeout,
        )
    if config.provider == "ollama":
        return OllamaModelClient(
            model=config.model,
            base_url=config.base_url,
            temperature=temperature,
            top_p=0.9,
            timeout=timeout,
        )

    client_class = AnthropicCompatibleModelClient
    if config.provider == "deepseek" and not is_deepseek_anthropic_base_url(config.base_url):
        client_class = DeepSeekModelClient
    client = client_class(
        model=config.model,
        base_url=config.base_url,
        api_key=config.api_key,
        temperature=temperature,
        timeout=timeout,
    )
    if config.provider == "deepseek":
        client.provider = "deepseek"
    return client
