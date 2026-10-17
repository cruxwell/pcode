"""Delegated runs must be cached, bounded, accounted for, and observable."""

import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace

import httpx2
import pytest
from anthropic import AsyncAnthropic
from anthropic_mock import assert_instructions_breakpoint
from pydantic_ai import Agent
from pydantic_ai.messages import ToolReturnPart
from pydantic_ai.models import ModelRequestContext, ModelRequestParameters
from pydantic_ai.models.anthropic import AnthropicModel
from pydantic_ai.models.function import DeltaToolCall, FunctionModel
from pydantic_ai.providers.anthropic import AnthropicProvider
from pydantic_ai.usage import UsageLimits
from pydantic_ai_harness.subagents import SubAgents

from pcode.agent import create_coder
from pcode.cache_settings import ANTHROPIC_CACHE_SETTINGS, ProviderCacheSettings

CHILD_INPUT_TOKENS = 100
CHILD_OUTPUT_TOKENS = 5
# Only the child reports cached reads, so the session total attributes them.
CHILD_CACHE_READ = 90


def mock_anthropic(bodies: list, client: httpx2.AsyncClient) -> AnthropicModel:
    """An Anthropic model whose requests are captured instead of sent."""
    return AnthropicModel(
        "claude-opus-5",
        provider=AnthropicProvider(
            anthropic_client=AsyncAnthropic(api_key="test", http_client=client)
        ),
    )


def responder(bodies: list):
    """Capture the request body and reply with a complete SSE stream.

    Delegated runs are streamed so the parent can report child activity, so a
    plain JSON body is rejected as a response that ended without content.
    """

    def handle(request):
        bodies.append(json.loads(request.content))
        events = [
            (
                "message_start",
                {
                    "message": {
                        "id": f"msg_{len(bodies)}",
                        "type": "message",
                        "role": "assistant",
                        "model": "claude-opus-5",
                        "content": [],
                        "usage": {
                            "input_tokens": CHILD_INPUT_TOKENS - CHILD_CACHE_READ,
                            "output_tokens": 0,
                            "cache_read_input_tokens": CHILD_CACHE_READ,
                        },
                    }
                },
            ),
            ("content_block_start", {"index": 0, "content_block": {"type": "text", "text": ""}}),
            (
                "content_block_delta",
                {"index": 0, "delta": {"type": "text_delta", "text": "explored"}},
            ),
            ("content_block_stop", {"index": 0}),
            (
                "message_delta",
                {
                    "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                    "usage": {"output_tokens": CHILD_OUTPUT_TOKENS},
                },
            ),
            ("message_stop", {}),
        ]
        return httpx2.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content="".join(
                f"event: {kind}\ndata: {json.dumps({'type': kind, **payload})}\n\n"
                for kind, payload in events
            ),
        )

    return handle


def delegating_parent(finish: str = "Done"):
    """A parent that delegates once, then answers from the child's result."""

    async def parent(messages, info):
        if any(isinstance(part, ToolReturnPart) for message in messages for part in message.parts):
            yield finish
            return
        yield {
            0: DeltaToolCall(
                name="delegate_task",
                json_args=json.dumps({"agent_name": "worker", "task": "Look"}),
            )
        }

    return parent


def coder_with_child(tmp_path, child_model=None):
    """Build the real capability tree, optionally pinning the sub-agent's model."""
    coder = create_coder(tmp_path)
    subagents = next(c for c in coder.capabilities if isinstance(c, SubAgents))
    if child_model is not None:
        subagents.agents[0].agent.model = child_model
    return coder


def test_delegated_anthropic_requests_carry_cache_control(tmp_path):
    """A parent's `model_settings` never reach a child run; the capability must."""
    bodies: list = []

    async def run():
        async with httpx2.AsyncClient(transport=httpx2.MockTransport(responder(bodies))) as http:
            coder = coder_with_child(tmp_path, mock_anthropic(bodies, http))
            agent = Agent(FunctionModel(stream_function=delegating_parent()), capabilities=[coder])
            await agent.run("Investigate", usage_limits=UsageLimits(request_limit=None))

    asyncio.run(run())
    assert bodies, "the sub-agent never reached the mocked provider"
    for body in bodies:
        assert body["cache_control"] == {"type": "ephemeral", "ttl": "5m"}
        assert body["tools"][-1]["cache_control"]
        assert_instructions_breakpoint(body["system"])


@pytest.mark.parametrize(
    "system, expected",
    [("anthropic", ANTHROPIC_CACHE_SETTINGS), ("openai", None)],
)
def test_cache_settings_apply_only_where_they_are_needed(system, expected):
    """Codex caches server-side, so only Anthropic gets client markers."""
    context = ModelRequestContext(
        model=SimpleNamespace(system=system),
        messages=[],
        model_settings=None,
        model_request_parameters=ModelRequestParameters(),
    )
    updated = asyncio.run(ProviderCacheSettings().before_model_request(None, context))
    assert updated.model_settings == expected

    # An explicit setting is the caller's decision and must survive.
    chosen = replace(context, model_settings={"anthropic_cache": "1h"})
    kept = asyncio.run(ProviderCacheSettings().before_model_request(None, chosen))
    assert kept.model_settings == {"anthropic_cache": "1h"}


def test_delegated_runs_are_recorded_without_becoming_the_conversation(tmp_path):
    """Child requests must be inspectable, and never restored as parent history."""
    import sqlite3

    from pcode.live import AgentRuntime
    from pcode.sessions import SavedSession

    bodies: list = []

    async def run(session):
        async with httpx2.AsyncClient(transport=httpx2.MockTransport(responder(bodies))) as http:
            coder = coder_with_child(tmp_path, mock_anthropic(bodies, http))
            runtime = AgentRuntime(
                Agent(FunctionModel(stream_function=delegating_parent()), capabilities=[coder]),
                session,
            )
            async for _ in runtime.stream("Explore"):
                pass

    session = SavedSession.create("test", tmp_path, tmp_path / "sessions")
    try:
        asyncio.run(run(session))
        database = session.directory / "steps.sqlite3"
        connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
        runs = connection.execute("SELECT run_id, parent_run_id FROM runs").fetchall()
        delegated = [run_id for run_id, parent in runs if parent is not None]
        connection.close()
        assert len(runs) == 2, runs
        assert len(delegated) == 1
        # Recovery must return the parent conversation. The child's history
        # starts from its task prompt and never contains the delegation call.
        recovered = asyncio.run(session.recover())
        parts = [part for message in recovered for part in message.parts]
        assert any(getattr(part, "content", None) == "Explore" for part in parts)
        assert any(getattr(part, "tool_name", None) == "delegate_task" for part in parts)
    finally:
        session.close()


def test_child_tokens_are_counted_once(tmp_path):
    """A per-delegation budget isolates request counts, not tokens.

    Child tokens still arrive in the parent's `result.usage`, so adding
    `DelegationEndEvent.usage` on top would double every delegated token.
    """
    from pcode.live import AgentRuntime

    bodies: list = []

    async def run():
        async with httpx2.AsyncClient(transport=httpx2.MockTransport(responder(bodies))) as http:
            coder = coder_with_child(tmp_path, mock_anthropic(bodies, http))
            runtime = AgentRuntime(
                Agent(FunctionModel(stream_function=delegating_parent()), capabilities=[coder])
            )
            async for _ in runtime.stream("Explore"):
                pass
            return runtime

    runtime = asyncio.run(run())
    assert bodies, "the sub-agent never reached the mocked provider"
    # Cached reads come only from the child, so they isolate its contribution
    # from the parent stub's synthetic usage: doubled would be 180.
    assert runtime.totals.cache_read == CHILD_CACHE_READ * len(bodies)
    # Its uncached input and output are included too, alongside the parent's.
    assert runtime.input_tokens >= CHILD_INPUT_TOKENS * len(bodies)
    assert runtime.output_tokens >= CHILD_OUTPUT_TOKENS * len(bodies)
