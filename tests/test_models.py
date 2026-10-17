"""Model suggestions inspect configuration, never real credential contents."""

from pathlib import Path
from unittest.mock import Mock

import pytest
from pydantic_ai.providers import infer_provider_class

from pcode.models import (
    ENV_PROVIDERS,
    PROVIDERS,
    active_providers,
    model_catalog,
    model_sort_key,
)


@pytest.fixture(autouse=True)
def isolated_providers(monkeypatch, tmp_path):
    for name in (
        "ANTHROPIC_API_KEY",
        "PCODE_LLM_PROXY",
        "CODEX_HOME",
        "CLAUDE_CONFIG_DIR",
    ):
        monkeypatch.delenv(name, raising=False)
    for requirements in ENV_PROVIDERS.values():
        for group in requirements:
            for name in group:
                monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr("pcode.models.shutil.which", lambda _: None)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)


@pytest.mark.parametrize("allowed", ["openai-codex,anthropic", " openai-codex , anthropic "])
def test_provider_filter_matches_exact_names(monkeypatch, tmp_path, allowed):
    from pcode.preferences import save_preferences

    monkeypatch.setenv("PCODE_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-key")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "synthetic-key")
    (tmp_path / ".codex").mkdir()
    (tmp_path / ".codex" / "auth.json").touch()
    save_preferences(model_providers=allowed)
    providers = active_providers("openai:custom")
    assert providers == {"openai-codex", "anthropic"}
    assert {
        name.partition(":")[0] for name in model_catalog(providers, "openai:custom")
    } == providers


def test_provider_filter_does_not_activate_providers(monkeypatch, tmp_path):
    from pcode.preferences import save_preferences

    monkeypatch.setenv("PCODE_CONFIG_DIR", str(tmp_path / "config"))
    monkeypatch.chdir(tmp_path)
    save_preferences(model_providers="anthropic")
    assert active_providers("openai:custom") == set()
    save_preferences(model_providers="")
    assert active_providers("openai:custom") == {"openai"}


@pytest.mark.parametrize(
    "value", ["openai-code", "anthropic,,openai", "anthropic,", "openai codex"]
)
def test_provider_filter_rejects_invalid_names(value):
    from pcode.preferences import SETTINGS

    with pytest.raises(ValueError):
        SETTINGS["model_providers"].validate("model_providers", value)


def test_no_configuration():
    assert active_providers(None) == set()


def test_current_provider_stays_visible():
    assert active_providers("anthropic:custom-id") == {"anthropic"}
    assert active_providers("openai-codex:custom-id") == {"openai-codex"}
    assert active_providers("test:local") == set()


def test_anthropic_configuration(monkeypatch):
    assert active_providers(None) == set()
    monkeypatch.setenv("ANTHROPIC_API_KEY", "synthetic-key")
    assert active_providers(None) == {"anthropic"}


def test_codex_presence_not_contents(monkeypatch, tmp_path):
    directory = tmp_path / "custom-codex"
    directory.mkdir()
    auth = directory / "auth.json"
    auth.write_text("synthetic-not-even-json")
    monkeypatch.setenv("CODEX_HOME", str(directory))
    # Reading pcode's own preferences is allowed; the provider's credential is not.
    read_text = Path.read_text
    forbidden = Mock(side_effect=AssertionError("must not read"))

    def guarded(self, *args, **kwargs):
        if self == auth:
            forbidden()
        return read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", guarded)
    assert active_providers("anthropic:custom") == {"anthropic", "openai-codex"}
    forbidden.assert_not_called()


def test_proxy_does_not_limit_providers(monkeypatch):
    monkeypatch.setenv("PCODE_LLM_PROXY", "http://localhost:8080")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "synthetic-key")
    assert active_providers("openai-codex:custom") == {"openai-codex", "anthropic"}


def test_anthropic_model_without_a_key_stays_unresolved():
    from pcode.agent import resolve_model

    # Unresolved, so the terminal opens and the first prompt explains the key.
    assert resolve_model("anthropic:claude-opus-5") == "anthropic:claude-opus-5"


@pytest.mark.parametrize("provider", sorted(PROVIDERS.keys() - {"claude"}))
def test_every_listed_provider_is_installed(provider):
    # A picker entry that raises ImportError on switch is worse than none.
    infer_provider_class(provider)


def test_claude_code_config_activates_claude(monkeypatch, tmp_path):
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    assert "claude" not in active_providers(None)
    (tmp_path / ".claude.json").touch()
    assert "claude" in active_providers(None)
    (tmp_path / ".claude.json").unlink()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "profile"))
    assert "claude" not in active_providers(None)
    (tmp_path / "profile").mkdir()
    assert "claude" in active_providers(None)
    # Picker entries are Anthropic's own model IDs.
    assert "claude:claude-opus-5" in model_catalog({"claude"})


def test_key_presence_enables_provider(monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "synthetic")
    monkeypatch.setenv("GEMINI_API_KEY", "synthetic")
    monkeypatch.setenv("AWS_PROFILE", "synthetic")
    assert active_providers(None) == {"groq", "google", "bedrock"}
    monkeypatch.setenv("GROQ_API_KEY", "  ")
    assert "groq" not in active_providers(None)


def test_endpoint_providers_need_both_variables(monkeypatch):
    monkeypatch.setenv("AZURE_OPENAI_API_KEY", "synthetic")
    assert active_providers(None) == set()
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://example.invalid")
    assert active_providers(None) == {"azure"}


def test_catalog_per_provider_and_typed_only_providers():
    models = model_catalog({"google", "xai", "openrouter", "claude"})
    assert any(name.startswith("google:gemini") for name in models)
    assert any(name.startswith("xai:grok") for name in models)
    assert any(name.startswith("claude:claude") for name in models)
    assert not any(name.startswith("openrouter:") for name in models)
    assert not any(name.startswith("anthropic:") for name in models)


def test_catalog_uses_installed_sdk_and_keeps_custom_current():
    models = model_catalog({"anthropic", "openai-codex"}, "anthropic:custom-id")
    assert "anthropic:custom-id" in models
    assert "anthropic:claude-opus-5" in models
    assert "openai-codex:gpt-5.6-luna" in models
    assert "openai-codex:gpt-6-astra" in models
    assert "openai-codex:gpt-5-chat-latest" in models
    assert len(models) == len(set(models))
    assert all(name.startswith(("anthropic:", "openai-codex:")) for name in models)
    assert model_catalog(set()) == []
    assert all(name.startswith("anthropic:") for name in model_catalog({"anthropic"}))


def test_numeric_version_sort_newest_first_with_alias_before_snapshots():
    expected = [
        "anthropic:claude-opus-5",
        "anthropic:claude-opus-4-10",
        "anthropic:claude-opus-4-9",
        "anthropic:claude-opus-4-5",
        "anthropic:claude-opus-4-5-20251101",
        "anthropic:claude-opus-4-5-20251001",
        "anthropic:claude-sonnet-5",
        "openai-codex:gpt-5.10",
        "openai-codex:gpt-5.9",
        "openai-codex:gpt-5.6-luna",
    ]
    assert sorted(reversed(expected), key=model_sort_key) == expected


def test_current_model_does_not_override_version_order():
    current = "anthropic:claude-opus-4-5"
    models = model_catalog({"anthropic"}, current)
    assert models.index("anthropic:claude-opus-5") < models.index(current)
