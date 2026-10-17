"""Agent construction is independent of the terminal and runtime adapter."""

import os
import shutil
import sys
from collections.abc import Sequence
from contextlib import asynccontextmanager
from contextvars import ContextVar
from copy import copy
from dataclasses import dataclass, fields, replace
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic_ai import Agent
from pydantic_ai.capabilities import (
    AbstractCapability,
    Capability,
    CombinedCapability,
    LocalWorkspace,
)
from pydantic_ai.models import Model
from pydantic_ai.profiles.openai import OpenAIModelProfile
from pydantic_ai.toolsets import CombinedToolset
from pydantic_ai.usage import UsageLimits
from pydantic_ai_harness.coder import Coder
from pydantic_ai_harness.compaction import ClearToolResults
from pydantic_ai_harness.filesystem import FileSystem
from pydantic_ai_harness.repo_context import RepoContext
from pydantic_ai_harness.shell import LLM_API_KEY_ENV_PATTERNS, Shell
from pydantic_ai_harness.subagents import ModelOption, SubAgent
from pydantic_ai_harness.tool_output_limits import ToolOutputLimits

from pcode import remote_profile
from pcode.background_delegation import BackgroundDelegation
from pcode.cache_settings import ProviderCacheSettings, model_settings
from pcode.cache_warnings import CacheBustReporting
from pcode.claude_sdk import ClaudeWorkspace
from pcode.code_mode import create_code_mode
from pcode.delegation import DelegationReporting, stream_child_activity
from pcode.ext import EXTENSION_GUIDE, ExtensionCapabilities
from pcode.filesystem import DisplayFileSystem
from pcode.images import ImageGate
from pcode.isolated_delegation import WorkspaceSubAgents
from pcode.job_notices import JobNotices
from pcode.jobs import isolated_registry
from pcode.mcp import configured_servers
from pcode.mcp_notice import MCPServers
from pcode.output_limits import ModelOutputLimits
from pcode.planning import IdentifiedPlanning
from pcode.preferences import SETTINGS, load_preferences, subagent_models
from pcode.repo_context import create_repo_context
from pcode.shell_tools import JobShell
from pcode.strict_tools import create_strict_tools
from pcode.tool_output_limits import create_tool_output_limits
from pcode.workspace import WorkspaceGuard

if TYPE_CHECKING:
    from pydantic_ai.models.openai_codex import OpenAICodexModel

# Harness gives a child its own usage counter only when its `SubAgent` carries
# `usage_limits`; without one the child shares the parent's counter and silently
# gets the library's 50-request default, which a busy session has already spent.
# The limits set no cap: like the parent turn, which is uncapped too, a child's
# activity is on screen and Ctrl+C stops it, while a fixed budget discarded a
# working child's whole result. `pcode.ext.subagent` applies this to extension
# delegates too.
SUBAGENT_USAGE_LIMITS = UsageLimits(request_limit=None)

# Coder's default prompt without "finish long-running work before responding",
# which kept the model waiting on jobs instead of answering steering. Job
# mechanics are in the shell tool descriptions; don't add workflow rules here.
CODER_INSTRUCTIONS = """\
You are a software engineering agent. Use tools to investigate, implement, and
verify the requested work. Read existing code and follow repository instructions
and conventions. Prefer focused changes that fix causes, not symptoms.

Apply DRY, YAGNI, SOLID, and the Zen of Python pragmatically: simple, explicit,
cohesive code beats abstractions without a present need.

Work autonomously until complete. Ask only for missing requirements, credentials,
consequential ambiguity, or approval for irreversible actions. Use reasonable
defaults for minor ambiguities. Run focused tests and appropriate lint/type checks;
report what you actually verified, assumptions, and remaining limitations.

Servers may remain running once readiness is verified; shut them down when no
longer needed.
"""

AGENT_INSTRUCTIONS = (
    "Responses are displayed in a terminal with Markdown rendering "
    "and syntax highlighting. "
    "Use fenced code blocks with a language tag for multiline code or shell examples, "
    "and inline backticks for identifiers and short commands. Close all code fences. "
    "Write ordinary prose outside code blocks. "
    "Full URLs are clickable in the terminal, so use them liberally and "
    "prefer them over bare references like issue numbers or page titles. "
    # /copy offers quotes as their own choice, since the rendered rail and wrap
    # make them hard to select by hand.
    "Put text meant for the user to paste elsewhere in a > blockquote. "
    # GPT-6 tends to edit through shell commands, bypassing captured edit diffs.
    "Prefer edit_file and write_file for file changes over shell tools. "
    # The model is the intended extension author, so it needs to know the
    # mechanism exists without the reference text sitting in every prompt.
    "pcode itself is extensible with small Python files (new slash commands, "
    "tools, guardrails on tool calls, extra instructions). When asked to change "
    f"how pcode behaves, first read {EXTENSION_GUIDE} and follow it."
)


_worker_toolsets: ContextVar[Sequence] = ContextVar("worker_toolsets", default=())


@dataclass
class WorkerRuntimeTools(CombinedToolset):
    """Stands in for the delegating turn's runtime toolsets in each worker run.

    Resolved once per run rather than by a per-step toolset function, which
    would exit and re-enter them at every step, and through their own
    `for_run`, so a live MCP toolset gets a fresh instance for this worker and
    still follows /mcp changes at each of its steps.
    """

    toolsets: Sequence = ()

    async def for_run(self, ctx):
        return await CombinedToolset(list(_worker_toolsets.get())).for_run(ctx)


@asynccontextmanager
async def worker_toolsets(toolsets: Sequence):
    """Give the built-in worker the same enabled runtime toolsets as its parent.

    Harness's inherit_tools covers constructor toolsets, not per-run MCP tools,
    and would also broaden specialized extension delegates. Context-local agent
    bindings keep this scoped to the worker and to this turn, including errors
    and cancellation; no stale MCP connection is retained after disable/reload.
    """
    token = _worker_toolsets.set(tuple(toolsets))
    try:
        yield
    finally:
        _worker_toolsets.reset(token)


def tool_retries() -> dict[str, int]:
    """The correction budget for tool-argument validation and `ModelRetry`.

    Pydantic AI defaults both tool and output retries to 1, so a second
    malformed call to a tool ends the whole turn with `UnexpectedModelBehavior`.
    A nested argument like `edit_file`'s `replacements` array is easy to mangle
    twice in a row, and a correction costs one round trip where the failure
    costs the turn. Output retries keep the stricter default: a model that
    cannot produce the final output shape twice is not going to converge.
    """
    configured = load_preferences().get("tool_retries", SETTINGS["tool_retries"].default)
    return {"tools": int(configured)}


def has_mcp_servers() -> bool:
    """Whether mcp.json configures any server, deciding once per agent.

    A broken file counts as none: nothing can be enabled from it, and `/mcp`
    reports the error when it is used.
    """
    try:
        return bool(configured_servers())
    except ValueError:
        return False


def _init_fields(capability) -> dict:
    """A dataclass capability's constructor arguments, to rebuild it as a subclass."""
    return {f.name: getattr(capability, f.name) for f in fields(capability) if f.init}


def _adapt(capability, workspace: Path, output_limits):
    """Map one of Coder's capabilities to pcode's replacement, or keep it.

    Ids must be set at construction (`replace(..., id=...)` or the factory):
    a `Capability` binds its instructions to its id in `__init__`, so a later
    `capability.id = ...` is silently ignored by /status attribution. Do not
    blanket-rename everything either: `replace()`-copied children compare
    fields with their parent and a renamed parent breaks that match.
    """
    match capability:
        # Coder's sole plain Capability holds its base instructions (and a
        # project line pcode's file-tool instructions already cover). Exact type:
        # every capability class below would also match `Capability()`.
        case Capability() if type(capability) is Capability:
            return Capability(instructions=CODER_INSTRUCTIONS)
        case RepoContext():
            return create_repo_context(workspace)
        # Named so /status can attribute its prompt; ids never reach the model.
        # No per-toolset retry budget: the `tool_retries` preference governs.
        case FileSystem():
            return replace(
                DisplayFileSystem.from_filesystem(capability), id="file_tools", max_retries=None
            )
        # Replace Coder's 64k truncation, so it cannot cut data before spilling.
        case ToolOutputLimits():
            return output_limits
        case Shell():
            return _job_shell(capability, workspace)
        case _:
            return capability


def _job_shell(shell: Shell, workspace: Path) -> JobShell:
    """Coder's persistent shell with commands run as named jobs.

    Same execution model, but commands become named jobs the session can wait
    on, report and stop. See `pcode.shell_tools`. Copied field by field
    (including `id`) because a capability binds its instructions to its id in
    `__init__`.
    """
    values = _init_fields(shell)
    # Commands inherit the whole host environment (see `JobShell`), so keep
    # provider credentials out of it, as Coder did before it left the
    # environment to the workspace. The env must be explicit: Harness applies
    # `denied_env_patterns` only to an explicit env, never to `None`.
    values["env"] = dict(shell.env or os.environ)
    values["denied_env_patterns"] = [*values["denied_env_patterns"], *LLM_API_KEY_ENV_PATTERNS]
    return JobShell(**values, workdir=workspace)


def _ensure_bundled_rg() -> None:
    """uv tool entry points do not activate their environment's bin directory.

    Append it only when rg is missing, preserving the user's command precedence.
    """
    bundled_bin = Path(sys.executable).parent
    if shutil.which("rg") is None and (bundled_bin / "rg").is_file():
        os.environ["PATH"] = os.pathsep.join(
            part for part in (os.environ.get("PATH", ""), str(bundled_bin)) if part
        )


def _cache_notices() -> CacheBustReporting | None:
    preferences = load_preferences()
    if preferences.get("cache_notices", SETTINGS["cache_notices"].default) != "on":
        return None
    return CacheBustReporting(
        dump_fingerprints=preferences.get("debug", SETTINGS["debug"].default) == "on"
    )


class TurnLimits(AbstractCapability):
    """Charges every model request and tool call to a remote host's turn budget.

    In the agent's capabilities and in delegation's shared ones, so a
    sub-agent's work counts against the turn that delegated it. A sub-agent's
    failure comes back to the parent as a retryable result; the parent's next
    request then fails too, as the budget stays spent.
    """

    async def before_model_request(self, ctx, request_context):
        remote_profile.BUDGET.charge_request()
        return request_context

    async def before_tool_execute(self, ctx, *, call, tool_def, args):
        remote_profile.BUDGET.charge_tool_call()
        return args


def turn_limits() -> list[TurnLimits]:
    """The limits when a remote profile is active (`pcode.remote_profile`), else none."""
    return [TurnLimits()] if remote_profile.active() is not None else []


def _worker_capabilities(capabilities: Sequence) -> list:
    """The parent's capabilities, copied for the built-in worker.

    Compose the worker from the same capabilities rather than maintaining a
    second tool/policy list. Per-run capability state is still managed upstream.
    The shared types are supplied by SubAgents.shared_capabilities instead (also
    for extension delegates); delegation itself is intentionally parent-only.
    """
    shared_types = (
        ToolOutputLimits,
        ModelOutputLimits,
        ImageGate,
        CacheBustReporting,
        TurnLimits,
    )
    return [
        copy(capability)
        for capability in capabilities
        if not isinstance(capability, (*shared_types, ClearToolResults, DelegationReporting))
    ]


def _delegation(
    workspace: Path,
    worker: Agent,
    subagents: Sequence,
    extensions: Sequence,
    output_limits,
    cache_notices: CacheBustReporting | None,
) -> WorkspaceSubAgents:
    """The `delegate_task` tool: the worker beside extension sub-agents."""

    @asynccontextmanager
    async def isolated_worker(child_workspace: Path):
        if extensions and not isinstance(extensions, ExtensionCapabilities):
            raise ValueError(
                "Isolated workers require rebindable extensions from load_extensions(); "
                "use workspace_mode='shared' for directly supplied capabilities."
            )
        rebound = (
            extensions
            if isinstance(extensions, ExtensionCapabilities)
            else ExtensionCapabilities([])
        )
        with isolated_registry() as jobs:
            async with rebound.for_workspace(child_workspace) as child_extensions:
                child_coder = create_coder(child_workspace, delegation=False)
                child_coder.capabilities.append(JobNotices(jobs))
                yield _create_worker(child_coder.capabilities, child_extensions, isolated=True)

    return WorkspaceSubAgents(
        workspace=workspace,
        worker_factory=isolated_worker,
        agents=[
            SubAgent(worker, usage_limits=SUBAGENT_USAGE_LIMITS),
            *subagents,
        ],
        agent_folders=None,
        # Unavailable names are left out here and reported by /subagents.
        models=subagent_menu(subagent_models())[0],
        event_stream_handler=stream_child_activity,
        # One child's crash (a dropped connection past its retries, a bug in a
        # tool) comes back to the parent as a retryable failure. Propagating it
        # fails the parent's turn, which cancels every sibling delegation.
        contain_errors=True,
        shared_capabilities=[
            # Extension sub-agents have no coder of their own to set it.
            ClaudeWorkspace(workspace, fallback=True),
            ModelOutputLimits(),
            ImageGate(),
            *([replace(cache_notices)] if cache_notices else []),
            ProviderCacheSettings(),
            replace(output_limits),
            # A remote host's turn budget counts its sub-agents' work too.
            *turn_limits(),
        ],
    )


def create_coder(
    workspace: Path, subagents: Sequence = (), extensions: Sequence = (), *, delegation: bool = True
) -> CombinedCapability:
    """Compose Harness's Coder with pcode's repository context and planning.

    `subagents` are extension-contributed Harness `SubAgent` entries, listed
    beside the worker under the one `delegate_task` tool.
    """
    workspace = workspace.resolve()
    _ensure_bundled_rg()
    # Delegation is pcode's own `WorkspaceSubAgents`, added below. Caching is
    # `ProviderCacheSettings`' per-provider choice: Coder's default `cache=True`
    # would also reach OpenAI's explicit-breakpoint models.
    coder = Coder(sub_agents=False, caching=False)
    output_limits = create_tool_output_limits()
    # Keep Coder's tool selection, including its persistent shell (as a
    # `JobShell`). File display and repository discovery remain local adapters;
    # planning is now opt-in.
    capabilities = [_adapt(c, workspace, output_limits) for c in coder.capabilities]
    capabilities = [
        # Ahead of the other capabilities in the list, so a tool call in a
        # deleted workspace stops before anything tries to read or write in it.
        WorkspaceGuard(workspace),
        # Harness capabilities read and write through `ctx.workspace`. Commands
        # bypass it (see `JobShell`), so it needs none of the host's environment.
        LocalWorkspace(workspace),
        *capabilities,
        # Isolated workers build their own coder, so each CLI runs in its checkout.
        ClaudeWorkspace(workspace),
        IdentifiedPlanning(),
        DelegationReporting(),
        ModelOutputLimits(),
        ImageGate(),
        *turn_limits(),
    ]
    # Ahead of the worker copy below, so a delegate edits under the same schema.
    if strict_tools := create_strict_tools():
        capabilities.append(strict_tools)
    # Ahead of the worker copy below, so a delegate that inherits MCP tools also
    # learns which servers they come from.
    capabilities.append(MCPServers(instruct=has_mcp_servers()))
    if cache_notices := _cache_notices():
        capabilities.append(cache_notices)

    worker_capabilities = _worker_capabilities(capabilities)
    if code_mode := create_code_mode():
        capabilities.append(code_mode)
        worker_capabilities.append(copy(code_mode))
    if not delegation:
        return CombinedCapability(worker_capabilities)

    worker = _create_worker(worker_capabilities, extensions)
    capabilities.append(
        _delegation(workspace, worker, subagents, extensions, output_limits, cache_notices)
    )
    # Steering mid-delegation reaches the parent; the child keeps working.
    capabilities.append(BackgroundDelegation())
    # Summarize evidence before discarding it; pcode owns compaction.
    return CombinedCapability([c for c in capabilities if not isinstance(c, ClearToolResults)])


def _create_worker(
    capabilities: Sequence, extensions: Sequence, *, isolated: bool = False
) -> Agent:
    return Agent(
        name="worker",
        retries=tool_retries(),
        description=(
            "Complete a self-contained task using the main agent's tools and permissions, "
            "including file edits, shell commands, tests, web research, and enabled MCP tools"
        ),
        instructions=AGENT_INSTRUCTIONS
        + (
            " You are a general-purpose worker. Complete only the delegated task and report "
            "your changes, verification, and remaining limitations. You inherit the main "
            "agent's instructions, tools, and permission checks, but not its conversation. "
            "Your plan is independent. You cannot delegate further or manage other task worktrees. "
            "Stop jobs you no longer need with stop_job. "
        )
        + (
            "You have an isolated checkout. Commit completed changes here; do not push or merge "
            "into any other checkout. Parent integration and cleanup happen after you return."
            if isolated
            else "You share the parent's workspace: coordinate edits with the parent."
        ),
        capabilities=[*capabilities, *extensions],
        toolsets=[WorkerRuntimeTools()],
    )


def codex_model(model: str) -> OpenAICodexModel:
    """Build a Codex model on pcode's stored login, else the CLI's `auth.json`.

    Without a pcode store the provider reads the CLI file at construction time
    and keeps refreshed tokens in memory only.
    """
    # Imported here, not at module level: `openai_codex` loads the whole OpenAI
    # SDK (about 0.3 s), which a session on any other provider never uses.
    from pydantic_ai.models.openai_codex import OpenAICodexModel
    from pydantic_ai.providers.openai_codex import OpenAICodexProvider

    from pcode.codex_login import credential_source
    from pcode.llm_proxy import ProxiedCodexProvider

    proxy = os.environ.get("PCODE_LLM_PROXY", "").strip()
    source = credential_source()
    if proxy:
        provider = ProxiedCodexProvider(proxy, credential_source=source)
    elif source is not None:
        provider = OpenAICodexProvider(credential_source=source)
    else:
        provider = None
    # Subscription endpoints reject the explicit cache markers that Harness
    # Planning adds after write_plan. Keep the native provider/auth/model name;
    # override only this advertised capability (verified against AI 2.43.0).
    # The tool-search deferral and addition modes come from the provider's
    # profile, which layers `OpenAIProvider.model_profile` under the Codex
    # dialect since AI 2.49.0 (pydantic/pydantic-ai#8693).
    return OpenAICodexModel(
        model.removeprefix("openai-codex:"),
        profile=OpenAIModelProfile(openai_supports_prompt_cache_breakpoints=False),
        **({"provider": provider} if provider is not None else {}),
    )


def resolve_model(model: str) -> Model | str:
    """The model object for a name, on pcode's own logins where it manages them.

    Names pcode has no special handling for come back unchanged for Pydantic
    AI's inference. So does an Anthropic name when API-key auth has no key yet,
    which lets the terminal open and reach /login.
    """
    if model.startswith("openai-codex:"):
        return codex_model(model)
    if model.startswith("claude:"):
        from pcode.claude_sdk import claude_model

        return claude_model(model)
    if not model.startswith("anthropic:"):
        return model
    from pcode.auth import anthropic_model

    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    return anthropic_model(model, key) if key else model


@dataclass(frozen=True)
class SideModel:
    """A model a single run uses instead of the agent's, with its own settings."""

    name: str
    model: Model
    settings: dict | None


def side_model(name: str, effort: str = "") -> SideModel:
    """Resolve `name` now, failing with a clear message rather than on first request.

    The settings are the ones the model would get as the conversation's model:
    its defaults plus its own saved /effort, or `effort` when one was asked
    for. None of the conversation model's settings carry over, since they
    belong to another model or provider.
    """
    from pydantic_ai.exceptions import UserError
    from pydantic_ai.models import infer_model

    from pcode.preferences import effort_for

    try:
        resolved = resolve_model(name)
        if isinstance(resolved, str):
            if name.startswith("anthropic:"):
                from pcode.models import anthropic_credential_hint

                raise ValueError(f"no Anthropic credentials. {anthropic_credential_hint()}")
            resolved = infer_model(resolved)
    except (UserError, ValueError, ImportError) as error:
        raise ValueError(f"Cannot use {name}: {error}") from error
    return SideModel(
        name,
        resolved,
        with_effort(name, resolved, model_settings(name), effort or effort_for(name)),
    )


def subagent_menu(names: Sequence[str]) -> tuple[dict[str, ModelOption], list[str]]:
    """The `delegate_task` model menu for `names`, and why any name was left out.

    Each option is resolved as /btw resolves another model: on pcode's own
    logins, with that model's defaults and saved /effort. Omitting `model` in a
    delegation still runs the sub-agent on the session's model.
    """
    menu: dict[str, ModelOption] = {}
    problems: list[str] = []
    for name in names:
        try:
            chosen = side_model(name)
        except ValueError as error:
            problems.append(str(error))
            continue
        menu[name] = ModelOption(chosen.model, settings=chosen.settings)
    return menu, problems


def with_effort(name: str, model, settings: dict | None, effort: str | None) -> dict | None:
    """`settings` with `effort` applied for `name` the way /effort applies it.

    `settings` itself is never mutated: it may be captured by a run in flight.
    """
    from types import SimpleNamespace

    from pcode.preferences import apply_effort

    holder = SimpleNamespace(model=model, model_settings=settings)
    apply_effort(holder, name, effort)
    return holder.model_settings


def create_agent(
    model: str, workspace: Path, extensions: Sequence = (), subagents: Sequence = ()
) -> Agent:
    """Build the terminal's agent; `extensions` and `subagents` come from `pcode.ext`."""
    resolved = resolve_model(model)
    # Allow the terminal to open so /login is reachable without credentials.
    defer_model_check = model.startswith("anthropic:") and isinstance(resolved, str)
    return Agent(
        resolved,
        defer_model_check=defer_model_check,
        model_settings=model_settings(model),
        name="pcode",
        retries=tool_retries(),
        instructions=AGENT_INSTRUCTIONS,
        capabilities=[create_coder(workspace, subagents, extensions), *extensions],
    )
