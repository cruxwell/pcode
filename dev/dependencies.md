# Dependency reference guide

Use this map when changing terminal behavior or the agent runtime. Documentation
is retrieved on demand; it is not a version-matched local documentation bundle.

## Versions and sources

`pyproject.toml` defines allowed versions; `uv.lock` records resolved versions.
Check the installed environment before relying on an API. The versions below are
a snapshot, not a second set of pins: update them when dependencies change.

| Library (distribution) | Verified installed version | Official documentation | Upstream source |
| --- | --- | --- | --- |
| prompt_toolkit (`prompt-toolkit`) | 3.0.53 | [Docs](https://python-prompt-toolkit.readthedocs.io/en/stable/) | [python-prompt-toolkit](https://github.com/prompt-toolkit/python-prompt-toolkit) |
| Rich (`rich`) | 15.0.0 | [Docs](https://rich.readthedocs.io/en/stable/) | [rich](https://github.com/Textualize/rich) |
| Pydantic AI (`pydantic-ai-slim`) | 2.56.0 | [Docs](https://ai.pydantic.dev/) | [pydantic-ai](https://github.com/pydantic/pydantic-ai) (package: `pydantic_ai_slim/`) |
| Pydantic AI Harness (`pydantic-ai-harness`) | 0.56.0 (released with Pydantic AI 2.56.0) | [Docs](https://pydantic.dev/docs/ai/harness/) | [pydantic-ai](https://github.com/pydantic/pydantic-ai) (package: `src/pydantic_ai_harness/`) |
| Playwright (`playwright`, via the Harness `playwright` extra; Chromium downloaded on first `/browser` use) | 1.64.0 | [Docs](https://playwright.dev/python/) | [playwright-python](https://github.com/microsoft/playwright-python) |
| Claude Agent SDK (`claude-agent-sdk`, bundles the Claude Code CLI) | 0.2.167 (CLI 2.1.300) | [Docs](https://code.claude.com/docs/en/agent-sdk/python) | [claude-agent-sdk-python](https://github.com/anthropics/claude-agent-sdk-python) |

From the repository root, this read-only command prints installed versions and
package source locations without importing the agent runtime or loading credentials:

```sh
.venv/bin/python - <<'PY'
from importlib.metadata import distribution

for name, module in (
    ('prompt-toolkit', 'prompt_toolkit'),
    ('rich', 'rich'),
    ('pydantic-ai-slim', 'pydantic_ai'),
    ('pydantic-ai-harness', 'pydantic_ai_harness'),
):
    dist = distribution(name)
    print(f'{name}=={dist.version}: {dist.locate_file(module)}')
PY
```

This requires an existing project environment. Typical source locations are
`.venv/lib/pythonX.Y/site-packages/<module>/`; do not hard-code the Python minor
version. Installed packages contain implementation and docstrings, but may omit
upstream tests, examples, and documentation sources.

### Local Harness checkout

Harness upstream source is the one dependency worth reading in full, because the
installed wheel omits its docs and tests. Harness lives in the Pydantic AI
repository and ships with each Pydantic AI release, so `make harness-src` checks
that repository out under `tmp/pydantic-ai` (gitignored, blobless) at the tag of
the `pydantic-ai-slim` version locked in `uv.lock`:

```sh
make harness-src   # prints "tmp/pydantic-ai @ v<version>"
```

The target reads the version from `uv.lock`, so it cannot drift from the lock.
It clones when the directory is absent, fetches only when the tag is missing,
and leaves a **detached** HEAD at that tag — rerun it after upgrading, and never
commit work on top of it. Takes about half a minute cold. Harness's source is
`src/pydantic_ai_harness/pydantic_ai_harness/`, its tests `tests/harness/`.

`docs/harness/coder.md`, `docs/harness/repo-context.md`, `docs/harness/compaction.md`,
`docs/harness/subagents.md`, and `docs/harness/shell.md` cover the capabilities
pcode composes; `docs/workspace.md` covers `ctx.workspace`.
Read these instead of the website, which tracks `main` and can describe an
unreleased API. Installed `site-packages` still decides what actually runs: the
checkout is for docs, tests, and history, not a substitute for verifying the
installed source. Do not build or install pcode from it.

## Where to look for this project

- **Interactive UI:** `src/pcode/ui.py`, `src/pcode/commands.py`, and
  `src/pcode/app.py`. Start with [prompt_toolkit prompts](https://python-prompt-toolkit.readthedocs.io/en/stable/pages/asking_for_input.html)
  and its reference sections for Application, Layout, Renderer, Input, and Output.
  Local source entry points include `prompt_toolkit/shortcuts/prompt.py`,
  `prompt_toolkit/layout/`, and `prompt_toolkit/renderer.py`.
- **Permanent terminal output:** `src/pcode/ui.py` and `src/pcode/tool_display.py`.
  Start with [Rich Console](https://rich.readthedocs.io/en/stable/console.html),
  Markdown, Syntax, and Text; inspect `rich/console.py` as needed.
  Rich owns permanent output; prompt_toolkit owns the mutable prompt and activity panels.
  prompt_toolkit is pinned exactly because pcode uses its private API (the
  scrollback handoff, renderer, layout division, input parser). Most of those
  uses only read state, so a rename misrenders rather than raising. To upgrade,
  run `tests/test_prompt_toolkit_internals.py` first, then `make test-all`.
- **Agent creation and streaming:** `src/pcode/agent.py` and `src/pcode/live.py`.
  Start with [Pydantic AI streaming events](https://ai.pydantic.dev/agents/#streaming-all-events).
  Relevant installed source includes `pydantic_ai/agent/`, `messages.py`,
  `capabilities/`, and `models/openai_codex.py`.
- **Tools, planning, repository context, and persistence:** `src/pcode/agent.py`,
  `src/pcode/live.py`, `src/pcode/repo_context.py`, and `src/pcode/sessions.py`.
  Start with [Harness Coder](https://ai.pydantic.dev/harness/coder/), then inspect
  the installed `pydantic_ai_harness` implementation for the capabilities used.
  Verify Coder's actual tool composition, planning, and step-persistence APIs.

### File access and worker shell

`src/pcode/workspace_filesystem.py` sets pcode's path policy on Harness's own
`FileSystem`: `root_dir='/'` lifts the boundary, so absolute paths, parent
traversal and external symlinks work, while relative paths resolve from the
workspace's working directory and `list_files`/`grep` return paths relative to
it (external ones as `..` paths). Nothing is overridden but construction and the
instructions. `DisplayFileSystem` layers mutation evidence on top; compare it
with installed source on upgrades. Keep `tests/test_filesystem.py`, the
real-tool tests in `tests/test_live.py`, and the repository-context tests when
changing this integration.

Allow/deny/read-only patterns match the path relative to `/` (no leading slash),
so read-only patterns get a `**/` prefix to apply at any depth. File events carry
a `path` relative to `root_dir`, which is `/`.

The opt-in `sandbox` extension (`src/pcode/extensions/sandbox.py`, policy in
`src/pcode/sandbox.py`) puts write roots back in as a `before_tool_execute` hook
rather than in `_resolve_path`. It resolves `path` the way the tools do
(`sandbox.tool_target`: join onto the workspace and normalize as text, so
`link/..` collapses before any symlink is followed and `~` stays literal, then
realpath), so keep the two in step if resolution changes. Following `link`
before `..`, as `sandbox.real` does, checks a different file than the tool
writes. Code
mode's nested reads go through the same hook. The shell half sets
`jobs.COMMAND_SANDBOX` around each `shell` call, and `JobRegistry.launch` runs the
job *supervisor* under that argv prefix (`sandbox-exec -p <profile>` or `bwrap`).
Wrapping the supervisor rather than the command keeps `CommandStartedEvent`,
`list_jobs` and the transcript showing the model's command, and a stop still
signals the whole process group. In a Seatbelt profile the last matching rule
wins, which the generated profile relies on (allow roots, then re-deny the
guarded paths). Policy state that must survive `/reload` (`SESSION_GRANTS`)
lives in `pcode.sandbox`, because `/reload` re-imports the extension.

Forward `tools`, `content_hashes`, and `max_read_chars` when constructing the
display toolset. Omitting them silently restores legacy tools and hash-bearing
schemas, and removes Coder's read pagination budget. `max_retries` is dropped on
purpose, so the `tool_retries` preference governs file tools too. The `coder` extra supplies
ripgrep, but an installed `pcode` entry point does not activate its environment's
`bin` on PATH. `create_coder` appends that bin directory when `rg` is absent,
without changing existing executable precedence. Test outside `uv run` too.

The built-in worker is composed from the parent's core capabilities and extension
capabilities, including write tools and guardrails. It has a distinct stock `Shell`
with the same settings and environment filtering. Delegation is excluded to avoid
recursion; the child retains independent run state, a plan, and request/time limits.
Shared accounting/cache/output capabilities still come from Harness `SubAgents`.
Runtime MCP toolsets are bound per turn through a context-local dynamic toolset:
Harness's `inherit_tools` does not include per-run toolsets, while
`Agent.override(toolsets=...)` would replace capability tools as well. Specialized
extension delegates do not receive this binding. Preserve `tests/test_worker.py`,
`tests/test_worker_shell.py`, `tests/test_mcp.py`, and `tests/test_coder_integration.py`.
Persistent shell refers to process lifetime, not sticky `cd`: each call still
starts at the workspace.

`AutomaticRepoContext` bridges `FilesSearchedEvent` into upstream's traversal
handler because `list_files` and `grep` no longer emit `DirectoryListedEvent`.
Keep the workspace containment and per-run deduplication checks; searches outside
the workspace must not inject external instruction files.

### Repository instruction discovery

[Harness RepoContext](https://pydantic.dev/docs/ai/harness/repo-context/) supports
ancestor loading in installed 0.31.0, but `home_dir=None` scans only the workspace.
Verified in `repo_context/_loader.py`: the bound is inclusive; when it is not an
ancestor of the resolved workspace, the loader falls back to workspace-only.
`src/pcode/repo_context.py:create_repo_context` therefore supplies the resolved home
for workspaces beneath it, or the filesystem root elsewhere, for both the main
agent and worker. Harness handles ancestor-first ordering, both instruction
filenames, real-path/content deduplication, and per-run cache isolation. No custom
instruction scanner is needed. `repo_context_walk_up=off` instead supplies no
bound, retaining workspace-local instructions. `repo_context_nested` independently
selects `off` (default), `pointer`, or `contents` through Harness's
`nested_traversal`/`nested_inject` options. Both preferences are snapshotted at
agent creation, not reloaded mid-run; the asset inventory remains workspace-local.

Installed 0.31.0 detects traversal via filesystem `FileReadEvent` and
`DirectoryListedEvent` capability events, not the deprecated tool-name sniffing
options shown in some website examples. Notes are enqueued in the conversation
tail, leaving the startup prefix stable. `_loader.find_dir_context_file` selects
only the first filename for nested discovery (unlike the startup walk, which
loads both). The traversal hook checks only the accessed directory within the
workspace, not intervening parents; it can also surface workspace instructions
already loaded at startup. Its per-run seen-directory set is separate from the
startup loader's deduplication. Keep `tests/test_repo_context.py` covering the
boundary, symlink, deduplication, refresh, and all discovery-setting combinations
with real filesystem tools on both main and worker agents when upgrading.

### Delegation activity

`WorkspaceSubAgents` in `src/pcode/isolated_delegation.py` subclasses the pinned
Harness `SubAgents` and `SubAgentToolset`. New isolated workers require both effective
`worker_isolation=on` (default off) and `worktree=on`, checked at each delegation;
otherwise auto delegation keeps the shared workspace and job registry. Task recovery
and lifecycle protections remain available when isolation is disabled. It changes
the tool schema and constructs a per-call worker for isolated workspaces, but reuses
`_run_delegation` and `_settle` for model selection, budgets, lifecycle events, and
usage accounting.
These are private upstream interfaces: keep the isolated delegation, limits,
cache, and persistence tests when upgrading. A shared toolset must not mutate its
agent roster for a child; concurrent calls select independent worker instances.
Creation/finalization wait on per-parent locks in joined threads, so cancellation
cannot abandon a record update. Integration remains nonblocking on contention.

`src/pcode/delegation.py` bridges Harness 0.31.0's `SubAgents.event_stream_handler`
into the parent's event stream. The handler receives a **child** `RunContext`,
not the parent tool identity; `DelegationReporting.wrap_tool_execute` binds the
parent context with a `ContextVar`, reset in `finally`, so parallel children do
not share attribution. Only child tool boundaries and phase labels are forwarded,
never child text/thinking deltas. Inspect installed `subagents/_toolset.py` and
`subagents/_events.py` before changing this integration.

`live.py` consumes `DelegationStartEvent`/`DelegationEndEvent` by `tool_call_id`.
Use the structured delegation outcome: timeout/budget limits can return ordinary
successful tool strings. A max-calls refusal emits no lifecycle events; cancellation
and uncontained errors may omit the end event, so keep turn-end interruption cleanup.

Do not add `DelegationEndEvent.usage` to session totals. `SubAgent.usage_limits`
isolates a child's *request count* (so a long child cannot trip the parent's
limit, and a delegate with a set cap gets a steering message rather than a
raised limit), but its tokens
still arrive in the parent's `result.usage`, so adding them again doubles every
delegated token. Verified against the installed `_toolset.py` and a delegated run.
Sub-agents also receive only `shared_capabilities`, never the per-run capabilities
the parent passes to `run_stream_events`: cache settings and step persistence must
be installed there, and `SavedSession.recover()` must skip runs with a
`parent_run_id`. See [prompt caching and plan reminders](prompt-caching.md).
Child tool IDs are scoped by parent call ID, and persisted tool events retain
`parent_call_id` for replay. The panel pins active delegates within its existing
row budget; keep `tests/test_delegation_tmux.py` exercising real CPR and resize.
A sub-agent's plan stays in Harness's default per-run store, private to its run.
`IdentifiedPlanning` announces the whole plan as a `PlanSnapshot` capability event
after each planning call; the child stream handler forwards changes as
display-only `ChildPlan` events, which are never journaled. Harness's own
per-item plan events cannot replace it: `write_plan` emits nothing for a pure
reorder, so a plan rebuilt from them can show steps in the wrong order.

### MCP integration

`src/pcode/mcp.py` uses Pydantic AI 2.43.0's `MCPToolset` and FastMCP's
`StdioTransport`; `src/pcode/live.py` supplies only enabled toolsets per run.
Unless a server sets `"direct": true`, `build_toolset` marks its tools
`defer_loading=True` (via `defer_loading()`, inside the prefix) so Pydantic AI's
auto-injected `ToolSearch` reveals them through `search_tools`. `FunctionModel`
claims **every** native tool by default, including server-side tool search it
cannot run, so a test that expects the local `search_tools` fallback must pass
`profile=ModelProfile(supported_native_tools=frozenset())`; otherwise the tools
are withheld with no discovery surface at all. Use `mcp.mcp_transport()` rather
than walking `.wrapped` by hand, since the wrapper chain now varies per server.
`MCPState.enable()` is async: for OAuth it enters/exits the prefixed MCP toolset
before publishing it as enabled. `MCPToolset.__aenter__` initializes the remote
client and completes OAuth without an agent/model run. Reuse the same toolset
object afterward so its OAuth tokens survive connection teardown. Other transports
remain lazy. The app runs activation separately from model turns, with a queue
gate and Ctrl+C/quit cleanup; keep the real-prompt tests in `tests/test_mcp_enable.py`.
Consult [Pydantic AI MCP](https://ai.pydantic.dev/mcp/client/) and
[FastMCP client transports](https://gofastmcp.com/clients/transports), then inspect
installed `pydantic_ai/mcp.py` and `fastmcp/client/transports/stdio.py`.
The `[mcp]` extra currently resolves `fastmcp-slim` 4.0.4 and `mcp` 2.2.0.
OAuth is already supported by `MCPToolset(auth="oauth")` in installed 2.43.0;
FastMCP resolves it to `fastmcp.client.auth.OAuth`. Consult
[FastMCP OAuth](https://gofastmcp.com/clients/auth/oauth) and inspect installed
`fastmcp/client/auth/oauth.py`, `fastmcp/client/transports/http.py`, and
`mcp/client/auth/oauth2.py`. In 4.0.4, default storage is **in-memory**, not disk;
the helper manages browser authorization, PKCE, callback validation, and refresh.
Do not assume older FastMCP documentation about persistent token caches applies.
Pcode passes its own file-backed `CredentialStore` (see `mcp_oauth.py`) instead. The slim install omits `websockets`, but FastMCP 4.0.4's
callback server explicitly selects Uvicorn's `websockets-sansio` implementation.
Pcode adds `websockets>=15.0.1,<17` (verified with 16.1.1) so browser callbacks
actually start; mocked OAuth exchange tests alone would miss this dependency.
`src/pcode/mcp_oauth.py` supplies a narrow `OAuth` subclass to own the callback
listener. FastMCP 4.0.4 probes and closes its port at construction, then binds it
much later; a collision causes Uvicorn 0.53.0 to raise `SystemExit(3)` in a child
task. Pcode reserves an IPv4 loopback socket before registration and passes that
same socket to `Server.serve(sockets=...)`. The embedded server does not capture
process signals and converts startup `SystemExit` inside the child task into a
normal error. This avoids both the port race and interference with Ctrl+C.
If a previously registered redirect port is occupied, pcode fails normally with
instructions to disable/re-enable rather than silently changing a registered URI.
The adapter uses FastMCP's `token_storage_adapter`, the SDK's
`context.client_metadata.redirect_uris`, and Uvicorn's `capture_signals` hook;
recheck these installed-source APIs on upgrades. A pre-registered `client_id`
(FastMCP's static client) never pins the callback port; only a dynamic
registration does. The module also replaces
`mcp.client.auth.oauth2.validate_metadata_issuer` so a root issuer matches with
or without its trailing slash (the SDK's own `issuers_match`). `mcp` 2.2.0
compares strictly when protected resource metadata names the server, and
Google's MCP servers name `https://accounts.google.com/` while Google's metadata
says `https://accounts.google.com`. Drop the patch once upstream relaxes that
check; the Google-shaped tests in `tests/test_mcp_oauth.py` fail if the SDK
renames the function out from under it. OAuth protocol handling, PKCE,
state validation, token exchange, and refresh remain in FastMCP/the MCP SDK.
Keep mocked-provider tests, real loopback success/cancellation tests, deliberate
port-collision tests, and the full MCP-client startup-failure subprocess test in
`tests/test_mcp_oauth.py`. Tests must not open the real browser, contact a real
service, or read real credential stores.
`mcp` 2.2.0's streamable HTTP client turns every failed response except a 404
into a bare `MCPError("Server returned an error response")`, dropping the status.
Streamable HTTP servers therefore get a `StreamableHttpTransport` subclass that
extends FastMCP's private `_capture_session_id` response hook (registered on
every client it builds, including the test factories) to record the endpoint's
last HTTP error, and every server gets an innermost wrapper that turns a failed
connection into `MCPConnectError`: the server name, the status, and a fixed hint.
SSE URLs keep Pydantic AI's own transport and report no status. Recheck the hook
name on FastMCP upgrades; `tests/test_mcp_diagnostics.py` fails if it stops firing.
FastMCP defaults `StdioTransport.keep_alive` to `True`: pcode explicitly sets it
to `False` so turn cleanup closes subprocesses. Keep the real-stdio tests in
`tests/test_mcp.py` for success, failure, cancellation, and reconnection. Filtering
schemas alone is insufficient to prevent disabled servers from connecting;
disabled servers must not enter the agent's toolset collection at all.

Deferral is only half a contract: a model whose profile claims `ToolSearchTool`
but no `tool_deferral_mode` sends `tool_search` on the wire and withholds the
deferred schemas instead of declaring them, which OpenAI Responses rejects with
`400 tools.tool_search requires at least one deferred tool` — for every request
of that session, not just the one. Before AI 2.49.0 the Codex provider took
the first from `openai_model_profile` and never got the second, which only
`OpenAIProvider.model_profile` sets; since
[pydantic-ai#8693](https://github.com/pydantic/pydantic-ai/pull/8693),
`OpenAICodexProvider.model_profile` layers that first-party profile under the
Codex dialect, so `agent.codex_model` overrides only cache breakpoints.
`tests/test_tool_search_profiles.py` checks the pairing for every model pcode
can build, so a provider that drifts apart again fails there first.
`AgentRuntime.stream` also treats such a 400 as a request shape rather than a
history: `MCPState.undefer()` rebuilds the wrappers around the *same*
`MCPToolset` (keeping its connection and OAuth tokens) without the
`DeferredLoadingToolset` layer and the turn is sent again. Both are pinned by
`tests/test_codex_profile.py` and `tests/test_mcp.py`.

### Codex sign-in

`src/pcode/codex_login.py` uses the installed Pydantic AI public
`OpenAICodexOAuthFlow` and `OpenAICodexCredentialSource` APIs. Pydantic owns PKCE,
token exchange, the localhost:1455 callback, and refresh; pcode owns browser
opening and an atomic owner-only credential store. No Codex CLI or Python Codex
SDK is required for this flow. Provider `load()` / `save()` callbacks persist
rotated tokens. The CLI credential loader remains the fallback when pcode's
file is absent, including when `PCODE_LLM_PROXY` is configured. The stores stay
separate and pcode never writes or deletes the CLI's file.

## Verification workflow and known pitfalls

1. Check the installed version against `uv.lock`.
2. Use official docs for concepts, but verify signatures and behavior against
   installed source. Prefer release-matched docs where available; `stable`,
   `latest`, and upstream `main` are not guarantees of compatibility.
3. For upstream examples, tests, or internals, browse the matching release tag
   or commit. For Harness, run `make harness-src` (see "Local Harness
   checkout"); for other dependencies, keep any checkout outside this project's
   normal source tree and record its revision. Do not clone or install
   dependencies merely to read an API already available locally.
4. Validate changes with this repository's relevant regression tests (see
   [development.md](development.md#validate)). Documentation alone cannot establish terminal behavior.

Specific traps already encountered here:

- Harness's latest website can describe an unreleased Coder API, extras, or a
  newer tool composition than the pinned snapshot. Follow the installed source,
  not a website example copied without verification.
- A PTY with `PROMPT_TOOLKIT_NO_CPR=1` does not exercise real prompt height:
  cursor-position reports can stretch the layout into the remaining pane. Keep
  the real-tmux height regression tests, not just PTY startup/exit checks.
- `src/pcode/repo_context.py` uses a private Harness inventory API. Recheck it
  against installed source and the repository-context tests on upgrades.
- `src/pcode/llm_proxy.py` uses Pydantic AI's private provider ownership hooks
  (`_own_http_client`, `_http_client_factory`, and Codex auth/client fields) so
  injected proxy clients close and reopen with the agent. Verified with 2.43.0
  and httpx2 2.13.0; rerun `tests/test_llm_proxy.py` on upgrades. Codex requires
  httpx2, not legacy httpx. Static models require entering the agent context;
  `run_stream_events()` alone does not manage their client lifetime.

Inspect library source and package metadata, not credential stores, `.env`
files, private keys, or token files. API discovery should not require live model
requests or authentication.

### Context indicator

`src/pcode/model_metadata.py` resolves context limits; `context_usage.py` and
`compaction.py` share its synchronous memory-only lookup. Refreshes run outside rendering. Preserve input, output, default context,
opt-in maximum, source, and fetch timestamp separately. Unknown deployments must
not inherit a familiar model name's direct-API limit. An explicit
`PCODE_CONTEXT_WINDOW` applies to every consumer and is capped by known
input/maximum limits.

Harness capabilities that take a `max_*_fraction` resolve the window from
genai-prices instead. It gives `openai-codex:` ids the direct API's window by
model name (1.05M for a model Codex serves at 272k) and silently assumes 200k
for ids it does not know. Pass them pcode's window (`context_window=`), or a
session is told the wrong fullness and compacts or cuts work short at the wrong
point.

- Public catalog: [Models.dev JSON](https://models.dev/api.json) and
  [schema](https://github.com/anomalyco/models.dev/blob/dev/README.md).
  Normalize only limits and provider routes, not costs. Cache atomically under
  `XDG_CACHE_HOME` for 24 hours; retain stale data on failure.
- Anthropic: [Models API](https://platform.claude.com/docs/en/api/models),
  `GET /v1/models/{model}`, fields `max_input_tokens` and `max_tokens` (nullable).
  Use the actual SDK client to preserve base URL, auth and credential rotation.
  Subscription OAuth needs its existing beta headers and must not borrow
  direct-API fallback limits. This does not imply official support for third-party subscription use.
- Codex: [first-party models endpoint implementation](https://github.com/openai/codex/blob/f1affbac/codex-rs/codex-api/src/endpoint/models.rs),
  `GET /models?client_version=…` relative to the provider's Codex base URL.
  The live backend filters model availability by version: 0.99.0 omitted
  `gpt-6-astra`, whereas 0.154.0 returned its 272k default / 872k maximum.
  Use verified fallback 0.154.0 or a newer semantic `client_version` from
  `$CODEX_HOME/models_cache.json` (default `~/.codex/`); read only the version hint,
  never reuse cached limits/availability from a potentially different account.
  This is a versioned backend protocol, not a stable public API. Reverify on
  upgrades. Use `context_window` as the default and `max_context_window` only for
  explicit overrides. Never map `openai-codex` to `openai`.
- Verified Pydantic AI 2.43.0 (project) and 2.44.0 (installed tool) provide `Model.provider`, provider context-manager
  ownership, and `provider.client`. Codex's httpx2 auth handles credential loading,
  refresh, and one-shot 401 replay; use that same SDK client, including its proxy.
  Verified `AsyncOpenAI.get` / `AsyncAnthropic.get` accept `cast_to=dict[str, Any]`
  and per-request `timeout`, `max_retries`, headers and query parameters. Bare
  `cast_to=dict` fails in installed Anthropic 1.6.0. Do not clone a custom SDK
  client with `with_options`, which requires constructor arguments it cannot infer.

Native metadata is kept in memory per exact model instance, not in a shared
account cache. Three-second fetch deadlines and one-minute failure backoff prevent
metadata from breaking chat; cancellation must still propagate. Adapter tests use
synthetic credentials/MockTransport, not live authenticated requests. The Astra version-filter diagnosis was
  additionally verified with read-only live catalog requests (no model inference).
Pydantic AI `RequestUsage.input_tokens` includes cache read/write tokens already;
show only the latest response's input usage, never cumulative billing usage.


### Context compaction

`src/pcode/compaction.py` uses installed Harness 0.31.0's `SummarizingCompaction`
and `compact_now(strategy, messages, model=..., focus=..., usage=...)`. Consult
[Harness compaction](https://pydantic.dev/docs/ai/harness/compaction/) and inspect
`pydantic_ai_harness/compaction/_manual.py`, `_summarizing_compaction.py`, and
`_shared.py` in the installed environment. Pcode isolates private token/cutoff helpers
and `drain_summary_events` here; verify these on upgrades. Streaming the dedicated,
tool-free summary request is required for streaming-only subscription endpoints.
Use the actual resolved model object to retain custom auth/proxy behavior.

Installed Coder includes `ClearToolResults(max_fraction=0.7)`; pcode removes it to
avoid discarding evidence before summarization. The installed summarizer defaults
to 500 characters per tool result, which pcode explicitly raises to 16,000. Its
`keep_tokens` cutoff can retain an oversized final tool batch, so pcode detects that
case and summarizes the whole settled history with `keep_messages=0` instead.

Manual checkpoints serialize `ModelMessage`s into a private, fsynced journal event
that creates/selects one immutable conversation-tree node. Automatic compaction
uses the public `ContinuableSnapshot` / `StepStore.save_snapshot` API before the
next model request; subsequent Harness `StepPersistence` snapshots supersede it.
Never use synthetic agent runs to install summaries or overwrite original snapshots.
Keep tests for immediate restart, branch isolation, failure/cancellation, safe tool
pairs, mid-tool-loop compaction, and stale usage anchors after rewriting history.

### Tool output limits

`tool_output_limits.py` composes the pinned Harness `ToolOutputLimits`, `Band`,
`Spill`, `Truncate`, `LocalFileStore`, and `indented_json` APIs. Coder already
includes a private `ToolOutputLimits` subclass with a 64k truncation band: replace
it by type rather than append another limiter, or data can be lost before spilling.
`SubAgents.shared_capabilities` receives a separate instance with the same settings
and store. Keep retrieval registered even with no bands, since resumed histories
can contain older handles. Spills are separate from the session journal and survive
`--no-save`; tests must isolate `XDG_STATE_HOME` as well as config/cache directories.

The pinned `read_tool_result` pages by lines, then caps the body at 50k characters.
A single longer line cannot be recovered with `offset` or `from_end`; the coding
adapter supplies the stable store path in instructions for shell-based character
slicing. `indented_json` makes structured returns pageable but does not split long
string fields. Verify this behavior against the installed source on upgrades.

`CodingToolOutputLimits` reduces only the persistent shell's body, leaving its
PID/log/status footer intact even for head truncation and small budgets, and
marks changed bodies with `REDUCED_SHELL_OUTPUT`.
Keep the real-shell parent/worker tests, long-line recovery, serialized-history
readback, and spill-failure fallback tests in `tests/test_tool_output_limits.py`.

### Images (verified Pydantic AI 2.56.0, Harness 0.56.0)

Pydantic AI carries an image as `BinaryContent`, an `ImageUrl` or an
`UploadedFile`, in a `UserPromptPart`'s content or in a tool result. A tool
returns one beside text (`[header, image]`) or alone. Only the top level counts:
`ToolReturnPart.files` and every adapter look no deeper. Where an adapter puts a
tool-result image varies and needs nothing from pcode: Anthropic, Bedrock (images
only, by `bedrock_supported_media_kinds_in_tool_returns`) and OpenAI Responses
put it in the tool result; Gemini does for the MIME types its profile lists and
otherwise on a following user turn; OpenAI Chat always moves it to a user message
after the tool results. A reloaded history comes back as `BinaryImage`, which
compares unequal to the `BinaryContent` it was saved from, so `read_file`
returns `BinaryImage` up front.

Core has no flag for whether a model takes images, only `supports_image_output`
and `supports_audio_input`, and an image sent to one that cannot (any Groq
Llama, a text-only DeepSeek) fails the whole request. Upstream tracks it:
pydantic-ai#8453 (`supports_image_input` and siblings), #10472 and PR #10476 (a
file a tool returned becomes a note per request when the model cannot take it).
pcode does not wait. `ImageGate` asks the catalog, whose `ModelLimits.inputs`
comes from Models.dev `modalities.input` (listed for all 8,831 models in the feed
checked 2026-10-17, 5,396 of them with `image`), Codex's `input_modalities` and
Anthropic's `capabilities.image_input.supported`. When the profile flags ship,
let `image_support` read them first and keep the catalog for what a profile does
not say.

`read_file`: Harness 0.56 and 0.57 answer every file the null-byte check calls
binary with `[Binary file: N bytes…]`. pydantic-ai#9596 (merged 2026-10-16, after
0.57.0) returns a PNG, JPEG, GIF, WebP or PDF up to 5 MB as `[header,
BinaryContent]`, sniffing the bytes. `DisplayFileSystemToolset._viewed` fills the
gap and only acts on a `str`, so it goes quiet by itself once the pin includes
#9596. Then delete `_viewed`, `BINARY_NOTE` and `IMAGE_SUFFIXES`, keep
`ImageGate`, and decide about PDFs, which that release also returns and the gate
does not cover. The size cap below must survive that change, as an
`after_tool_execute` check if Harness's result no longer passes through
`_read_file`.

Traps found while building it:

- `ToolOutputLimits` measures a result by serializing it, and only raw `bytes`
  count as binary. A `[text, BinaryContent]` list was JSON with base64, spilled
  to a file past 10,000 characters and replaced by a preview of the encoding.
  `CodingToolOutputLimits` returns any result holding a file untouched; the
  spill test in `tests/test_read_file_images.py` fails without it.
- `RequestCheckpoint` is innermost and saves the request as sent, which is after
  `ImageGate` has swapped images for notes. Saved as is, one failed turn would
  lose those images for good. The gate marks each copy it makes with the message
  it came from (an attribute, never serialized) and the checkpoint saves
  `as_in_history(...)`. Harness's step snapshots read the run's own history and
  need nothing.
- Anthropic's 5 MB image limit is on the base64 (5 MiB), so the raw cap is 3/4
  of it; Harness's #9596 caps at 5,000,000 raw bytes, which lets a 4 MB PNG
  through to a 400 that every later request repeats. Keep pcode's cap when the
  stopgap goes. Anthropic also refuses an image over 8000 px a side; PNG and GIF
  headers say, JPEG and WebP are not checked.
- The budget leaves the oldest images out `IMAGE_STEP` at a time: moving the line
  one image per new image would end the prompt cache at that image every request.
- Harness compaction names a file by identifier and drops prompt images from the
  summary request's text, so summarizing needs no gate. The kept tail does carry
  its images, and the gate's budget applies to it.
- The terminal imports `tool_display`, and `tests/test_startup.py` forbids
  `pydantic_ai` on that path: import `BinaryContent` inside the function.
- A `claude:` alias (`claude:opus`) is in no catalog, so the provider prefix, not
  the name, says it sees.

Prompt images (`pcode.prompt_images`) keep the prompt a string end to end: the
queue, steering, `/resend`, the conversation tree, history recall and the host
protocol all carry text, and `AgentRuntime._stream` and `_consume_steering`
turn a prompt into `[text, label, image, …]` as it is sent. Steering that
reaches `RunContext.enqueue` (background delegation) is flattened, since
`enqueue` takes content items, not lists. Terminals cannot paste an image:
bracketed paste carries text only. Researched 2026-10-17, the options were:

- A dropped file arrives as its path, shell-escaped (iTerm2, Terminal.app) or
  quoted, so path detection covers drag and drop with no terminal support.
- Reading the OS clipboard from the process: `pngpaste` or AppleScript's
  `«class PNGf»` on macOS (its output is UTF-8, `«data PNGf…»`, not ASCII),
  `wl-paste` or `xclip` on Linux. Claude Code and Codex do this on Ctrl+V. It
  reads the wrong machine's clipboard over ssh, so pcode refuses there.
- kitty's OSC 5522 clipboard protocol carries images in band, ssh included,
  but only kitty ships it (Ghostty is adding it) and kitty asks permission on
  every read unless `clipboard_control` allows it. Worth adding when more
  terminals ship it.
- kitty and Ghostty are reported (anomalyco/opencode#25806, not verified here)
  to send an empty bracketed paste for an image-only clipboard, which pcode
  takes as `/paste-image`. Ctrl+V runs `/paste-image` by default, as in
  Claude Code and Codex; `/show-edits` moved to Ctrl+Z. In raw mode Ctrl+Z
  reaches the prompt as a key (prompt_toolkit's default inserts a literal ^Z),
  and pressing it in tmux toggled edits without stopping pcode.

### Reasoning effort

`preferences.apply_effort` uses `openai_reasoning_effort` for OpenAI/Codex and
`anthropic_effort` for Anthropic and Claude Code.
Verified Pydantic AI 2.50.0's `AnthropicModelSettings.anthropic_effort` and
`AnthropicModel._build_output_config` send `output_config.effort` without changing
thinking settings. `_build_output_config` consults `anthropic_supports_effort`
only when deriving effort from unified thinking: an explicitly set
`anthropic_effort` is forwarded even to a model whose profile reports no effort
support. That the provider then rejects it is inferred from the profile flag and
Anthropic's own documented model list, not observed against the live API. So
`preferences.effort_setting` checks the flag itself and reports no effort
control for those models rather than sending the parameter. The profile's
`anthropic_supports_xhigh_effort` selects native `xhigh`; otherwise pcode maps
its top level to `max`. Support for effort and its highest levels varies by
model; do not infer support from the route alone.
See [Anthropic effort](https://platform.claude.com/docs/en/build-with-claude/effort).

### Anthropic prompt caching

See [prompt caching and plan reminders](prompt-caching.md) for the upstream design
history, why the durable-user-prompt anchor was insufficient for long tool loops,
and the former append-on-change fix. Plan reminders are now disabled by default.
`make cache-report` reads the
provider's own per-request verdict out of saved sessions; run it after changing
anything that touches message history.

Verified against Pydantic AI 2.45.0 and saved-session usage records: the Anthropic
adapter adds no `cache_control` of its own. Without explicit settings an Anthropic
conversation re-reads its whole prefix at full price on every request. `agent.model_settings`
therefore sets `anthropic_cache` (the server-side automatic breakpoint, which moves
forward as history grows) plus `anthropic_cache_instructions` and
`anthropic_cache_tool_definitions`. Capturing real request bodies is the only proof
that breakpoints reach the wire; `tests/test_prompt_cache.py` asserts top-level
`cache_control`, the last tool, and the last system block.

Harness's upstream `Planning` appends an ephemeral reminder and anchors its
`CachePoint` on the last durable user prompt, not the latest tool result. The next
request removes the previous reminder, invalidating the automatic cache entry
covering it. Saved usage showed reads stuck at the old user-prompt boundary while
the growing tool-loop tail was repeatedly written. Checking only that cache
controls reach the first request does not catch this; test a multi-request loop.

For every provider, pcode's `IdentifiedPlanning` disables reminder injection by
default. Planning tool results report changes, and `read_plan` retrieves the stored
state; UI snapshots are independent of model reminders. Existing reminders in
saved history are left untouched. pcode adds no near-limit warnings (Harness's
`Coder` dropped `WarnNearLimits` in 0.56; auto-compaction runs before the warning's
90% threshold, and its replace-in-place rewrite busts the cache). Reminders pcode does
send (`append_reminder`, used by the MCP notice) use `before_model_request`, whose
messages Pydantic AI persists, not the ephemeral `wrap_model_request` boundary.
Reminder deduplication compares
the text of the last reminder in the current history, so saved resume, retry, and
branch selection do not depend on process-local state. Do not store the dedup key
in `ModelRequest.metadata`: Pydantic AI's history normalization merges consecutive
requests and keeps only its reserved `__pydantic_ai__` namespace, so an
application marker silently vanishes on resume and every turn re-appends its
reminder. Anthropic and native Codex
wire-prefix regressions live in `tests/test_reminders.py` and
`tests/test_planning_cache.py`.

### Cache-bust warnings

`cache_warnings.py` subclasses the pinned Harness `WarnOnCacheBusts` and converts
its `CacheBustWarning` into a capability event. Import from
`pydantic_ai_harness.warn_on_cache_busts`, not the root used by the docs' first
example (the pinned package does not re-export it). The inherited `for_run`
uses `dataclasses.replace`, preserving the adapter while resetting detector state.

The pinned `after_model_request` coroutine never suspends. Its narrow
`warnings.catch_warnings(record=True)` scope ends before `ctx.emit`, which can
suspend; do not expand capture across model/tool execution. Recheck this on
upgrades, especially for Python versions where warning filters are process-global.
Explicit ignore/error filters still apply, and unrelated warnings are re-emitted.
Only the warning's first paragraph is shown, excluding its Python suppression
example. Detection, per-model keys, TTL hint, and collapse latch stay upstream.

The main Coder and `SubAgents.shared_capabilities` install the adapter. Child
warnings are forwarded by `stream_child_activity` without forwarding child prose.
`CacheBust` presentation events are journaled and replayed as literal warnings,
not inserted into model history. Tests in `tests/test_cache_warnings.py` exercise
real streamed cache usage, isolated runs, delegation, warning filters, terminal
handoff ordering, and saved-session/redraw replay. Monitoring is per agent run,
not conversation-wide; no cache history is restored from saved sessions.

### Cache-collapse fingerprints

`cache_diagnostics.py` answers the question the upstream warning cannot: the
provider reports only token counts, which conflate *the prefix was rewritten* with
*the cache expired*. Both produce an identical collapse, and upstream deliberately
does not attribute a cause. Every request is therefore fingerprinted into a bounded
per-run window, and `divergence()` compares the collapsing request with the healthy
one before it. `Prefix intact` (all earlier messages byte-identical, the rest
appended) rules out mutation and points at TTL or breakpoint placement; otherwise
the first moved message index is named. Instructions, tool definitions and cache
settings are checked first because they sit ahead of every message, so a change
there would otherwise be misattributed to the first message that follows it.

Fingerprints are digests, sizes, part kinds and breakpoint positions — never prompt
text. Prompts carry file contents and command output, so a debug dump of them would
leak precisely what `diagnostics.redact` exists to prevent. The dump is written only
when a collapse fires, so the healthy path costs one hash per request.

Two failure modes are load-bearing and covered by tests: the step counter is
independent of the bounded window (the window's length stops growing, which would
repeat step numbers for exactly the long runs a collapse shows up in), and dump
filenames de-duplicate (two runs in one process can collapse in the same second at
the same step). A fingerprinting failure clears the window and degrades to the
plain upstream warning rather than ending the run, since part shapes vary by
provider and capability. `PCODE_CACHE_DIAGNOSTICS` disables (`off`) or redirects
the dumps; tests must isolate `XDG_STATE_HOME`.

### Claude Agent SDK provider (verified 0.2.160, CLI 2.1.283)

`claude:` models (`src/pcode/claude_sdk/`) keep one `ClaudeSDKClient` per
conversation and park pcode's tool calls in an in-process MCP server; see
[Anthropic provider options](anthropic-providers.md#what-shipped). Each item below
was verified live against the bundled CLI, and most of them are traps:

- The CLI puts the model's tool_use id in the `tools/call` request's `_meta` as
  `claudecode/toolUseId`. `create_sdk_mcp_server` hides `_meta` from handlers, so
  pcode builds its own `mcp.server.Server` with mcp 2's constructor callbacks.
  Moving to mcp 1 would need the decorator API instead.
- The handler is called when its tool_use block ends, *before* `message_stop`,
  and a handler parked for minutes is fine (`MCP_TOOL_TIMEOUT` is raised anyway).
  Results are keyed by id, so the call order does not matter.
- Closing a process while a handler is parked does not stop the turn. The CLI
  records the cancelled calls as errors and makes more billed requests while it
  shuts down, so `ClaudeSession.close` calls `interrupt()` first. The SDK's own
  atexit hook only sends SIGTERM, so pcode's exits `await claude_sdk.shutdown()`
  (`app.py` for `run` and `run_print`, and the host's teardown).
- The CLI makes requests pcode never asked for inside one turn: a nudge after a
  thinking-only reply, output-limit recovery, and a retry of an unparseable
  tool_use (the bundled CLI's strings show all three). None of these can answer a
  pcode request. The pump drops any message that begins while pcode is not
  expecting one and retires the process. `answer()` also waits for the CLI to have
  parked one of the open calls, because an unparseable or refused call is never
  parked. Waiting for one call rather than all of them matters: the CLI may call
  tools one at a time. For the same reason, a result counts as pcode's only once a
  handler has returned it. A later call the CLI refuses after pcode has sent every
  result fails that request, and the runtime's transient retry forks. A live
  request that finds its process gone this way forks straight away.
- A fork whose transcript or message is gone fails in `connect()` ("No
  conversation found with session ID" / "No message found with message.uuid"),
  before any request is made. pcode then forgets that session and replays instead.
- The system prompt goes in a 0600 temp file (`system_prompt={"type": "file"}`),
  removed with the process. On argv it would be visible to other local users and
  would hit Linux's 128 KiB per-argument limit.
- The SDK buffers only 100 parsed messages and every token delta is one, so each
  session drains its stream continuously (`_pump`).
- Without a `stderr` callback the child inherits pcode's stderr and writes onto the
  terminal UI.
- The child inherits the environment, and an empty value counts as unset.
  `CLI_ENV` blanks pcode's Anthropic key, token and base URL. A fake key left in
  the parent was verified to be ignored.
- On a subscription login the CLI writes 1-hour cache entries unless told
  otherwise. `CLI_ENV` sets `CLAUDE_CODE_PROMPT_CACHE_TTL=5m` (verified on
  2026-09-27: the transcript's `cache_creation` shows only
  `ephemeral_5m_input_tokens`). An upgrade that drops or renames the variable
  shows up there as `ephemeral_1h_input_tokens` again; see
  [anthropic-providers](anthropic-providers.md#what-shipped) for why 5 minutes.
- A user message written while the CLI waits on tool results is queued. It joins
  the *same* request as the results, wrapped in a `<system-reminder>` that says the
  user sent it mid-turn. Writing it before releasing the handlers makes this
  deterministic, because both travel over stdin in order. A model can distrust an
  odd-looking instruction delivered this way (Haiku ignored one as a possible
  injection). This path is for user input; pcode no longer injects plan reminders
  by default.
- `--thinking-display summarized` works without `--thinking`. Without it, thinking
  blocks arrive empty, carrying only a signature.
- `resume` + `fork_session` + `resume_session_at=<assistant uuid>` truncates the
  transcript there. A following user message may start with `tool_result` blocks
  for that message's tool calls. Transcripts are keyed by the process `cwd`, so
  the index records it; `ClaudeWorkspace` sets it to the agent's workspace, which
  in worktree mode is not pcode's own directory.
- The CLI attaches the environment (working directory, shell, OS), the date, the
  model identity and the account's email to the first request, and a token-budget
  reminder to every request. With `setting_sources=[]` nothing loads from
  `CLAUDE.md` or settings.
- `CLAUDE_CODE_MAX_OUTPUT_TOKENS` sets the request's `max_tokens`, clamped to the
  model's upper limit, and the CLI lowers a thinking budget to fit under it. pcode
  sends its own ceiling there, but not the 16k offline fallback: for a model with
  no known limits the CLI's default is higher.
- `claude-agent-sdk` is the optional `claude` extra, and the dev group pulls in
  `pcode[claude]` so the tests always have it. Code outside `claude_sdk/` must
  not import the SDK: `models.claude_sdk_installed()` gates the picker, and
  `claude_model()` and `/login claude` report the missing extra.
- `CLAUDE_CODE_MAX_RETRIES=0` makes an API error end the turn with an
  `AssistantMessage.error` kind and `ResultMessage.api_error_status` (the result's
  subtype is still `success`, with `is_error` set). pcode raises those as
  `ClaudeHTTPError`, leaving retries to the runtime. A request that got no HTTP
  response at all (connection reset, refused, or `API_TIMEOUT_MS` passed) is also
  a `server_error`, but with no status, whereas a real 500 or 529 carries one, so
  pcode raises `ClaudeConnectionError` for it instead. Guessing a 500 there would
  make the one failure pcode's other providers retry fail the turn outright.
  That, and a process that dies mid-request (`ClaudeProcessError`), are what
  `diagnostics.transient` retries, and the retry forks.

`tests/test_claude_sdk.py` drives the provider through a scripted client that
calls pcode's real MCP server the way the CLI does.

### Codex thinking streaming

Verified Pydantic AI 2.43.0's `OpenAICodexModel` inherits the Responses model's
`openai_reasoning_summary` setting. `_build_reasoning` serializes `"detailed"` as
`reasoning.summary`; without it, reasoning effort alone does not request visible
summary text. Pcode requests summaries for Codex independently of `show_thinking`
so the scrollback view can be enabled mid-turn. This requests provider-exposed
summaries, not raw internal reasoning, and does not change effort. Other routes
are unchanged. See the setting's installed-source documentation in
`pydantic_ai/models/openai.py` and
[OpenAI reasoning summaries](https://platform.openai.com/docs/guides/reasoning#reasoning-summaries).
`tests/test_codex_profile.py` checks the serialized request and preservation across
effort changes; event persistence and terminal visibility are covered separately.

### Anthropic thinking requests

For direct `anthropic:` routes (API-key and OAuth authentication), `show_thinking=on`
now also opts into thinking generation on the next turn. `preferences.apply_thinking`
uses the installed model profile's `anthropic_supports_adaptive_thinking` flag:
adaptive models receive `anthropic_thinking={"type": "adaptive", "display": "summarized"}`;
older models receive `{"type": "enabled", "budget_tokens": 2048, "display": "summarized"}`,
with the budget below the adapter's default
4096 output-token limit. This requires a thinking-capable model. Adaptive models
can choose not to think on a particular response. Enabling thinking can increase
latency and token usage; this does not modify the separately selected effort.

Verified against Pydantic AI 2.43.0's `AnthropicModel.prepare_request`,
`_translate_thinking`, and `_messages_create`. The profile check is important:
newer models reject budgeted thinking. Before login, use `anthropic_model_profile`
without forcing credential resolution. Settings dictionaries are replaced, not
mutated, so in-flight requests are unaffected; off removes the opt-in and restores
the provider default. Startup, model switching, session resume, `/show-thinking`,
and Ctrl+T apply the same policy.

`tests/test_anthropic_thinking.py` checks serialized adaptive/budgeted requests,
real Anthropic SSE decoding into readable thinking events with both API-key and
OAuth transports, off/on/off transitions, preserved effort, deferred login, switching,
and resume. Installed Anthropic SDK 1.6.0 accepts `display`, and Pydantic AI
2.43.0's `_translate_thinking` passes that dictionary through unchanged. Explicit
`summarized` is important for models whose API default omits readable thinking. See
[Anthropic extended thinking](https://platform.claude.com/docs/en/build-with-claude/extended-thinking)
and [adaptive thinking](https://platform.claude.com/docs/en/build-with-claude/adaptive-thinking).

### Saved thinking and scrollback replay

`live.py` maps readable `ThinkingPart`/`ThinkingPartDelta` content into
`ThinkingDelta` and emits a `Thinking` completion at `PartEndEvent`. These events
are journaled independently of visibility. Never put signatures or redacted
thinking data in them. `SavedSession.transcript_records` coalesces thinking deltas,
flushes them before interleaved display records, skips duplicate completion text,
and preserves incomplete blocks on cancellation/failure/reopen. Resume captures
these writes into `TranscriptLog` under the same `transcript_max_chars` budget as
live output, then requests the ordinary redraw; it has no separate record limit.
Native model history remains separate from this readable presentation history.

`TerminalOutput.thinking_delta` commits complete plain-text lines through
`Transcript.thinking`, flushing the unfinished line on block/turn termination.
Thinking is control-sanitized like command text, and uses the
named Rich `pcode.thinking` style (muted + dim; terminal-color mode uses dim default).
Define compound styles in the theme: a string such as `pcode.muted dim` is not a
valid composite of a theme alias and an attribute in Rich's style parser.
Consecutive thinking writes coalesce in `TranscriptLog`, without the old 8-KB
preview truncation. Retention is bounded by the text a replay would emit, not by
entry count. An entry-only cap previously dropped visible history from long
sessions on every resize. Live Markdown blocks and user prompts are now recorded
atomically with their separators, so a trailing blank cannot evict an oversized
newest message. The loose entry cap scales with the character budget and remains
only so a flood of tiny writes cannot grow the deque forever.
Markdown is retained as its source (`RetainedMarkdown`), because `Transcript.print`
rebuilds the renderable with the current theme anyway and a parsed token tree
costs tens of times its source.

`show_thinking` only controls the projection of these stored writes (plus the
Anthropic next-request opt-in above). Ctrl+T and `/show-thinking` request the same
atomic rebuild as `/redraw`. Hidden writes must remain in the log; pending writes
must not duplicate on regeneration or resize. No thinking is allocated to the
live prompt or task header. Legacy `thinking_display`/`thinking_lines` preferences
are ignored. Keep real-tmux tests for streamed text before completion, show/hide
while editing, cancellation/completion retention, history deduplication, resize,
and compact prompt/task height with real CPR. A PTY without CPR is insufficient.

Persistence is intentional even when hidden. Do not promise secure deletion by
toggling visibility or clearing terminal history, and do not backfill old sessions
from potentially opaque native model history without a separate migration design.

### Popup text selection and Rich's wrap points

`text_selection.py` replaces `rich.text.Text.wrap` at import with a wrapper
that does nothing unless `marked_rendering()` is active, which only
`RichPane.fragments` enters. Inside it, each row Rich split gets zero-width
markers (U+2062/U+2063 at the end for a fold or a word break, U+2064 where a
continued row's text starts), which `unmark` strips before drawing. That is how
a copy rejoins wrapped lines and drops indents and gutters. The marker is a
ContextVar, so a Rich render on another thread or task never sees one.

Rich pads a row before returning it, so the wrapper can't see whether a break
was between words. It calls Rich's private `rich._wrap.divide_line` the way
`Text.wrap` does to find out. Recheck both on a Rich upgrade:
`tests/test_text_selection.py` fails if `Text.wrap` stops splitting on newlines
before wrapping, or `divide_line` changes signature. A disagreement falls back
to joining with a space rather than failing.

### Explicit newline key encodings

`input_keys.py` installs a bounded per-parser interceptor around prompt_toolkit
3.0.53's `Vt100Parser.feed`. It decodes Kitty flag-1 CSI-u and xterm modified-key
reports before vi sees Escape, leaving paste, CPR, mouse and legacy input to the
original parser. Shift+Enter becomes `Keys.ControlJ`; Ctrl+Enter uses the internal
`Keys.ControlF24` identifier for interrupt-send. Recheck the private `_input_parser`
and `_Flush` integration on dependency upgrades. Unsupported enhanced keys are
consumed rather than inserted as escape-sequence text.

`keyboard_protocol.py` wraps real VT100 output and pushes Kitty flag 1 when the
renderer enables bracketed paste. It pops before paste is disabled or the alternate
screen is left, so terminal handoffs, renderer resets and exit restore the prior
flags on the correct screen. It requests neither key releases nor all-key reporting.
This is flag-1 support, not the full Kitty protocol. Under tmux, the wrapper instead
queries `#{pane_key_mode}` once before rendering. A `VT10x` pane temporarily gets
`modifyOtherKeys` mode 1 and is restored to mode 0 on handoff; inherited `Ext 1` /
`Ext 2` modes are left alone. Failed or unknown mode queries do not change the
mode. No tmux server options are changed, so `extended-keys=off` still prevents
modified-key reporting. Xterm mode is not enabled outside tmux. See the
[Kitty protocol](https://sw.kovidgoyal.net/kitty/keyboard-protocol/) for the per-screen
push/pop stacks. Atomic transcript handoffs preserve keyboard reporting across
renderer resets and the CPR wait: they keep input raw, so disabling the mode even
temporarily can turn modified Enter into submit during streaming. External-program
handoffs still restore it. Real-tmux tests must use named `S-Enter` / `C-Enter` keys
as well as literal bytes: byte injection alone passes even when mode negotiation
is broken. Use named `C-j` too; tmux's Ext 1 mode drops literal LF injection.

Inspiration: [pi-vim](https://github.com/lajarre/pi-vim/blob/main/index.ts)
passes insert-mode input to Pi's editor and implements `o`/`O` with explicit
newline insertion; [Pi's key decoder](https://github.com/earendil-works/pi/blob/main/packages/tui/src/keys.ts)
distinguishes legacy, CSI-u, and modifyOtherKeys input. We retain prompt_toolkit's
native vi `o`/`O` bindings. Pipe-input tests cover both editing modes and prompt
implementations; real-tmux tests retain CPR, multiline height, Escape, and resize
checks for each supported newline encoding.


### Shortcut prefix and key precedence

`prefix_keys.py` gives the prompt and every popup their shortcuts behind the
`key_prefix` setting. Verified against prompt_toolkit 3.0.53:

- A native multi-key binding cannot be a leader. `KeyProcessor` flushes a
  partial sequence after `Application.timeoutlen` (1 s), dropping the leader,
  and replays an unmatched follow-up as ordinary input, typing it. `PrefixKeys`
  keeps its own `pending` flag instead, set by the leader binding.
- Precedence is eager over non-eager, then fewer `Keys.Any`, then the later
  registry (focused container over parents over `Application.key_bindings` over
  defaults). An eager `<any>` dispatcher still loses to any specific eager
  binding, such as a popup's Esc, so while a leader waits every pcode binding is
  switched off through `ConditionalKeyBindings` rather than outranked. Default
  bindings are not gated; their only eager ones are quoted insert and Emacs
  incremental search.
- `key_processor.key_buffer` already holds the key being matched when filters
  run. `PrefixKeys` reads it to let cursor-position reports, mouse events and
  pastes through while a leader waits; swallowing a CPR would stall rendering.
- Ctrl chord shortcuts are eager too, or a chord that begins a longer default
  sequence (Ctrl+X in Emacs mode) would wait out `timeoutlen` first.

Recheck `tests/test_prefix_keys.py` and `tests/test_prefix_keys_tmux.py` on
upgrades.

### Provider summaries versus internal reasoning

[OpenAI's reasoning guide](https://developers.openai.com/api/docs/guides/reasoning)
describes exposed summaries, not raw internal tokens. `summary="auto"` does not
mean heading-only or promise a particular update frequency. Installed Pydantic AI
2.43.0 maps decoded summary content to `ThinkingPart`/`ThinkingPartDelta`.
[Anthropic's thinking guide](https://platform.claude.com/docs/en/build-with-claude/thinking)
also describes visible summaries and empty blocks with `display="omitted"`.
Neither reasoning-token usage nor a thinking-status event proves readable text
exists. We stream all exposed text rather than extracting a task-header heading;
we cannot reconstruct provider-omitted content.


## Live shell output

Harness comes from PyPI, pinned to one minor in `pyproject.toml`. It pins
`pydantic-ai-slim` to its own release exactly (Harness 0.N.x ships with Pydantic
AI 2.N.x), so upgrade the two together.

Since Harness 0.52 every capability works through the run's workspace
(`ctx.workspace`) rather than a directory argument: `Coder(path)`,
`Shell.cwd`, `FileSystem.cwd` and `RepoContext.workspace_dir` are deprecated and
ignored. `create_coder` attaches `LocalWorkspace(workspace)`, and a run without
one fails at its start. `WorkspaceGuard` runs first so a deleted worktree is named
before Harness reports a generic `WorkspaceUnavailableError`. Upstream
`SubAgents` passes the parent's `ctx.workspace` to every delegate explicitly, so
isolated delegation hands its child run a context with the task worktree's own
workspace. Coder's own `SubAgents` is off (`sub_agents=False`); pcode's
`WorkspaceSubAgents` provides `delegate_task`.

Pydantic AI records the run's workspace as `workspace_ref` on each response
and refuses to continue history recorded in another directory. pcode moves
sessions on purpose (a removed worktree's session continues in the mainline,
`-C DIR --continue`), so `live.py` names the workspace on each run
(`workspace=LocalWorkspaceBackend(dir)`), which takes precedence over the ref
in history. Two things that look simpler don't work: clearing the refs from
history changes the messages, which breaks the byte-identical request prefix
side questions and thread merges rely on; and a `LocalWorkspace` subclass that
ignores the ref fails the run's check that the capability returned the
workspace it was asked for.

A `LocalWorkspace` passes commands only `PATH`, `HOME` and locale variables.
pcode's commands don't go through it: `JobShell` runs them through `pcode.jobs`
with the whole host environment (direnv tokens included) from its own
`workdir`, minus `LLM_API_KEY_ENV_PATTERNS`, which Coder applied before it left
the environment to the workspace.

File tools are `WorkspaceFileSystem`: Harness's toolset with `root_dir='/'`, so
paths are unconfined while relative ones resolve from the workspace and walked
paths come back relative to it; read-only patterns get a `**/` prefix to match at
any depth. `DisplayFileSystemToolset` takes the before snapshot for edit
previews in `_request`, which Harness calls under its per-file lock just before
writing, and reads through the real path so an alias is reported as the file
it reaches.

The port leans on private upstream names, which the `<0.53` pin keeps stable
but an upgrade must recheck: `FileSystemToolset._request`, `_write_file`,
`_edit_file`, `_apply_replacements`, `_is_binary`; `repo_context._inventory.scan_assets`
and `_loader.discover_instruction_files`; `RepoContext._working_dir`,
`_render_instructions`, `_context_files`; `ShellToolset._resolve_env`; and
`pydantic_ai._utils.PeekableAsyncStream` in `claude_sdk/model.py`. With `root_dir='/'`,
`allowed_patterns`/`denied_patterns` match paths relative to `/` (no leading
slash); nothing in pcode sets them, but a workspace-relative pattern would need
a `**/` prefix.

Coder selects `Shell(tools=['shell'], default_timeout=270)`. Pcode replaces that
capability with `pcode.shell_tools.JobShell`, which keeps upstream's command
policy, environment handling and output cap (it subclasses `Shell` and
`ShellToolset`) but drops upstream's persistent `shell` tool from the registered
set and supplies its own, plus `wait_for_job`, `job_output`, `stop_job` and
`list_jobs`. Execution moved to `pcode.jobs`: the supervisor is
`src/pcode/_job_supervisor.py`, not `pydantic_ai_harness.shell._persistent`.
Upstream's `CommandStartedEvent`, `CommandOutputEvent` and `CommandFinishedEvent`
are still the UI contract and are emitted unchanged, so `src/pcode/shell.py` and
`live.py` keep working. Events carry `tool_call_id`; keep buffers local to a run
and keyed by that identity. They do not add model calls. Planning and SubAgents
are no longer in Coder and must be composed explicitly. `ClearToolResults` remains
removed so pcode can summarize before discarding evidence.

Pcode also replaces Coder's instruction-only `Capability` with the local
`CODER_INSTRUCTIONS` in `agent.py`, before copying capabilities to the worker.
It is upstream's prompt minus the paragraph telling the model to finish
long-running work before responding and to poll status, which kept it waiting
on jobs instead of answering steering. Do not add job workflow rules back there;
the shell tool descriptions cover the mechanics. Recheck the copy when bumping
the Harness pin. `test_parent_and_worker_replace_finish_before_responding_guidance`
checks the resolved prompt for both agents.

Why the replacement rather than the upstream tool: a command that outlives its
call needs a name. Without one, a still-running command can only be handed back
as a PID and two paths, so the model's only way to learn it finished is to poll
with `sleep`, an interrupted wait kills the command, nothing can list what a
session left running, and every trivial `ls` carries a handle block it will never
use. Upstream also leaks its temp directory on every successful call.

`purpose` is optional and scoped to `background=True`, not to expected duration:
"is this slow?" is a prediction the model is bad at, while "am I backgrounding
this?" is a decision it has already made, so it is a rule it can follow. The
schema cost sits in the cached prefix, and the per-call cost is paid only by
background calls. `Job.label()` falls back to the command, so nothing depends on
the model supplying one; `Job.summary()` and `tool_display.target` keep the
command alongside it, because a stated intention is not evidence of what is
running.

`pcode.jobs.registry()` is process-wide for the parent and shared-workspace workers:
a run is exactly the scope a job escapes. Isolated workers instead bind a context-local
registry, with their own `JobNotices` capability, and stop its jobs on teardown.
The parent's per-run `JobNotices` cannot reach those jobs and is not inherited by
sub-agents. Tests must call `registry().reset()` (the `isolated_jobs` autouse fixture
does). The process-wide registry persists under `jobs_root()/<pid>/` (job dirs plus
`registry.json`, resolved lazily so the test env's `XDG_STATE_HOME` applies);
`JobRegistry()` with no `state` is ephemeral and uses temp dirs. `adopt_orphans`
treats a record whose `owner_pid` is dead as up for grabs and skips its own
pid, so a test standing in for another process must set `_home` to a
differently named directory and rewrite `owner_pid`.

`stop` is `SIGTERM` to the group, and `refresh` escalates to `SIGKILL` after
`STOP_GRACE_SECONDS`; nothing blocks, because the caller can be a cancellation
handler. The supervisor survives the `SIGTERM` with a Python-level handler so it
can still publish the command's exit. It must not use `SIG_IGN`: an ignored
disposition is inherited across `exec`, which made the command itself immune to
the stop the first time round.

The executor polls the status file and log every 50 ms and emits at most 16,000
bytes of events per wait; `until_output` keeps reading to 1 MiB after the event
budget is spent, because a chatty server would otherwise print its readiness
banner past the end of the stream. A finished result is the log tail plus one
`[jN · exit N · elapsed]` marker and nothing else; only a running job gets
handles. `CommandFinishedEvent` still means the wait ended, not that the process
exited, so the marker in the result — written after the wait — is authoritative:
`tool_display.job_status` parses the last marker rather than trusting the event.
The event's PID is the supervisor/session leader; the status JSON's PID is its
child command, so comparing those PIDs to validate a status discards genuine
completions, and `killpg` needs the supervisor's pid, not the command's.

`JobNotices` delivers exits at the next request only. `PreviewApp.wake_prompt`
covers the case where no request is coming: while idle, a finished job the
model launched (`announceable`, not stopped, not adopted, not yet announced to
`model`) is queued as a `"wake"` prompt whose text is the same notice, and
marked announced so the capability does not repeat it. `run_live(wake=True)`
labels the row as system work and skips the scrollback quote. The jobs rows
(`Activity.jobs`) are computed by `refresh_jobs` on the watcher's one-second
tick, busy or idle, never per frame; `Job.waiting` keeps a job a tool call is
blocking on out of them, since the spinner row already names it. A watched job
(`/jobs watch`) is a `CommandOutput` keyed `job:<id>`, which `preview_layout`
shows even when `show_commands` is off and `run_live` leaves in place at the
end of a turn.

Cancellation consults `registry().cancel_policy`, which the app sets before
cancelling: `detach` for a typed follow-up (abandon the wait, keep the command),
`stop` for Ctrl+C. The tool call cannot tell these apart by the time it sees
`CancelledError`, which is why the policy lives on the registry. `run_live`
resets it to `detach` after every turn. A job the model backgrounded is never
caught by either, because nothing was waiting on it.

An already-truncated result is shown as it is; nothing on screen is
secret-redacted, only control-sanitized. `shell.split_envelope` is the single parser for the boundary
between output and the job envelope, shared with `CodingToolOutputLimits` so
reduction can never spill the handles a running job is reached through. The model
result and raw log are never altered. `--no-save` does not disable job logs.

A live preview and the settled block it becomes are framed alike: `pcode.block`
owns the rule character, the two-space body indent and the heading text, Rich
draws it in scrollback (`CommandTranscript`, `EditTranscript`) and prompt_toolkit
windows draw it in the panel. Change the framing there, not in one surface.

Wait for complete lines, and sanitize before clipping, so a credential split
across chunks is masked whole before a preview shows it. `CommandOutput`
bypasses session/tree journals. Remove per-call previews when tool results arrive,
and clear them on cancellation/failure/reset. Preserve `tests/test_shell_streaming.py`,
`tests/test_jobs.py`, and the real-tmux command-height tests; no-CPR PTYs cannot
prove compact height.

### Completed file diffs (verified Harness 0.52.0)

`FileWrittenEvent` still has no before/after contents. `FileEditedEvent` carries
a bounded diff, but it is already truncated; it cannot replace pcode's
full-source diff and precise counts. Reading the file when
either event reaches the UI cannot recover the previous version.

`DisplayFileSystemToolset` leaves Harness's `_write_file` and `_edit_file`
alone and captures evidence at their seam: `_request` runs under the per-file
lock (`_changing`, keyed by workspace object and real path) after validation
and before the write, so the snapshot it reads is the content being replaced.
The after side is the written content, or the same `Replacement`s applied to
the snapshot. A result starting `Wrote `/`Edited ` is the success signal; errors
and listener refusals emit nothing. A snapshot the workspace refuses to read
(write-only file) omits the diff rather than failing the write. On upgrades,
check that `_request` is still called inside the lock and before the write, and
that the result strings are unchanged. Nothing here makes the operation
transactional against writers outside the run.

Pydantic AI exposes tool arguments through `ToolCallPartDelta.args_delta`.
`StreamingEditPreview` handles single pairs and replacement arrays without
executing anything, using
`pydantic_core.from_json(..., allow_partial="trailing-strings")` for incomplete
string values. A separate parse with `allow_partial=True` requires a complete
path before exposing content; resolve it against the workspace root to exclude
symlinks that lead outside it. Preview events bypass the session journal;
completed change events replay through the same recorded transcript method even
when hidden. Keep `tests/test_edits_tmux.py`: no-CPR PTYs don't verify the shared
preview/editor height budget or redraw behavior in a real terminal.
