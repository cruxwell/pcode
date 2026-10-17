# Anthropic provider options

pcode reaches a Claude subscription through `claude:` models, which drive the
real Claude Code CLI through the Claude Agent SDK and use Claude Code's own login
(`/login claude`). `anthropic:` models use `ANTHROPIC_API_KEY` only. This page
records a September 2026 look at the risk and cost of the subscription routes,
the alternatives that were considered, and what we decided.

Measurements were taken on 2026-09-23 with Claude Code 2.1.280 and
`claude-agent-sdk` 0.2.158 unless noted.

## Recommendation

1. Reach the subscription only through the Agent SDK provider. It shipped as
   `claude:` models; see [What shipped](#what-shipped). The pass criteria it was
   spiked against are under [Direct SDK provider](#direct-sdk-provider).
2. Do not build ACP as a model provider. An ACP client that lets Claude Code run
   the whole conversation is a separate product decision.

| Route | Policy risk | Caching | Latency | pcode agent features | Status |
|---|---|---|---|---|---|
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
flow". pcode once had its own `/login` that ran the Claude Code OAuth client from
inside pcode and stored the resulting tokens, which is what those sentences
describe. It was removed for that reason.

The same page does not prevent "an end user from signing in to the unmodified
Claude Code binary with their own Claude subscription", and it describes Pro and
Max limits as assuming "ordinary, individual usage of Claude Code and the Agent
SDK". An Agent SDK integration runs the real `claude` binary with its own login,
so it sits on that side of the line.

Handing pcode-held tokens to the SDK through `CLAUDE_CODE_OAUTH_TOKEN` would not
change this, because pcode would still be collecting and holding them. The CLI
also cannot refresh a token passed that way.

The wording moved during 2026. In February the page said subscription tokens
were not permitted in any other product "including the Agent SDK"
([The Register](https://www.theregister.com/2026/02/20/anthropic_clarifies_ban_third_party_claude_access/));
that sentence is gone. In June Anthropic paused a plan to bill Agent SDK usage
from a separate monthly credit, and said SDK and third-party app usage still
draw from subscription limits
([help center](https://support.claude.com/en/articles/15036540-use-the-claude-agent-sdk-with-your-claude-plan)).
If that plan returns it applies to the SDK provider too.

Server-side checks on direct subscription traffic also kept tightening while
pcode had its own sign-in: the endpoint began answering
`claude_code_version_too_old`, and another harness reported plan traffic
rejected with "Third-party apps now draw from extra usage, not plan limits" when
its requests lacked the billing block the real CLI sends as its first system
block ([hermes-agent #48176](https://github.com/NousResearch/hermes-agent/issues/48176)).
Running the real CLI sidesteps both.

This is a reading of published pages, not legal advice.

## Measurements

The live runs used `claude-haiku-4-5`, a system prompt padded to about 9k tokens
so it clears the minimum cacheable size, and one `lookup` tool, with a
three-round tool task run through the Agent SDK directly. Times run from
delivering the tool result to the end of the next message.

Starting the CLI through the SDK (connect and initialize handshake, no inference)
took 0.60 s median over five runs. A design that starts a CLI process per request
pays that on every tool round.

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
  `setting_sources=[]`. Passing a `disallowedTools` list, `plugins: []` and
  environment variables that quiet the CLI brought a comparable request down to
  about 11,500. Most of the difference is cached, but it still occupies context
  window.
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
| Compaction | New session seeded with the compacted history | One cold write of the smaller history, as on the API-key path |
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

- Prompt size close to the leanest CLI configuration for the same request.
- A measurable wall-clock saving per turn on a real pcode task, not a toy prompt.
- Acceptable memory per CLI process with two or three delegates running.
- Thinking, effort changes, Ctrl+C and a warm resume after restart all work.

Known costs: the model sees `mcp__pcode__*` names, built-in web search is
unavailable with `tools=[]`, transcripts land in `~/.claude/projects/` and show up
in `claude --resume`, and the SDK ships releases every few days and marks
`Transport` as internal.

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

A forced replay (no shared message, so the history goes as flattened text) is a
one-time cost rather than a broken cache, since the new session caches normally
afterwards. For a 100k-token history the replay writes those tokens at 1.25 times
the input price instead of reading them at 0.1 times.

Those sessions wrote 1-hour cache entries, Claude Code's default on a
subscription within plan usage. pcode now sets `CLAUDE_CODE_PROMPT_CACHE_TTL=5m`,
matching the 5 minutes it asks for on the `anthropic:` API-key path, for three
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
