"""Metadata discovery uses synthetic credentials and mocked HTTP only."""

import asyncio
import json
import time
from unittest.mock import AsyncMock

import httpx2
import pytest
from pydantic_ai.models.anthropic import AnthropicModel
from pydantic_ai.models.openai import OpenAIResponsesModel
from pydantic_ai.models.openai_codex import OpenAICodexModel
from pydantic_ai.providers.anthropic import AnthropicProvider
from pydantic_ai.providers.openai import OpenAIProvider
from pydantic_ai.providers.openai_codex import OpenAICodexCredentials, OpenAICodexProvider

from pcode.model_metadata import (
    CATALOG_TTL,
    CODEX_CATALOG_VERSION,
    NATIVE_TTL,
    ContextCatalog,
    ModelLimits,
    catalog_key,
    parse_anthropic,
    parse_catalog,
    parse_codex,
)


@pytest.fixture(autouse=True)
def isolated_codex_catalog_version(monkeypatch, tmp_path):
    # Model metadata tests must not inspect the user's CLI catalog either.
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))


PUBLIC = {
    "openai": {
        "models": {
            "example": {"limit": {"context": 1_050_000, "input": 922_000, "output": 128_000}}
        }
    },
    "anthropic": {"models": {"example": {"limit": {"context": 200_000, "output": 64_000}}}},
}
CODEX = {
    "models": [{"slug": "example", "context_window": 272_000, "max_context_window": 1_000_000}]
}


def codex(client, account="synthetic-account"):
    return OpenAICodexModel(
        "example",
        provider=OpenAICodexProvider(
            OpenAICodexCredentials(
                access_token="synthetic-token",
                refresh_token="synthetic-refresh",
                account_id=account,
            ),
            http_client=client,
        ),
    )


def test_normalize_limits_and_preserve_provenance():
    catalog = parse_catalog(PUBLIC, 123)
    limits = catalog["openai:example"]
    assert limits == ModelLimits(
        context=1_050_000, input=922_000, output=128_000, source="models.dev", fetched_at=123
    )
    assert limits.working_window() == 922_000
    assert limits.working_window(100_000) == 100_000
    assert limits.working_window(2_000_000) == 922_000
    assert "openai-codex:example" not in catalog
    native = parse_codex(CODEX, "example", 456)
    assert native.source == "codex" and native.fetched_at == 456
    assert native.working_window() == 272_000
    assert native.working_window(900_000) == 900_000
    assert native.working_window(2_000_000) == 1_000_000
    assert parse_codex(CODEX, "other", 456) is None
    native = parse_anthropic({"max_input_tokens": 180_000, "max_tokens": 16_000}, 123)
    assert native.input == 180_000 and native.output == 16_000
    assert native.context is None
    assert native.working_window(1_000_000) == 180_000


@pytest.mark.parametrize("bad", [None, {}, [], "unknown", 0, -1, True, "272000", 1.5])
def test_malformed_metadata_is_not_a_limit(bad):
    assert parse_catalog(bad, 1) == {}
    assert parse_codex(bad, "example", 1) is None
    assert parse_anthropic(bad, 1) is None
    assert parse_anthropic({"max_input_tokens": bad}, 1) is None
    assert (
        parse_codex({"models": [{"slug": "example", "context_window": bad}]}, "example", 1) is None
    )
    assert parse_catalog({"openai": {"models": {"example": {"limit": {"context": bad}}}}}, 1) == {}


def test_native_codex_transport_auth_singleflight_and_account_isolation(tmp_path):
    requests = []

    async def handler(request):
        requests.append(request)
        await asyncio.sleep(0)
        assert request.url.path == "/backend-api/codex/models"
        assert request.url.params["client_version"] == CODEX_CATALOG_VERSION
        assert request.headers["authorization"] == "Bearer synthetic-token"
        assert request.headers["chatgpt-account-id"] == "synthetic-account"
        return httpx2.Response(200, json=CODEX)

    async def run():
        async with httpx2.AsyncClient(transport=httpx2.MockTransport(handler)) as client:
            model = codex(client)
            service = ContextCatalog(tmp_path / "cache.json")
            service.public = parse_catalog(PUBLIC, time.time())
            assert service.window(model) is None  # No OpenAI fallback.
            await asyncio.gather(service.refresh_native(model), service.refresh_native(model))
            assert len(requests) == 1
            assert service.window(model) == 272_000
            assert service.limits(model).source == "codex"
            async with httpx2.AsyncClient() as other_client:
                other = codex(other_client, "other-account")
                assert service.window(other) is None
            # Authenticated data is never persisted in the public cache.
            assert not (tmp_path / "cache.json").exists()

    asyncio.run(run())


def test_native_anthropic_preserves_auth_base_url_and_input_limit(tmp_path):
    async def run():
        def handler(request):
            assert request.url.host == "deployment.example"
            assert request.url.path == "/prefix/v1/models/example"
            assert request.headers["x-api-key"] == "synthetic-key"
            return httpx2.Response(
                200, json={"id": "example", "max_input_tokens": 90_000, "max_tokens": 8_000}
            )

        async with httpx2.AsyncClient(transport=httpx2.MockTransport(handler)) as client:
            model = AnthropicModel(
                "example",
                provider=AnthropicProvider(
                    api_key="synthetic-key",
                    base_url="https://deployment.example/prefix",
                    http_client=client,
                ),
            )
            service = ContextCatalog(tmp_path / "cache.json")
            service.public = parse_catalog(PUBLIC, time.time())
            assert service.window(model) is None
            await service.refresh_native(model)
            assert service.window(model) == 90_000
            assert service.limits(model).output == 8_000

    asyncio.run(run())


def test_public_api_limits_do_not_leak_to_proxies():
    async def run():
        async with httpx2.AsyncClient() as client:
            direct = OpenAIResponsesModel(
                "example", provider=OpenAIProvider(api_key="synthetic", http_client=client)
            )
            proxy = OpenAIResponsesModel(
                "example",
                provider=OpenAIProvider(
                    api_key="synthetic", base_url="https://proxy.example/v1", http_client=client
                ),
            )
            anthropic = AnthropicModel(
                "example", provider=AnthropicProvider(api_key="synthetic", http_client=client)
            )
            service = ContextCatalog()
            service.public = parse_catalog(PUBLIC, 1)
            assert service.window(direct) == 922_000
            assert service.window(proxy) is None
            assert service.window(anthropic) == 200_000
            assert catalog_key("openai-responses:example") == "openai:example"
            assert catalog_key("unknown:example") == "unknown:example"
            assert catalog_key("unqualified") is None

    asyncio.run(run())


def test_public_refresh_disk_cache_expiry_stale_fallback_and_no_secrets(monkeypatch, tmp_path):
    requests = []
    broken = False

    def handler(request):
        requests.append(request)
        assert str(request.url) == "https://models.dev/api.json"
        assert "authorization" not in request.headers
        assert "x-api-key" not in request.headers
        return httpx2.Response(503 if broken else 200, json=PUBLIC)

    real_client = httpx2.AsyncClient
    monkeypatch.setattr(
        "pcode.model_metadata.httpx2.AsyncClient",
        lambda **kwargs: real_client(transport=httpx2.MockTransport(handler), **kwargs),
    )

    async def run():
        nonlocal broken
        path = tmp_path / "cache.json"
        service = ContextCatalog(path)
        await asyncio.gather(service.refresh_public(), service.refresh_public())
        assert len(requests) == 1
        assert service.window("openai:example") == 922_000
        stored = json.loads(path.read_text())
        assert stored["version"] == 1
        assert "cost" not in path.read_text()
        assert "credential" not in path.read_text()
        restored = ContextCatalog(path)
        await restored.refresh_public()
        assert len(requests) == 1
        assert restored.window("openai:example") == 922_000
        stored["fetched_at"] = time.time() - CATALOG_TTL - 1
        path.write_text(json.dumps(stored))
        broken = True
        offline = ContextCatalog(path)
        await offline.refresh_public()
        assert len(requests) == 2
        assert offline.window("openai:example") == 922_000
        await offline.refresh_public()
        assert len(requests) == 2  # Failure backoff, not one request per render/turn.
        assert json.loads(path.read_text()) == stored

    asyncio.run(run())


@pytest.mark.parametrize("content", ["not json", "[]", '{"version": 9}', '{"version": 1}'])
def test_corrupt_cache_and_network_failure_leave_unknown(monkeypatch, tmp_path, content):
    path = tmp_path / "cache.json"
    path.write_text(content)
    real_client = httpx2.AsyncClient
    monkeypatch.setattr(
        "pcode.model_metadata.httpx2.AsyncClient",
        lambda **kw: real_client(
            transport=httpx2.MockTransport(lambda request: httpx2.Response(503)), **kw
        ),
    )
    service = ContextCatalog(path)
    asyncio.run(service.refresh_public())
    assert service.window("openai:example") is None


def test_native_failure_retains_previous_value_and_cancellation_propagates(monkeypatch):
    requests = []
    fail = False
    cancel = False

    async def handler(request):
        requests.append(request)
        if cancel:
            raise asyncio.CancelledError()
        return httpx2.Response(503 if fail else 200, json=CODEX)

    async def run():
        nonlocal fail, cancel
        async with httpx2.AsyncClient(transport=httpx2.MockTransport(handler)) as client:
            model = codex(client)
            service = ContextCatalog()
            await service.refresh_native(model)
            original = service.limits(model)
            fail = True
            monkeypatch.setattr(
                "pcode.model_metadata.time.time", lambda: original.fetched_at + NATIVE_TTL + 1
            )
            await service.refresh_native(model)
            assert len(requests) == 2
            assert service.limits(model) is original
            await service.refresh_native(model)
            assert len(requests) == 2
            cancel = True
            service.state(model).attempted_at = 0
            with pytest.raises(asyncio.CancelledError):
                await service.refresh_native(model)

    asyncio.run(run())


def test_lookup_is_memory_only_and_ui_matches_compaction(monkeypatch):
    from pcode import model_metadata
    from pcode.compaction import effective_window
    from pcode.context_usage import context_label

    async def run():
        async with httpx2.AsyncClient() as client:
            model = codex(client)
            service = ContextCatalog()
            service.state(model).limits = parse_codex(CODEX, "example", 1)
            service.refresh = AsyncMock(side_effect=AssertionError("rendering did I/O"))
            monkeypatch.setattr(model_metadata, "catalog", service)
            assert effective_window(model) == 272_000
            assert context_label(model, []) == " · 0/272k"
            monkeypatch.setenv("PCODE_CONTEXT_WINDOW", "2000000")
            assert effective_window(model) == 1_000_000
            assert context_label(model, []) == " · 0/1m"
            service.refresh.assert_not_called()

    asyncio.run(run())


def test_codex_metadata_reuses_provider_refresh_and_replays_401():
    class Credentials:
        current = OpenAICodexCredentials(
            access_token="old-synthetic", refresh_token="refresh-synthetic", account_id="account"
        )
        saved = 0

        async def load(self):
            return self.current

        async def save(self, value):
            self.saved += 1
            self.current = value

    async def run():
        source = Credentials()
        seen = []

        def handler(request):
            seen.append((request.method, request.url.host, request.headers.get("authorization")))
            if request.url.host == "auth.openai.com":
                assert request.method == "POST"
                assert b"refresh-synthetic" in request.content
                return httpx2.Response(
                    200,
                    json={"access_token": "new-synthetic", "refresh_token": "new-refresh"},
                )
            if request.headers["authorization"] == "Bearer old-synthetic":
                return httpx2.Response(401, json={"error": {"message": "expired"}})
            assert request.headers["authorization"] == "Bearer new-synthetic"
            return httpx2.Response(200, json=CODEX)

        async with httpx2.AsyncClient(transport=httpx2.MockTransport(handler)) as client:
            model = OpenAICodexModel(
                "example",
                provider=OpenAICodexProvider(credential_source=source, http_client=client),
            )
            service = ContextCatalog()
            await service.refresh_native(model)
            assert service.window(model) == 272_000
            assert source.saved == 1
            assert [row[:2] for row in seen] == [
                ("GET", "chatgpt.com"),
                ("POST", "auth.openai.com"),
                ("GET", "chatgpt.com"),
            ]

    asyncio.run(run())


def test_proxied_codex_metadata_uses_owned_client_and_reopens(monkeypatch):
    from pcode.llm_proxy import ProxiedCodexProvider

    clients = []
    seen_proxy = []
    monkeypatch.setattr(
        "pydantic_ai.providers.openai_codex._read_codex_cli_credentials",
        lambda: OpenAICodexCredentials(
            access_token="synthetic", refresh_token="synthetic", account_id="synthetic"
        ),
    )

    def factory(self):
        seen_proxy.append(self._proxy_url)
        client = httpx2.AsyncClient(
            transport=httpx2.MockTransport(lambda request: httpx2.Response(200, json=CODEX))
        )
        clients.append(client)
        return client

    monkeypatch.setattr(ProxiedCodexProvider, "_new_http_client", factory)

    async def run():
        provider = ProxiedCodexProvider("http://127.0.0.1:12345")
        model = OpenAICodexModel("example", provider=provider)
        service = ContextCatalog()
        await service.refresh_native(model)
        assert service.window(model) == 272_000
        assert clients[0].is_closed
        # Next run still uses the same provider, not a standalone direct client.
        async with model:
            assert not clients[-1].is_closed
        assert all(client.is_closed for client in clients)
        assert len(clients) == 2
        assert seen_proxy == ["http://127.0.0.1:12345"] * 2

    asyncio.run(run())


def test_catalog_specific_provider_route_without_guessing(monkeypatch, tmp_path):
    from types import SimpleNamespace

    from pydantic_ai.models.test import TestModel

    from pcode.model_metadata import catalog_routes

    data = {
        "other": {
            "api": "https://other.example/v1",
            "models": {"example": {"limit": {"context": 128_000}}},
        }
    }
    service = ContextCatalog(tmp_path / "cache.json")
    service.public = parse_catalog(data, time.time())
    service.routes = catalog_routes(data)

    class RoutedModel(TestModel):
        @property
        def provider(self):
            return self._provider

    model = RoutedModel(model_name="example")
    model._provider = SimpleNamespace(
        name="other", client=SimpleNamespace(base_url="https://other.example/v1/")
    )
    assert service.window(model) == 128_000
    model.provider.client.base_url = "https://other.example:9999/v1"
    assert service.window(model) is None
    model.provider.client.base_url = "https://other.example/v1?deployment=custom"
    assert service.window(model) is None
    model.provider.client.base_url = "https://other.example/v2"
    assert service.window(model) is None


def test_runtime_refreshes_the_current_model_before_streaming(monkeypatch):
    from pydantic_ai import Agent
    from pydantic_ai.models.test import TestModel

    from pcode import model_metadata
    from pcode.live import AgentRuntime
    from pcode.turn import TurnContext

    first, second = TestModel(), TestModel()
    runtime = AgentRuntime(Agent(first))

    async def run():
        await runtime.refresh_context()
        model_metadata.catalog.refresh.assert_awaited_with(first)
        runtime.replace_agent(Agent(second))
        async for _ in runtime._stream("hello", TurnContext(run_id="run")):
            pass
        model_metadata.catalog.refresh.assert_awaited_with(second)

    asyncio.run(run())


def test_deferred_runtime_retains_model_for_native_cache(monkeypatch):
    from pydantic_ai import Agent

    from pcode import model_metadata
    from pcode.live import AgentRuntime

    async def run():
        async with httpx2.AsyncClient(
            transport=httpx2.MockTransport(lambda request: httpx2.Response(200, json=CODEX))
        ) as client:
            model = codex(client)
            service = ContextCatalog()
            service.refresh_public = AsyncMock()
            monkeypatch.setattr(model_metadata, "catalog", service)
            infer = AsyncMock()  # Only used to assert no second construction below.
            monkeypatch.setattr("pydantic_ai.models.infer_model", lambda _: model)
            runtime = AgentRuntime(Agent("openai-codex:example", defer_model_check=True))
            await runtime.refresh_context()
            assert runtime.agent.model is model
            assert service.window(runtime.agent.model) == 272_000
            monkeypatch.setattr("pydantic_ai.models.infer_model", infer)
            await runtime.refresh_context()
            infer.assert_not_called()
            # String discovery is public-only, not ephemeral native auth state.
            service.refresh_native = AsyncMock()
            await service.refresh("openai-codex:example")
            service.refresh_native.assert_not_called()

    asyncio.run(run())


@pytest.mark.parametrize(
    "cached, expected",
    [
        (None, CODEX_CATALOG_VERSION),
        ("0.99.0", CODEX_CATALOG_VERSION),
        ("0.154.0", CODEX_CATALOG_VERSION),
        ("0.155.0", "0.155.0"),
        ("1.0.0", "1.0.0"),
        ("0.1.2505171619", CODEX_CATALOG_VERSION),
        ("999.9.9&bad=query", CODEX_CATALOG_VERSION),
        (True, CODEX_CATALOG_VERSION),
    ],
)
def test_codex_catalog_version_uses_cache_only_as_a_version_hint(
    tmp_path, monkeypatch, cached, expected
):
    from pcode.model_metadata import codex_catalog_version

    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    (tmp_path / "models_cache.json").write_text(
        json.dumps(
            {
                "client_version": cached,
                "models": [{"slug": "example", "context_window": 9_999_999}],
            }
        )
    )
    assert codex_catalog_version() == expected
    assert ContextCatalog().window("openai-codex:example") is None


def test_astra_catalog_version_filter_regression(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    (tmp_path / "models_cache.json").write_text(json.dumps({"client_version": "0.155.0"}))

    async def run():
        def handler(request):
            # Real backend behavior: old versions silently omit newer models.
            models = []
            if request.url.params.get("client_version") == "0.155.0":
                models = [
                    {
                        "slug": "gpt-6-astra",
                        "context_window": 272_000,
                        "max_context_window": 872_000,
                    }
                ]
            return httpx2.Response(200, json={"models": models})

        async with httpx2.AsyncClient(transport=httpx2.MockTransport(handler)) as client:
            model = OpenAICodexModel("gpt-6-astra", provider=codex(client).provider)
            service = ContextCatalog()
            await service.refresh_native(model)
            assert service.window(model) == 272_000
            monkeypatch.setenv("PCODE_CONTEXT_WINDOW", "1050000")
            assert service.window(model) == 872_000

    asyncio.run(run())
