"""Local model suggestions for configured providers; no credential or network reads."""

import importlib.util
import os
import re
import shutil
from pathlib import Path
from typing import get_args

# Every prefix the picker accepts, with a display label. Each provider's SDK
# extra must be installed (see pyproject.toml) or infer_model raises on switch.
PROVIDERS = {
    "alibaba": "Alibaba",
    "anthropic": "Anthropic",
    "azure": "Azure OpenAI",
    "bedrock": "Bedrock",
    "bedrock-mantle": "Bedrock Mantle",
    "cerebras": "Cerebras",
    "claude": "Claude Code",
    "crusoe": "Crusoe",
    "deepseek": "DeepSeek",
    "fireworks": "Fireworks",
    "github-copilot": "GitHub Copilot",
    "google": "Google",
    "google-cloud": "Google Cloud",
    "groq": "Groq",
    "heroku": "Heroku",
    "moonshotai": "Moonshot",
    "nebius": "Nebius",
    "ollama": "Ollama",
    "openai": "OpenAI",
    "openai-chat": "OpenAI (chat)",
    "openai-codex": "OpenAI Codex",
    "openai-responses": "OpenAI (responses)",
    "openrouter": "OpenRouter",
    "ovhcloud": "OVHcloud",
    "sambanova": "SambaNova",
    "snowflake": "Snowflake",
    "together": "Together",
    "vercel": "Vercel",
    "vllm": "vLLM",
    "xai": "xAI",
    "zai": "Z.ai",
}

# Providers activated by environment. Each inner tuple is one requirement
# satisfied by any listed variable; every requirement must hold. Only names
# are inspected, never values. Providers needing an endpoint as well as a key
# (azure, github-copilot) list both so a lone key is not a false positive.
ENV_PROVIDERS: dict[str, tuple[tuple[str, ...], ...]] = {
    "alibaba": (("ALIBABA_API_KEY", "DASHSCOPE_API_KEY"),),
    "azure": (("AZURE_OPENAI_API_KEY",), ("AZURE_OPENAI_ENDPOINT",)),
    "bedrock": (("AWS_BEARER_TOKEN_BEDROCK", "AWS_ACCESS_KEY_ID", "AWS_PROFILE"),),
    "bedrock-mantle": (("AWS_BEARER_TOKEN_BEDROCK",),),
    "cerebras": (("CEREBRAS_API_KEY",),),
    "crusoe": (("CRUSOE_API_KEY",),),
    "deepseek": (("DEEPSEEK_API_KEY",),),
    "fireworks": (("FIREWORKS_API_KEY",),),
    "github-copilot": (
        ("GITHUB_COPILOT_API_KEY", "GITHUB_COPILOT_API_TOKEN", "COPILOT_GITHUB_TOKEN"),
        ("GITHUB_COPILOT_BASE_URL", "GITHUB_COPILOT_API_BASE", "COPILOT_API_URL"),
    ),
    "google": (("GOOGLE_API_KEY", "GEMINI_API_KEY"),),
    "google-cloud": (("GOOGLE_CLOUD_PROJECT", "GOOGLE_APPLICATION_CREDENTIALS"),),
    "groq": (("GROQ_API_KEY",),),
    "heroku": (("HEROKU_INFERENCE_KEY",),),
    "moonshotai": (("MOONSHOTAI_API_KEY",),),
    "nebius": (("NEBIUS_API_KEY",),),
    "ollama": (("OLLAMA_BASE_URL",),),
    "openai": (("OPENAI_API_KEY",),),
    "openrouter": (("OPENROUTER_API_KEY",),),
    "ovhcloud": (("OVHCLOUD_API_KEY",),),
    "sambanova": (("SAMBANOVA_API_KEY",),),
    "snowflake": (("SNOWFLAKE_TOKEN",), ("SNOWFLAKE_ACCOUNT",)),
    "together": (("TOGETHER_API_KEY",),),
    "vercel": (("VERCEL_AI_GATEWAY_API_KEY", "VERCEL_OIDC_TOKEN"),),
    "vllm": (("VLLM_BASE_URL",),),
    "xai": (("XAI_API_KEY",),),
    "zai": (("ZAI_API_KEY",),),
}


def login_sources() -> tuple[str, ...]:
    """What `/login` accepts; the first is what a bare `/login` signs in to."""
    return ("claude", "openai-codex")


def logout_sources() -> tuple[str, ...]:
    """What `/logout` offers; the first is what a bare `/logout` removes."""
    return ("openai-codex",)


def anthropic_credential_hint() -> str:
    """How to give an `anthropic:` model that has no credential one."""
    return "Set ANTHROPIC_API_KEY, or switch to a claude: model."


# Catalog prefix -> KnownModelName prefixes whose IDs it accepts. Codex has no
# separate SDK catalog, so expose every OpenAI model ID and let the provider
# enforce account access; Claude Code fronts Anthropic models.
CATALOG_SOURCES = {"openai-codex": "openai", "claude": "anthropic"}


def claude_code_configured() -> bool:
    """Whether this machine has used Claude Code, whose login `claude:` models share.

    The Agent SDK bundles the CLI, so an install is not required; its config
    is the signal, checked for existence only.
    """
    config = os.environ.get("CLAUDE_CONFIG_DIR", "").strip()
    try:
        return bool(
            (Path(config).expanduser() if config else Path.home() / ".claude").is_dir()
            or (Path.home() / ".claude.json").is_file()
            or shutil.which("claude")
        )
    except OSError:
        return False


def claude_sdk_installed() -> bool:
    """Whether pcode was installed with its `claude` extra (claude-agent-sdk)."""
    return importlib.util.find_spec("claude_agent_sdk") is not None


def _configured(requirements: tuple[tuple[str, ...], ...]) -> bool:
    return all(any(os.environ.get(name, "").strip() for name in group) for group in requirements)


def active_providers(current: str | None) -> set[str]:
    active = set()
    if current and current.partition(":")[0] in PROVIDERS:
        active.add(current.partition(":")[0])
    if claude_sdk_installed() and claude_code_configured():
        active.add("claude")
    active.update(name for name, needs in ENV_PROVIDERS.items() if _configured(needs))
    if os.environ.get("ANTHROPIC_API_KEY", "").strip():
        active.add("anthropic")
    from pcode.codex_login import have_credentials

    # Only check existence, never read a credential to populate the picker.
    codex_home = os.environ.get("CODEX_HOME", "").strip()
    directory = Path(codex_home).expanduser() if codex_home else Path.home() / ".codex"
    try:
        if have_credentials() or (directory / "auth.json").is_file():
            active.add("openai-codex")
    except OSError:
        pass
    from pcode.preferences import load_preferences

    allowed = load_preferences().get("model_providers", "")
    if allowed:
        active.intersection_update(name.strip() for name in allowed.split(","))
    return active


def model_catalog(providers: set[str], current: str | None = None) -> list[str]:
    # Imported here so the terminal can read this module's settings without
    # loading the agent stack. KnownModelName is a TypeAliasType on supported
    # Pydantic versions.
    from pydantic_ai.models import KnownModelName

    known = get_args(getattr(KnownModelName, "__value__", KnownModelName))
    by_source: dict[str, set[str]] = {}
    for name in known:
        if isinstance(name, str):
            source, _, model = name.partition(":")
            by_source.setdefault(source, set()).add(model)
    models = set()
    for provider in providers:
        source = CATALOG_SOURCES.get(provider, provider)
        # Providers without a catalog (openrouter, ollama, vllm...) take any
        # model ID typed into the picker instead.
        models.update(f"{provider}:{model}" for model in by_source.get(source, ()))
    if current and current.partition(":")[0] in providers:
        models.add(current)
    return sorted(models, key=model_sort_key)


def model_sort_key(name: str):
    """Group providers/families alphabetically, then numeric versions newest first.

    Compare numeric components as integers so 4.10 precedes 4.9. Undated aliases
    sort before dated snapshots of the same version. Do not pin the current model
    above newer suggestions; the picker marks it separately.
    """
    provider, _, model = name.partition(":")
    parts = tuple(
        (1, -int(part)) if part.isdigit() else (0, part.casefold())
        for part in re.split(r"(\d+)", model)
    )
    return provider, parts
