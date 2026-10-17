"""Shared authentication helpers for Anthropic access."""


class LoginError(ValueError):
    """Safe-to-display authentication setup failure."""


def anthropic_model(model: str, key: str, *, http_client=None):
    from anthropic import AsyncAnthropic
    from pydantic_ai.models.anthropic import AnthropicModel
    from pydantic_ai.providers.anthropic import AnthropicProvider

    # Runtime owns visible transport retries; HTTP errors must surface immediately.
    client = AsyncAnthropic(api_key=key, http_client=http_client, max_retries=0)
    return AnthropicModel(
        model.removeprefix("anthropic:"), provider=AnthropicProvider(anthropic_client=client)
    )
