import asyncio
import json

import httpx2
import pytest
from pydantic_ai import CachePoint
from pydantic_ai.exceptions import ModelHTTPError
from pydantic_ai.models.openai_codex import OpenAICodexModel
from pydantic_ai.providers.openai_codex import OpenAICodexCredentials, OpenAICodexProvider
from pydantic_ai.toolsets import FunctionToolset

from pcode.agent import create_agent


def test_native_codex_wire_payload_omits_unsupported_cache_marker(monkeypatch, tmp_path):
    monkeypatch.delenv("PCODE_LLM_PROXY", raising=False)
    payloads = []

    def handle(request):
        payloads.append(json.loads(request.content))
        return httpx2.Response(400, json={"error": {"message": "end of mock request"}})

    async def run():
        async with httpx2.AsyncClient(transport=httpx2.MockTransport(handle)) as client:
            provider = OpenAICodexProvider(
                credentials=OpenAICodexCredentials(
                    access_token="test-access",
                    refresh_token="test-refresh",
                    account_id="test-account",
                ),
                http_client=client,
            )

            def native_model(name, *, profile):
                return OpenAICodexModel(name, provider=provider, profile=profile)

            monkeypatch.setattr("pydantic_ai.models.openai_codex.OpenAICodexModel", native_model)
            agent = create_agent("openai-codex:gpt-5.6-sol", tmp_path)
            with pytest.raises(ModelHTTPError):
                await agent.run(["A durable user message", CachePoint()])
        assert len(payloads) == 1
        assert payloads[0]["model"] == "gpt-5.6-sol"
        assert payloads[0]["store"] is False
        assert payloads[0]["stream"] is True
        assert payloads[0]["reasoning"]["summary"] == "detailed"
        assert "prompt_cache_breakpoint" not in json.dumps(payloads[0])

    asyncio.run(run())


def test_deferred_mcp_schemas_reach_the_wire_beside_the_tool_search(monkeypatch, tmp_path):
    """`tool_search` without a deferred tool is a 400 that fails every later turn too."""
    monkeypatch.delenv("PCODE_LLM_PROXY", raising=False)
    payloads = []

    def handle(request):
        payloads.append(json.loads(request.content))
        return httpx2.Response(400, json={"error": {"message": "end of mock request"}})

    def deferred_toolset():
        toolset = FunctionToolset(id="fake")

        def search_drive(query: str) -> str:
            """Search Google Drive."""
            return query

        toolset.add_function(search_drive, name="search_drive")
        return toolset.defer_loading().prefixed("mcp_gdrive")

    async def run():
        async with httpx2.AsyncClient(transport=httpx2.MockTransport(handle)) as client:
            provider = OpenAICodexProvider(
                credentials=OpenAICodexCredentials(
                    access_token="test-access",
                    refresh_token="test-refresh",
                    account_id="test-account",
                ),
                http_client=client,
            )

            def native_model(name, *, profile):
                return OpenAICodexModel(name, provider=provider, profile=profile)

            monkeypatch.setattr("pydantic_ai.models.openai_codex.OpenAICodexModel", native_model)
            agent = create_agent("openai-codex:gpt-6-astra", tmp_path)
            with pytest.raises(ModelHTTPError):
                async with agent:
                    await agent.run("hello", toolsets=[deferred_toolset()])
        tools = payloads[0]["tools"]
        assert {"type": "tool_search"} in tools
        deferred = [tool["name"] for tool in tools if tool.get("defer_loading")]
        assert deferred == ["mcp_gdrive_search_drive"]

    asyncio.run(run())


def test_codex_summary_request_is_independent_of_display_and_effort(monkeypatch, tmp_path):
    from pcode.preferences import apply_effort, save_preferences

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    # create_agent builds the native Codex model, which loads the CLI's auth.json;
    # point it at a stub so the suite never reads a developer's real sign-in.
    codex_home = tmp_path / "codex"
    codex_home.mkdir()
    (codex_home / "auth.json").write_text(
        json.dumps(
            {
                "tokens": {
                    "access_token": "test-access",
                    "refresh_token": "test-refresh",
                    "account_id": "test-account",
                }
            }
        )
    )
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    for visibility in ("off", "status-line", "scrollback"):
        save_preferences(show_thinking=visibility)
        agent = create_agent("openai-codex:gpt-5.6-sol", tmp_path)
        assert agent.model_settings == {"openai_reasoning_summary": "detailed"}
        apply_effort(agent, "openai-codex:gpt-5.6-sol", "medium")
        assert agent.model_settings == {
            "openai_reasoning_summary": "detailed",
            "openai_reasoning_effort": "medium",
        }
        apply_effort(agent, "openai-codex:gpt-5.6-sol", "default")
        assert agent.model_settings == {"openai_reasoning_summary": "detailed"}


def test_non_codex_model_does_not_receive_codex_summary_setting(tmp_path):
    assert create_agent("test", tmp_path).model_settings is None
