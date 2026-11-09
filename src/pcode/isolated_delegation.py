"""Worktree-aware delegation over the pinned Harness run/event/budget machinery.

Only the built-in worker is reconstructible in another workspace. Specialized
extension agents keep their own capabilities and shared-workspace semantics.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
from contextvars import ContextVar
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

import anyio
from pydantic_ai import ModelRetry, RunContext
from pydantic_ai.messages import UserPromptPart
from pydantic_ai.workspaces import LocalWorkspaceBackend, Workspace
from pydantic_ai_harness.subagents import SubAgents
from pydantic_ai_harness.subagents._toolset import SubAgentToolset, _Ended

from pcode.delegation import ResumableAgent
from pcode.error_report import error_message
from pcode.preferences import SETTINGS, load_preferences
from pcode.steering import run_request
from pcode.task_worktrees import TaskWorktrees
from pcode.worker_channel import (
    NOTIFY_SECONDS,
    PARENT_INSTRUCTIONS,
    WorkerHub,
    bind_worker,
    current_worker,
    labelled,
    notify,
    render_brief,
    report_text,
    unbind_worker,
)
from pcode.worktree import WorktreeError, describe, setup_scripts

logger = logging.getLogger(__name__)
_outcome: ContextVar[str | None] = ContextVar("isolated_delegation_outcome", default=None)

# Setup hooks run with no model watching and their output discarded, so a hang
# is invisible until the user gives up. Room for a cold dependency install.
SETUP_TIMEOUT_SECONDS = 900


# Set for the session by a mode that wants every worker isolated (`/manager`)
# without rewriting the user's preferences. `worktree` must still be on.
force_isolation = False


def isolation_enabled() -> bool:
    """Require explicit worker opt-in as well as the active worktree preference.

    Never under a remote profile: a task worktree runs the project's
    `worktree-setup` outside the sandbox, from a branch the model can commit to.
    """
    from pcode import remote_profile

    if remote_profile.active() is not None:
        return False
    preferences = load_preferences()
    if force_isolation and preferences.get("worktree", SETTINGS["worktree"].default) == "on":
        return True
    return all(
        preferences.get(key, SETTINGS[key].default) == "on"
        for key in ("worktree", "worker_isolation")
    )


async def _joined(task):
    """Do not abandon a mutation/cleanup thread when the caller is cancelled."""
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
    try:
        result = task.result()
    except Exception:
        if cancelled:
            raise asyncio.CancelledError from None
        raise
    if cancelled:
        raise asyncio.CancelledError
    return result


async def _finish(store, task_id, status, summary):
    return await _joined(
        asyncio.create_task(asyncio.to_thread(store.finish, task_id, status, summary, wait=True))
    )


async def _stop_setup(process):
    # Setup owns a whole process group, including grandchildren holding pipes.
    # Always kill the group after the grace period, even if its leader exited.
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        await asyncio.wait_for(process.wait(), timeout=1)
    except asyncio.TimeoutError:
        pass
    finally:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        await process.wait()


async def _setup(tree):
    """Use the trusted setup policy, but own cancellable subprocesses per task."""
    env = {
        **os.environ,
        "PCODE_MAIN": str(tree.main),
        "PCODE_WORKTREE": str(tree.path),
        "PCODE_BRANCH": tree.branch,
    }
    for script in setup_scripts(tree):
        command = [str(script)] if os.access(script, os.X_OK) else ["sh", str(script)]
        launch = asyncio.create_task(
            asyncio.create_subprocess_exec(
                *command,
                cwd=tree.path,
                env=env,
                start_new_session=True,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
        )
        process = None
        try:
            # If cancelled during spawn, join it to obtain and reap its process.
            process = await asyncio.shield(launch)
            code = await process.wait()
            if code:
                raise WorktreeError(f"{script} exited {code}")
        except asyncio.CancelledError:
            if process is None:
                try:
                    await _joined(launch)
                except asyncio.CancelledError:
                    pass
                process = launch.result()
            await _joined(asyncio.create_task(_stop_setup(process)))
            raise


def _worker_slots() -> asyncio.Semaphore | None:
    limit = int(
        load_preferences().get("worker_concurrency", SETTINGS["worker_concurrency"].default)
    )
    return asyncio.Semaphore(limit) if limit else None


@dataclass(kw_only=True)
class WorkspaceSubAgents(SubAgents):
    workspace: Path
    worker_factory: Any
    worker_slots: asyncio.Semaphore | None = field(
        default_factory=_worker_slots, repr=False, compare=False
    )
    # The parent's workers; found by `worker_channel.hub_for(ctx)`.
    hub: WorkerHub = field(default_factory=WorkerHub, repr=False, compare=False)

    async def before_model_request(self, ctx, request_context):
        # A worker's question rides the parent's next request, like steering.
        request_context = await super().before_model_request(ctx, request_context)
        for message in self.hub.take_questions():
            run_request(ctx, request_context).parts.append(UserPromptPart(message))
        return request_context

    def get_toolset(self):
        return WorkspaceSubAgentToolset(
            workspace=self.workspace,
            worker_factory=self.worker_factory,
            worker_slots=self.worker_slots,
            hub=self.hub,
            agents=self._by_name,
            forward_usage=self.forward_usage,
            inherit_tools=self.inherit_tools,
            shared_capabilities=self.shared_capabilities,
            event_stream_handler=self.event_stream_handler,
            tool_name=self.tool_name,
            tool_retries=self.tool_retries,
            contain_errors=self.contain_errors,
            call_counts=self._call_counts,
            models=self._menu,
        )

    def get_instructions(self):
        return (super().get_instructions() or "") + (
            "\nWorker workspace_mode defaults to auto: shared unless both worktree=on and "
            "worker_isolation=on in the active workspace's config. worker_isolation defaults "
            "to off; worktree alone never isolates workers. Isolation requires a clean tracked "
            "parent checkout; commit a checkpoint first, never stash or silently switch modes. "
            "Use shared explicitly for work that needs the live parent files; coordinate writes. "
            "Specialized delegates retain their own tools and use shared mode. "
            "Isolated results are branches, not changes to your checkout. Review them, then "
            "integrate_task into this parent branch and verify the combined changes. "
            "Use list_task_worktrees to recover task IDs after restart. "
            "Never push worker branches or merge them directly into mainline. "
            "Ask the user before discard_task(confirm=True) destroys unintegrated work."
            + PARENT_INSTRUCTIONS
            + (
                # Harness's listing above reads as the only keys allowed.
                "\nBesides the keys listed, `model` takes any other provider:model name."
                if self._menu
                else ""
            )
        )


class WorkspaceSubAgentToolset(SubAgentToolset):
    def __init__(self, *, workspace: Path, worker_factory, worker_slots, hub=None, **kwargs):
        self.workspace = workspace
        self.worker_factory = worker_factory
        self.worker_slots = worker_slots
        self.hub = hub if hub is not None else WorkerHub()
        super().__init__(**kwargs)
        self.add_function(self.integrate_task)
        self.add_function(self.discard_task)
        self.add_function(self.list_task_worktrees)
        self.add_function(self.answer_worker)
        self.add_function(self.message_worker)
        self.add_function(self.stop_worker)
        self.add_function(self.list_workers)

    def _prepare_delegate(self, ctx, tool_def):
        """Offer `model` as any `provider:model` name, not only a /subagents key.

        The description is static, so the schema stays cache-stable however
        many names get resolved on demand.
        """
        tool_def = super()._prepare_delegate(ctx, tool_def)
        if tool_def is None:
            return None
        schema = {**tool_def.parameters_json_schema}
        schema["properties"] = {
            **schema.get("properties", {}),
            "model": {
                "type": "string",
                "description": (
                    "Optional model to run the sub-agent on, as provider:model (any "
                    "/model name), such as for a second opinion from another model. "
                    "Omit it to use the sub-agent's default, the session's model."
                ),
            },
        }
        return replace(tool_def, parameters_json_schema=schema)

    async def _add_model(self, agent_name: str, key: str | None) -> None:
        """Put a name off the /subagents menu on it, resolved the way /btw resolves one.

        Harness's own key check then runs unchanged. A delegate restricted to
        some keys is left for that check to refuse, without resolving anything.
        """
        sub_agent = self._agents.get(agent_name)
        if key is None or key in self._models or sub_agent is None or sub_agent.models:
            return
        from pydantic_ai_harness.subagents import ModelOption

        from pcode.agent import side_model

        try:
            # Reads logins and preferences from disk.
            chosen = await asyncio.to_thread(side_model, key)
        except ValueError as error:
            raise ModelRetry(str(error)) from error
        except Exception as error:
            # A broken login file is the model's to work around, not a failed turn.
            raise ModelRetry(f"Cannot use {key}: {type(error).__name__}: {error}") from error
        # Kept for later delegations in this session; the schema never lists it.
        self._models[key] = ModelOption(chosen.model, settings=chosen.settings)

    async def delegate_task(
        self,
        ctx: RunContext,
        agent_name: str,
        task: str,
        workspace_mode: Literal["auto", "isolated", "shared"] = "auto",
        model: str | None = None,
        purpose: str = "",
        context: str = "",
        boundaries: str = "",
        done_when: str = "",
        include_reports: list[str] | None = None,
        plan_step: str = "",
        run_in_background: bool = False,
        continue_worker: str = "",
    ) -> Any:
        """Delegate a self-contained task; the child does not see this conversation.

        auto shares the parent's live files unless both worktree=on and
        worker_isolation=on in the active config. isolated requires both settings
        and clean tracked files, creates a branch at the parent's HEAD, and returns
        a persistent task record. shared is not read-only. Specialized agents only
        support shared mode. Review isolated results before integrate_task.

        Args:
            purpose: What the sub-agent is doing, at most 8 words, present tense
                (e.g. "fixing the /btw cache prefix"). It labels the delegation
                in the user's task list; the sub-agent never sees it.
            context: Background the worker needs: relevant files, earlier decisions.
            boundaries: What the worker must not touch or change.
            done_when: How the worker knows it is finished, e.g. which test passes.
            include_reports: Worker ids whose final reports are added to the brief.
            plan_step: The id of the plan step this worker carries out, if any.
            run_in_background: Return at once with the worker id; the result arrives
                as a later message. Use it to run independent workers in parallel.
            continue_worker: Resume this finished shared-mode worker with its own
                history; `task` is the follow-up instruction.
        """
        # `purpose` is display-only: the event layer reads it from the call's arguments.
        # `run_in_background` is acted on by `BackgroundDelegation`, around this call.
        arguments = {
            "agent_name": agent_name,
            "task": task,
            "purpose": purpose,
            "plan_step": plan_step,
            "model": model,
            "continue_worker": continue_worker,
            "include_reports": include_reports,
            "workspace_mode": workspace_mode,
        }
        # Validates everything that can refuse the call before changing any state.
        handle = self.hub.claim(ctx.tool_call_id or uuid4().hex, arguments)
        reports = [self.hub.require(worker_id) for worker_id in include_reports or ()]
        brief = render_brief(
            task, context=context, boundaries=boundaries, done_when=done_when, reports=reports
        )
        token = bind_worker(handle)
        outcome = _outcome.set(None)
        handle.status = "running"
        handle.started = True
        handle.previous = None
        unread = 0
        try:
            await notify(handle, ctx, status="running")
            if agent_name == "worker" and self.worker_slots is not None:
                async with self.worker_slots:
                    result = await self._delegate(ctx, agent_name, brief, workspace_mode, model)
            else:
                result = await self._delegate(ctx, agent_name, brief, workspace_mode, model)
        except BaseException:
            handle.status = "stopped" if handle.stop_requested else "failed"
            raise
        else:
            failed = _outcome.get() not in (None, "ok") or (
                isinstance(result, dict) and result.get("status") == "failed"
            )
            handle.status = "failed" if failed else "done"
            handle.report = report_text(result)
        finally:
            _outcome.reset(outcome)
            unbind_worker(token)
            handle.resume_from = None
            handle.question = None
            unread = len(handle.inbox)
            handle.inbox.clear()
            if handle.status != "running":
                with anyio.move_on_after(NOTIFY_SECONDS, shield=True):
                    await notify(handle, ctx, status=handle.status, tokens=handle.tokens)
        result = labelled(handle, result)
        if unread and isinstance(result, str):
            result += (
                f"\n\n[pcode] {unread} message(s) sent with message_worker arrived after the "
                "worker's last step and were not read."
            )
        return result

    async def _delegate(self, ctx, agent_name, task, workspace_mode, model):
        await self._add_model(agent_name, model)
        isolated = workspace_mode == "isolated" or (
            workspace_mode == "auto" and agent_name == "worker" and isolation_enabled()
        )
        handle = current_worker()
        if handle is not None and handle.resume_from is not None:
            # A resumed worker carries on in the workspace it worked in: shared.
            isolated = False
        if handle is not None:
            handle.workspace_mode = "isolated" if isolated else "shared"
            await notify(handle, ctx, workspace=handle.workspace_mode)
        if not isolated:
            return await super().delegate_task(ctx, agent_name, task, model)
        if not isolation_enabled():
            raise ModelRetry(
                "Isolated delegation requires both worktree=on and worker_isolation=on "
                "in the active config. Use shared mode without worker isolation."
            )
        if agent_name != "worker":
            raise ModelRetry("Only the built-in worker supports isolated workspaces; use shared.")
        key = self._resolve_model_key(agent_name, self._agents[agent_name], model)
        try:
            store = TaskWorktrees(self.workspace)
            creation = asyncio.create_task(asyncio.to_thread(store.create, wait=True))
            try:
                record = await _joined(creation)
            except asyncio.CancelledError:
                if not creation.cancelled() and creation.exception() is None:
                    await _finish(
                        store,
                        creation.result().task_id,
                        "cancelled",
                        "Delegation cancelled during creation; checkout preserved.",
                    )
                raise
        except WorktreeError as error:
            raise ModelRetry(str(error)) from error
        token = _outcome.set(None)
        try:
            # Timeout or failure preserves the artifact below.
            try:
                async with asyncio.timeout(SETUP_TIMEOUT_SECONDS):
                    await _setup(describe(Path(record.worktree)))
            except TimeoutError:
                raise WorktreeError(f"Worktree setup exceeded {SETUP_TIMEOUT_SECONDS}s") from None
            async with self.worker_factory(Path(record.worktree)) as worker:
                prompt = (
                    f"{task}\n\nYou are working in an isolated task worktree.\n"
                    f"Task ID: {record.task_id}\nBase commit: {record.base_commit}\n"
                    f"Branch: {record.branch}\nWorkspace: {record.worktree}\n"
                    "Edit and test here. Commit completed changes on this branch. "
                    "Do not push, merge into another checkout, or remove this worktree. "
                    "The parent will review and integrate your result. Stop all jobs before "
                    "finishing; remaining jobs will be terminated when delegation ends."
                )
                # Harness hands a delegate the parent's `ctx.workspace`; this
                # one works in its own checkout instead.
                child_ctx = replace(
                    ctx, workspace=Workspace(LocalWorkspaceBackend(record.worktree))
                )
                output = await self._run_delegation(
                    child_ctx,
                    agent_name,
                    replace(self._agents[agent_name], agent=worker),
                    task=prompt,
                    key=key,
                )
            status = "completed" if _outcome.get() == "ok" else "failed"
            record = await _finish(store, record.task_id, status, output)
        except asyncio.CancelledError:
            await _finish(
                store, record.task_id, "cancelled", "Delegation cancelled; partial work preserved."
            )
            raise
        except Exception as error:
            # Preserve the artifact even when Harness reports a retry or setup fails.
            # A contained crash arrives as Harness's retry, already described.
            summary = str(error) if isinstance(error, ModelRetry) else None
            record = await _finish(
                store, record.task_id, "failed", summary or f"{type(error).__name__}: {error}"
            )
        finally:
            _outcome.reset(token)
        return {
            **asdict(record),
            "workspace_mode": "isolated",
            "verification": "Worker-reported; see summary.",
        }

    async def _run_delegation(self, ctx, agent_name, sub_agent, *, task, key):
        resumable = replace(sub_agent, agent=ResumableAgent(sub_agent.agent))
        return await super()._run_delegation(ctx, agent_name, resumable, task=task, key=key)

    def _crash_outcome(self, agent_name, sub_agent, exc):
        """Harness's containment, minus the raw exception text and the traceback.

        Harness puts `str(exc)` in the parent's retry message, which the model,
        the transcript and the panel all see; SDK messages can carry provider
        bodies, so describe it the way a failed turn is described instead. Its
        warning prints a traceback over the terminal, which has no log handler.
        """
        contain = sub_agent.contain_errors
        if not (self._contain_errors if contain is None else contain):
            raise exc
        logger.debug("Contained crash from sub-agent %r", agent_name, exc_info=exc)
        return _Ended(
            outcome="contained",
            output=(
                f"Sub-agent {agent_name!r} crashed: {error_message(exc)} "
                "Treat this as a recoverable failure and decide from existing evidence."
            ),
            cause=exc,
        )

    async def _settle(self, *args, **kwargs):
        ended = await super()._settle(*args, **kwargs)
        _outcome.set(ended.outcome)
        return ended

    async def integrate_task(self, task_id: str) -> dict:
        """Merge a completed task into its recorded parent, never mainline.

        Requires clean parent and child checkouts. Conflicts remain in the parent;
        resolve and commit them, then retry. The child is kept for worktree cleanup.
        """
        try:
            record = await _joined(
                asyncio.create_task(
                    asyncio.to_thread(TaskWorktrees(self.workspace).integrate, task_id)
                )
            )
            return asdict(record)
        except WorktreeError as error:
            raise ModelRetry(str(error)) from error

    async def discard_task(self, task_id: str, confirm: bool = False) -> dict:
        """Remove an inactive task checkout and branch.

        Unintegrated or dirty work requires confirm=True, only after the user has
        explicitly approved discarding it. Active tasks cannot be discarded.
        """
        try:
            record = await _joined(
                asyncio.create_task(
                    asyncio.to_thread(TaskWorktrees(self.workspace).discard, task_id, confirm)
                )
            )
            return asdict(record)
        except WorktreeError as error:
            raise ModelRetry(str(error)) from error

    async def answer_worker(self, ctx: RunContext, worker_id: str, answer: str) -> str:
        """Answer the question a running worker asked with ask_parent; it resumes at once."""
        handle = self.hub.require(worker_id)
        question = handle.question
        if question is None:
            raise ModelRetry(
                f"Worker {handle.id} has no open question"
                + ("." if handle.running else f"; it is {handle.status}.")
                + (" Use message_worker to tell it something." if handle.running else "")
            )
        question.answer = answer
        question.delivered = True
        question.answered.set()
        return f"Answered worker {handle.id}."

    async def message_worker(self, ctx: RunContext, worker_id: str, message: str) -> str:
        """Send a running worker a message; it sees it before its next model request."""
        handle = self.hub.require(worker_id)
        if not handle.running:
            raise ModelRetry(
                f"Worker {handle.id} is {handle.status}. Use delegate_task with "
                f"continue_worker='{handle.id}' for a follow-up."
            )
        handle.inbox.append(message)
        return f"Queued for worker {handle.id}; it reads it before its next step."

    async def stop_worker(self, ctx: RunContext, worker_id: str) -> str:
        """Stop a worker running in the background. Its partial changes stay where they are."""
        handle = self.hub.require(worker_id)
        if not handle.running:
            return f"Worker {handle.id} is already {handle.status}."
        if handle.cancel is None:
            raise ModelRetry(
                f"Worker {handle.id} is not in the background, so its delegate_task call is "
                "still waiting on it; it cannot be stopped from here."
            )
        handle.stop_requested = True
        handle.cancel()
        return f"Stopping worker {handle.id}; a notice arrives when it has stopped."

    async def list_workers(self, ctx: RunContext) -> list[dict]:
        """List this session's workers: id, purpose, status, plan step, tokens and review."""
        return [handle.summary() for handle in self.hub.handles()]

    async def list_task_worktrees(self) -> list[dict]:
        """List this parent's persistent worker artifacts, including failed tasks."""
        try:
            return [asdict(record) for record in TaskWorktrees(self.workspace).list()]
        except WorktreeError as error:
            raise ModelRetry(str(error)) from error
