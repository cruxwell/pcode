"""Workflows: fan work out to many sub-agents from one script (experimental).

Off by default. `/extensions on workflows` adds a `run_workflow` tool: the model
writes a short Python script in which `worker(task=...)` is an async function,
so it can gather a batch, chain one result into the next, and filter in plain
Python. The whole script is one tool call and only its last expression comes
back, where a hundred `delegate_task` reports would each land in the
conversation. This is Harness's `DynamicWorkflow`.

An extension cannot reach the session's own worker, so this builds one from the
same coder capabilities and, at each call, hands it what the parent agent gives
a `delegate_task` worker: the capabilities `delegate_task` shares with every
delegate (token accounting, turn limits, output limits) and the other
extensions' tools and guardrails. Those are found on the running agent by their
`ext.` id, so a capability an extension added under an id of its own is missed.

What a workflow worker still is not, compared with a `delegate_task` worker:

- It always shares the parent's checkout. There is no worktree isolation, so
  concurrent workers must not edit the same files.
- It has no worker id: no `ask_parent`, `manage_worker`, or detach on steering.
- Its tool calls are not shown. Progress arrives as a notice per finished worker.
- Its concurrency cap is its own, beside the one `delegate_task` workers share.
"""

import asyncio
from contextvars import ContextVar

DEFAULT_ENABLED = False  # Opt-in: /extensions on workflows.

TOOL = "run_workflow"
# Harness's default of 50 is under one sub-agent per item for a large batch.
MAX_AGENT_CALLS = 200
# Used when `worker_concurrency` is 0 (unlimited): a script can gather a
# hundred workers at once, and each is a full agent with its own shell.
DEFAULT_CONCURRENCY = 8

INSTRUCTIONS = f"""\
`{TOOL}` runs a Python script that calls sub-agents as async functions. Use it \
when the same kind of task repeats over many items (review each file, report on \
each PR) or when sub-agents feed each other without needing your judgment in \
between; use `delegate_task` for a few delegations whose results you want to read.
- Sub-agents see only their `task` string: put everything they need in it.
- For more than a handful of items, have each sub-agent write its full output to \
a file and return a short summary. A script that fails midway keeps only a \
preview of its latest results; the files are what a rerun can skip.
- Catch `RuntimeError` around a call whose failure should not abort the batch.
- Workflow sub-agents share your checkout and cannot ask you questions. Do not \
have concurrent ones edit the same files.
- At most {MAX_AGENT_CALLS} sub-agent runs per turn; pcode caps how many run at once."""

# The parent's capabilities for the workflow now running, set around the tool call.
_inherited: ContextVar[tuple] = ContextVar("workflow_inherited", default=())


def setup(pcode) -> None:
    if pcode.is_worker:
        return
    from pydantic_ai import Agent, ModelRetry
    from pydantic_ai.capabilities import AbstractCapability
    from pydantic_ai_harness.dynamic_workflow import DynamicWorkflow
    from pydantic_ai_harness.subagents import SubAgents

    from pcode.agent import (
        AGENT_INSTRUCTIONS,
        SUBAGENT_USAGE_LIMITS,
        WorkerRuntimeTools,
        create_coder,
        tool_retries,
    )
    from pcode.delegation import ResumableAgent
    from pcode.ext import ID_PREFIX
    from pcode.preferences import SETTINGS, load_preferences
    from pcode.worker_channel import ChildChannel, current_worker

    limit = int(
        load_preferences().get("worker_concurrency", SETTINGS["worker_concurrency"].default)
    )
    slots = asyncio.Semaphore(limit or DEFAULT_CONCURRENCY)
    counts = {"running": 0, "done": 0, "failed": 0}

    def own(capability) -> bool:
        identifier = str(getattr(capability, "id", None) or "")
        return identifier == pcode.id or identifier.startswith(pcode.id + ".")

    def inherited(agent) -> tuple:
        """What `_create_worker` and `_delegation` give a `delegate_task` worker."""
        found = []
        for capability in agent.root_capability.capabilities:
            if isinstance(capability, SubAgents):
                # `ask_parent` needs a worker handle, which only `delegate_task` binds.
                found += [
                    shared
                    for shared in capability.shared_capabilities
                    if not isinstance(shared, ChildChannel)
                ]
            elif str(getattr(capability, "id", None) or "").startswith(ID_PREFIX):
                if not own(capability):
                    found.append(capability)
        return tuple(found)

    class Leaf(ResumableAgent):
        """One workflow sub-agent run: under the concurrency cap, and counted."""

        async def run(self, user_prompt=None, *, capabilities=None, **kwargs):
            async with slots:
                counts["running"] += 1
                outcome = "failed"
                try:
                    result = await super().run(
                        user_prompt,
                        capabilities=[*(capabilities or ()), *_inherited.get()],
                        **kwargs,
                    )
                    outcome = "done"
                    return result
                except asyncio.CancelledError:
                    outcome = ""  # Interrupted: nothing to announce after the turn stopped.
                    raise
                finally:
                    counts["running"] -= 1
                    if outcome:
                        counts[outcome] += 1
                        failed = f", {counts['failed']} failed" if counts["failed"] else ""
                        pcode.ui.notify(
                            f"Workflow: {counts['done']} done{failed}, {counts['running']} running"
                        )

    worker = Agent(
        name="worker",
        retries=tool_retries(),
        description=(
            "Complete a self-contained task with the main agent's tools and permissions, "
            "in its checkout, and return a report"
        ),
        instructions=AGENT_INSTRUCTIONS
        + " You are a worker inside a scripted workflow. Complete only the task you were "
        "given and return exactly what it asks for; your reply goes to a script, not a "
        "person. You have none of the main conversation and cannot ask questions or "
        "delegate. Other workers share this checkout: do not edit files the task does "
        "not name. Stop jobs you no longer need with stop_job.",
        capabilities=[create_coder(pcode.workspace, delegation=False)],
        # The turn's MCP tools, as for a `delegate_task` worker.
        toolsets=[WorkerRuntimeTools()],
    )

    pcode.add_capability(
        DynamicWorkflow(
            agents=[Leaf(worker)],
            id=f"{pcode.id}.tool",
            tool_name=TOOL,
            max_agent_calls=MAX_AGENT_CALLS,
            # The session's model and a counter of its own per worker, as for a
            # `delegate_task` worker. Tokens are counted by the inherited capabilities.
            inherit_model=True,
            forward_usage=False,
            sub_agent_usage_limits=SUBAGENT_USAGE_LIMITS,
        )
    )

    class Guidance(AbstractCapability):
        """The parent's guidance. A shared `delegate_task` worker is handed this
        extension too, and has neither tool the text names."""

        def get_instructions(self):
            return lambda ctx: None if current_worker() is not None else INSTRUCTIONS

    pcode.add_capability(Guidance())

    @pcode.hooks.on.tool_execute
    async def with_parent_capabilities(ctx, *, call, tool_def, args, handler):
        if tool_def.name != TOOL:
            return await handler(args)
        if ctx.agent is None:
            # Without the agent there are no guardrails to hand on: do not run bare.
            raise ModelRetry(f"{TOOL} is unavailable in this run; use delegate_task instead.")
        counts.update(running=0, done=0, failed=0)
        token = _inherited.set(inherited(ctx.agent))
        try:
            return await handler(args)
        finally:
            _inherited.reset(token)

    @pcode.hooks.on.prepare_tools
    async def parent_only(ctx, tool_defs):
        # A delegate fanning out on its own is not what the parent asked for.
        if current_worker() is None:
            return tool_defs
        return [tool_def for tool_def in tool_defs if tool_def.name != TOOL]
