# Anthropic provider options

pcode reaches a Claude subscription two ways today: its own `/login`, which talks
to the Claude Code endpoint directly, and a [Meridian](https://github.com/rynfar/meridian)
proxy. This page records a September 2026 look at the risk and cost of each, the
alternatives that were considered, and what we decided. API-key access through
`ANTHROPIC_API_KEY` is unaffected by any of it.

Measurements were taken on 2026-09-23 with Claude Code 2.1.280, Meridian 1.72.0
and `claude-agent-sdk` 0.2.158.

Update, 2026-09-27: `claude:` models are being tried as the way forward, so the
`/login` direct route and Meridian are turned off behind one constant,
`LEGACY_ANTHROPIC_AUTH` in `src/pcode/models.py`. Their code stays, and setting
it to `True` restores both.

## Recommendation

1. Make Meridian the easy subscription route. pcode's own `/login` stays for now,
   with its risk documented here, and `/login meridian` signs Meridian in through
   Claude Code instead.
2. Smooth the rough edges of the Meridian integration, starting with the
   compaction fix. What shipped is under [Meridian work](#meridian-work).
3. Spike a native Agent SDK provider before committing to build one. The pass
   criteria are under [Direct SDK provider](#direct-sdk-provider). It shipped as
   `claude:` models; see [What shipped](#what-shipped).
4. Do not build ACP as a model provider. An ACP client that lets Claude Code run
   the whole conversation is a separate product decision.

| Route | Policy risk | Caching | Latency | pcode agent features | Status |
|---|---|---|---|---|---|
| `/login` direct | High | Tuned by pcode | Best | All | Shipped, turned off |
| Meridian | Low | Warm on continuation, one cold write on divergence | New CLI process per request | All | Shipped, turned off |
| Agent SDK provider (`claude:`) | Low | Warm on continuation and on forks; one cold write on replay | One CLI process per conversation | All | Shipped |
| ACP as a provider | Low | Same as the SDK provider | Same, plus a Node process | Usage and cache data degraded | Rejected |
| ACP client | Lowest | Claude Code's own | One CLI process per session | None for Claude sessions | Separate decision |

## Policy

The risk sits in how the credential is obtained and held, not in the HTTP
transport.

Anthropic's [legal and compliance page](https://code.claude.com/docs/en/legal-and-compliance)
says third-party developers may not "offer Claude.ai login into their own
applications" and "may not collect, store, or intermediate Claude.ai credentials
or session tokens", and that sign-in "must complete through Anthropic's own
flow". pcode's `/login` runs the Claude Code OAuth client from inside pcode and
stores the resulting tokens, which is what those sentences describe.

The same page does not prevent "an end user from signing in to the unmodified
Claude Code binary with their own Claude subscription", and it describes Pro and
Max limits as assuming "ordinary, individual usage of Claude Code and the Agent
SDK". Meridian and any Agent SDK integration run the real `claude` binary with
its own login, so they sit on that side of the line.

Handing pcode's tokens to the SDK through `CLAUDE_CODE_OAUTH_TOKEN` would not
change this, because pcode would still be collecting and holding them. The CLI
also cannot refresh a token passed that way.

The wording moved during 2026. In February the page said subscription tokens
were not permitted in any other product "including the Agent SDK"
([The Register](https://www.theregister.com/2026/02/20/anthropic_clarifies_ban_third_party_claude_access/));
that sentence is gone. In June Anthropic paused a plan to bill Agent SDK usage
from a separate monthly credit, and said SDK and third-party app usage still
draw from subscription limits
([help center](https://support.claude.com/en/articles/15036540-use-the-claude-agent-sdk-with-your-claude-plan)).
If that plan returns it applies to Meridian and an SDK provider alike.

Server-side checks on direct traffic keep tightening. Commits `7dc0dff`,
`11a0d2d` and `d7a891b` exist only because the endpoint began answering
`claude_code_version_too_old`. Another harness reported plan traffic rejected
with "Third-party apps now draw from extra usage, not plan limits" when its
requests lacked the billing block that the real CLI sends as its first system
block ([hermes-agent #48176](https://github.com/NousResearch/hermes-agent/issues/48176)).
pcode does not send that block.

This is a reading of published pages, not legal advice.

## How Meridian works

Meridian is a Node proxy around `@anthropic-ai/claude-agent-sdk`, which drives
the `claude` CLI as a subprocess. In passthrough mode, which pcode uses, each
HTTP request:

1. Registers the client's tools as an in-process MCP server with stub handlers.
2. Calls `query()`, which starts a fresh CLI process that resumes the
   conversation's transcript at the last assistant message (`resumeSessionAt`
   with `forkSession`).
3. Catches each tool call in a `PreToolUse` hook, blocks it so the CLI never runs
   it, ends the turn early, and returns the captured `tool_use` blocks as an
   ordinary Anthropic response.

The next request carries the tool results and the cycle repeats. The model sees
the tools as `mcp__<server>__<name>`, and Meridian maps the names back.

### Caching through Meridian

Meridian strips client `cache_control` and lets the CLI place breakpoints.
Whether the cache stays warm depends on how it classifies each request against
the history it stored for that conversation (`verifyLineage` in the bundled
source):

| Lineage | What Meridian does | Cache |
|---|---|---|
| continuation (history only grew) | Resume the CLI session and send only the new messages | Warm |
| undo (history got shorter) | Resume truncated at an earlier message | Warm up to that point |
| compaction (only the tail still matches) | Resume the old, uncompacted session and send only the messages after the matching tail | Warm, but see below |
| diverged (edited, replayed or unrelated history) | Start a new CLI session with the history flattened into `[Assistant: …]` text | System and tools stay cached; the history is written again once |

When a request is classified as compaction, the summary never reaches the model:
upstream, the CLI keeps the full history until its own auto-compaction runs.
pcode now moves to a new Meridian session after compacting to avoid this;
[Meridian work](#meridian-work) has the live reproduction.

A diverged request is a one-time cost rather than a broken cache, since the
replayed session caches normally afterwards. For a 100k-token history the replay
writes those tokens at 1.25 times the input price instead of reading them at 0.1
times. This is why pcode keeps plan and MCP reminders append-only on
Meridian (see [dependencies](dependencies.md#anthropic-prompt-caching)).

## Measurements

The live runs used `claude-haiku-4-5`, a system prompt padded to about 9k tokens
so it clears the minimum cacheable size, and one `lookup` tool. The same
three-round tool task ran through Meridian and through the Agent SDK directly.
Meridian times run from HTTP request to complete non-streamed response; SDK times
run from delivering the tool result to the end of the next message.

Starting the CLI through the SDK (connect and initialize handshake, no inference)
took 0.60 s median over five runs. Meridian pays that on every request.

Meridian 1.72.0, passthrough, external proxy:

| Round | Time | Cache read | Cache write |
|---|---|---|---|
| 1 | 2.68 s | 0 | 11,471 |
| 2 | 1.84 s | 11,471 | 203 |
| 3 | 1.78 s | 11,674 | 142 |
| 4 (final) | 3.02 s | 11,816 | 142 |

Agent SDK with one `ClaudeSDKClient` kept alive and each tool handler parked while
the tool "runs":

| Round | Time | Cache read | Cache write |
|---|---|---|---|
| 1 | not comparable (includes connect) | 0 | 18,819 |
| 2 | 1.34 s | 18,819 | 222 |
| 3 | 1.34 s | 19,041 | 160 |
| 4 (final) | 1.04 s | 19,201 | 157 |

Two follow-ups on the same session:

- A new process resuming the session with `resume=<session id>` connected in
  0.92 s and read 19,358 tokens from cache while writing 114. Restarting costs
  nothing in cache as long as the CLI's own transcript is reused.
- A new session given the history as flattened text read 16,872 tokens (system
  and tools) and wrote 2,016 (the history).

Other observations:

- A handler parked for 75 s did not time out.
- The CLI invokes a tool handler at the end of the assistant message, about 10 ms
  before the consumer sees `message_stop`. An integration must not assume the
  handler runs afterwards.
- The same prompt cost 18,819 tokens through the SDK with `tools=[]` and
  `setting_sources=[]`, against 11,471 through Meridian, which also passes a
  `disallowedTools` list, `plugins: []` and environment variables that quiet the
  CLI. Most of the difference is cached, but it still occupies context window.
- Judging by the numbers, the CLI adds about 1.9k tokens to the first user message
  by itself (18,819 less the 16,872-token system prefix, for a 60-token prompt). A
  transcript synthesized from pcode's history would have to reproduce those bytes
  exactly to hit the cache, so synthesis is not a caching strategy.

These are small samples taken minutes apart. Treat them as direction, not a
benchmark.

## Direct SDK provider

The proposal is a Pydantic AI `Model` backed by one live `ClaudeSDKClient` per
conversation. pcode's tools are registered as in-process MCP tools whose handlers
wait until Pydantic AI runs the tool and sends the result in its next request:

```python
async def request_stream(self, messages, settings, params, run_context):
    session = self.sessions.for_conversation(run_context.conversation_id)
    if session.can_continue(messages):  # only tool results or a new prompt added
        session.deliver(tool_returns(messages))  # releases the parked handlers
    else:
        session = await self.sessions.recover(messages)
    events = to_beta_events(session.stream_until_turn_boundary())
    yield await self._process_streamed_response(events, ...)
```

Several things make this cheaper than it looks:

- `StreamEvent.event` is the raw Anthropic streaming event, and
  `AnthropicModel._process_streamed_response` accepts any async iterable of them,
  so text, thinking, tool calls and usage go through existing code. Tool names
  need mapping back from `mcp__pcode__*`.
- SDK 0.2.158 provides `tools=[]`, `setting_sources=[]`, a plain-string
  `system_prompt`, `include_partial_messages`, `set_model()`, `interrupt()`,
  `resume`, `fork_session`, `resume_session_at`, JSON Schema tool inputs and an
  injectable `Transport` for tests.
- The CLI binary recognizes `CLAUDE_CODE_MAX_RETRIES`, `DISABLE_AUTO_COMPACT`,
  `MAX_MCP_OUTPUT_TOKENS`, `MCP_TOOL_TIMEOUT` and
  `CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC`, which would hand retries,
  compaction and output limits back to pcode. Their exact behaviour is
  unverified.
- No pcode credentials are involved. The child process must not inherit
  `ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN` or `ANTHROPIC_BASE_URL`, or it
  silently bills the key instead of the subscription.

pcode's session store stays the source of truth for resume, `/tree`, compaction
checkpoints, recall and `cache-report`. The Claude Code transcript is a
disposable copy of what was sent, found through a saved mapping from conversation
ID to CLI session ID and CLI message UUIDs:

| Event | Action | Cache |
|---|---|---|
| Tool round or new prompt | Keep the live process | Warm |
| Restart or `--continue` | New process with `resume` | Warm (measured) |
| `/tree` to an earlier turn, or a retry | `resume_session_at` the mapped UUID | Warm up to that point |
| Compaction | New session seeded with the compacted history | One cold write of the smaller history, as on the direct path |
| Transcript missing or history edited | Flattened replay | One cold write |
| Model or tool-list change | Any | Cold on every route |

Rough work breakdown:

| Piece | Size |
|---|---|
| Model adapter and event conversion | M |
| Parked handlers, parallel tool calls, interrupt | M |
| History reconciliation and replay | L |
| Process pool keyed by conversation, cleanup, environment scrubbing | S–M |
| Settings: effort needs a respawn, model uses `set_model`, pcode cache settings off | S |
| Login UX that points at Claude Code's own login | S |
| Dependency: wheels are about 90 MB each because they bundle the CLI, or use the sdist with `cli_path` | S |
| Tests against a fake `Transport`, plus one live smoke test | M |

A spike of a day or two should pass these before the full build, which is
roughly two weeks:

- Prompt size at parity with Meridian for the same request.
- A measurable wall-clock saving per turn on a real pcode task, not a toy prompt.
- Acceptable memory per CLI process with two or three delegates running.
- Thinking, effort changes, Ctrl+C and a warm resume after restart all work.

Known costs: the model sees `mcp__pcode__*` names, built-in web search is
unavailable with `tools=[]`, transcripts land in `~/.claude/projects/` and show up
in `claude --resume`, and the SDK ships releases every few days and marks
`Transport` as internal.

Embedding Meridian's library does not avoid this work. Its entry point,
`createProxyServer()`, is the HTTP app itself, so using it still means a Node
process doing a `query()` per request.

### What shipped

`src/pcode/claude_sdk/` implements the proposal against `claude-agent-sdk`
0.2.160, whose bundled CLI is 2.1.283. It departs from the sketch above in these
ways:

- Sessions are matched by history, not by conversation ID. Each message pcode sends
  or receives is hashed along with everything before it. A live process is reused
  only when it holds exactly the history before the new user message, so
  delegations, `/btw` and compaction runs cannot land on the wrong process.
- Recovery never synthesizes a transcript. An index of history hashes, persisted
  in `$XDG_STATE_HOME/pcode/claude-sessions.jsonl`, maps each assistant message to
  its CLI session and transcript entry. A request that no live process can take
  forks the transcript at the last shared message with `resume`, `fork_session`
  and `resume_session_at`, and sends the rest as one user turn. The tool results
  the fork point waits for stay structured; anything longer is replayed as text.
  With no shared message at all, the whole history is replayed that way.
- The system prompt, tool list, model, effort and thinking settings are fixed per
  process, so changing any of them forks to a new process rather than using
  `set_model()`.
- No `Transport` is involved. pcode drives the public `ClaudeSDKClient` and serves
  its tools from its own MCP server, and the tests script the client instead. What
  can break on an upgrade is the CLI's own behavior, listed under
  [dependencies](dependencies.md#claude-agent-sdk-provider-verified-02160-cli-21283).
- `claude-agent-sdk` is an optional extra (`pcode[claude]`), since each wheel
  bundles the CLI. `make install` and the Homebrew formula include it. The sdist
  is not used, because it would run whatever `claude` is on `PATH` rather than
  the CLI version these notes were verified against.
- Text pcode appends beside tool results is written to the CLI while it is
  parked. The CLI queues it and sends it in the same request as the results,
  wrapped as a mid-turn user message (verified, including the wording). No
  respawn is needed for plan reminders or steering.

Measured on 2026-09-26 with `claude-haiku-4-5` through pcode itself (`pcode -p`
and `make cache-report`):

| Case | Result |
|---|---|
| Tool round on the live process | 0.9–1.4 s including generation; no process start |
| Continuation cache | 8,083 read, 318 written on the second request |
| New pcode process, `--continue` | Forked: 8,401 read, 166 written; 3.7 s for the whole command |
| Fork connect | 0.8 s |
| Memory | 260–310 MB RSS per CLI process; about 200 MB of that is the CLI's code, shared between processes |

Caching was checked again on 2026-09-27 against real traffic: four
`claude:claude-opus-5-5` sessions, 209 requests, read with `cache_report.py`.
Every request after a session's first read the previous request's whole input
less 2 tokens and wrote only the new turn; the longest session read 97% of its
input from cache. Forks were as warm as the live process (early builds forked a
new process for every request, and their reads look the same). A new session's
first request already reads about 7,900 tokens, the CLI's shared prefix. The only
cold writes were a switch from another model and a turn after ten hours idle.

Those sessions wrote 1-hour cache entries, Claude Code's default on a
subscription within plan usage. pcode now sets `CLAUDE_CODE_PROMPT_CACHE_TTL=5m`,
matching the 5 minutes it asks for on the direct `anthropic:` path, for three
reasons:

- A 1-hour write costs 2x input against 1.25x, and pays back only when the next
  request comes 5-60 minutes later. pcode turns average about 35 requests, 97% of
  them under five minutes apart. Replaying saved sessions' request gaps at Opus 5.5
  prices (reads 0.05x), an hour cost about 10% more on `claude:`, 12% on
  `anthropic:` and 7% across all Anthropic traffic.
- Anthropic does not publish how a subscription meters cache tokens, but one Max
  user's reconstruction of an exhausted week at list API prices, 1-hour writes at
  2x, matched the measured pool to 0.3%
  ([claude-code#88352](https://github.com/anthropics/claude-code/issues/88352)).
  Claude Code's maintainers describe the hour as a per-workload call, not a
  general win
  ([claude-code#46829](https://github.com/anthropics/claude-code/issues/46829)).
- The one case the hour covered well is a parent blocked in `delegate_task`, which
  has no wait cap and routinely outlasts five minutes. On five minutes that parent
  rewrites its prefix when the child returns, as `anthropic:` parents already do.

The variable outranks `ENABLE_PROMPT_CACHING_1H` and settings; only
`FORCE_PROMPT_CACHING_5M` beats it. The API offers no TTL other than 5 minutes and
1 hour.

The spike's open criteria have answers now. Prompt size is Claude Code's own plus
pcode's, since the CLI adds its environment, date and model attachments.

Memory was measured again on 2026-09-26 with `vmmap`. After one short request,
a process showed 232 MB RSS but a 110 MB physical footprint (134 MB peak); the
rest is the Bun binary's ~200 MB of code, which every process shares. So each
process after the first costs about 110–135 MB, growing with the conversation.
Each session keeps one finished process for ten minutes, and a process waiting
on tool results, such as a parent waiting for delegations, for thirty. Under
memory pressure none are kept.

One CLI process cannot serve several conversations: the system prompt, tools,
model, effort, thinking and working directory are fixed when it starts, and the
SDK's per-message `session_id` does not change that. A shared broker owning
every session's processes would only add a machine-wide idle cap, at the cost of
relaying every tool call between processes, so each session host keeps its own
pool.
Thinking, effort changes, Ctrl+C and a warm resume after restart all work.

## ACP

[claude-agent-acp](https://github.com/agentclientprotocol/claude-agent-acp)
(0.81.1 inspected) exposes the Agent SDK as an Agent Client Protocol agent. It
keeps one long-running `query()` per session, logs in through the CLI's own
`auth login --claudeai`, and lets a client override the system prompt, tools and
setting sources through `_meta`.

As a pcode model provider it would be the SDK design with more parts. pcode's
tools would have to be served as an MCP server over stdio or HTTP, and ACP
delivers text and thought chunks rather than raw Anthropic events. Per-request
boundaries, stop reasons, thinking signatures and per-request cache counts are
lost; usage arrives only as a context total.

As an ACP client, pcode would render sessions in which Claude Code runs the whole
loop with its own tools, compaction and history. That carries the lowest policy
risk and gets Claude Code's own caching, and the same client could drive other
ACP agents. It also means pcode's tools, extensions, Harness capabilities,
`/tree`, cross-provider switching and `cache-report` do not apply to those
sessions. Harness's experimental ACP module serves a Pydantic AI agent to editors,
the opposite direction, so the client side would be new code.

## Meridian work

These findings come from live runs against Meridian 1.72.0 on 2026-09-23, with
the current 1.76.1 checked by reading its bundle.

Meridian ignores pcode's compaction. The probe built a four-round tool
conversation, then sent a compacted history whose summary alone contained a
codename, and asked for it:

| Request | Lineage | Answer | Cache read | Cache write |
|---|---|---|---|---|
| Compacted, same session ID | compaction | NONE | 17,915 | 2,266 |
| Compacted, new session ID | new | PELICAN | 8,066 | 7,567 |

Under the same ID the model kept reading the full uncompacted history and never
saw the summary. Under a new ID the summary arrived, at the cost of one cold write
of the smaller history, which is what compaction costs on any route. 1.76.1
classifies compaction the same way.

Managed mode cannot start on any current release because it requires exactly
1.71.1. With that check relaxed, 1.72.0 was ready in 3.6 s using about 100 MB,
and 1.76.1 still reads `sdk-features.json`, serves `/settings/api/features` and
honours the same environment variables.

Managed mode also keeps Meridian's session store in a temporary directory, so a
resumed pcode conversation starts over. Restarting Meridian between rounds two
and three gave:

| Session store | Round 3 lineage | Cache read | Cache write |
|---|---|---|---|
| Temporary directory (today) | new | 8,066 | 1,152 |
| Persistent directory | continuation | 9,138 | 263 |

In a real session the first row's write is the whole history, replayed as
flattened text.

A stopped external proxy surfaces as a generic transient connection error that
is retried once and then reported as "Check provider/proxy connectivity". Nothing
says that Meridian is not running or not logged in. The long-running external
proxy on this machine also has Thinking Passthrough off, so thinking never
appears.

| # | Work | Status |
|---|---|---|
| 1 | Rotate the session ID on compaction | Done. Keyed on the summary rather than every first prompt, so conversations that were never compacted keep their existing ID. |
| 2 | Accept current releases in managed mode | Done: 1.71.1 or newer, with the readiness checks as the guard. No warning above a verified release. |
| 3 | Clear errors | Done without a preflight request. A failed request is recognized as Meridian's by its URL and explained: proxy not answering, private instance restarting, Claude login to refresh, or key rejected. A proxy that drops thinking is reported once. |
| 4 | Persistent managed session store | Done. Shared by pcode processes; a hard crash of Meridian can leave about a minute of `503 overloaded_error` while its lock expires. |
| 5 | Choose the mode automatically | Done. `auto` is the default; the private instance already starts while the terminal opens, and a crashed one is restarted on the same port. |
| 6 | Route `anthropic:` through Meridian | Deferred: pcode keeps its own sign-in for now. |
| 7 | `/login meridian` | Done. Runs `claude auth login` for the login the Meridian in use reads, which can be a Meridian profile's own config directory rather than `~/.claude`. |
| 8 | `pcode --upgrade-meridian` | Done. Upgrades the `PATH` install and the running proxy's install, each with its own npm, and prints how to restart a launchd-run proxy. |

Private instances also turned out to see different profiles on different
releases; they now link the user's profiles in and name the one to use (see
[dependencies](dependencies.md#managed-meridian-isolation-verified-1711-to-1761)).

Item 6 would add `meridian` as an Anthropic auth source, next to `api-key` and
`oauth`, which already share the `anthropic:` prefix, so saved sessions, effort
settings and the default model keep their names. Model settings would have to
follow the resolved route rather than the model string: prompt-cache settings are
pointless through Meridian, and the web-search profile narrowing on the OAuth
model needs checking against what Meridian passes through.

Later candidates: a picker fed by Meridian's account-aware `/v1/models`, and an
explicit command to turn on Thinking Passthrough for an external proxy.

The per-request CLI start, about 0.5 s per tool round, is inherent to Meridian;
`claude:` models avoid it. When Meridian does start fresh it replays history as
flattened text. Items 1 and 4 make that rarer but cannot change the format.

### Requests and telemetry

Moved from the user reference. Every Meridian request carries
`x-meridian-agent: passthrough`, so pcode, not Meridian's built-in agent, runs
the tools, and `x-litellm-session-id`, derived from the pcode conversation ID
(`MeridianSessionIdentity` in `src/pcode/meridian.py`). Tool rounds and
saved-session resume reuse it; `/new` and independent delegates get their own,
even in parallel. After compaction the ID gains a suffix from the summary (item 1
above).

Meridian's request telemetry should show `lineage=continuation` on ordinary
follow-up tool rounds. Repeated `independent-request:headerless-tool-result`
means the running client lacks the session header; restart pcode after upgrading,
since a running Python process does not reload it.

When the spinner sits silent, compare the telemetry's queue wait, time to first
byte, upstream duration, status and lineage. An early first byte is not
necessarily visible text, and a large output-token count alone does not show what
happened during the pause. "Waiting for model…" means no new displayable event,
not necessarily an idle upstream connection.

The private instance runs with an empty plugin directory and working directory,
checks `/health` and its effective settings before connecting, and links the
user's Meridian profiles into its configuration rather than copying them. That is
configuration and session isolation, not an authentication sandbox: Meridian
still reads the Claude login.
