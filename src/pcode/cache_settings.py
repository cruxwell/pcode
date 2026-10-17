"""Prompt-cache settings that follow a run, including delegated ones.

`agent.model_settings` covers the main agent only. A sub-agent inherits the
parent's *model object* but is run with `model_settings=None`
(`subagents/_toolset.py`), and cache settings live on the agent, not the model,
so a delegated run would otherwise request no caching at all. Applying them per
request also tracks a model switch without rebuilding the sub-agent.
"""

from dataclasses import replace
from typing import Any

from pydantic_ai import RunContext
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.models import ModelRequestContext

# Pydantic AI 2.45.0 adds no `cache_control` of its own: without these settings an
# Anthropic conversation re-reads its whole prefix at full price every request
# (confirmed against captured request bodies and saved-session usage records).
# `anthropic_cache` is the server-side automatic breakpoint, which moves forward as
# history grows; the two explicit breakpoints keep instructions and tool definitions
# cached.
ANTHROPIC_CACHE_SETTINGS = {
    "anthropic_cache": "5m",
    "anthropic_cache_instructions": True,
    "anthropic_cache_tool_definitions": True,
}


def model_settings(model: str) -> dict | None:
    """Default settings for a model name, as `Agent(model_settings=...)` wants them."""
    if model.startswith("openai-codex:"):
        # Codex does not emit visible reasoning unless summaries are requested.
        # Always receive them so Ctrl+T can reveal the preview mid-turn; the
        # display preference remains local and never changes reasoning effort.
        return {"openai_reasoning_summary": "detailed"}
    if model.startswith("anthropic:"):
        return dict(ANTHROPIC_CACHE_SETTINGS)
    return None


class ProviderCacheSettings(AbstractCapability):
    """Request prompt caching on providers that need it asked for explicitly.

    Settings already on the request win, so `/effort`, `/thinking` and an
    explicit override are never overwritten.
    """

    async def before_model_request(
        self, ctx: RunContext[Any], request_context: ModelRequestContext
    ) -> ModelRequestContext:
        if request_context.model.system != "anthropic":
            return request_context
        settings = request_context.model_settings or {}
        if any(key in settings for key in ANTHROPIC_CACHE_SETTINGS):
            return request_context
        return replace(request_context, model_settings={**ANTHROPIC_CACHE_SETTINGS, **settings})
