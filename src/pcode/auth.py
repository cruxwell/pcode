"""The login error every sign-in raises, and the API-key Anthropic model."""

import re


class LoginError(ValueError):
    """Safe-to-display authentication setup failure."""


def anthropic_model(model: str, key: str, *, http_client=None):
    from anthropic import AsyncAnthropic
    from pydantic_ai.models.anthropic import AnthropicModel
    from pydantic_ai.providers.anthropic import AnthropicProvider

    # Runtime owns visible transport retries; HTTP errors must surface immediately.
    client = AsyncAnthropic(api_key=key, http_client=http_client, max_retries=0)
    name = model.removeprefix("anthropic:")
    return AnthropicModel(
        name,
        provider=AnthropicProvider(anthropic_client=client),
        profile=FABLE_5_2_PROFILE if FABLE_5_2_MODELS.match(name) else None,
    )


# Pydantic AI 2.66 resolves these through the `claude-fable-5` and
# `claude-mythos-5` prefixes to the 5.0 profiles. Unlike those, they reject a
# forced tool choice with a 400 and bind thinking blocks to the conversation
# prefix on every account; without the binding flag, an edited prefix fails the
# turn instead of being retried with `drop_block`. Only the API-key provider is
# covered: Bedrock and Vertex names go straight to Pydantic AI. Remove once
# pydantic/pydantic-ai#12970 is in a release. `(?!\d)` keeps a future
# `claude-fable-5-20` out.
FABLE_5_2_MODELS = re.compile(r"claude-(?:fable|mythos)-5-2(?!\d)")
FABLE_5_2_PROFILE = {"supports_forced_tool_choice": False, "anthropic_binds_thinking_blocks": True}
