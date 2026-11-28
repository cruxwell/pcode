"""Worktree-aware delegation over the pinned Harness run/event/budget machinery.

Only the built-in worker is reconstructible in another workspace. Specialized
extension agents keep their own capabilities and shared-workspace semantics.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
from collections.abc import Callable
from contextvars import ContextVar
from copy import copy
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any, Literal
from uuid import uuid4

import anyio
from pydantic_ai import ModelRetry, RunContext
from pydantic_ai.messages import UserPromptPart
from pydantic_ai.workspaces import LocalWorkspaceBackend, Workspace
from pydantic_ai_harness.sub_agents import SubAgents, SubAgentToolset

# Private: `_crash_outcome` must return the record Harness's `_settle` reads,
# and `at_max_depth` enforces Harness's shared delegation-depth counter.
from pydantic_ai_harness.sub_agents._toolset import _Ended, at_max_depth

from pcode.delegation import ResumableAgent
from pcode.diagnostics import provider_context
from pcode.error_report import error_message
from pcode.preferences import SETTINGS, load_preferences
from pcode.steering import run_request
from pcode.task_worktrees import TaskWorktrees
from pcode.worker_channel import (
    NOTIFY_SECONDS,
    PARENT_INSTRUCTIONS,
    QUEUED,
    SETTING_UP,
    WorkerHub,
    _number,
    bind_worker,
    current_worker,
    interrupted_note,
    labelled,
    notify,
    render_brief,
    report_text,
    resume_hint,
    unbind_worker,
)
from pcode.worktree import WorktreeError, describe, setup_scripts

logger = logging.getLogger(__name__)
_outcome: ContextVar[str | None] = ContextVar("isolated_delegation_outcome", default=None)
# Per-session runtime capabilities (`StepPersistence`, `TokenAccounting`) attached
# to the top-level `WorkspaceSubAgents` and inherited by nested workers (shared
# and isolated) so L2/L3 runs persist steps and count tokens in the session too.
_inherited_shared: ContextVar[tuple[Any, ...]] = ContextVar(
    "delegation_inherited_shared", default=()
)
_inherited_error_recorder: ContextVar[Callable[..., Any] | None] = ContextVar(
    "delegation_inherited_error_recorder", default=None
)
_worker_model: ContextVar[Any] = ContextVar("delegation_worker_model", default=None)


def _model_label(model: Any) -> str:
    if isinstance(model, str):
        return model
    return str(getattr(model, "model_id", "") or getattr(model, "model_name", "") or "")


# Setup hooks run with no model watching and their output discarded, so a hang
# is invisible until the user gives up. Room for a cold dependency install.
SETUP_TIMEOUT_SECONDS = 900

# Top-level run (1) plus up to three nested sub-agent layers (2, 3, 4).
# Lets a parent spin up a work sub-agent (2) that spins up a review sub-agent (3)
# and even one further helper (4, leaf) without unbounded recursion.
MAX_DELEGATION_DEPTH = 4


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
    max_depth: int = MAX_DELEGATION_DEPTH
    # True on sub-agents that can themselves delegate: each run gets its own
    # WorkerHub (dotted ids like `w1.1`) and its own slot semaphore so nested
    # workers never collide with siblings or deadlock on the parent's slots.
    nested: bool = False
    worker_slots: asyncio.Semaphore | None = field(
        default_factory=_worker_slots, repr=False, compare=False
    )
    # The parent's workers; found by `worker_channel.hub_for(ctx)`.
    hub: WorkerHub = field(default_factory=WorkerHub, repr=False, compare=False)
    # Records contained worker crashes into `<session>/errors.log`.
    record_error: Callable[..., Any] | None = field(default=None, repr=False, compare=False)

    async def for_run(self, ctx):
        if at_max_depth(self.max_depth):
            run = copy(self)
            run._delegation_off = True
            return run
        if not self.nested:
            return await super().for_run(ctx)
        parent = current_worker()
        prefix = f"{parent.id}." if parent is not None and parent.id else "w"
        run = copy(self)
        run.hub = WorkerHub(prefix=prefix)
        run.worker_slots = _worker_slots()
        if run.record_error is None:
            run.record_error = _inherited_error_recorder.get()
        if extra := _inherited_shared.get():
            extra_types = tuple(type(cap) for cap in extra)
            run.shared_capabilities = [
                *[c for c in run.shared_capabilities if not isinstance(c, extra_types)],
                *extra,
            ]
        run._per_run = True
        run._run_toolset = run._make_toolset()
        return run

    async def wrap_run(self, ctx, *, handler):
        try:
            return await super().wrap_run(ctx, handler=handler)
        finally:
            if self.nested:
                self.hub.retire_all()

    async def before_model_request(self, ctx, request_context):
        # A worker's question rides the parent's next request, like steering.
        request_context = await super().before_model_request(ctx, request_context)
        if self._delegation_off:
            return request_context
        for message in self.hub.take_questions():
            run_request(ctx, request_context).parts.append(UserPromptPart(message))
        return request_context

    def get_toolset(self):
        # Do not cache `self._toolset` on the top-level instance: `AgentRuntime`
        # updates `self.shared_capabilities` per turn/session (`/new`, `/resume`)
        # to attach the active `StepPersistence` and `TokenAccounting`.
        if self._delegation_off:
            return None
        if self._per_run:
            return lambda _ctx: self._run_toolset
        return self._make_toolset()

    def _make_toolset(self):
        if not self._has_delegates():
            return None
        return WorkspaceSubAgentToolset(
            workspace=self.workspace,
            worker_factory=self.worker_factory,
            worker_slots=self.worker_slots,
            hub=self.hub,
            record_error=self.record_error,
            allow_isolation=not self.nested,
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
            max_depth=self.max_depth,
        )

    def _render_instructions(self):
        if self._delegation_off:
            return None
        base = super()._render_instructions()
        if base is None:
            return None
        if self.nested:
            extra = (
                "\nNested sub-agents run in shared mode in this workspace; coordinate writes. "
                "Specialized delegates retain their own tools and use shared mode."
            )
        else:
            extra = (
                "\nWorker workspace_mode defaults to auto: shared unless both worktree=on and "
                "worker_isolation=on in the active workspace's config. worker_isolation defaults "
                "to off; worktree alone never isolates workers. Isolation requires a clean tracked "
                "parent checkout; commit a checkpoint first, never stash or silently switch modes. "
                "Use shared explicitly for work that needs the live parent files; "
                "coordinate writes. "
                "Specialized delegates retain their own tools and use shared mode. "
                "Isolated results are branches, not changes to your checkout. Review them, then "
                "task_worktree(action='integrate') into this parent branch and verify the combined "
                "changes. task_worktree(action='list') recovers task IDs after a restart. "
                "Never push worker branches or merge them directly into mainline. "
                "Ask the user before a confirmed discard destroys unintegrated work."
            )
        return (
            base
            + extra
            + PARENT_INSTRUCTIONS
            + (
                # Harness's listing above reads as the only keys allowed.
                "\nBesides the keys listed, `model` takes any other provider:model name, "
                "or a bare model name for the session's provider."
                if self._menu
                else ""
            )
        )


class WorkspaceSubAgentToolset(SubAgentToolset):
    def __init__(
        self,
        *,
        workspace: Path,
        worker_factory,
        worker_slots,
        hub=None,
        record_error: Callable[..., Any] | None = None,
        allow_isolation: bool = True,
        **kwargs,
    ):
        self.workspace = workspace
        self.worker_factory = worker_factory
        self.worker_slots = worker_slots
        self.hub = hub if hub is not None else WorkerHub()
        self.record_error = record_error
        self.allow_isolation = allow_isolation
        super().__init__(**kwargs)
        if allow_isolation:
            self.add_function(self.task_worktree)
        # One tool, not four: the rarely used worker controls cost one schema.
        self.add_function(self.manage_worker)

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
                    "A bare model name (no provider:) resolves against the session's "
                    "provider first. Omit it to use the sub-agent's default, the "
                    "session's model."
                ),
            },
        }
        return replace(tool_def, parameters_json_schema=schema)

    async def _add_model(self, ctx, agent_name: str, key: str | None) -> str | None:
        """Put a name off the /subagents menu on it, resolved the way /btw resolves one.

        Returns the key to delegate with: a bare name qualified with the
        provider of the session's model (`ctx.model`) when that provider has
        it, so it matches a /subagents key of the same name and a later
        /model switch never reuses another provider's entry. Harness's own key
        check then runs unchanged. A delegate restricted to some keys is left
        for that check to refuse, without resolving anything.
        """
        sub_agent = self._agents.get(agent_name)
        # A name as configured (on /subagents, or a restricted delegate's own
        # list) wins over qualifying it, which could turn it into another key.
        if key is None or key in self._models or key in (getattr(sub_agent, "models", None) or ()):
            return key
        from pcode.models import qualify_model

        session = getattr(ctx, "model", None)
        # Reads the catalog and preferences from disk.
        key = await asyncio.to_thread(qualify_model, key, session)
        if key in self._models or sub_agent is None or sub_agent.models:
            return key
        from pydantic_ai_harness.sub_agents import ModelOption

        from pcode.agent import worker_model

        try:
            # Reads logins and preferences from disk.
            chosen = await asyncio.to_thread(worker_model, key, session)
        except ValueError as error:
            raise ModelRetry(str(error)) from error
        except Exception as error:
            # A broken login file is the model's to work around, not a failed turn.
            raise ModelRetry(f"Cannot use {key}: {type(error).__name__}: {error}") from error
        # Kept for later delegations in this session; the schema never lists it.
        self._models[key] = ModelOption(chosen.model, settings=chosen.settings)
        return key

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
        support shared mode. Review isolated results before integrating them.

        Args:
            purpose: What the sub-agent is doing, at most 8 words, present tense
                (e.g. "fixing the /btw cache prefix"). It labels the delegation
                in the user's task list; the sub-agent never sees it.
            context: Relevant files and earlier decisions.
            boundaries: What not to touch.
            done_when: How the worker knows it is finished.
            include_reports: Worker ids whose reports to add to the brief.
            plan_step: Your plan step id this worker carries out.
            run_in_background: Return at once so independent workers run in parallel.
            continue_worker: Resume this finished shared-mode worker; `task` is the follow-up.
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
        brief = interrupted_note(handle) + render_brief(
            task, context=context, boundaries=boundaries, done_when=done_when, reports=reports
        )
        token = bind_worker(handle)
        outcome = _outcome.set(None)
        # Propagate runtime-attached shared capabilities (`StepPersistence`,
        # `TokenAccounting`) down to nested workers, including dynamically built
        # isolated workers.
        from pydantic_ai_harness.step_persistence import StepPersistence

        from pcode.token_accounting import TokenAccounting

        runtime_shared = tuple(
            cap
            for cap in self._shared_capabilities
            if isinstance(cap, (StepPersistence, TokenAccounting))
        )
        shared_token = _inherited_shared.set(runtime_shared) if runtime_shared else None
        error_token = (
            _inherited_error_recorder.set(self.record_error)
            if self.record_error is not None
            else None
        )
        handle.status = "running"
        handle.started = True
        handle.previous = None
        slots = self.worker_slots if agent_name == "worker" else None
        handle.mark_started(QUEUED if slots is not None and slots.locked() else "Starting")
        unread = 0
        try:
            await notify(handle, ctx, status="running")
            if slots is not None:
                async with slots:
                    handle.mark_started()  # Elapsed counts from when it got a slot.
                    result = await self._delegate(ctx, agent_name, brief, workspace_mode, model)
            else:
                result = await self._delegate(ctx, agent_name, brief, workspace_mode, model)
        except ModelRetry as error:
            # A contained crash, timeout or refusal: Harness's retry never reaches
            # `labelled`, so name the worker and say whether it can be resumed.
            handle.status = "stopped" if handle.stop_requested else "failed"
            handle.report = str(error)
            if hint := resume_hint(handle):
                raise ModelRetry(f"{error}{hint}") from error
            raise
        except BaseException as error:
            cancelled = isinstance(error, asyncio.CancelledError)
            handle.status = "stopped" if (handle.stop_requested or cancelled) else "failed"
            handle.report = (
                "Stopped before it reported."
                if handle.stop_requested
                else "Interrupted before it reported."
                if cancelled
                else f"Failed before it reported ({type(error).__name__})."
            )
            raise
        else:
            failed = _outcome.get() not in (None, "ok") or (
                isinstance(result, dict) and result.get("status") == "failed"
            )
            handle.status = "failed" if failed else "done"
            handle.report = report_text(result)
            if failed and isinstance(result, str):
                result += resume_hint(handle)
        finally:
            if error_token is not None:
                _inherited_error_recorder.reset(error_token)
            if shared_token is not None:
                _inherited_shared.reset(shared_token)
            _outcome.reset(outcome)
            unbind_worker(token)
            handle.resume_from = None
            handle.question = None
            unread = len(handle.inbox)
            handle.inbox.clear()
            if handle.status != "running":
                if handle.status != "done" and handle.worker_model is not None:
                    # Its `agy` session ends here and ran tools pcode never saw,
                    # so a resume would start blind: refuse it (`resume_hint`).
                    handle.messages = []
                if (
                    handle.status != "done" or handle.workspace_mode == "isolated"
                ) and handle.worker_model is not None:
                    handle.worker_model.retire()
                    handle.worker_model = None
                self.hub.save(handle)
                with anyio.move_on_after(NOTIFY_SECONDS, shield=True):
                    await notify(handle, ctx, status=handle.status, tokens=handle.tokens)
        result = labelled(handle, result)
        if unread and isinstance(result, str):
            result += (
                f"\n\n[pcode] {unread} message(s) sent with manage_worker arrived after the "
                "worker's last step and were not read."
            )
        return result

    async def _delegate(self, ctx, agent_name, task, workspace_mode, model):
        model = await self._add_model(ctx, agent_name, model)
        if not self.allow_isolation:
            if workspace_mode == "isolated":
                raise ModelRetry(
                    "Nested sub-agents run in shared mode; use workspace_mode='shared' or 'auto'."
                )
            isolated = False
        else:
            isolated = workspace_mode == "isolated" or (
                workspace_mode == "auto" and agent_name == "worker" and isolation_enabled()
            )
        handle = current_worker()
        if handle is not None:
            if model:
                handle.model = model  # As run, not as typed.
            elif not handle.model:
                sub_agent = self._agents.get(agent_name)
                resolved = (
                    sub_agent.agent.model
                    if sub_agent is not None and sub_agent.agent.model is not None
                    else getattr(ctx, "model", None)
                )
                handle.model = _model_label(resolved)
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
        if handle is not None:
            handle.activity = SETTING_UP
            handle.mark_progress()
        try:
            store = TaskWorktrees(self.workspace)
            creation = asyncio.create_task(asyncio.to_thread(store.create, wait=True))
            try:
                record = await _joined(creation)
            except asyncio.CancelledError:
                if not creation.cancelled() and creation.exception() is None:
                    created = creation.result()
                    if handle is not None:
                        handle.worktree_task_id = created.task_id
                    await _finish(
                        store,
                        created.task_id,
                        "cancelled",
                        "Delegation cancelled during creation; checkout preserved.",
                    )
                raise
            if handle is not None:
                handle.worktree_task_id = record.task_id
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
            if handle is not None:
                handle.activity = "Starting"
                handle.mark_progress()
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
                # Harness hands a delegate the parent's workspace; this one
                # works in its own checkout instead.
                workspace = Workspace(LocalWorkspaceBackend(record.worktree))
                ended = await self._run_delegation(
                    replace(ctx, workspace=workspace),
                    agent_name,
                    replace(self._agents[agent_name], agent=worker),
                    task=prompt,
                    key=key,
                    workspace=workspace,
                )
            summary = ended.output if isinstance(ended, _Ended) else str(ended)
            status = (
                "completed"
                if (ended.outcome == "ok" if isinstance(ended, _Ended) else _outcome.get() == "ok")
                else "failed"
            )
            record = await _finish(store, record.task_id, status, summary)
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

    async def _run_delegation(self, ctx, agent_name, sub_agent, **kwargs):
        from pcode.antigravity import AntigravityModel, unwrap

        key = kwargs.get("key")
        effective = (
            self._models[key].model
            if key is not None and key in self._models
            else sub_agent.agent.model
            if sub_agent.agent.model is not None
            else getattr(ctx, "model", None)
        )
        override_model = None
        handle = current_worker()
        if (native := unwrap(effective)) is not None:
            # Each Antigravity worker runs in its own isolated `agy` session,
            # never sharing the parent's (which holds `_lock` during its turn)
            # or a sibling worker's process.
            reusable = handle.worker_model if handle is not None else None
            if isinstance(reusable, AntigravityModel) and reusable.model_name == native.model_name:
                override_model = reusable
            else:
                if isinstance(reusable, AntigravityModel):
                    reusable.retire()
                override_model = AntigravityModel(native.model_name, worker=True)
                if handle is not None:
                    handle.worker_model = override_model
        elif handle is not None and handle.worker_model is not None:
            handle.worker_model.retire()
            handle.worker_model = None
        resumable = replace(sub_agent, agent=ResumableAgent(sub_agent.agent, model=override_model))
        model_token = _worker_model.set(override_model or effective)
        try:
            return await super()._run_delegation(ctx, agent_name, resumable, **kwargs)
        finally:
            _worker_model.reset(model_token)
            if override_model is not None and handle is None:
                override_model.retire()

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
        recorder = getattr(self, "record_error", None) or _inherited_error_recorder.get()
        if callable(recorder):
            handle = current_worker()
            run_id = f"worker:{handle.id}" if handle is not None and handle.id else "worker"
            model = _worker_model.get()
            if model is None:
                model = (handle.model if handle is not None and handle.model else None) or (
                    sub_agent.agent.model
                )
            context = provider_context(model) if model is not None else None
            try:
                recorder(exc, run_id=run_id, provider_context=context or None)
            except Exception:
                logger.debug("Could not record worker crash to session diagnostics", exc_info=True)
        return _Ended(
            outcome="contained",
            output=(
                f"Sub-agent {agent_name!r} crashed: {error_message(exc)} "
                "Treat this as a recoverable failure; its changes so far stay in the workspace."
            ),
            cause=exc,
        )

    async def _settle(self, *args, **kwargs):
        ended = await super()._settle(*args, **kwargs)
        _outcome.set(ended.outcome)
        return ended

    async def task_worktree(
        self,
        action: Literal["list", "integrate", "discard"],
        task_id: str = "",
        confirm: bool = False,
    ) -> Any:
        """List isolated worker tasks, merge one into its parent branch, or discard one.

        integrate needs clean parent and child checkouts and never touches mainline;
        conflicts stay in the parent to resolve, commit and retry. discard of
        unintegrated or dirty work needs confirm=True, only after the user approves.

        Args:
            action: list (survives restarts), integrate, or discard.
            task_id: The task, for integrate and discard.
            confirm: For discard: the user approved losing unintegrated work.
        """
        if action != "list" and not task_id.strip():
            raise ModelRetry(f"task_worktree action={action!r} needs a task_id.")
        try:
            # Opening the store runs git and fails outside a checkout: keep it
            # off the event loop for writes, and inside the retry handling.
            if action == "list":
                return [asdict(record) for record in TaskWorktrees(self.workspace).list()]
            if action == "integrate":
                work = asyncio.to_thread(lambda: TaskWorktrees(self.workspace).integrate(task_id))
            else:
                work = asyncio.to_thread(
                    lambda: TaskWorktrees(self.workspace).discard(task_id, confirm)
                )
            return asdict(await _joined(asyncio.create_task(work)))
        except WorktreeError as error:
            raise ModelRetry(str(error)) from error

    async def manage_worker(
        self,
        ctx: RunContext,
        action: Literal["answer", "message", "stop", "list"],
        worker_id: str = "",
        text: str = "",
    ) -> Any:
        """Answer a worker's question, message or stop a running one, or list workers.

        Args:
            action: answer (its ask_parent question), message (read before its next
                step), stop (background workers only), or list. list gives each running
                worker's elapsed_seconds, seconds_since_progress, activity, running_tools
                and a `liveness` verdict (tool_calls counts this run only); tokens is
                null until a model call ends.
            worker_id: The worker, such as w3; not needed for list.
            text: The answer or message.
        """
        if action == "list":
            by_id = {handle.id: handle.summary() for handle in self.hub.handles()}
            for worker_id in self.hub._saved_ids():
                if worker_id in by_id:
                    continue
                if (saved := self.hub.saved_summary(worker_id)) is not None:
                    by_id[worker_id] = saved
            return [by_id[worker_id] for worker_id in sorted(by_id, key=_number)]
        if not worker_id.strip():
            raise ModelRetry(f"manage_worker action={action!r} needs a worker_id.")
        if action == "stop":
            return self._stop_worker(worker_id)
        if not text.strip():
            raise ModelRetry(f"manage_worker action={action!r} needs text.")
        if action == "answer":
            return self._answer_worker(worker_id, text)
        return self._message_worker(worker_id, text)

    def _answer_worker(self, worker_id: str, answer: str) -> str:
        handle = self.hub.require(worker_id)
        question = handle.question
        if question is None:
            raise ModelRetry(
                f"Worker {handle.id} has no open question"
                + ("." if handle.running else f"; it is {handle.status}.")
                + (" Use action='message' to tell it something." if handle.running else "")
            )
        question.answer = answer
        question.delivered = True
        question.answered.set()
        return f"Answered worker {handle.id}."

    def _message_worker(self, worker_id: str, message: str) -> str:
        handle = self.hub.require(worker_id)
        if not handle.running:
            raise ModelRetry(
                f"Worker {handle.id} is {handle.status}. Use delegate_task with "
                f"continue_worker='{handle.id}' for a follow-up."
            )
        handle.inbox.append(message)
        return f"Queued for worker {handle.id}; it reads it before its next step."

    def _stop_worker(self, worker_id: str) -> str:
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
