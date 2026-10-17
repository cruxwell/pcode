import asyncio
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import httpx2
import pytest
from pydantic_ai.exceptions import ModelHTTPError
from pydantic_ai.providers.openai_codex import OpenAICodexCredentials

from pcode.agent import create_agent
from pcode.live import AgentRuntime
from pcode.llm_proxy import ProxiedCodexProvider


@pytest.fixture(autouse=True)
def isolated_configuration(monkeypatch):
    monkeypatch.delenv("PCODE_LLM_PROXY", raising=False)
    monkeypatch.setattr(
        "pydantic_ai.providers.openai_codex._read_codex_cli_credentials",
        lambda: OpenAICodexCredentials(
            access_token="test-access", refresh_token="test-refresh", account_id="test-account"
        ),
    )


@pytest.mark.parametrize("value", [None, "", "  "])
def test_unset_or_blank_preserves_default_provider(monkeypatch, tmp_path, value):
    if value is not None:
        monkeypatch.setenv("PCODE_LLM_PROXY", value)
    agent = create_agent("openai-codex:test", tmp_path)
    assert not isinstance(agent.model._provider, ProxiedCodexProvider)
    assert create_agent("test", tmp_path).model is not None

    async def close():
        async with agent:
            pass

    asyncio.run(close())


@pytest.mark.parametrize("proxy", ["http://127.0.0.1:8080", "not-a-url"])
@pytest.mark.parametrize("model", ["openai:gpt-4o", "anthropic:claude", "test"])
def test_other_providers_ignore_codex_proxy(monkeypatch, tmp_path, model, proxy):
    monkeypatch.setenv("PCODE_LLM_PROXY", proxy)
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-key")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "synthetic-key")
    agent = create_agent(model, tmp_path)
    assert not isinstance(getattr(agent.model, "_provider", None), ProxiedCodexProvider)

    async def close():
        async with agent:
            pass

    asyncio.run(close())


@pytest.mark.parametrize(
    "url", ["not-a-url", "http://", "socks5://localhost:8080", "http://user:secret@host:bad"]
)
def test_invalid_proxy_error_does_not_echo_url(monkeypatch, tmp_path, url):
    monkeypatch.setenv("PCODE_LLM_PROXY", url)
    with pytest.raises(ValueError) as error:
        create_agent("openai-codex:test", tmp_path)
    assert str(error.value) == "PCODE_LLM_PROXY must be a valid http:// or https:// proxy URL"
    assert error.value.__suppress_context__


def test_agent_runs_close_and_recreate_proxied_client(monkeypatch, tmp_path):
    clients = []
    requests = []

    def handle(request):
        requests.append(request)
        return httpx2.Response(400, json={"error": {"message": "mock response"}})

    def new_client(self):
        client = httpx2.AsyncClient(transport=httpx2.MockTransport(handle))
        clients.append(client)
        return client

    monkeypatch.setattr(ProxiedCodexProvider, "_new_http_client", new_client)
    monkeypatch.setenv("PCODE_LLM_PROXY", " http://127.0.0.1:8080 ")
    agent = create_agent("openai-codex:test", tmp_path)
    provider = agent.model._provider
    assert isinstance(provider, ProxiedCodexProvider)
    assert provider._proxy_url == "http://127.0.0.1:8080"

    runtime = AgentRuntime(agent)

    async def run():
        for _ in range(2):
            with pytest.raises(ModelHTTPError):
                _ = [event async for event in runtime.stream("hello")]
            assert clients[-1].is_closed

    asyncio.run(run())
    assert len(clients) == len(requests) == 2
    for request in requests:
        assert request.headers["Authorization"] == "Bearer test-access"
        payload = json.loads(request.content)
        assert payload["stream"] is True
        assert payload["store"] is False
        assert "prompt_cache_breakpoint" not in json.dumps(payload)
    assert provider._http_client is clients[-1]
    assert provider.client._client is clients[-1]


def test_nested_usage_and_cancellation_close_client(monkeypatch, tmp_path):
    monkeypatch.setenv("PCODE_LLM_PROXY", "http://127.0.0.1:8080")
    agent = create_agent("openai-codex:test", tmp_path)
    provider = agent.model._provider

    async def run():
        with pytest.raises(asyncio.CancelledError):
            async with agent:
                async with provider:
                    client = provider._http_client
                assert not client.is_closed
                raise asyncio.CancelledError
        assert client.is_closed
        async with agent:
            assert provider._http_client is not client
            assert not provider._http_client.is_closed
        assert provider._http_client.is_closed

    asyncio.run(run())


def test_only_model_client_uses_proxy(monkeypatch, tmp_path):
    received = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            received.append(("GET", self.path))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")

        def do_CONNECT(self):
            received.append(("CONNECT", self.path))
            # No external connection: recording CONNECT is enough to verify routing.
            self.send_response(502)
            self.end_headers()

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    proxy = f"http://127.0.0.1:{server.server_port}"
    monkeypatch.setenv("PCODE_LLM_PROXY", proxy)
    global_settings = {
        "HTTP_PROXY": "http://127.0.0.1:1",
        "HTTPS_PROXY": "http://127.0.0.1:1",
        "ALL_PROXY": "http://127.0.0.1:1",
        "NO_PROXY": "*",
    }
    for key, value in global_settings.items():
        monkeypatch.setenv(key, value)

    async def run():
        agent = create_agent("openai-codex:test", tmp_path)
        async with agent:
            client = agent.model._provider._http_client
            assert client.timeout.read == 600
            assert client.timeout.connect == 5
            await client.get("http://model.invalid/request")
            with pytest.raises(httpx2.ProxyError):
                await client.get("https://chatgpt.com/backend-api/codex/responses")
            # An unrelated HTTP client still follows its normal environment:
            # NO_PROXY=* means this request goes directly to the local server.
            async with httpx2.AsyncClient() as tool_client:
                await tool_client.get(f"{proxy}/tool")
        assert client.is_closed
        for key, value in global_settings.items():
            assert os.environ[key] == value

    try:
        asyncio.run(run())
    finally:
        server.shutdown()
        server.server_close()
        thread.join()
    assert received == [
        ("GET", "http://model.invalid/request"),
        ("CONNECT", "chatgpt.com:443"),
        ("GET", "/tool"),
    ]
