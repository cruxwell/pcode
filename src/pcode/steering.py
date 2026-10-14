"""Deliver pending steering between model requests; never skip tools."""

from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.messages import ModelRequest, UserPromptPart


def run_request(ctx, request_context) -> ModelRequest:
    """The step's request as the run's history holds it, for parts that must persist.

    The request being sent can end in a message only it carries (a capability
    such as Harness's `WarnNearLimits` appends one), and a before-hook's request is not written
    back to history, so a part added to that message would be sent once and lost.
    The history's own request is in the request list too, so it is still sent.
    """
    last = ctx.messages[-1] if ctx.messages else None
    return last if isinstance(last, ModelRequest) else request_context.messages[-1]


class Steering(AbstractCapability):
    def __init__(self, take_messages, has_messages=lambda: False):
        self.take_messages = take_messages
        # A peek that consumes nothing, for waits that should hand back early.
        self.has_messages = has_messages

    async def before_model_request(self, ctx, request_context):
        # Append to the framework's request so tool results remain ahead of the
        # new user input and the input is included in persisted message history.
        for text in self.take_messages():
            run_request(ctx, request_context).parts.append(UserPromptPart(text))
        return request_context


def _steering(ctx):
    """This run's own, never a parent's or a concurrent side question's input."""
    return [c for c in ctx.capabilities.values() if isinstance(c, Steering)]


def steering_pending(ctx) -> bool:
    return any(capability.has_messages() for capability in _steering(ctx))


def take_steering(ctx) -> list[str]:
    """Consume this run's steering now, for a hook with no model request to append to."""
    return [text for capability in _steering(ctx) for text in capability.take_messages()]
