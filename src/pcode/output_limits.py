"""Model-aware output defaults, separate from reasoning effort and visibility."""

from dataclasses import replace

from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.models.anthropic import AnthropicModel

from pcode.model_metadata import catalog, refresh_context

# Anthropic requires max_tokens. Pydantic AI before 2.52 sent 4096, which thinking
# or one file-writing tool call can exhaust; 2.52 asks for the model's maximum
# output when its own profile knows it. pcode resolves the serving route's limit
# (Claude Code aliases, for one) from its catalog instead, and unknown or
# offline routes cannot safely borrow another endpoint's model limits.
FALLBACK_OUTPUT_TOKENS = 16_384


class ModelOutputLimits(AbstractCapability):
    """Resolve each request's default, including model switches and subagents.

    Use the serving model's advertised maximum, not a guessed family-name table.
    A maximum is a ceiling, not a requested response length or thinking budget.
    Other adapters retain their provider defaults; explicit settings always win.
    """

    async def before_model_request(self, ctx, request_context):
        if not isinstance(request_context.model, AnthropicModel):
            return request_context
        settings = request_context.model_settings or {}
        if settings.get("max_tokens") is not None:
            return request_context
        await refresh_context(request_context.model)
        limits = catalog.limits(request_context.model)
        maximum = limits.output if limits else None
        if maximum is None and request_context.model.system == "claude":
            # The CLI's own default beats a guess below it (a `claude:opus` alias).
            return request_context
        return replace(
            request_context,
            model_settings={**settings, "max_tokens": maximum or FALLBACK_OUTPUT_TOKENS},
        )
