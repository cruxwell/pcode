"""Tokens the provider billed must be counted, including on a turn that dies."""

import asyncio
import json

import httpx2
import pytest
from anthropic import AsyncAnthropic
from pydantic_ai import Agent
from pydantic_ai.models.anthropic import AnthropicModel
from pydantic_ai.providers.anthropic import AnthropicProvider
from pydantic_ai.usage import RequestUsage

from pcode.live import AgentRuntime
from pcode.token_accounting import TokenTotals

INPUT_TOKENS = 100
OUTPUT_TOKENS = 5
CACHE_READ = 90
CACHE_WRITE = 5


def usage(index: int) -> RequestUsage:
    """Distinct per-request usage, so a miscount is visible in the total."""
    return RequestUsage(
        input_tokens=INPUT_TOKENS * index,
        output_tokens=OUTPUT_TOKENS * index,
        cache_read_tokens=CACHE_READ * index,
        cache_write_tokens=CACHE_WRITE * index,
    )


def test_totals_track_reads_and_writes_separately():
    totals = TokenTotals()
    totals.add(usage(1))
    totals.add(usage(2))
    assert (totals.input, totals.output) == (300, 15)
    assert (totals.cache_read, totals.cache_write) == (270, 15)
    # Pydantic AI defines `input_tokens` as including cached reads and writes.
    assert totals.uncached_input == 300 - 270 - 15


def sse(events: list[tuple[str, dict]]) -> str:
    return "".join(
        f"event: {kind}\ndata: {json.dumps({'type': kind, **payload})}\n\n"
        for kind, payload in events
    )


def message(*, blocks: list[tuple[str, dict]], stop_reason: str) -> str:
    """An Anthropic stream reporting the cache split the accounting reads."""
    events: list[tuple[str, dict]] = [
        (
            "message_start",
            {
                "message": {
                    "id": "msg",
                    "type": "message",
                    "role": "assistant",
                    "model": "claude-opus-5",
                    "content": [],
                    "usage": {
                        "input_tokens": INPUT_TOKENS - CACHE_READ - CACHE_WRITE,
                        "output_tokens": 0,
                        "cache_read_input_tokens": CACHE_READ,
                        "cache_creation_input_tokens": CACHE_WRITE,
                    },
                }
            },
        )
    ]
    for index, (kind, block) in enumerate(blocks):
        events.append(("content_block_start", {"index": index, "content_block": block}))
        if kind == "text":
            events.append(
                (
                    "content_block_delta",
                    {"index": index, "delta": {"type": "text_delta", "text": "hi"}},
                )
            )
        else:
            events.append(
                (
                    "content_block_delta",
                    {"index": index, "delta": {"type": "input_json_delta", "partial_json": "{}"}},
                )
            )
        events.append(("content_block_stop", {"index": index}))
    events.append(
        (
            "message_delta",
            {
                "delta": {"stop_reason": stop_reason, "stop_sequence": None},
                "usage": {"output_tokens": OUTPUT_TOKENS},
            },
        )
    )
    events.append(("message_stop", {}))
    return sse(events)


def runtime_with(handler) -> AgentRuntime:
    client = httpx2.AsyncClient(transport=httpx2.MockTransport(handler))
    model = AnthropicModel(
        "claude-opus-5",
        provider=AnthropicProvider(
            # Without this the SDK's default of 2 retries the 5xx itself, below
            # the runtime's own visible retry budget.
            anthropic_client=AsyncAnthropic(api_key="t", http_client=client, max_retries=0)
        ),
    )
    agent = Agent(model, retries=0)

    @agent.tool_plain
    async def probe() -> str:
        return "evidence"

    runtime = AgentRuntime(agent)
    runtime.retry_attempts = 0
    return runtime


def drain(runtime: AgentRuntime):
    async def run() -> None:
        async for _ in runtime.stream("Work"):
            pass

    return run


def test_failed_turn_still_counts_the_request_it_paid_for():
    """End-of-run accounting recorded nothing when a turn died mid-loop."""
    requests = 0

    def handle(request):
        nonlocal requests
        requests += 1
        if requests > 1:
            return httpx2.Response(500, json={"error": {"message": "provider exploded"}})
        return httpx2.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=message(
                blocks=[
                    ("tool_use", {"type": "tool_use", "id": "t1", "name": "probe", "input": {}})
                ],
                stop_reason="tool_use",
            ),
        )

    runtime = runtime_with(handle)
    with pytest.raises(Exception):
        asyncio.run(drain(runtime)())

    assert requests == 2, "the turn must fail on a later request, not the first"
    # The first request completed and was billed; the second never returned.
    assert runtime.input_tokens == INPUT_TOKENS
    assert runtime.output_tokens == OUTPUT_TOKENS
    assert runtime.totals.cache_read == CACHE_READ
    assert runtime.totals.uncached_input == INPUT_TOKENS - CACHE_READ - CACHE_WRITE


def test_successful_turn_counts_each_request_exactly_once():
    def handle(request):
        return httpx2.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=message(
                blocks=[("text", {"type": "text", "text": ""})], stop_reason="end_turn"
            ),
        )

    runtime = runtime_with(handle)
    asyncio.run(drain(runtime)())
    assert runtime.input_tokens == INPUT_TOKENS
    assert runtime.output_tokens == OUTPUT_TOKENS
    assert runtime.totals.cache_write == CACHE_WRITE


def test_totals_survive_a_saved_session_round_trip(tmp_path):
    """Cache counters are new fields; a session written before them must still load."""
    from pcode.sessions import SavedSession

    session = SavedSession.create("test", tmp_path, tmp_path / "sessions")
    try:
        runtime = AgentRuntime(Agent("test"), session)
        runtime.totals.add(usage(3))
        runtime._save_totals(session.info)
        session.save_info()
    finally:
        session.close()

    reopened = SavedSession.open("latest", tmp_path / "sessions")
    try:
        assert reopened.info.cache_read_tokens == CACHE_READ * 3
        restored = AgentRuntime(Agent("test"), reopened)
        assert restored.input_tokens == INPUT_TOKENS * 3
        assert restored.totals.cache_write == CACHE_WRITE * 3
    finally:
        reopened.close()
