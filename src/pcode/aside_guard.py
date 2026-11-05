"""Refuse the tools a side question would use to change the conversation's state.

A side question runs on the conversation's own agent so its requests share the
provider cache with the turns around it, which means it also shares their
capability instances. Most tools are safe to share: a shell command or a file
edit changes the workspace, which the user asked about anyway. The plan and
delegated workers are different: they belong to the conversation, so a write
from a popup nobody is reading would silently replace the plan the main turn is
following, or start a worker billed to a turn that never asked for it.

The tools stay declared -- dropping them would change the request prefix and
break the cache -- and a call is answered with a failed result instead, so the
model reads why and carries on answering.
"""

from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.exceptions import ToolFailed
from pydantic_ai.messages import UserPromptPart

from pcode.steering import run_request
from pcode.tool_display import PLAN_TOOLS

# Reading the plan is how a side question learns what the turn is doing.
PLAN_READS = frozenset({"read_plan", "get_available_tasks"})
# `list_task_worktrees` only reads, so it stays available with the plan reads.
DELEGATION_TOOLS = frozenset({"delegate_task", "integrate_task", "discard_task"})
REFUSED_TOOLS = (PLAN_TOOLS - PLAN_READS) | DELEGATION_TOOLS


class AsideGuard(AbstractCapability):
    """Per-run capability for side questions; see the module docstring."""

    async def wrap_tool_execute(self, ctx, *, call, tool_def, args, handler):
        if call.tool_name in REFUSED_TOOLS:
            # A failed result rather than `ModelRetry`: nothing about the call
            # can be corrected, and a retry would spend the tool's retry budget.
            raise ToolFailed(
                f"{call.tool_name} is unavailable in a side question: the plan and "
                "delegated tasks belong to the main conversation. Answer without it."
            )
        return await handler(args)


class AsideBudget(AbstractCapability):
    """Warn a side question two requests before its request limit stops it.

    Hitting the limit throws away everything the run found. The warning is
    appended to the request the history keeps, as steering is, rather than
    added and later stripped the way Harness's `WarnNearLimits` does: a rewritten
    tail would break the cache and the Claude SDK's warm-session match.
    """

    def __init__(self, limit: int):
        self.limit = limit

    async def before_model_request(self, ctx, request_context):
        # `requests` counts the ones already sent, so this one and one more remain.
        if self.limit - ctx.usage.requests == 2:
            run_request(ctx, request_context).parts.append(
                UserPromptPart(
                    "[Side question] For this question, only this request and one more "
                    "remain before the limit ends it with no answer. Answer now with what "
                    "you have, and say what you could not check."
                )
            )
        return request_context
