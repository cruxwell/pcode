"""Wire-level prefix checks, without upstream requests or credentials."""

import asyncio
import json

import httpx2
from anthropic import AsyncAnthropic
from anthropic_mock import anthropic_response
from pydantic_ai import Agent
from pydantic_ai.messages import ModelMessagesTypeAdapter
from pydantic_ai.models.anthropic import AnthropicModel
from pydantic_ai.providers.anthropic import AnthropicProvider
from pydantic_ai_harness.planning import InMemoryPlanStore, PlanItem

from pcode.cache_settings import ANTHROPIC_CACHE_SETTINGS
from pcode.planning import IdentifiedPlanning


def test_wire_prefix_tools_plan_changes_and_saved_resume():
    async def run():
        bodies = []
        store = InMemoryPlanStore()
        await store.set_items([PlanItem(id="first", content="First task")])

        def handle(request):
            body = json.loads(request.content)
            bodies.append(body)
            n = len(bodies)
            content = (
                [{"type": "tool_use", "id": f"call_{n}", "name": "probe", "input": {}}]
                if n < 4
                else [{"type": "text", "text": "done"}]
            )
            return anthropic_response(
                request,
                {
                    "id": f"msg_{n}",
                    "type": "message",
                    "role": "assistant",
                    "model": body["model"],
                    "content": content,
                    "stop_reason": "tool_use" if n < 4 else "end_turn",
                    "usage": {"input_tokens": 100, "output_tokens": 1},
                },
            )

        async with httpx2.AsyncClient(transport=httpx2.MockTransport(handle)) as http:
            model = AnthropicModel(
                "claude-opus-5",
                provider=AnthropicProvider(
                    anthropic_client=AsyncAnthropic(api_key="test", http_client=http)
                ),
            )

            def make_agent():
                return Agent(
                    model,
                    capabilities=[IdentifiedPlanning(store=store)],
                    model_settings=ANTHROPIC_CACHE_SETTINGS,
                )

            agent = make_agent()
            calls = 0

            @agent.tool_plain
            async def probe() -> str:
                nonlocal calls
                calls += 1
                if calls == 1:
                    await store.set_items([PlanItem(id="second", content="Changed task")])
                if calls == 2:
                    await store.set_items([])
                return "result"

            async with agent:
                result = await agent.run("Start")
            # JSON round trip is the saved-session boundary; new capabilities
            # must deduplicate from history, not private process-local counters.
            history = ModelMessagesTypeAdapter.validate_json(
                ModelMessagesTypeAdapter.dump_json(result.all_messages())
            )
            resumed = make_agent()
            async with resumed:
                await resumed.run("Continue", message_history=history)

        assert len(bodies) == 5
        assert "<plan-reminder>" not in json.dumps(bodies)
        assert all(body["cache_control"] == {"type": "ephemeral", "ttl": "5m"} for body in bodies)
        for before, after in zip(bodies, bodies[1:]):
            assert after["messages"][: len(before["messages"])] == before["messages"]

    asyncio.run(run())


def test_append_reminder_skips_a_history_ending_in_a_response():
    from types import SimpleNamespace

    from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, UserPromptPart

    from pcode.reminders import append_reminder

    def context(messages):
        ctx = SimpleNamespace(run_id="run", conversation_id="conv", messages=list(messages))
        return ctx, SimpleNamespace(messages=list(messages))

    # A suspended response is resumed, not answered; nothing may follow it.
    ctx, request = context([ModelResponse(parts=[TextPart("partial")])])
    append_reminder(ctx, request, "<tag>", "<tag> one")
    assert len(request.messages) == len(ctx.messages) == 1

    # A request gets the reminder once; the same text again is a no-op.
    ctx, request = context([ModelRequest(parts=[UserPromptPart("hi")])])
    append_reminder(ctx, request, "<tag>", "<tag> one")
    append_reminder(ctx, request, "<tag>", "<tag> one")
    assert [m.parts[0].content for m in request.messages] == ["hi", "<tag> one"]
    assert ctx.messages == request.messages
