"""Capture the exact request boundary, never replay completed tool work."""

from copy import deepcopy

from pydantic_ai.capabilities import AbstractCapability, CapabilityOrdering
from pydantic_ai_harness.step_persistence import is_provider_valid

from pcode.images import as_in_history

# Seconds before a request that got no answer is resent, by a turn or a worker.
RETRY_DELAY = 1.0


class RequestCheckpoint(AbstractCapability):
    def __init__(self):
        self.messages = None
        self.step = 0

    def get_ordering(self):
        # Innermost, so this before-request hook runs after every other one
        # (steering, job notices, compaction) and sees the request as sent.
        return CapabilityOrdering(position="innermost")

    async def before_model_request(self, ctx, request_context):
        # Captured here rather than in `wrap_model_request`: that hook's handler
        # runs the whole before-request chain, and a streamed request's error
        # reaches the consumer while the wrap is still parked on the stream.
        # Keep a copy: the graph mutates history while unwinding a broken stream.
        # Images `ImageGate` left out of this request go back in: a recovered
        # history must not lose them because one request could not carry them.
        messages = as_in_history(list(request_context.messages))
        self.messages = deepcopy(messages) if is_provider_valid(messages) else None
        self.step = ctx.run_step
        return request_context

    async def wrap_model_request(self, ctx, *, request_context, handler):
        response = await handler(request_context)
        # Answered: resending this request would replay what it started.
        self.messages = None
        return response
