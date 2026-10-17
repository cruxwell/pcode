"""`ClaudeModel`: an `AnthropicModel` whose requests go through a CLI process."""

import json
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from functools import cached_property
from typing import Any

from anthropic import AsyncAnthropic
from anthropic._models import construct_type
from anthropic.types.beta import BetaRawMessageStreamEvent
from pydantic_ai import RunContext
from pydantic_ai._utils import PeekableAsyncStream
from pydantic_ai.models import (
    ModelRequestParameters,
    StreamedResponse,
    check_allow_model_requests,
)
from pydantic_ai.models.anthropic import AnthropicModel
from pydantic_ai.providers.anthropic import AnthropicProvider

from pcode.claude_sdk.errors import MISSING_SDK, ClaudeSDKMissing
from pcode.claude_sdk.messages import _digest, lineage, normalize
from pcode.claude_sdk.session import SessionConfig
from pcode.claude_sdk.session_pool import pool
from pcode.claude_sdk.web import cli_tool_names
from pcode.claude_sdk.workspace import CWD_SETTING

PREFIX = "claude:"
EFFORTS = frozenset({"low", "medium", "high", "xhigh", "max"})


class ClaudeProvider(AnthropicProvider):
    """Names responses `claude`. Its client is never called: the CLI makes every request."""

    @property
    def name(self) -> str:
        return "claude"

    def __init__(self) -> None:
        super().__init__(
            anthropic_client=AsyncAnthropic(
                api_key="unused", base_url="https://claude-code.invalid", max_retries=0
            )
        )


class _Events:
    """Stream events as the Anthropic SDK types `_process_streamed_response` reads."""

    def __init__(self, events: AsyncIterator[dict]) -> None:
        self._events = events

    def __aiter__(self):
        return self

    async def __anext__(self):
        event = await self._events.__anext__()
        return construct_type(type_=BetaRawMessageStreamEvent, value=event)

    async def close(self) -> None:
        await self._events.aclose()


# Model setting: False for a one-off request (a session title), whose process
# is stopped once it answers. Kept, it would be the newest idle process and
# evict the conversation's warm one under `claude_idle_processes`.
KEEP_WARM_SETTING = "pcode_claude_keep_warm"


class ClaudeModel(AnthropicModel):
    @cached_property
    def profile(self):
        # No Anthropic server tool reaches the model through the CLI, but the
        # CLI's own WebSearch and WebFetch stand in for those two (`web.py`).
        # Wire tool deferral is off: hidden tools are withheld until found.
        from pydantic_ai.native_tools import WebFetchTool, WebSearchTool
        from pydantic_ai.profiles import merge_profile
        from pydantic_ai.profiles.anthropic import AnthropicModelProfile

        return merge_profile(
            super().profile,
            AnthropicModelProfile(
                supported_native_tools=frozenset({WebSearchTool, WebFetchTool}),
                tool_deferral_mode=None,
                tool_addition_mode=None,
                # The system prompt is fixed per process; mid-conversation
                # system text must travel as user text instead.
                supports_inline_system_prompts=False,
            ),
        )

    def session_config(self, system, tools, settings, native_tools=()) -> SessionConfig:
        if isinstance(system, str):
            prompt = system
        else:
            prompt = "\n\n".join(block["text"] for block in system if block.get("text"))
        wire_tools = [
            {
                "name": tool["name"],
                "description": tool.get("description") or "",
                "input_schema": tool["input_schema"],
            }
            for tool in tools
            if "input_schema" in tool
        ]
        effort = settings.get("anthropic_effort")
        thinking = settings.get("anthropic_thinking")
        max_tokens = settings.get("max_tokens")
        return SessionConfig(
            model=self.model_name,
            cwd=str(settings.get(CWD_SETTING) or os.getcwd()),
            system_prompt=prompt,
            tools=json.dumps(wire_tools, sort_keys=True),
            effort=effort if effort in EFFORTS else None,
            thinking=json.dumps(thinking, sort_keys=True) if isinstance(thinking, dict) else None,
            max_tokens=max_tokens if isinstance(max_tokens, int) and max_tokens > 0 else None,
            web_tools=cli_tool_names(native_tools),
        )

    async def request(self, messages, model_settings, model_request_parameters):
        async with self.request_stream(
            messages, model_settings, model_request_parameters
        ) as stream:
            async for _ in stream:
                pass
        return stream.get()

    async def count_tokens(self, messages, model_settings, model_request_parameters):
        raise NotImplementedError("claude: models cannot count tokens ahead of a request")

    @asynccontextmanager
    async def request_stream(
        self,
        messages,
        model_settings,
        model_request_parameters: ModelRequestParameters,
        run_context: RunContext[Any] | None = None,
    ) -> AsyncIterator[StreamedResponse]:
        check_allow_model_requests()
        settings, parameters = self.prepare_request(model_settings, model_request_parameters)
        settings = dict(settings or {})
        system, mapped = await self._map_message(messages, parameters, settings)
        tools, _ = self._prepare_tools_and_tool_choice(settings, parameters)
        config = self.session_config(system, tools, settings, parameters.native_tools)
        mapped = normalize(mapped)
        chain = lineage(mapped)
        sessions = pool()
        checkout = await sessions.checkout(config, mapped, chain)
        session = checkout.session
        ok = False
        try:
            # `_process_streamed_response` peeks the first event before reading.
            events = PeekableAsyncStream(_Events(session.response()))
            stream = await self._process_streamed_response(events, parameters, settings)
            yield stream
            # Anything short of the whole message leaves the process mid-turn.
            # A whole one is a fork point even if the process has since died.
            if session.complete and session.stray_call:
                # Resuming any of this history would show the model its own
                # refused calls again: the next request replays it instead.
                sessions.index.drop(chain)
            elif session.complete and settings.get(KEEP_WARM_SETTING, True):
                _, answer = await self._map_message([stream.get()], parameters, settings)
                answer = normalize(answer)
                if len(answer) == 1 and answer[0]["role"] == "assistant":
                    sessions.record(session, _digest(chain[-1], answer[0]))
                    ok = not session.dead
        finally:
            sessions.release(session, ok=ok)


def claude_model(model: str) -> ClaudeModel:
    from pcode.models import claude_sdk_installed

    name = model.removeprefix(PREFIX)
    if not name.strip():
        raise ValueError("Claude requires a model ID: claude:<model-id>")
    if not claude_sdk_installed():
        raise ClaudeSDKMissing(MISSING_SDK)
    return ClaudeModel(name, provider=ClaudeProvider())
