"""Web research ships as a bundled extension: native tools first, Exa or local ones otherwise."""

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from pydantic_ai import Agent
from pydantic_ai.messages import (
    ModelResponse,
    NativeToolCallPart,
    NativeToolReturnPart,
    TextPart,
    ToolReturnPart,
)
from pydantic_ai.models.function import DeltaToolCall, FunctionModel
from pydantic_ai.profiles import ModelProfile

from pcode.agent import create_agent, create_coder
from pcode.ext import BUNDLED_DIR, load_extensions, user_extension_dir
from pcode.extensions.web_research import EXA_PAGE_CHARS
from pcode.live import AgentRuntime
from pcode.preferences import save_preferences
from pcode.runtime import ToolStarted, ToolSummary

# FunctionModel advertises every native tool; this profile stands in for a
# provider without server-side search.
NO_NATIVE = ModelProfile(supported_native_tools=frozenset())


def web_extension(workspace):
    loaded = load_extensions(workspace)
    (extension,) = [e for e in loaded.extensions if e.name == "web_research"]
    assert extension.loaded, extension.error
    return extension


def request_shape(capabilities, profile=None, system=None):
    seen = {}

    def record(info):
        seen["tools"] = [tool.name for tool in info.function_tools]
        seen["native"] = [tool.kind for tool in info.model_request_parameters.native_tools]

    def model(messages, info):
        record(info)
        return ModelResponse(parts=[TextPart("ok")])

    async def stream(messages, info):
        record(info)
        yield "ok"

    function_model = FunctionModel(model, stream_function=stream, profile=profile)
    if system:
        function_model._system = system
    Agent(function_model, capabilities=capabilities).run_sync("hi")
    return seen


def test_bundled_extension_is_discovered_last_and_shadowed_by_user_files(tmp_path):
    extension = web_extension(tmp_path)
    assert extension.scope == "bundled"
    assert extension.path == BUNDLED_DIR / "web_research.py"
    assert extension.summary() == "2 tools"
    assert load_extensions(tmp_path).report(tmp_path) == [
        "basic_memory (bundled): off by default (/extensions on basic_memory)",
        "browser (bundled): /browser",
        "plan_mode (bundled): 1 tool, 2 hooks, /plan",
        "ponytail (bundled): off by default (/extensions on ponytail)",
        "sandbox (bundled): off by default (/extensions on sandbox)",
        "session_history (bundled): 2 tools",
        "web_research (bundled): 2 tools",
    ]

    user_extension_dir().mkdir(parents=True, exist_ok=True)
    (user_extension_dir() / "web_research.py").write_text("def setup(pcode):\n    pass\n")
    extension = web_extension(tmp_path)
    assert extension.scope == "user"
    assert extension.capabilities == []


def test_coder_itself_has_no_web_tools(tmp_path, monkeypatch):
    monkeypatch.setenv("EXA_API_KEY", "test-exa-key")
    shape = request_shape([create_coder(tmp_path)])
    assert {"read_file", "edit_file", "shell"} <= set(shape["tools"])
    assert not {"web_search", "get_page"} & set(shape["tools"])
    assert shape["native"] == []


@pytest.mark.parametrize("key", [None, "", "   ", "test-exa-key"])
def test_native_search_and_fetch_hide_the_local_tools(tmp_path, monkeypatch, key):
    monkeypatch.delenv("EXA_API_KEY", raising=False)
    if key is not None:
        monkeypatch.setenv("EXA_API_KEY", key)
    capabilities = web_extension(tmp_path).capabilities
    assert request_shape(capabilities) == {"tools": [], "native": ["web_search", "web_fetch"]}
    assert request_shape(capabilities, NO_NATIVE) == {
        "tools": ["web_search", "get_page"],
        "native": [],
    }


def test_local_policy_never_advertises_native_tools(tmp_path, monkeypatch):
    monkeypatch.delenv("EXA_API_KEY", raising=False)
    save_preferences(web_search="local")
    capabilities = web_extension(tmp_path).capabilities
    assert request_shape(capabilities) == {
        "tools": ["web_search", "get_page"],
        "native": [],
    }


def test_local_fetch_keeps_native_search(tmp_path, monkeypatch):
    monkeypatch.delenv("EXA_API_KEY", raising=False)
    save_preferences(web_fetch="local")
    capabilities = web_extension(tmp_path).capabilities
    assert request_shape(capabilities) == {"tools": ["get_page"], "native": ["web_search"]}


@pytest.mark.parametrize(
    "fetch,expected",
    [
        # Claude Code's own fetch answers with a summary, so it is opt-in.
        (None, {"tools": ["get_page"], "native": ["web_search"]}),
        ("native", {"tools": [], "native": ["web_search", "web_fetch"]}),
        ("local", {"tools": ["get_page"], "native": ["web_search"]}),
    ],
)
def test_claude_code_reads_pages_verbatim_unless_told_otherwise(
    tmp_path, monkeypatch, fetch, expected
):
    monkeypatch.delenv("EXA_API_KEY", raising=False)
    if fetch:
        save_preferences(web_fetch=fetch)
    capabilities = web_extension(tmp_path).capabilities
    assert request_shape(capabilities, system="claude") == expected
    # Any other model with a native fetch still uses it under the default.
    if fetch != "local":
        assert request_shape(capabilities, system="anthropic")["native"] == [
            "web_search",
            "web_fetch",
        ]


def test_off_policy_contributes_nothing(tmp_path):
    save_preferences(web_search="off")
    assert web_extension(tmp_path).capabilities == []


def test_exa_backs_the_local_tools_when_a_key_is_set(tmp_path, monkeypatch):
    monkeypatch.setenv("EXA_API_KEY", "test-exa-key")
    result = SimpleNamespace(
        url="https://example.com/docs",
        title="Example docs",
        published_date=None,
        author=None,
        highlights=["Search excerpt"],
        text="Full page content",
    )
    client = SimpleNamespace(
        search=AsyncMock(return_value=SimpleNamespace(results=[result], output=None)),
        get_contents=AsyncMock(return_value=SimpleNamespace(results=[result])),
    )
    requests = 0

    async def model(messages, info):
        nonlocal requests
        requests += 1
        names = {tool.name for tool in info.function_tools}
        assert {"web_search", "get_page", "read_file", "shell"} <= names
        assert "deep_search" not in names
        assert "test-exa-key" not in str(info)
        assert "research the web" in info.instructions
        if requests == 1:
            yield {0: DeltaToolCall(name="web_search", json_args='{"query":"example docs"}')}
            return
        parts = [p for m in messages for p in m.parts if isinstance(p, ToolReturnPart)]
        last = parts[-1]
        assert "https://example.com/docs" in str(last.content)
        if requests == 2:
            assert "Search excerpt" in str(last.content)
            yield {
                0: DeltaToolCall(name="get_page", json_args='{"url":"https://example.com/docs"}')
            }
        else:
            assert "Full page content" in str(last.content)
            yield "Found the docs."

    async def run():
        with patch("pydantic_ai_harness.exa._toolset.AsyncExa", return_value=client) as ctor:
            extension = web_extension(tmp_path)
            agent = create_agent("test", tmp_path, extension.capabilities)
            response = await agent.run(
                "Find example docs", model=FunctionModel(stream_function=model, profile=NO_NATIVE)
            )
            assert response.output == "Found the docs."
            assert all(args == () and kwargs == {} for args, kwargs in ctor.call_args_list)
        client.search.assert_awaited_once()
        client.get_contents.assert_awaited_once_with(
            # One over the cap, so a longer page is marked truncated.
            "https://example.com/docs",
            text={"max_characters": EXA_PAGE_CHARS + 1},
        )

    asyncio.run(run())


def test_duckduckgo_and_http_fetch_back_the_local_tools_without_a_key(tmp_path, monkeypatch):
    monkeypatch.delenv("EXA_API_KEY", raising=False)
    requests = 0

    async def model(messages, info):
        nonlocal requests
        requests += 1
        if requests == 1:
            yield {0: DeltaToolCall(name="web_search", json_args='{"query":"example docs"}')}
            return
        last = [p for m in messages for p in m.parts if isinstance(p, ToolReturnPart)][-1]
        assert "Example docs\nhttps://example.com/docs\nA snippet" in str(last.content)
        yield "Searched."

    async def run():
        extension = web_extension(tmp_path)
        fetch = next(
            tool
            for capability in extension.capabilities
            if type(capability).__name__ == "WebFetch"
            for tool in [capability.local]
        )
        assert fetch.name == "get_page"
        agent = create_agent("test", tmp_path, extension.capabilities)
        with patch("ddgs.ddgs.DDGS.text") as text:
            text.return_value = [
                {"title": "Example docs", "href": "https://example.com/docs", "body": "A snippet"}
            ]
            response = await agent.run(
                "Find example docs", model=FunctionModel(stream_function=model, profile=NO_NATIVE)
            )
        assert response.output == "Searched."
        text.assert_called_once_with("example docs", max_results=5)

    asyncio.run(run())


def test_native_search_streams_as_tool_rows():
    """Provider-executed searches have no function events; the response parts drive the rows."""

    async def model(messages, info):
        yield {
            0: NativeToolCallPart(
                tool_name="web_search", args={"query": "pydantic ai"}, tool_call_id="srv1"
            )
        }
        yield {
            1: NativeToolReturnPart(
                tool_name="web_search",
                content=[
                    {
                        "type": "web_search_result",
                        "title": "Example",
                        "url": "https://example.com",
                        "page_age": "2 weeks ago",
                        "encrypted_content": "EvEICioIFBgCIiQ2MmRjNGMxMy0zZDA2",
                    }
                ],
                tool_call_id="srv1",
            )
        }
        yield {
            2: NativeToolCallPart(
                tool_name="web_fetch", args={"url": "https://example.com"}, tool_call_id="srv2"
            )
        }
        yield {
            3: NativeToolReturnPart(
                tool_name="web_fetch",
                content={"type": "web_fetch_tool_result_error", "error_code": "url_not_accessible"},
                tool_call_id="srv2",
            )
        }
        yield "Done."

    async def run():
        runtime = AgentRuntime(Agent(FunctionModel(stream_function=model)))
        try:
            events = [event async for event in runtime.stream("hello")]
        finally:
            runtime.close()
        started = [(e.name, e.detail) for e in events if isinstance(e, ToolStarted)]
        assert started == [("web_search", "pydantic ai"), ("web_fetch", "https://example.com")]
        summaries = [(e.name, e.detail, e.failed) for e in events if isinstance(e, ToolSummary)]
        assert summaries == [
            ("web_search", "pydantic ai → 1 result", False),
            ("web_fetch", "https://example.com → Failed · url_not_accessible", True),
        ]
        # /tools shows the readable hit, not the provider-only ciphertext.
        search = next(e for e in events if isinstance(e, ToolSummary) and e.name == "web_search")
        assert "EvEICioI" not in search.result
        assert "Example\nhttps://example.com\nAge: 2 weeks ago" in search.result
        assert "encrypted" in search.result

    asyncio.run(run())


def test_bundled_file_is_valid_guide_material():
    """The shipped file doubles as the example for overriding it."""
    source = Path(BUNDLED_DIR / "web_research.py").read_text()
    assert "def setup(pcode)" in source
    assert "~/.config/pcode/extensions/web_research.py" in source
