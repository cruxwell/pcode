"""Exercise the real SDK with synthetic responses, never live credentials."""

import asyncio
import json

import httpx2
import pytest
from pydantic_ai import Agent
from pydantic_ai.exceptions import ModelAPIError, ModelHTTPError

from pcode.auth import anthropic_model
from pcode.live import AgentRuntime, error_message


def build_model(client):
    return anthropic_model("anthropic:test-model", "synthetic-key", http_client=client)


@pytest.mark.parametrize("status", [400, 429, 500, 529])
def test_http_errors_surface_without_sdk_backoff(status):
    requests = []
    notices = []

    def handle(request):
        requests.append(request)
        return httpx2.Response(
            status,
            headers={"retry-after": "60", "x-should-retry": "true"},
            json={"error": {"type": "rate_limit_error", "message": "Synthetic provider failure"}},
        )

    async def run():
        async with httpx2.AsyncClient(transport=httpx2.MockTransport(handle)) as client:
            runtime = AgentRuntime(Agent(build_model(client)))
            runtime.retry_notice = notices.append
            try:
                async with asyncio.timeout(2):
                    with pytest.raises(ModelHTTPError) as caught:
                        async for _ in runtime.stream("hello"):
                            pass
                message = error_message(caught.value)
                assert f"HTTP {status}" in message
                if status == 429:
                    assert "rate limit reached" in message
                else:
                    assert "Synthetic provider failure" in message
            finally:
                runtime.close()

    asyncio.run(run())
    assert len(requests) == 1
    assert notices == []


@pytest.mark.parametrize("retries", [0, 2])
@pytest.mark.parametrize("overloaded", [False, True])
def test_openai_streamed_overload_uses_runtime_retry_budget(monkeypatch, retries, overloaded):
    from pydantic_ai.models.openai import OpenAIResponsesModel
    from pydantic_ai.providers.openai import OpenAIProvider

    from pcode import retries as retry_settings

    monkeypatch.setattr(retry_settings, "RETRY_DELAY", 0)
    requests, notices = [], []
    message = (
        "Our servers are currently overloaded. Please try again later."
        if overloaded
        else "Invalid request."
    )

    def handle(request):
        requests.append(request)
        payload = json.dumps({"error": {"message": message}})
        return httpx2.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=f"event: error\ndata: {payload}\n\n",
        )

    async def run():
        async with httpx2.AsyncClient(transport=httpx2.MockTransport(handle)) as client:
            model = OpenAIResponsesModel(
                "test-model", provider=OpenAIProvider(api_key="synthetic", http_client=client)
            )
            runtime = AgentRuntime(Agent(model))
            runtime.retry_attempts = retries
            runtime.retry_notice = notices.append
            try:
                with pytest.raises(ModelAPIError, match=message):
                    async for _ in runtime.stream("hello"):
                        pass
            finally:
                runtime.close()

    asyncio.run(run())
    expected_retries = retries if overloaded else 0
    assert len(requests) == expected_retries + 1
    assert len(notices) == expected_retries
    assert all("Retrying provider request" in notice for notice in notices)


@pytest.mark.parametrize("retries", [0, 1])
def test_transport_retries_are_owned_and_announced_by_runtime(retries):
    requests = []
    notices = []

    def handle(request):
        requests.append(request)
        raise httpx2.ReadTimeout("synthetic-private-transport-details", request=request)

    async def run():
        async with httpx2.AsyncClient(transport=httpx2.MockTransport(handle)) as client:
            runtime = AgentRuntime(Agent(build_model(client)))
            runtime.retry_attempts = retries
            runtime.retry_notice = notices.append
            try:
                with pytest.raises(ModelAPIError):
                    async for _ in runtime.stream("hello"):
                        pass
            finally:
                runtime.close()

    asyncio.run(run())
    assert len(requests) == retries + 1
    assert len(notices) == retries
    if retries:
        assert "timed out" in notices[0]
        assert "1/1" in notices[0]
        assert "synthetic-private" not in notices[0]
