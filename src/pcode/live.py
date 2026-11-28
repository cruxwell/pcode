"""Translate Pydantic streams to UI-independent application events."""

import asyncio
import re
import warnings
from collections.abc import AsyncIterator, Callable
from contextlib import aclosing, asynccontextmanager, nullcontext
from copy import deepcopy
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from uuid import uuid4

from pydantic_ai import (
    Agent,
    AgentRunResultEvent,
    FunctionToolCallEvent,
    FunctionToolResultEvent,
    PartDeltaEvent,
    PartEndEvent,
    PartStartEvent,
    TextPart,
    TextPartDelta,
    ThinkingPart,
    ThinkingPartDelta,
    ToolReturn,
)
from pydantic_ai.capabilities import AbstractCapability, LocalWorkspace
from pydantic_ai.exceptions import ModelHTTPError, UsageLimitExceeded
from pydantic_ai.messages import (
    ModelMessage,
    ModelMessagesTypeAdapter,
    ModelRequest,
    ModelResponse,
    NativeToolCallPart,
    NativeToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models.anthropic import AnthropicStaleThinkingBlockWarning
from pydantic_ai.usage import RunUsage, UsageLimits
from pydantic_ai.workspaces import LocalWorkspaceBackend
from pydantic_ai_harness.file_system import FileSystem
from pydantic_ai_harness.planning import InMemoryPlanStore, Planning
from pydantic_ai_harness.shell import Shell
from pydantic_ai_harness.step_persistence import ContinuableSnapshot, StepPersistence
from pydantic_ai_harness.sub_agents import SubAgents
from pydantic_ai_harness.tool_output_limits import ToolOutputLimits

from pcode import retries
from pcode.agent import SideModel, is_unknown_model, worker_toolsets
from pcode.aside import SideReply
from pcode.background_delegation import AbandonedDelegations
from pcode.compaction import AutoCompaction, ContextTracking, effective_window, summarize
from pcode.conversation_ids import model_conversation
from pcode.conversation_tree import ConversationTree
from pcode.diagnostics import (
    error_details,
    provider_context,
    quota_message,
    transient,
    transport_types,
)
from pcode.edit_preview import StreamingEditPreview
from pcode.inspection import ToolArchive
from pcode.job_notices import JobNotices
from pcode.jobs import registry as job_registry
from pcode.mcp import (
    OAUTH_PACKAGES,
    MCPConnectError,
    MCPState,
    config_path,
    deferred_schemas_rejected,
    find_cause,
)
from pcode.mcp_notice import enabled_servers
from pcode.native_results import (
    drop_orphaned_native_parts,
    drop_unreadable_results,
    unreadable_native_results,
)
from pcode.openai_region import regional_host, track, use_regional_host
from pcode.plan_preview import StreamingPlanPreview
from pcode.planning import SizedPlanItem
from pcode.preferences import SETTINGS, load_preferences, parse_token_count
from pcode.profiling import activity as profiled_activity
from pcode.prompt_images import attach
from pcode.retries import RequestCheckpoint
from pcode.runtime import (
    ChildPlan,
    ChildText,
    CommandOutput,
    EditCompleted,
    EditPreview,
    Event,
    Message,
    PlanPreview,
    ToolStarted,
    ToolSummary,
    WorkerStatus,
)
from pcode.sessions import SavedSession, SessionError, SessionInfo
from pcode.shell_mode import ShellRun, reduce_result, shell_exchange
from pcode.shell_tools import JobShell
from pcode.steering import STEERING_REMINDER, Steering
from pcode.stream_events import EventTranslator
from pcode.token_accounting import TokenAccounting, TokenTotals
from pcode.tool_display import label, target
from pcode.turn import TurnContext
from pcode.worker_channel import WorkerHub

# Pydantic AI already recovered (it resent without the stale thinking) and
# recorded the drop on the response, so the warning is noise even in the
# library log, and a wall of stderr where that log is not installed.
warnings.filterwarnings("ignore", category=AnthropicStaleThinkingBlockWarning)


_SIGNING_PROVIDERS = frozenset({"anthropic", "claude"})


def _drop_interrupted_thinking(messages: list[ModelMessage]) -> list[ModelMessage]:
    """`messages` without unsigned thinking from providers that always sign it.

    Anthropic only leaves a thinking block unsigned when its stream was cut
    off. Pydantic AI replays such a block as `<assistant_thinking>` text in a
    user turn, which Anthropic's classifier refuses as `reasoning_extraction`
    on every later request, so one dropped connection bricked the session.
    Other providers' thinking is legitimately unsigned and stays.
    """
    cleaned = []
    for message in messages:
        if isinstance(message, ModelResponse) and message.provider_name in _SIGNING_PROVIDERS:
            parts = [
                p for p in message.parts if not (isinstance(p, ThinkingPart) and not p.signature)
            ]
            if len(parts) != len(message.parts):
                message = replace(message, parts=parts)
        cleaned.append(message)
    return cleaned


def _for_run(messages: list[ModelMessage]) -> list[ModelMessage]:
    """`messages` ready to replay: interrupted thinking dropped, older `claude:` saves restamped."""
    messages = _drop_interrupted_thinking(messages)
    # Imported only when needed: the `claude:` package is otherwise unused.
    if any(isinstance(m, ModelResponse) and m.provider_name == "claude" for m in messages):
        from pcode.claude_sdk.model import restamp

        restamp(messages)
    return messages


def _local_workspace(agent) -> Path | None:
    """The directory of the agent's own `LocalWorkspace` capability, if it has one."""
    for capability in agent.root_capability.capabilities:
        if isinstance(capability, LocalWorkspace):
            return Path(capability.working_dir)
    return None


@dataclass(frozen=True)
class TurnModel:
    """What one turn runs on in place of the conversation's model, from `$MODEL` or `+EFFORT`.

    `model` is another model, with its own settings; `settings` alone replace
    the conversation model's for this turn, at another effort.
    `conversation_id` is the one that model's requests carry; see
    `AgentRuntime._model_conversation`.
    """

    model: SideModel | None
    settings: dict | None
    conversation_id: str

    @property
    def borrowed(self) -> bool:
        """Whether this turn runs on something other than the conversation's agent."""
        return self.model is not None or self.settings is not None

    @asynccontextmanager
    async def applied(self, agent):
        settings = self.model.settings if self.model is not None else self.settings
        if self.model is None and settings is None:
            yield
            return
        with agent.override(model_settings=settings):
            yield


class FollowAgent(AbstractCapability):
    """Send each request of a run to the runtime's current agent's model.

    `/model` replaces the agent while a turn runs, and the turn's next request
    goes to the new model, as steering reaches it. The request in flight keeps
    its model. Pydantic AI evaluates the selector before every request step and
    enters a newly selected model for the rest of the run. Its settings follow
    through `AgentRuntime._following`. Only the model and settings follow: the
    run keeps the agent it started with for tools, instructions and sub-agents,
    which pcode builds the same way for every model.
    """

    def __init__(self, agent: Callable[[], Agent], context: TurnContext) -> None:
        self.agent = agent
        self.context = context

    def get_model(self):
        return lambda ctx: self.agent().model

    async def before_model_request(self, ctx, request_context):
        self.context.model = request_context.model
        return request_context


class AgentRuntime:
    def __init__(
        self,
        agent: Agent,
        session: SavedSession | None = None,
        *,
        session_factory: Callable[[], SavedSession] | None = None,
    ) -> None:
        self.agent = agent
        self.session = session
        if session is not None:
            model, workspace, root = (
                session.info.model,
                Path(session.info.workspace),
                session.directory.parent,
            )

            def session_factory():
                return SavedSession.create(model, workspace, root)

        self.session_factory = session_factory
        preferences = load_preferences()
        self.auto_compact = preferences.get("autocompact", "on") == "on"
        # Tokens at which to compact even when the window has more room; None
        # leaves the threshold to the model's window.
        limit = preferences.get("autocompact_tokens")
        self.auto_compact_limit: int | None = parse_token_count(limit) if limit else None
        # Snapshotted like autocompact: a saved default applies to the next launch.
        self.retry_attempts = int(
            preferences.get("retry_attempts", SETTINGS["retry_attempts"].default)
        )
        self.compaction_notice = lambda text: None
        self.retry_notice = lambda text: None
        self.warning_notice = lambda text: None
        # A plain transcript note, such as the images a prompt brought along.
        self.notice = lambda text: None
        # A note that survives redraws, for output a model sends between turns.
        self.retained_notice = lambda text: None
        # Copies session settings (the /advisor choice) into the manifest a turn saves.
        self.record_settings: Callable[[SessionInfo], None] = lambda info: None
        # A session title reported by a model that names its own sessions (`antigravity:`).
        self.title_notice = lambda title: None
        # Callback invoked when `antigravity:` emits unsolicited post-turn updates.
        self._provider_wake_notice: Callable[[], None] | None = None
        # Prompts whose images were reported this turn; see `_with_images`.
        self._images_reported: set[str] = set()
        # Shell jobs outlive both the run and the conversation, so the registry
        # is not reset by `_clear`, `/new`, or conversation checkout.
        self.jobs = job_registry()
        self.take_steering = lambda **_: []
        self.commit_steering = lambda messages: None
        self.has_steering = lambda: False
        # Prompt overhead describes the agent's configuration, not one
        # conversation, so it outlives /new and conversation checkout.
        self.request_parameters = None
        self._clear()
        self.replace_agent(agent)

    @property
    def provider_wake_notice(self) -> Callable[[], None] | None:
        return self._provider_wake_notice

    @provider_wake_notice.setter
    def provider_wake_notice(self, callback: Callable[[], None] | None) -> None:
        from pcode.antigravity import unwrap

        self._provider_wake_notice = callback
        agent = getattr(self, "agent", None)
        model = unwrap(agent.model) if agent is not None else None
        if model is not None:
            model.wake_listener = callback

    def has_provider_wake(self) -> bool:
        """Whether the current model has buffered unsolicited post-turn updates."""
        from pcode.antigravity import unwrap

        model = unwrap(self.agent.model)
        return model is not None and model.has_pending_wake(self.history)

    def clear_provider_wake(self, *, quarantine: bool = False) -> None:
        """Drop any buffered unsolicited post-turn updates on the current model."""
        from pcode.antigravity import unwrap

        model = unwrap(self.agent.model)
        if model is not None:
            model.clear_wake(quarantine=quarantine)

    def replace_agent(self, agent: Agent) -> None:
        """Change the agent without resetting conversation-scoped state."""
        from pcode.antigravity import unwrap

        previous = getattr(self, "agent", None)
        if previous is not None and previous.model is not agent.model:
            # An `antigravity:` model's `agy` process would otherwise run until exit.
            retire = getattr(previous.model, "retire", None)
            if callable(retire):
                retire()
        if hasattr(agent.model, "listener"):
            # What `antigravity:` says between turns, as a transcript note.
            agent.model.listener = lambda text: self.retained_notice(text)
        if hasattr(agent.model, "title_listener"):
            # Titles `antigravity:` assigns its own session via `session_info_update`.
            agent.model.title_listener = lambda title: self.title_notice(title)
        ag_model = unwrap(agent.model)
        if ag_model is not None:
            # Unsolicited post-turn updates open a continuation turn when a controller is wired.
            ag_model.wake_listener = self._provider_wake_notice
        self.agent = agent
        # The agent's capabilities are flattened by Pydantic AI. A resolver
        # keeps the store conversation-scoped, including after /new. It resolves
        # per request, so this is also where a second turn would be handed its
        # own store: `ctx` names the run the tools are being called for.
        for capability in self.agent.root_capability.capabilities:
            if isinstance(capability, Planning):
                capability.store_resolver = lambda ctx: self.plan_store

    def _persist_child_runs(self, agent: Agent) -> None:
        """Record delegated runs in the current session's store.

        Sub-agents receive `shared_capabilities`, not the per-run capabilities the
        parent passes to `run_stream_events`, so child requests are otherwise
        absent from the store: their tokens reach session totals but nothing says
        how they were spent. The store changes with `/new`, so this is resolved
        per turn rather than at construction. `agent_name` is left unset: one
        shared capability serves every sub-agent, and `parent_run_id` already
        marks a run as delegated.
        """
        for capability in agent.root_capability.capabilities:
            if not isinstance(capability, SubAgents):
                continue
            shared = [
                shared_capability
                for shared_capability in capability.shared_capabilities
                if not isinstance(shared_capability, (StepPersistence, TokenAccounting))
            ]
            if self.session is not None:
                shared.append(StepPersistence(store=self.session.store))
            # Count a delegated request where it happens. Child tokens also
            # aggregate into the parent's run usage, which nothing reads.
            shared.append(TokenAccounting(record=self.totals.add))
            capability.shared_capabilities = shared
            # Finished workers are saved with the session, for follow-ups after a restart.
            if isinstance(hub := getattr(capability, "hub", None), WorkerHub):
                hub.attach(self.session.directory if self.session is not None else None)
            if hasattr(capability, "record_error"):
                capability.record_error = (
                    self.session.record_error if self.session is not None else None
                )

    async def refresh_context(self) -> None:
        """Refresh optional metadata outside rendering and before model requests."""
        from pydantic_ai.models import infer_model

        from pcode.model_metadata import refresh_context

        if isinstance(self.agent.model, str):
            try:
                # Retain the resolved provider so the UI and future requests
                # share its identity-scoped metadata, including deferred login.
                self.agent.model = infer_model(self.agent.model)
            except Exception:
                # Optional discovery must not prevent reaching /login.
                pass
        # Agent() already resolved most names, so this catches a rebuilt
        # agent (/model, resume) before its first request hits the wrong host.
        track(self.agent.model)
        await refresh_context(self.agent.model)

    def startup_context(self) -> list[str]:
        """Report only repository context configured on this agent."""
        from pcode.repo_context import AutomaticRepoContext

        lines = []
        for capability in self.agent.root_capability.capabilities:
            if isinstance(capability, AutomaticRepoContext):
                lines.extend(capability.startup_summary())
        return lines

    def _mcp_connect_failed(self, name: str, error: BaseException) -> None:
        """One server is down; the turn continues with every other tool."""
        text = self.mcp.unavailable[name]
        saved = self.session
        path = saved.record_error(error, run_id=f"mcp:{name}") if saved else None
        if path is not None:
            text += f" Diagnostics: {path}"
        self.warning_notice(text)

    def _clear(self) -> None:
        info = self.session.info if self.session else None
        self.tree = self.session.tree if self.session else ConversationTree()
        self.inspections = ToolArchive()
        # The active branch's turn state. Everything a turn reads and writes
        # back lives here rather than on the runtime; see `TurnContext`.
        self.context = TurnContext()
        self.conversation_id = info.id if info else str(uuid4())
        self.turns = info.turns if info else 0
        self.totals = TokenTotals(
            input=info.input_tokens if info else 0,
            output=info.output_tokens if info else 0,
            cache_read=info.cache_read_tokens if info else 0,
            cache_write=info.cache_write_tokens if info else 0,
        )
        self.recovery_blocked = ""
        self.mcp = MCPState()
        self.mcp.on_connect_failure = self._mcp_connect_failed

    # The active branch's turn state, under the names callers already use.
    @property
    def history(self) -> list[ModelMessage]:
        return self.context.history

    @history.setter
    def history(self, messages: list[ModelMessage]) -> None:
        self.context.history = messages

    @property
    def pending_shell(self) -> list[ModelMessage]:
        return self.context.pending_shell

    @pending_shell.setter
    def pending_shell(self, messages: list[ModelMessage]) -> None:
        self.context.pending_shell = messages

    @property
    def plan_store(self) -> InMemoryPlanStore:
        return self.context.plan_store

    @plan_store.setter
    def plan_store(self, store: InMemoryPlanStore) -> None:
        self.context.plan_store = store

    @property
    def context_history(self) -> list[ModelMessage] | None:
        return self.context.context_history

    @context_history.setter
    def context_history(self, messages: list[ModelMessage] | None) -> None:
        self.context.context_history = messages

    @property
    def input_tokens(self) -> int:
        return self.totals.input

    @property
    def output_tokens(self) -> int:
        return self.totals.output

    def _save_totals(self, info) -> None:
        info.input_tokens = self.totals.input
        info.output_tokens = self.totals.output
        info.cache_read_tokens = self.totals.cache_read
        info.cache_write_tokens = self.totals.cache_write

    def reset(self) -> None:
        reset = getattr(self.agent.model, "reset", None)
        if callable(reset):
            reset()
        if self.session:
            self.session.close()
            self.session = None
        self._clear()

    async def restore(self) -> None:
        if self.session:
            self.history = await self.session.recover()
            await self.plan_store.set_items(
                [SizedPlanItem.model_validate(item) for item in self.session.latest_plan()]
            )
            active = self.tree.nodes.get(self.tree.active) if self.tree.active else None
            self.context.abandoned_delegations = (
                deepcopy(active.abandoned_delegations)
                if active is not None and active.abandoned_delegations
                else []
            )

    async def navigate(self, identity: str | None, *, edit: bool = False) -> str:
        """Restore a safe checkpoint without running a model or replaying tools."""
        if self.recovery_blocked:
            raise SessionError(self.recovery_blocked)
        self.tree.path(identity)
        node = self.tree.nodes[identity] if identity else None
        target = node.parent if edit and node else identity
        draft = node.prompt if edit and node else ""
        history = (
            await self.session.history_at(target)
            if self.session
            else deepcopy(self.tree.nodes[target].history or [])
            if target
            else []
        )
        plan = InMemoryPlanStore()
        await plan.set_items(
            [
                SizedPlanItem.model_validate(item)
                for item in (self.tree.nodes[target].plan if target else [])
            ]
        )
        # Publish the cursor only after all restoration/validation succeeds.
        if self.session:
            self.session.append("tree_selected", node_id=target, sync=True)
        else:
            self.tree.active = target
        self.history = history
        self.plan_store = plan
        target_node = self.tree.nodes.get(target) if target else None
        self.context.abandoned_delegations = (
            deepcopy(target_node.abandoned_delegations)
            if target_node is not None and target_node.abandoned_delegations
            else []
        )
        from pcode.antigravity import unwrap

        ag_model = unwrap(self.agent.model)
        if ag_model is not None:
            ag_model.sync_history(history)
        return draft

    def aside_context(self) -> list[ModelMessage]:
        """The newest context a side question can be asked against.

        `context_history` is the request in flight, so a question asked mid-turn
        sees what the model is working on rather than the state before the turn
        began. Only the settled prefix is usable; see `settled_context`.

        The copy is not a precaution but the isolation itself: a side question is
        appended to the last request the way steering is, and these message
        objects belong to the running turn's own history.
        """
        from pcode.aside import settled_context

        history = self.context_history if self.context_history is not None else self.history
        return deepcopy(settled_context(list(history)))

    async def aside(
        self,
        question: str,
        *,
        report=None,
        model: SideModel | None = None,
        settings: dict | None = None,
        after: SideReply | None = None,
        fresh: bool = False,
        framing: Callable[[str], str] | None = None,
    ) -> SideReply:
        """Answer `question` beside the conversation, recording nothing.

        Returns the answer with the run's messages, conversation id, model and
        settings: everything `after` needs to ask a follow-up to it.

        Nothing here touches conversation state: no journal record, no tree
        node, no plan, and `self.history` is only read. The run is billed to the
        session's token totals, because the tokens were really spent. `report`
        receives `(answer_so_far, activity)` as the answer streams.

        The run is set up the way `_stream` sets up a turn -- same agent, MCP
        toolsets, server list, conversation id, and model settings -- because
        the provider caches a request prefix, and a side question whose
        instructions or tool definitions differ by a byte re-bills the whole
        conversation. Per-run capabilities here add neither instructions nor
        tools; `AsideGuard` refuses the plan and delegation tools at execution
        instead of hiding them.

        `model` runs the question on another model instead. It keeps the same
        agent, tools and history but has no cache to share, so it takes that
        model's own settings rather than the conversation's, and a conversation
        id of its own (see `pcode.conversation_ids`).

        `settings` replaces the conversation's model settings for this question
        alone, which is how `/btw +EFFORT` asks the conversation's own model at
        another effort. It keeps the conversation id: the cache may not match
        at a different effort, but the user asked for that trade.

        `after` asks a follow-up to that earlier reply instead, continuing its
        messages rather than the conversation's newest context, on the agent,
        model, settings and conversation id it ran with. Its history is exactly
        what that run sent plus what it answered, so the follow-up reuses its
        cache, even after /model replaced the conversation's agent. A `model`
        or `settings` given with it, or `fresh` for the conversation's own
        model as it is now, switches the follow-up the way they switch a new
        question: on the conversation's agent and with the conversation id that
        model would get, at the price of that cache.

        `framing` turns `question` into the message sent, in place of the side
        question or follow-up framing; a thread's summary is asked that way.
        """
        from pcode.aside import framed, framed_follow_up
        from pcode.aside_guard import AsideGuard

        agent = self.agent
        inherit = after is not None and not fresh and model is None and settings is None
        if inherit:
            agent = agent if after.agent is None else after.agent
            model = after.model
            settings = after.settings
        target_model = model.model if model is not None else agent.model
        created_side_model = None
        if getattr(target_model, "system", None) == "antigravity":
            from pcode.antigravity import AntigravityModel, unwrap

            native = unwrap(target_model)
            # Never share the main conversation's AntigravityModel: its lock is
            # held mid-turn and its session would be polluted by the side chat.
            if model is None:
                created_side_model = AntigravityModel(target_model.model_name, side=True)
                model = SideModel(
                    f"antigravity:{target_model.model_name}",
                    created_side_model,
                    settings if settings is not None else agent.model_settings,
                )
            elif native is not None and not native._side:
                created_side_model = AntigravityModel(target_model.model_name, side=True)
                model = SideModel(model.name, created_side_model, model.settings)
        if after is not None:
            messages = list(after.messages)
        else:
            messages = self.aside_context()
        # A turn in flight ends on a user-role request: its new prompt, or the
        # tool results it is working through. A second user message after one of
        # those is what providers reject as non-alternating roles, so the
        # question joins that request the way steering does, and the run
        # continues from history instead of adding a message of its own.
        joined = bool(messages) and isinstance(messages[-1], ModelRequest)
        frame = framing or (framed_follow_up if after is not None else framed)
        pending = [frame(question)]
        capabilities = [AsideGuard(), TokenAccounting(record=self.totals.add)]
        if joined:
            capabilities.append(Steering(lambda: [pending.pop()] if pending else []))
        conversation_id = self._model_conversation(model, "btw")
        other: dict = {}
        if model is not None:
            other = {"model": model.model}
            override = agent.override(model_settings=model.settings)
        elif settings is not None:
            override = agent.override(model_settings=settings)
        else:
            override = nullcontext()
        if inherit:
            # A follow-up belongs to its thread's session, not a fresh one.
            conversation_id = after.conversation_id
        try:
            with override:
                async with (
                    agent,
                    model.model if model is not None else nullcontext(),
                    worker_toolsets([self.mcp.live()]),
                    enabled_servers(self.mcp.servers, self.mcp.unavailable),
                    agent.run_stream_events(
                        None if joined else pending.pop(),
                        message_history=_for_run(messages),
                        workspace=self._run_workspace(agent),
                        toolsets=[self.mcp.live()],
                        conversation_id=conversation_id,
                        capabilities=capabilities,
                        # Explicit: left out, the library's 50-request default applies.
                        usage_limits=UsageLimits(request_limit=None),
                        **other,
                    ) as events,
                ):
                    answer, messages = await self._aside_answer(events, report)
        except BaseException:
            if created_side_model is not None:
                created_side_model.retire()
            elif not inherit and model is not None:
                retire = getattr(model.model, "retire", None)
                if callable(retire):
                    retire()
            raise
        return SideReply(
            answer=answer,
            messages=messages,
            conversation_id=conversation_id,
            agent=agent,
            model=model,
            settings=settings,
        )

    def _model_conversation(self, model: SideModel | None, kind: str) -> str:
        """The conversation id a run on `model` carries; `None` is the conversation's model.

        Another model gets an id of its own; see `pcode.conversation_ids`.
        """
        if model is None:
            return self.conversation_id
        return model_conversation(self.conversation_id, kind)

    async def _aside_answer(self, events, report) -> tuple[str, list[ModelMessage]]:
        """Collect a side question's answer and its run's messages, reporting progress."""
        messages: list[ModelMessage] = []
        blocks: list[str] = []
        partial = ""
        activity = "Waiting for model…"
        tools: dict[str, str] = {}

        def publish() -> None:
            if report is not None:
                report("\n\n".join([*blocks, partial] if partial else blocks), activity)

        # The phases are the main status row's words, since the viewer draws
        # them with that row: `Thinking…`, `Responding…`, `Run shell · ls`.
        async for event in events:
            if isinstance(event, PartStartEvent) and isinstance(event.part, TextPart):
                partial += event.part.content
                activity = "Responding…"
            elif isinstance(event, PartDeltaEvent) and isinstance(event.delta, TextPartDelta):
                partial += event.delta.content_delta
                activity = "Responding…"
            elif (isinstance(event, PartStartEvent) and isinstance(event.part, ThinkingPart)) or (
                isinstance(event, PartDeltaEvent) and isinstance(event.delta, ThinkingPartDelta)
            ):
                if activity == "Thinking…":
                    continue  # Nothing new to show: thoughts are not the answer.
                activity = "Thinking…"
            elif isinstance(event, PartEndEvent) and isinstance(event.part, TextPart):
                if event.part.content:
                    blocks.append(event.part.content)
                partial = ""
            elif isinstance(event, PartStartEvent) and isinstance(event.part, NativeToolCallPart):
                try:
                    args = event.part.args_as_dict()
                except ValueError, TypeError:
                    args = {}
                where = target(event.part.tool_name, args)
                tools[event.part.tool_call_id] = event.part.tool_name
                activity = label(event.part.tool_name) + (f" · {where}" if where else "")
            elif isinstance(event, PartStartEvent) and isinstance(event.part, NativeToolReturnPart):
                tools.pop(event.part.tool_call_id, None)
                activity = "Waiting for model…" if not tools else activity
            elif isinstance(event, FunctionToolCallEvent):
                try:
                    args = event.part.args_as_dict()
                except ValueError, TypeError:
                    args = {}
                where = target(event.part.tool_name, args)
                tools[event.part.tool_call_id] = event.part.tool_name
                activity = label(event.part.tool_name) + (f" · {where}" if where else "")
            elif isinstance(event, FunctionToolResultEvent):
                tools.pop(event.tool_call_id, None)
                activity = "Waiting for model…" if not tools else activity
            elif isinstance(event, AgentRunResultEvent):
                messages = event.result.all_messages()
                continue
            else:
                continue
            publish()
        if partial:
            blocks.append(partial)
            partial = ""
        activity = ""
        publish()
        return "\n\n".join(blocks), messages

    async def record_aside(self, aside, conversation: str) -> bool:
        """Journal a settled side question as a branch off its parent; whether it was.

        It becomes a node like a turn's, flagged `side` (see `side_record`):
        the question, what of the answer arrived, the history a follow-up
        continues from, and how it ended. The active node never moves, so a
        turn running meanwhile records on as before. Nothing is recorded for
        a question asked in another conversation, or before this one had a
        turn, since there is no node here for it to branch from; nor for one
        stopped before any answer arrived, which would only be clutter.
        """
        from pcode.diagnostics import strip_controls

        session = self.session

        def recordable() -> bool:
            # Asked again after the snapshot's await: /new or /resume may have
            # replaced the session meanwhile, and its journal is not ours.
            return (
                not self.recovery_blocked
                and self.session is session
                and conversation == self.conversation_id
                and aside.id not in self.tree.nodes
                and aside.parent in self.tree.nodes
                and (session is not None or self.session_factory is None)
            )

        if not recordable() or (aside.status == "cancelled" and not aside.answer.strip()):
            return False
        reply = aside.reply
        started = {
            "prompt": aside.question,
            "run_id": aside.id,
            "parent_id": aside.parent,
            "continuation": False,
            "aside": True,
            "side": {
                "thread": aside.thread,
                "model": aside.model,
                "label": aside.label,
                "effort": aside.effort,
                "session_id": reply.conversation_id if reply is not None else "",
                "elapsed": round(aside.elapsed, 1),
            },
        }
        outcome = {"answered": "turn_completed", "cancelled": "turn_cancelled"}.get(
            aside.status, "turn_failed"
        )
        ending = {
            "run_id": aside.id,
            "side_status": aside.status,
            "side_error": strip_controls(aside.error),
        }
        messages = reply.messages if reply is not None else None
        if session is not None:
            if messages:
                # First, and the only wait: the records below then go in
                # together, so nothing can close the session between them.
                await session.store.save_snapshot(
                    ContinuableSnapshot(
                        run_id=aside.id,
                        step_index=0,
                        messages=messages,
                        conversation_id=self.conversation_id,
                    )
                )
                if not recordable():
                    return False
            session.append("turn_started", sync=True, **started)
            if aside.answer:
                session.event(Message(aside.answer), run_id=aside.id)
            session.append(outcome, sync=True, **ending)
        else:
            tree = self.tree
            tree.consume({"kind": "turn_started", **started})
            if aside.answer:
                tree.consume({"kind": "Message", "run_id": aside.id, "markdown": aside.answer})
            tree.consume({"kind": outcome, **ending})
            tree.nodes[aside.id].history = deepcopy(messages) if messages else None
        return True

    async def side_history(self, identity: str) -> list[ModelMessage]:
        """The history a recorded side question ended with, for a follow-up to continue."""
        if self.session:
            return await self.session.history_at(identity)
        node = self.tree.nodes[identity]
        return deepcopy(node.history or [])

    def mark_side(self, identity: str, how: str) -> None:
        """Note on a side question's node how its thread was kept: merged or summarized."""
        if self.session:
            self.session.append("side_kept", run_id=identity, how=how, sync=True)
        else:
            self.tree.consume({"kind": "side_kept", "run_id": identity, "how": how})

    async def merge_aside(self, leaf: str, base: str | None) -> bool:
        """Continue the conversation from a side thread's `leaf`, if it extends it exactly.

        The thread is already a branch off `base`. When it was asked at the
        active node with nothing since, the conversation moves onto it and
        continues from its last answer. Otherwise the active node stays put --
        moving would drop what the conversation did after the thread was
        asked -- and the branch waits in /tree. Returns whether it moved.
        """
        if self.recovery_blocked:
            raise SessionError(self.recovery_blocked)
        history = await self.side_history(leaf)
        extends = base == self.tree.active and history[: len(self.history)] == self.history
        if extends:
            await self.navigate(leaf)
            self.mark_side(leaf, "merged")
        return extends

    async def summarize_aside(
        self, reply: SideReply, instructions: str = "", *, report=None
    ) -> str:
        """Ask a side thread to summarize itself; returns the summary, recording nothing.

        Asked in the thread, after `reply`, where the questions and answers
        are and its cache is warm. Like any side question it only reads the
        conversation, so it can run beside a turn; the caller sends the
        summary on as a message.
        """
        from pcode.aside import framed_summary, retire_reply

        summary = await self.aside(instructions, report=report, after=reply, framing=framed_summary)
        retire_reply(summary, keep=reply)
        return summary.answer

    def _open_session(self) -> SavedSession | None:
        """The session to record in, created on first use as a turn creates it."""
        if self.session is None and self.session_factory is not None:
            self.session = self.session_factory()
            self.conversation_id = self.session.info.id
            self.tree = self.session.tree
        return self.session

    async def compact(self, focus: str = ""):
        """Persist a new branch-local context checkpoint before publishing it."""
        if self.recovery_blocked:
            raise SessionError(self.recovery_blocked)
        from pcode.antigravity import unwrap

        if (native := unwrap(self.agent.model)) is not None:
            from pcode.compaction import CompactionResult

            sizes = await native.compact(self.history, str(self._workspace_dir()), focus)
            if sizes is None:
                # With history, None means Antigravity never saw it, not that
                # it is small: "within the budget" would be false.
                return CompactionResult(self.history, 0, 0, False, unseen=bool(self.history))
            return CompactionResult(self.history, sizes[0] or 0, sizes[1] or 0, False, native=True)
        usage = RunUsage()
        # Size the kept history and summary to the autocompact cap, as automatic
        # compaction does, or the result can land just under it and recompact.
        window = None
        if self.auto_compact and (limit := self.auto_compact_limit):
            window = min(effective_window(self.agent.model) or limit, limit)
        try:
            async with self.agent:
                result = await summarize(
                    self.history,
                    model=self.agent.model,
                    focus=focus or None,
                    usage=usage,
                    window=window,
                )
        finally:
            # A cancelled/failed summary can still have incurred provider usage.
            self.totals.add(usage)
            if self.session:
                self._save_totals(self.session.info)
                self.session.save_info()
        if not result.changed:
            return result
        record = {
            "node_id": str(uuid4()),
            "parent_id": self.tree.active,
            "focus": focus,
            "messages": ModelMessagesTypeAdapter.dump_python(result.messages, mode="json"),
            "plan": [item.model_dump(mode="json") for item in await self.plan_store.get_items()],
            "before": result.before,
            "after": result.after,
        }
        if self.session:
            self.session.append("compaction_checkpoint", sync=True, **record)
        else:
            self.tree.consume({"kind": "compaction_checkpoint", **record})
        self.history = result.messages
        return result

    def close(self) -> None:
        if self.session:
            self.session.close()
        # `/resume` closes this runtime rather than replacing its agent.
        retire = getattr(self.agent.model, "retire", None)
        if callable(retire):
            retire()
        # Drops the logs of finished jobs only. A still-running job is the
        # whole point of the design and is left alone, still writing its log.
        self.jobs.shutdown()

    def shell_environment(self) -> tuple[Path, dict[str, str] | None]:
        """Where and with what environment `!command` runs: the agent's own shell settings."""
        for capability in self.agent.root_capability.capabilities:
            if isinstance(capability, JobShell):
                return Path(capability.workdir), capability.env
            if isinstance(capability, Shell):
                return self._workspace_dir(), capability.env
        return Path.cwd(), None

    def _workspace_dir(self) -> Path:
        """The directory the agent's `LocalWorkspace` works in, else the process's own."""
        return _local_workspace(self.agent) or Path.cwd()

    @staticmethod
    def _run_workspace(agent) -> LocalWorkspaceBackend | None:
        """The run's workspace, named explicitly rather than taken from history.

        Pydantic AI records each run's workspace on its responses and, left to
        choose, refuses history recorded in another directory. pcode continues
        sessions elsewhere on purpose (a removed worktree's session moves to the
        mainline, `-C DIR --continue`), and the history stays byte-identical.
        """
        directory = _local_workspace(agent)
        return None if directory is None else LocalWorkspaceBackend(directory)

    async def record_shell(self, run: ShellRun) -> str | ToolReturn:
        """Queue a finished `!command` as a shell tool exchange for the next request.

        It is not written to history yet: nothing has been sent, and a turn
        that fails before its first request must not leave a tool call the
        session's snapshots never saw. `_stream` appends it to the request's
        message history, so the turn's own persistence carries it from then on.
        Returns what the model will see, reduced by the agent's output limits.
        """
        call_id = f"shell_mode_{uuid4().hex[:12]}"
        content: str | ToolReturn = run.tool_result()
        limits = next(
            (c for c in self.agent.root_capability.capabilities if isinstance(c, ToolOutputLimits)),
            None,
        )
        if limits is not None:
            content = await reduce_result(
                limits, call_id=call_id, command=run.command, result=content
            )
        first = not self.history and not self.pending_shell
        self.pending_shell.extend(
            shell_exchange(run.command, content, call_id=call_id, first=first)
        )
        return content

    def resend_prompt(self) -> str:
        """The request /continue asks for again, which labels the resent turn.

        It is not sent again unless the history no longer holds it. After an
        interrupt left tool effects unsettled, `stream` sends it inside a
        briefing on those tools instead.
        """
        if self.recovery_blocked:
            raise SessionError(self.recovery_blocked)
        active = self.tree.nodes.get(self.tree.active)
        if active and active.kind == "aside":
            # Regenerating it here would answer a side question, or a summary
            # of a thread this history does not hold, as a conversation turn.
            raise SessionError(
                "The last exchange came from a side thread; send a message instead of /continue."
            )
        for identity in reversed(self.tree.path(self.tree.active)):
            node = self.tree.nodes[identity]
            if node.kind == "turn" and (request := node.request or node.prompt):
                return request
        raise SessionError("There is no earlier prompt to resend; send a message instead.")

    def _interrupted_tools(self) -> list[dict] | None:
        """The calls an interrupt left unsettled, or None when replaying is safe.

        A briefing turn that did not complete may never have reached the model,
        so it carries its interrupted parent's calls forward along with its own.
        Including them when it did is only repetition; dropping them could
        replay the request that started them.
        """
        tools: list[dict] = []
        blocked = False
        identity = self.tree.active
        while identity is not None:
            node = self.tree.nodes[identity]
            if node.kind != "turn" or node.status == "completed":
                break
            if not (node.resend_blocked or node.request):
                break
            blocked = blocked or node.resend_blocked
            tools[:0] = node.tools
            if not node.request:
                # The interrupted turn the briefings went back to.
                break
            identity = node.parent
        return tools if blocked else None

    async def stream(
        self,
        prompt: str | None,
        *,
        model: SideModel | None = None,
        settings: dict | None = None,
        provider_continuation: bool = False,
    ) -> AsyncIterator[Event]:
        """Retry only failed provider requests, with one budget per submitted turn.

        `model` runs this turn alone on another model, and `settings` replaces
        the conversation's model settings for it; see `TurnModel`. Retries keep
        them, so a retried request goes where the failed one went.

        `provider_continuation=True` drains unsolicited post-turn updates from
        a provider that keeps working after a turn settles (such as `agy acp`)
        without sending a new user prompt.

        A history the current credential cannot replay is the exception: the
        same request would be rejected every time, so it is repaired once and
        resent outside that budget. Provider errors arrive here rather than at a
        capability because the model request is streamed, and its failure
        surfaces while the event stream is consumed.

        The whole generator is one profiled span, including the consumer's
        rendering of each event, so a capture separates a session's working cost
        from what it burns sitting at an idle prompt.
        """
        if self.recovery_blocked:
            raise SessionError(self.recovery_blocked)
        send = prompt
        request = ""
        if provider_continuation:
            from pcode.antigravity import PROVIDER_CONTINUATION_PROMPT

            if not self.has_provider_wake():
                return
            send = PROVIDER_CONTINUATION_PROMPT
        elif send is None:
            from pcode.antigravity import CONTINUE, PROVIDER_CONTINUATION_PROMPT

            original = self.resend_prompt()
            node = self.tree.nodes[self.tree.active]
            if (tools := self._interrupted_tools()) is not None:
                # The settled history may predate tools that already ran, and
                # resuming it would have the model call them again. Tell it
                # what started instead, as a new message.
                send, request = interrupted_briefing(tools, original), original
            elif not self.history:
                send = original
            # A completed text response would short-circuit Agent.run(None).
            # Regenerate just that response, retaining all settled tool results.
            elif isinstance(self.history[-1], ModelResponse):
                if self.history[-1].tool_calls:
                    raise SessionError("The checkpoint has unsettled tools; cannot resend safely.")
                if node.status != "completed" and not node.continuation:
                    send = original
                else:
                    self.history = self.history[:-1]
            # Never replay a synthetic provider-continuation request as a real
            # user prompt (to Antigravity or after switching models): pop it and
            # send a clean continuation instead.
            if (
                send is None
                and self.history
                and isinstance(self.history[-1], ModelRequest)
                and len(self.history[-1].parts) == 1
                and isinstance(self.history[-1].parts[0], UserPromptPart)
                and self.history[-1].parts[0].content == PROVIDER_CONTINUATION_PROMPT
            ):
                self.history = self.history[:-1]
                send = CONTINUE
        attempt = self.retry_attempts if provider_continuation else 0
        repaired = False
        orphans_checked = False
        undeferred = False
        rehosted = False
        self._images_reported.clear()
        chosen = None
        if model is not None or settings is not None:
            chosen = TurnModel(model, settings, self._model_conversation(model, "turn"))
        while True:
            try:
                with profiled_activity("turn"):
                    async with aclosing(
                        self._turn(
                            send,
                            chosen,
                            request,
                            provider_continuation=provider_continuation,
                        )
                    ) as turn:
                        async for event in turn:
                            yield event
            except Exception as error:
                if provider_continuation:
                    raise
                # Deferred MCP schemas are this request's shape, not its history:
                # a provider that rejects them rejects the next turn too, and the
                # session is stuck until they are sent in full. Repairing before
                # the checkpoint guard is what keeps a failed *first* request
                # recoverable, since there is no history to continue from yet.
                if (
                    not undeferred
                    and not self.recovery_blocked
                    and deferred_schemas_rejected(error)
                ):
                    if servers := self.mcp.undefer():
                        undeferred = True
                        if self.context.checkpoint.messages is not None:
                            send = None
                        listed = ", ".join(servers)
                        self.retry_notice(
                            "This model rejected hidden MCP tool schemas, so "
                            f"{listed} now {'sends' if len(servers) == 1 else 'send'} "
                            "every tool up front. Retrying…"
                        )
                        continue
                # A data-residency project rejects the default host on every
                # request, the first included, so this also runs before the
                # checkpoint guard. See `openai_region`.
                if (
                    not rehosted
                    and not self.recovery_blocked
                    and (host := self._follow_region(error, chosen))
                ):
                    rehosted = True
                    if self.context.checkpoint.messages is not None:
                        send = None
                    self.retry_notice(
                        f"This OpenAI project only accepts requests at {host}, so pcode "
                        "now sends them there. Retrying…"
                    )
                    continue
                if self.recovery_blocked or self.context.checkpoint.messages is None:
                    raise
                # Results the current login cannot decrypt fail identically on
                # every attempt, so repair the history once rather than spend
                # the retry budget on a request that cannot succeed.
                if not repaired and unreadable_native_results(error):
                    dropped = drop_unreadable_results(self.context.history)
                    if dropped:
                        repaired = True
                        send = None
                        self.retry_notice(
                            f"Dropped {dropped} unreadable web search "
                            f"{'result' if dropped == 1 else 'results'} from an earlier "
                            "sign-in, and retrying…"
                        )
                        continue
                # A history compacted before the cut learned about native
                # calls spanning responses still holds the orphans. Checking
                # the history rather than the wording also covers a 400 that
                # names an orphaned result instead of a caller.
                if (
                    not orphans_checked
                    and isinstance(error, ModelHTTPError)
                    and error.status_code == 400
                ):
                    orphans_checked = True
                    if dropped := drop_orphaned_native_parts(self.context.history):
                        send = None
                        self.retry_notice(
                            f"Dropped {dropped} server tool "
                            f"{'block' if dropped == 1 else 'blocks'} whose originating "
                            "call was compacted away, and retrying…"
                        )
                        continue
                if attempt == self.retry_attempts or not transient(error):
                    raise
                # _turn saved the exact failed request, including steering and
                # compaction. Never infer progress from the length of history.
                attempt += 1
                send = None
                self.retry_notice(
                    f"{error_message(error)} Retrying provider request "
                    f"{attempt}/{self.retry_attempts}…"
                )
                await asyncio.sleep(retries.RETRY_DELAY)
            else:
                return

    def _follow_region(self, error: BaseException, chosen: TurnModel | None) -> str | None:
        """The regional host this turn's OpenAI requests now go to, if the error named one."""
        host = regional_host(error)
        if host is None:
            return None
        if isinstance(self.agent.model, str):
            from pydantic_ai.models import infer_model

            # A model still held by name has no client yet to repoint.
            try:
                self.agent.model = infer_model(self.agent.model)
            except Exception:
                return None
        turn = chosen.model.model if chosen and chosen.model else None
        return host if use_regional_host(host, [self.agent.model, turn]) else None

    async def _turn(
        self,
        send: str | None,
        chosen: TurnModel | None = None,
        request: str = "",
        *,
        provider_continuation: bool = False,
    ) -> AsyncIterator[Event]:
        """Run one attempt. A `None` prompt continues from history without adding to it.

        `request` is the user's request when `send` is a briefing wrapping it.
        """
        prompt = "" if provider_continuation else (send or "")
        is_continuation = send is None or provider_continuation
        # This turn's state, read and written through one object rather than
        # through the runtime. One turn runs at a time, so it is still the
        # active branch's context; a second turn would be given its own.
        context = self.context
        # Restored if this turn fails before sending anything, so the marker
        # never outlives the prompt it was added for.
        unmarked = marked = context.history
        if send is not None:
            context.history = marked = mark_unanswered(context.history, self._last_turn())
        context.checkpoint = RequestCheckpoint()
        saved = self._open_session()
        run_id = str(uuid4())
        if saved:
            saved.append(
                "turn_started",
                prompt=prompt,
                run_id=run_id,
                parent_id=self.tree.active,
                continuation=is_continuation,
                request=request,
                sync=True,
            )
            saved.info.status = "running"
            self.record_settings(saved.info)
            saved.save_info()
        else:
            self.tree.consume(
                {
                    "kind": "turn_started",
                    "prompt": prompt,
                    "continuation": is_continuation,
                    "request": request,
                    "run_id": run_id,
                    "parent_id": self.tree.active,
                }
            )
        self.inspections.run_id = run_id
        context.run_id = run_id
        context.compaction_usage = RunUsage()
        tools_started = False
        try:
            async with aclosing(
                self._stream(
                    send,
                    context,
                    chosen,
                    provider_continuation=provider_continuation,
                )
            ) as stream:
                async for event in stream:
                    if isinstance(event, ToolStarted):
                        tools_started = True
                    if isinstance(
                        event,
                        (
                            PlanPreview,
                            ChildPlan,
                            ChildText,
                            WorkerStatus,
                            CommandOutput,
                            EditPreview,
                        ),
                    ):
                        # Unexecuted arguments and a sub-agent's transient plan
                        # and prose must never enter replay/tree history.
                        yield event
                        continue
                    if saved:
                        saved.event(event, run_id=run_id)
                    if saved is None:
                        # Named with its turn, the way the journal records it.
                        record = {"kind": type(event).__name__, **asdict(event)}
                        record["run_id"] = record.get("run_id") or run_id
                        self.tree.consume(record)
                    if saved is None and isinstance(
                        event, (ToolStarted, ToolSummary, EditCompleted)
                    ):
                        self.inspections.event(event)
                    yield event
        except BaseException as error:
            resend_blocked = tools_started and context.checkpoint.messages is None
            # The model the failed request went to, which a `$MODEL` turn chose.
            ran_on = context.model or self.agent.model
            if saved:
                self._save_totals(saved.info)
            self.inspections.settle(
                "interrupted"
                if isinstance(error, (asyncio.CancelledError, KeyboardInterrupt, GeneratorExit))
                else "unknown"
            )
            if saved:
                cancelled = isinstance(
                    error, (asyncio.CancelledError, KeyboardInterrupt, GeneratorExit)
                )
                abandoned = (
                    {"abandoned_delegations": deepcopy(context.abandoned_delegations)}
                    if context.abandoned_delegations
                    else {}
                )
                saved.append(
                    "turn_cancelled" if cancelled else "turn_failed",
                    run_id=run_id,
                    error=error_details(error),
                    provider_context=provider_context(ran_on),
                    resend_blocked=resend_blocked,
                    sync=True,
                    **abandoned,
                )
                if not cancelled:
                    # A bug reaches the user as a type and a message; the frames
                    # that name the responsible line live only in this process.
                    # Stopping on purpose is not a defect worth a traceback.
                    saved.record_error(
                        error,
                        run_id=run_id,
                        provider_context=provider_context(ran_on),
                    )
                saved.info.status = "cancelled" if cancelled else "failed"
                saved.save_info()
                try:
                    # Keep completed tool results even if the *following* request
                    # failed. Never silently re-run a side effect on retry.
                    checkpoint = context.checkpoint
                    if checkpoint.messages is not None:
                        # The failed request already carried any shell-mode
                        # exchange; recovering it below must not queue it twice.
                        if not provider_continuation:
                            context.pending_shell = []
                        # Override any partial response saved during unwind. This
                        # also persists a bare first prompt, which Harness omits.
                        await saved.store.save_snapshot(
                            ContinuableSnapshot(
                                run_id=run_id,
                                step_index=checkpoint.step,
                                messages=checkpoint.messages,
                                conversation_id=self.conversation_id,
                            )
                        )
                    context.history = await saved.recover()
                except SessionError as recovery_error:
                    self.recovery_blocked = str(recovery_error)
            else:
                cancelled = isinstance(
                    error, (asyncio.CancelledError, KeyboardInterrupt, GeneratorExit)
                )
                if context.checkpoint.messages is not None:
                    if not provider_continuation:
                        context.pending_shell = []
                    context.history = context.checkpoint.messages
                elif context.history is marked:
                    # Untouched since: nothing was sent. Failed auto-compaction
                    # instead leaves the settled tool results it recovered.
                    context.history = unmarked
                abandoned = (
                    {"abandoned_delegations": deepcopy(context.abandoned_delegations)}
                    if context.abandoned_delegations
                    else {}
                )
                self.tree.consume(
                    {
                        "kind": "turn_cancelled" if cancelled else "turn_failed",
                        "run_id": run_id,
                        "resend_blocked": resend_blocked,
                        **abandoned,
                    }
                )
                self.tree.nodes[run_id].history = deepcopy(context.history)
            raise
        else:
            self.inspections.settle("unknown")
            if saved:
                saved.append("turn_completed", run_id=run_id, sync=True)
                saved.info.status = "complete"
                saved.info.turns = self.turns
                self._save_totals(saved.info)
                saved.save_info()
            else:
                self.tree.consume({"kind": "turn_completed", "run_id": run_id})
                self.tree.nodes[run_id].history = deepcopy(context.history)

        finally:
            # The auto-compaction summarizer is its own agent run: its usage
            # reaches neither `after_model_request` nor this run's result. It is
            # reset per attempt, so a retry loop cannot double-count it.
            self.totals.add(context.compaction_usage)
            context.context_history = None

    def _last_turn(self):
        """The active branch's latest turn, looking past compactions of it.

        A compaction node is always `completed`, yet the tail it keeps can still
        end in the request a cancelled turn left unanswered.
        """
        for identity in reversed(self.tree.path(self.tree.active)):
            node = self.tree.nodes[identity]
            if node.kind != "compaction":
                return node
        return None

    async def _with_images(self, prompt: str | None, model) -> str | list | None:
        """`prompt` with the images it names, saying what came along and what could not."""
        if prompt is None:
            return None
        attached = await asyncio.to_thread(attach, prompt, self._workspace_dir())
        # A retried attempt that sends the prompt again reads its images again,
        # but the user heard about them the first time.
        if prompt not in self._images_reported:
            self._images_reported.add(prompt)
            self._report_images(attached, model)
        return attached.content

    def _report_images(self, attached, model) -> None:
        from pcode.images import image_support

        if attached.names:
            count = len(attached.names)
            self.notice(f"Attached {count} image{'s' * (count != 1)}: {', '.join(attached.names)}")
            if image_support(model) is not True:
                name = model if isinstance(model, str) else model.model_id
                self.warning_notice(
                    f"{name} may not see images, so it gets a note in their place. "
                    "The conversation keeps them for a model that can."
                )
        for problem in attached.problems:
            self.warning_notice(f"Image not attached: {problem}.")

    @asynccontextmanager
    async def _following(self, agent: Agent):
        """Layer the current agent's settings on each request, as `FollowAgent` does its model.

        A run otherwise captures its agent's settings when it starts, so a
        mid-turn `/model` or `/effort` would reach the next turn only. Read per
        request, the new agent's settings replace the old ones outright: none of
        the old model's keys carry over to the new one.
        """
        with agent.override(model_settings=lambda ctx: self.agent.model_settings):
            yield

    def _consume_steering(self, run_id: str, *, defer_commit: bool = False) -> list:
        if defer_commit:
            # Let TypeError propagate when `take_steering` doesn't accept `defer_commit`,
            # so `Steering.take_deferred` falls back to eager `take()` with a no-op commit.
            messages = self.take_steering(defer_commit=True)
            # Keep `Steer` objects intact so `_commit_steering` records `sent_at`.
            return list(messages)
        try:
            messages = self.take_steering(defer_commit=False)
        except TypeError:
            messages = self.take_steering()
        self._journal_steering(run_id, messages)
        # Plain text onward: the stamp is for the journal, not the model request.
        return [str(text) for text in messages]

    def _commit_steering(self, run_id: str, messages: list) -> None:
        self.commit_steering(messages)
        self._journal_steering(run_id, messages)

    def _journal_steering(self, run_id: str, messages: list) -> None:
        if self.session:
            for text in messages:
                # When it was typed, beside the record's own delivery `time`:
                # the gap is how long the tools ahead of it held it back.
                sent = {"sent_at": text.sent_at} if hasattr(text, "sent_at") else {}
                self.session.append("steering", run_id=run_id, prompt=str(text), **sent)

    def _run_capabilities(
        self, context: TurnContext, chosen: TurnModel, *, provider_continuation: bool = False
    ) -> list:
        """Per-run capabilities bind to this turn's context, not to the runtime:
        what they publish and rewrite belongs to this turn."""
        run_id = context.run_id
        return (
            ([StepPersistence(store=self.session.store)] if self.session else [])
            + [
                Steering(
                    lambda *, defer_commit=False: self._consume_steering(
                        run_id, defer_commit=defer_commit
                    ),
                    lambda: self.has_steering(),
                    # A message naming images brings them, as a prompt does.
                    prepare=lambda text: self._with_images(text, self.agent.model),
                    reminder=STEERING_REMINDER,
                    commit_messages=lambda messages: self._commit_steering(run_id, messages),
                ),
            ]
            # Provider continuations send no prompt over ACP, so keep pcode's
            # job and abandoned-delegation notices queued for the next real prompt.
            + (
                []
                if provider_continuation
                else [
                    # Finished jobs reach the model here rather than by
                    # being polled for; see `pcode.job_notices`. Ahead of the
                    # checkpoint, so a saved request carries the notices it
                    # was really sent with, as steering and compaction do.
                    JobNotices(self.jobs),
                    AbandonedDelegations(context.abandoned_delegations),
                ]
            )
            + [
                context.checkpoint,
                TokenAccounting(record=self.totals.add),
                ContextTracking(self, context),
            ]
            # Compaction rewrites the conversation's history, so it is
            # judged on the conversation's model, never on a model one
            # `$MODEL` turn borrowed: its window and summarizer would
            # decide what the conversation keeps from then on.
            # Installed whatever `auto_compact` says; it checks that per request.
            + ([AutoCompaction(self, context)] if chosen.model is None else [])
            # A borrowed turn or provider continuation keeps its model to the end.
            # Pydantic AI refuses to resume a suspended response under a model
            # selector, so a turn continuing one stays on its model too.
            + (
                []
                if provider_continuation or chosen.borrowed or ends_suspended(context.messages())
                else [FollowAgent(lambda: self.agent, context)]
            )
        )

    async def _translator(
        self, context: TurnContext, agent: Agent, *, provider_continuation: bool = False
    ) -> EventTranslator:
        capabilities = agent.root_capability.capabilities
        plan_items = [item.model_dump(mode="json") for item in await context.plan_store.get_items()]
        plan_preview = (
            StreamingPlanPreview() if any(isinstance(c, Planning) for c in capabilities) else None
        )
        # Relative edit paths resolve from the workspace, whatever `root_dir` bounds.
        writable = any(isinstance(c, FileSystem) and not c.read_only for c in capabilities)
        edit_preview = StreamingEditPreview(self._workspace_dir()) if writable else None
        return EventTranslator(
            self,
            context,
            plan_items,
            plan_preview=plan_preview,
            edit_preview=edit_preview,
            provider_continuation=provider_continuation,
        )

    async def _stream(
        self,
        prompt: str | None,
        context: TurnContext,
        chosen: TurnModel | None = None,
        *,
        provider_continuation: bool = False,
    ) -> AsyncIterator[Event]:
        from pcode.antigravity import unwrap

        context.model = None
        await self.refresh_context()
        # One agent for the whole entry: a `/model` landing at one of these
        # awaits must not set up one agent and run another.
        agent = self.agent
        self._persist_child_runs(agent)
        translator = await self._translator(
            context, agent, provider_continuation=provider_continuation
        )
        chosen = chosen or TurnModel(None, None, self.conversation_id)
        context.model = chosen.model.model if chosen.model is not None else agent.model
        ag_model = unwrap(context.model) if provider_continuation else None
        if provider_continuation and (
            ag_model is None or not ag_model.has_pending_wake(context.history)
        ):
            # Model switched or wake cleared during `refresh_context()`: exit cleanly
            # rather than sending the synthetic placeholder to another provider.
            return
        content = (
            prompt if provider_continuation else await self._with_images(prompt, context.model)
        )
        continuation_ctx = ag_model.continuation() if ag_model is not None else nullcontext()
        history_for_run = context.history if provider_continuation else context.messages()
        # Unlike run_stream(), this completes the tool loop even when the model
        # sends explanatory text alongside its tool calls.
        # Enter the agent too: a run alone does not own a statically supplied
        # model's HTTP client. Exit closes it on success, failure, or cancellation.
        async with (
            chosen.applied(agent) if chosen.borrowed else self._following(agent),
            agent,
            chosen.model.model if chosen.model is not None else nullcontext(),
            worker_toolsets([self.mcp.live()]),
            enabled_servers(self.mcp.servers, self.mcp.unavailable),
        ):
            with continuation_ctx:
                async with agent.run_stream_events(
                    content,
                    message_history=_for_run(history_for_run),
                    workspace=self._run_workspace(agent),
                    toolsets=[self.mcp.live()],
                    conversation_id=chosen.conversation_id,
                    run_id=context.run_id,
                    **({"model": chosen.model.model} if chosen.model is not None else {}),
                    capabilities=self._run_capabilities(
                        context, chosen, provider_continuation=provider_continuation
                    ),
                    # Explicitly disable the cap; omitting this restores the library default.
                    usage_limits=UsageLimits(request_limit=None),
                ) as events:
                    async for event in events:
                        # Closed here, not by GC, when the consumer stops mid-event.
                        async with aclosing(translator.translate(event)) as outputs:
                            async for out in outputs:
                                yield out


def ends_suspended(history: list[ModelMessage]) -> bool:
    """Whether `history` ends in a response a provider paused, which the next run resumes."""
    last = history[-1] if history else None
    return isinstance(last, ModelResponse) and last.state == "suspended"


CANCELLED_MARKER = "[Request interrupted by user]"
UNANSWERED_MARKER = "[Request ended without a response]"


def mark_unanswered(history: list[ModelMessage], node) -> list[ModelMessage]:
    """Close a request the previous turn left unanswered before a new prompt follows it.

    A cancelled or failed turn keeps the request it sent, so completed tool
    results are never re-run. Without a marker the next prompt lands right
    after it as another user message; providers merge the two, and the model
    reads the abandoned prompt and the new one as a single message ("is" then
    "ci is failing" arrives as "isci is failing"). The marker goes on the
    trailing request itself, so its `interrupted` state, which tells Pydantic
    AI to close out dangling tool calls, is kept. `/continue` sends no new prompt,
    so it still repeats the request exactly as it was.
    """
    if node is None or node.status == "completed" or not history:
        return history
    last = history[-1]
    if not isinstance(last, ModelRequest):
        return history
    marker = CANCELLED_MARKER if node.status == "cancelled" else UNANSWERED_MARKER
    tail = last.parts[-1] if last.parts else None
    if isinstance(tail, UserPromptPart) and tail.content in (CANCELLED_MARKER, UNANSWERED_MARKER):
        return history
    return [*history[:-1], replace(last, parts=[*last.parts, UserPromptPart(marker)])]


def interrupted_briefing(tools: list[dict], request: str) -> str:
    """What /continue sends after a turn stopped with tool effects its history lacks."""
    lines = [
        "Your previous turn was interrupted after it had started tools, so some of "
        "their effects may already be in place even where no result came back:"
    ]
    for tool in tools:
        outcome = tool["outcome"] or "no result, interrupted"
        label = f": {tool['label']}" if tool["label"] else ""
        lines.append(f"- {tool['name']}{label} ({outcome})")
    if not tools:
        lines.append("- (the calls were not recorded)")
    lines += [
        "",
        "Check the current state before repeating any of them, then carry on with the request:",
        "",
        request,
    ]
    return "\n".join(lines)


RETRY_CEILING = re.compile(r"^Tool '(?P<tool>[^']+)' exceeded max retries count of (?P<limit>\d+)")


def retry_ceiling(error: Exception) -> str | None:
    """Explain a turn that ended because a tool call could not be corrected in time.

    This is a local failure, not a provider one: the model kept sending
    arguments the tool's schema rejected (or the tool kept raising ModelRetry),
    and Pydantic AI stopped offering corrections. Saying "check your
    credentials and connectivity" for it sends the reader to the wrong place.

    Name the tool and the fields that failed, never their values: a rejected
    argument is model-authored content that can quote a file or a secret.
    """
    cause = error.__cause__
    if type(error).__name__ != "UnexpectedModelBehavior" or cause is None:
        return None
    match = RETRY_CEILING.match(str(error))
    if match is None:
        return None
    tool, limit = match["tool"], int(match["limit"])
    fields = ""
    if callable(getattr(cause, "errors", None)):
        try:
            names = {
                ".".join(str(part) for part in entry.get("loc", ()))
                for entry in cause.errors()
                if entry.get("loc")
            }
        except Exception:
            names = set()
        if names:
            fields = " Rejected argument: " + ", ".join(sorted(names)) + "."
    reason = (
        "arguments that failed validation"
        if type(cause).__name__ == "ValidationError"
        else "a call the tool rejected"
    )
    return (
        f"The model sent {reason} to `{tool}` more times than the retry limit "
        f"({limit}) allowed, so the turn stopped.{fields} "
        "Nothing is wrong with the model or the connection; rephrasing the request "
        "usually clears it. Raise the budget with `/config set tool_retries N`. "
        "See the saved session diagnostics."
    )


CODEX_LOGIN_HINT = "Run `/login openai-codex` (or `codex login`, then restart pcode)."

# Packages whose exceptions mean an MCP server failed, not the model or provider.
_MCP_AUTH_PACKAGES = OAUTH_PACKAGES
_MCP_PACKAGES = ("mcp", "fastmcp", "pydantic_ai.mcp", "pcode.mcp", *_MCP_AUTH_PACKAGES)


def _mcp_failure(error: BaseException) -> str | None:
    """`"auth"` or `"server"` when an MCP client raised the error or its explicit cause.

    Recognized by the raising package, never by message text: an MCP tool call
    that fails mid-turn otherwise reaches the generic guess, which blames the
    model and provider.
    """
    found = None
    for _ in range(8):
        module = type(error).__module__
        if any(module == pkg or module.startswith(f"{pkg}.") for pkg in _MCP_AUTH_PACKAGES):
            return "auth"
        if any(module == pkg or module.startswith(f"{pkg}.") for pkg in _MCP_PACKAGES):
            found = "server"
        if error.__cause__ is None:
            break
        error = error.__cause__
    return found


def error_message(error: Exception, *, unexpected: str | None = None) -> str:
    """Don't print raw provider bodies/validation inputs; they can contain secrets.

    `unexpected` replaces the closing guess for an unrecognized error, which
    otherwise blames the model or provider: right for a turn, wrong for a
    slash command that never reached one.
    """
    if isinstance(error, BaseExceptionGroup) and error.exceptions:
        return error_message(error.exceptions[0], unexpected=unexpected)
    if getattr(type(error), "sanitized", False):
        # Already passed through here in the session host that raised it.
        return str(error)
    from pcode.auth import LoginError
    from pcode.compaction import CompactionError
    from pcode.workspace import WorkspaceGoneError
    from pcode.worktree import WorktreeError

    if isinstance(error, (CompactionError, WorktreeError, WorkspaceGoneError)):
        # All three carry only local paths and git's own messages.
        return str(error)
    name = type(error).__name__
    if isinstance(error, (SessionError, LoginError)):
        # LoginError contains only fixed, sanitized setup/refresh guidance.
        return str(error)
    if (mcp := _mcp_failure(error)) is not None:
        # SDK messages can carry token-endpoint bodies; the diagnostics log has them.
        if mcp == "auth":
            from mcp.client.auth.exceptions import OAuthRegistrationError

            if find_cause(error, OAuthRegistrationError) is not None:
                # Google's Workspace servers refuse dynamic registration; retrying
                # sign-in cannot help until the entry names a client.
                return (
                    "MCP server sign-in failed: it did not accept pcode's automatic OAuth "
                    "client registration, so it needs a pre-registered client. Add "
                    f"`client_id` and `client_secret` to its entry in {config_path()} "
                    "(docs: MCP > Pre-registered clients), then `/mcp disable NAME` and "
                    "`/mcp enable NAME`. Not the model or provider."
                )
            return (
                f"MCP server sign-in failed ({name}), not the model or provider. "
                "Retry with `/mcp enable NAME`, or `/mcp logout NAME` to start over. "
                "See the saved session diagnostics."
            )
        if (failed := find_cause(error, MCPConnectError)) is not None:
            # Fixed text, the server name, and the config path only.
            return f"{failed} Not the model or provider. See the saved session diagnostics."
        return (
            f"MCP server request failed ({name}), not the model or provider. "
            "Check it with `/mcp list`, or turn it off with `/mcp disable NAME`. "
            "See the saved session diagnostics."
        )
    if isinstance(error, UsageLimitExceeded):
        # A budget the run was given, not the provider failing; the generic
        # closing guess below sent people checking credentials. The library's message
        # is fixed text plus the limit, followed by a docs pointer we drop.
        reason = str(error).split(". ", 1)[0].rstrip(".")
        return f"Stopped at a usage limit, not a provider error: {reason}."
    from pcode.claude_sdk import failure_hint

    if (hint := failure_hint(error)) is not None:
        return hint
    if is_unknown_model(error):
        # A saved session or preference can name a provider pcode has dropped.
        text = str(error)
        return f"{text}{'' if text.endswith(('.', '?')) else '.'} Pick one with /model."
    if name == "UserError" and "Codex CLI credentials" in str(error):
        return f"Provider login missing or invalid. {CODEX_LOGIN_HINT}"
    if name == "UserError" and "ANTHROPIC_API_KEY" in str(error):
        # pcode defers the model check so the terminal opens without a key;
        # the failure then surfaces here, on the first prompt.
        from pcode.models import anthropic_credential_hint

        return f"ANTHROPIC_API_KEY is not set. {anthropic_credential_hint()}"
    if name == "CredentialsRefreshError":
        # Recognize only fixed public error codes, never echo token-endpoint bodies.
        for code in (
            "refresh_token_invalidated",
            "refresh_token_reused",
            "refresh_token_expired",
            "invalid_grant",
        ):
            if code in str(error):
                return f"Provider login is no longer valid ({code}). {CODEX_LOGIN_HINT}"
        return f"Provider token refresh failed. {CODEX_LOGIN_HINT}"
    if "Credential" in name or "Authentication" in name:
        return f"Authentication failed ({name}). Refresh your provider login and restart."
    if (quota := quota_message(error)) is not None:
        return quota
    status = getattr(error, "status_code", None)
    if status is not None:
        details = error_details(error)
        detail = details.get("provider_message", "")
        suffix = f" {detail}" if detail else " See the saved session diagnostics."
        if "reasoning summar" in detail.lower():
            # An unverified OpenAI organisation, refused the summaries that
            # `/show-thinking activity-panel` and `scrollback` ask for.
            suffix += " `/show-thinking off` stops asking for them."
        return f"Provider request failed (HTTP {status}).{suffix}"
    if isinstance(error, ImportError):
        return "Provider dependency missing. Install its pydantic-ai-slim extra and try again."
    if (exhausted := retry_ceiling(error)) is not None:
        return exhausted
    # SDKs wrap transport failures in ModelAPIError. Classify the bounded cause
    # chain, but never echo transport text: it may contain URLs or credentials.
    names = transport_types(error)
    if "RemoteProtocolError" in names:
        return (
            "Provider connection closed or returned an incomplete/invalid response. "
            "Check provider/proxy connectivity and retry when ready. "
            "See the saved session diagnostics."
        )
    if "ModelRequestTimeout" in names:
        from pydantic_ai.exceptions import ModelRequestTimeout

        # Pydantic AI's own message: the model name and the seconds, nothing private.
        return f"{find_cause(error, ModelRequestTimeout)}."
    if names & {"APITimeoutError", "ConnectTimeout", "ReadTimeout", "WriteTimeout"}:
        return (
            "Provider request timed out. Check provider/proxy connectivity and retry when ready. "
            "See the saved session diagnostics."
        )
    if names & {"APIConnectionError", "ConnectError", "ReadError", "WriteError"}:
        return (
            "Could not communicate with the provider. "
            "Check network/proxy settings and provider availability, then retry when ready. "
            "See the saved session diagnostics."
        )
    if unexpected is not None:
        return f"{unexpected} ({name})."
    return f"Run failed ({name}). Check the model string, provider credentials, and connectivity."
