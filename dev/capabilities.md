# How pcode bends Pydantic AI

pcode is a stock Pydantic AI `Agent`. There is no forked agent loop, no
monkeypatching of the graph, and no custom runner. The whole agent is built in
`src/pcode/agent.py:create_agent`:

```python
Agent(model, capabilities=[create_coder(workspace, subagents, extensions), *extensions])
```

`create_coder` starts from Harness's `Coder` and walks its capabilities through
`_adapt`, which swaps a few of them for pcode subclasses (file tools that report
diffs, a shell whose commands become named jobs, repo context that snapshots per
run) and keeps the rest. It then appends pcode's own capabilities. Things that
belong to one turn rather than to the agent (steering, job notices, the retry
checkpoint, token accounting, context tracking, auto-compaction) are added per
run in `src/pcode/live.py:AgentRuntime._run_capabilities`, so each binds to the
turn it was created for.

On the way out, `src/pcode/stream_events.py:EventTranslator` turns the typed
events from `agent.run_stream_events` (including the custom `CapabilityEvent`s
some capabilities below emit) into the plain dataclasses the terminal renders.
Extensions plug into the same seam: `src/pcode/ext.py` gives each one
`pcode.add_capability(...)`, and whatever it registers lands in the
`*extensions` half of the list above.

## Capabilities

Every class in `src/pcode` that derives from
`pydantic_ai.capabilities.AbstractCapability`. "Static" means it is in the list
`create_coder` returns; "shared" means it is also in
`WorkspaceSubAgents.shared_capabilities`, so delegated runs get it too.
`tests/test_capability_map.py` fails when this table and the code drift apart.

| Class | File | Base class | Hooks overridden | Where it's attached | Why |
| --- | --- | --- | --- | --- | --- |
| `WorkspaceGuard` | `workspace.py` | `AbstractCapability` | `before_run`, `wrap_tool_execute` | Static, first in the list | Stop cleanly when the session's worktree was deleted underneath it, instead of retrying doomed tool calls. |
| `WorkspaceFileSystem` | `workspace_filesystem.py` | Harness `FileSystem` | `get_instructions`, `_toolset_type` | Base of `DisplayFileSystem` only | Workspace-relative paths without making the workspace a hard boundary. |
| `DisplayFileSystem` | `filesystem.py` | `WorkspaceFileSystem` | `_toolset_type` (uses `DisplayFileSystemToolset`) | Static, replaces `FileSystem` in `_adapt` | Emit a `FileChangeEvent` with before/after content so the UI can show diffs. Its toolset also lets `read_file` return an image, a stopgap for Harness's own (pydantic-ai#9596, unreleased as of 0.57); delete it when the pin moves past that. |
| `AutomaticRepoContext` | `repo_context.py` | Harness `RepoContext` | `for_run`, `before_run`, `get_instructions` | Static, replaces `RepoContext` in `_adapt` | Keep `AGENTS.md` loading but give each run its own repo inventory snapshot. |
| `CodingToolOutputLimits` | `tool_output_limits.py` | Harness `ToolOutputLimits` | `get_instructions`, `after_tool_execute` | Static, replaces `ToolOutputLimits` in `_adapt`; shared | Spill big tool output to a file, but never spill the job envelope the model needs to reach a running command, nor a file (an image) that serialized is only base64. |
| `JobShell` | `shell_tools.py` | Harness `Shell` | `_make_toolset` (uses `JobShellToolset`) | Static, replaces `Shell` in `_adapt` | Shell commands become named background jobs the model can wait on, read, and stop. |
| `ClaudeWorkspace` | `claude_sdk/workspace.py` | `AbstractCapability` | `before_model_request` | Static; shared as a fallback | Run `claude:` models' CLI in the agent's workspace, not pcode's process directory. |
| `IdentifiedPlanning` | `planning.py` | Harness `Planning` | `before_model_request`, `wrap_model_request`, `get_instructions`, `after_tool_execute` | Static | Replace Harness's planning guidance and `write_plan` description with text that names the user as the plan's reader and sets a trigger below "multi-step work"; show stable plan step ids so the model stops confusing them with row numbers; publish `PlanSnapshot` events. |
| `BackgroundDelegation` | `background_delegation.py` | Harness `BackgroundTools` | `wrap_tool_execute`, `wrap_node_run`, `wrap_run_event_stream`, `after_node_run`, `wrap_run` | Static, parent only | When the user steers mid-delegation, return the `delegate_task` call so the message reaches the model now; the child keeps running and its result is enqueued. Holds the run open, still streaming child events, until every detached child reports. |
| `DelegationReporting` | `delegation.py` | `AbstractCapability` | `wrap_tool_execute` | Static, parent only | Bind the parent's `delegate_task` call to its child's event stream so worker activity lands under the right tool call. |
| `ModelOutputLimits` | `output_limits.py` | `AbstractCapability` | `before_model_request` | Static; shared | Default `max_tokens` to the serving model's advertised maximum, per request. |
| `ImageGate` | `images.py` | `AbstractCapability` | `before_model_request` | Static; shared | Send each request only the images its model can see, and only the newest that fit a size budget. The rest become notes naming the image and why it is gone; history keeps every picture, so a `/model` switch neither fails nor loses one. |
| `MCPServers` | `mcp_notice.py` | `AbstractCapability` | `get_instructions`, `before_model_request` | Static | Tell the model which MCP servers are enabled, and again when that changes mid-session. |
| `CacheBustReporting` | `cache_warnings.py` | Harness `WarnOnCacheBusts` | `after_model_request` | Static when the `cache_notices` preference is on; shared | Surface prompt-cache misses as a `CacheBustEvent` for the UI. |
| `WorkspaceSubAgents` | `isolated_delegation.py` | Harness `SubAgents` | `get_toolset`, `get_instructions` | Static, built by `_delegation` | `delegate_task` with optional isolated git worktrees, plus integrate/discard tools. |
| `TurnLimits` | `agent.py` | `AbstractCapability` | `before_model_request`, `before_tool_execute` | Static and shared, only under a remote profile (`remote_profile.py`) | Charge each request and tool call, sub-agents' included, to the turn's budget; past it, raise `TurnLimitReached` so the turn ends saying why. |
| `ProviderCacheSettings` | `cache_settings.py` | `AbstractCapability` | `before_model_request` | Shared only | Sub-agents run with `model_settings=None`, so request prompt caching per request instead. |
| `Steering` | `steering.py` | `AbstractCapability` | `before_model_request` | Per-run; also side questions | Inject messages the user typed mid-turn between model requests, never mid-tool. |
| `AbandonedDelegations` | `background_delegation.py` | `AbstractCapability` | `before_model_request` | Per-run; main turns only | Tell the model that a detached delegation died with an interrupted or failed run, so it stops waiting for a result it was promised. |
| `FollowAgent` | `live.py` | `AbstractCapability` | `get_model` (per-step selector) | Per-run; not on `$MODEL`/`+EFFORT` turns | Send each request to the runtime's current agent's model, so `/model` mid-turn lands at the next request. `AgentRuntime._following` layers that agent's settings the same way. |
| `JobNotices` | `job_notices.py` | `AbstractCapability` | `before_model_request` | Per-run; isolated workers append their own | Tell the model a background job finished at its next request, so it never polls. |
| `RequestCheckpoint` | `retries.py` | `AbstractCapability` | `wrap_model_request` | Per-run (one per turn context) | Record the exact request boundary so a failed turn can retry without replaying finished tool work. |
| `TokenAccounting` | `token_accounting.py` | `AbstractCapability` | `after_model_request` | Per-run; shared with sub-agents each turn (`_persist_child_runs`); also side questions | Count each response's usage exactly once, where it happened. |
| `ContextTracking` | `compaction.py` | `AbstractCapability` | `get_ordering`, `before_model_request`, `after_model_request` | Per-run | Publish what each request carries for the footer, `/status`, and `/compact`. |
| `AutoCompaction` | `compaction.py` | `AbstractCapability` | `get_ordering`, `before_model_request` | Per-run, when autocompact is on and the turn uses the conversation's model | Compact history before a request that would overflow the window, including mid-turn. |
| `AsideGuard` | `aside_guard.py` | `AbstractCapability` | `wrap_tool_execute` | Per-run, side questions only | A side question shares the conversation's capabilities, so refuse the tools that would change its plan or start workers. |
| `Browser` | `extensions/browser.py` | `Capability` | `before_tool_execute` | Extension (defined inside `_capability`) | Launch Chrome lazily on the first browser tool call, for the parent and the browser sub-agent alike. |

Harness capabilities pcode uses unchanged (`LocalWorkspace`, `StepPersistence`,
code mode, strict tools, and whatever else `Coder` ships) are not listed. Two
bundled extensions, `web_research.py` and `session_history.py`, register plain
`WebSearch`/`WebFetch`/`Capability` instances rather than subclasses.

## Models, providers and toolsets

| Class | File | Base class | Why |
| --- | --- | --- | --- |
| `ClaudeModel` | `claude_sdk/model.py` | `AnthropicModel` | `claude:` models: requests go through a long-lived Claude Code CLI process instead of HTTP, reusing Pydantic AI's Anthropic message mapping. |
| `ClaudeProvider` | `claude_sdk/model.py` | `AnthropicProvider` | Names responses `claude`; its client is never called. |
| `ProxiedCodexProvider` | `llm_proxy.py` | `OpenAICodexProvider` | Codex through a proxy on a dedicated HTTP client, without touching process-wide HTTP settings. |
| `DisplayFileSystemToolset` | `filesystem.py` | Harness `FileSystemToolset` | Snapshots a file under Harness's write lock to build before/after diffs. |
| `JobShellToolset` | `shell_tools.py` | Harness `ShellToolset` | Replaces the persistent shell tool with `shell`, `wait_for_job`, `job_output`, `stop_job`, `list_jobs`. |
| `WorkspaceSubAgentToolset` | `isolated_delegation.py` | Harness `SubAgentToolset` | `delegate_task` in a throwaway worktree, plus `integrate_task`, `discard_task`, `list_task_worktrees`. |
| `WorkerRuntimeTools` | `agent.py` | `CombinedToolset` | Gives each worker run the delegating turn's runtime toolsets (live MCP), resolved once per run via `for_run`. |

## Smallest examples worth reading first

Each of these is one hook doing one job, and short enough to read in a minute:

- `src/pcode/steering.py` (16 lines): `before_model_request` appends queued user messages to the request about to go out.
- `src/pcode/retries.py` (25 lines): `wrap_model_request` snapshots the request so a failed turn retries from the right boundary.
- `src/pcode/output_limits.py` (41 lines): `before_model_request` fills in a model-specific default setting without overriding explicit ones.
- `src/pcode/workspace.py` (57 lines): `before_run` and `wrap_tool_execute` turn a deleted directory into one clear, final error.
- `src/pcode/planning.py` (71 lines): subclassing a Harness capability to change its instructions and emit a custom event.
- `src/pcode/extensions/web_research.py` (89 lines): an extension adding stock `WebSearch`/`WebFetch` capabilities, with native or local backends.
