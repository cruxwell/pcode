"""Anthropic prompt caching is requested explicitly; other routes stay untouched."""

import json
from pathlib import Path

import pytest
from anthropic_mock import assert_instructions_breakpoint

from pcode.agent import create_agent
from pcode.cache_settings import ANTHROPIC_CACHE_SETTINGS, model_settings
from pcode.preferences import apply_effort, apply_thinking


@pytest.mark.parametrize(
    "model, expected",
    [
        ("anthropic:claude-opus-5", ANTHROPIC_CACHE_SETTINGS),
        ("openai-codex:gpt-6", {"openai_reasoning_summary": "detailed"}),
        ("test", None),
    ],
)
def test_cache_settings_only_on_anthropic(model, expected):
    assert model_settings(model) == expected


def test_agent_requests_caching_without_sharing_mutable_defaults(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    agent = create_agent("anthropic:claude-opus-5", tmp_path)
    assert agent.model_settings == ANTHROPIC_CACHE_SETTINGS
    agent.model_settings["anthropic_cache"] = "1h"
    assert ANTHROPIC_CACHE_SETTINGS["anthropic_cache"] == "5m"


def test_effort_and_thinking_preserve_cache_settings(tmp_path, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    agent = create_agent("anthropic:claude-opus-5", tmp_path)
    apply_effort(agent, "anthropic:claude-opus-5", "high")
    apply_thinking(agent, "anthropic:claude-opus-5", True)
    apply_thinking(agent, "anthropic:claude-opus-5", False)
    for key, value in ANTHROPIC_CACHE_SETTINGS.items():
        assert agent.model_settings[key] == value


def test_cache_control_reaches_the_wire(tmp_path, monkeypatch):
    """The settings must land as real breakpoints, not just as local settings."""
    import asyncio

    import httpx2
    from anthropic import AsyncAnthropic
    from pydantic_ai.models.anthropic import AnthropicModel
    from pydantic_ai.providers.anthropic import AnthropicProvider

    bodies = []

    events = [
        (
            "message_start",
            {
                "message": {
                    "id": "msg_test",
                    "type": "message",
                    "role": "assistant",
                    "model": "claude-opus-5",
                    "content": [],
                    "usage": {"input_tokens": 1, "output_tokens": 0},
                }
            },
        ),
        ("content_block_start", {"index": 0, "content_block": {"type": "text", "text": ""}}),
        ("content_block_delta", {"index": 0, "delta": {"type": "text_delta", "text": "ok"}}),
        ("content_block_stop", {"index": 0}),
        (
            "message_delta",
            {
                "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                "usage": {"output_tokens": 1},
            },
        ),
        ("message_stop", {}),
    ]

    def handle(request):
        bodies.append(json.loads(request.content))
        return httpx2.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content="".join(
                f"event: {kind}\ndata: {json.dumps({'type': kind, **payload})}\n\n"
                for kind, payload in events
            ),
        )

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key")
    agent = create_agent("anthropic:claude-opus-5", Path(tmp_path))
    agent.model = AnthropicModel(
        "claude-opus-5",
        provider=AnthropicProvider(
            anthropic_client=AsyncAnthropic(
                api_key="test-key",
                http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(handle)),
            )
        ),
    )

    async def run():
        async with agent:
            await agent.run("hello")

    asyncio.run(run())
    body = bodies[0]
    assert body["cache_control"] == {"type": "ephemeral", "ttl": "5m"}
    assert body["tools"][-1]["cache_control"]
    assert_instructions_breakpoint(body["system"])
