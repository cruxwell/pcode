"""Every model pcode can build pairs native tool search with a way to defer schemas.

A profile that offers `ToolSearchTool` without a `tool_deferral_mode` puts the
search on the wire and withholds every deferred tool instead of declaring it.
OpenAI answers `400 tools.tool_search requires at least one deferred tool`, and
since MCP servers are deferred by default, that fails every request of any
session with a server enabled. The per-provider profiles drift independently
across upgrades, so check the pairing for every buildable model at once rather
than trusting whichever provider happened to be fixed last.
"""

import json
import time
from typing import get_args

import pytest
from pydantic_ai.models import KnownModelName, infer_model
from pydantic_ai.native_tools._tool_search import ToolSearchTool

from pcode.agent import codex_model
from pcode.anthropic_oauth import AnthropicOAuthModel, OAuthTokens, write_tokens

# Construction-only placeholders: nothing here makes a request.
PLACEHOLDER_ENV = (
    "ANTHROPIC_API_KEY",
    "AWS_BEARER_TOKEN_BEDROCK",
    "CEREBRAS_API_KEY",
    "CRUSOE_API_KEY",
    "DEEPSEEK_API_KEY",
    "GOOGLE_API_KEY",
    "GROQ_API_KEY",
    "HEROKU_INFERENCE_KEY",
    "MOONSHOTAI_API_KEY",
    "OPENAI_API_KEY",
    "XAI_API_KEY",
    "ZAI_API_KEY",
)


def known_names() -> list[str]:
    return [name for name in get_args(KnownModelName.__value__) if isinstance(name, str)]


def unpaired(model) -> bool:
    searches = ToolSearchTool in model.profile.get("supported_native_tools", ())
    return searches and model.tool_deferral_mode is None


@pytest.fixture
def placeholder_credentials(monkeypatch, tmp_path):
    for name in PLACEHOLDER_ENV:
        monkeypatch.setenv(name, "placeholder")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")
    monkeypatch.delenv("PCODE_LLM_PROXY", raising=False)
    # codex_model loads the CLI's auth.json without a pcode store; never a real one.
    codex_home = tmp_path / "codex"
    codex_home.mkdir()
    tokens = {"access_token": "a", "refresh_token": "r", "account_id": "x"}
    (codex_home / "auth.json").write_text(json.dumps({"tokens": tokens}))
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    return tmp_path


def test_inferred_models_never_offer_search_without_deferral(placeholder_credentials):
    built = 0
    broken = []
    for name in known_names():
        try:
            model = infer_model(name)
            model.profile  # noqa: B018 - some providers resolve lazily
        except Exception:
            continue  # Needs real configuration (an endpoint, a gateway key).
        built += 1
        if unpaired(model):
            broken.append(name)
    # Guard against the check silently covering nothing after an upgrade.
    assert built > 100
    assert broken == []


def test_pcode_built_models_never_offer_search_without_deferral(placeholder_credentials):
    # Every OpenAI name through the Codex path: a family that gains native
    # search upstream is covered here the day it does.
    openai_names = [name.split(":", 1)[1] for name in known_names() if name.startswith("openai:")]
    models = [codex_model(f"openai-codex:{name}") for name in openai_names]
    assert any(ToolSearchTool in m.profile["supported_native_tools"] for m in models)

    credential = placeholder_credentials / "anthropic.json"
    write_tokens(credential, OAuthTokens("a", "r", time.time() + 3600))
    anthropic_names = [name for name in known_names() if name.startswith("anthropic:")]
    models += [AnthropicOAuthModel(name, path=credential) for name in anthropic_names]

    assert [m.model_name for m in models if unpaired(m)] == []
