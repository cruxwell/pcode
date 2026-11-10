"""Recognize a tool retry in message history, in either form Pydantic AI writes."""

from pydantic_ai.messages import RetryPromptPart, ToolReturnPart


def is_retry(part) -> bool:
    """A `RetryPromptPart`, or (Pydantic AI 2.66+) a tool return marked `retried`."""
    return isinstance(part, RetryPromptPart) or (
        isinstance(part, ToolReturnPart) and part.outcome == "retried"
    )
