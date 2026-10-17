# Prompt caching and plan reminders

Pcode now disables automatic plan reminders by default. Planning tool results
report changes, and `read_plan` retrieves the stored state. Existing reminders in
saved history are not removed or rewritten.

The historical design below kept plan reminders append-only because a reusable
cache prefix must stay in place as the conversation grows. An unchanged reminder
string is not enough if it disappears from its old position on the next request.

This explains the planning fix in `e28791b`. It does not claim that all cache
misses come from planning or that stable requests guarantee server-side hits.

## Why the upstream guarantee was not enough

There are two upstream designs to distinguish. When investigated, the
[public Planning documentation](https://pydantic.dev/docs/ai/harness/planning/)
described a breakpoint after the reminder's stable opening tag. The Harness
source pinned at the time, `12bce878da99bca61a5d8d798bff0a3bc93bd153`, already
included a correction to that design. Treat the versioned source, not the live website, as
the description of what this installation does.

### Earlier design: a breakpoint after a moving tag

The website described an ephemeral reminder added to the per-request copy of
history, with a cache breakpoint between its opening tag and mutable plan text:

```text
Request 1: [history A] [<plan-reminder>] CACHE [plan]
Request 2: [history A] [new response + tool results] [<plan-reminder>] CACHE [plan]
```

Although the tag is identical, the prefix through it is not. Request 2 puts new
conversation content where request 1 had the tag. The entry written through that
tag therefore cannot be reused as that same prefix. Moving changing text to the
tail avoids rewriting the system prompt, but removing and relocating that tail
still matters for caching.

### Pinned design: a durable but potentially distant breakpoint

Upstream [PR #833](https://github.com/pydantic/pydantic-ai-harness/pull/833), commit
`178fa842`, moved the breakpoint onto the last durable user content. Our pin
includes this change. In `planning/_capability.py`, `_anchor_cache_breakpoint`
searches backward for a `UserPromptPart`; it does not anchor on a `ToolReturnPart`.
The temporary reminder is then appended in `wrap_model_request`.

```text
[user prompt] EXPLICIT CACHE [growing tool-loop history] [temporary plan]
```

The explicit prefix really is reusable now. However, during a long tool loop the
last user prompt can remain far behind the latest tool results. Protecting that
older prefix is not the same as making the entire growing conversation reusable.
Other capabilities that inject user parts can affect where the anchor lands.

### Interaction with Anthropic automatic caching

Pcode also enables Anthropic's automatic caching, which advances a breakpoint to
the last cacheable block:

```text
[user prompt] EXPLICIT CACHE [tool-loop history] [temporary plan] AUTOMATIC CACHE
```

On the next request, the old user-prompt prefix still matches. But the previous
automatic entry includes a reminder that has disappeared from its old position.
That entry no longer matches the growing conversation, so the tail can be written
again instead of reused.

Saved usage showed this pattern: cache-read tokens stayed fixed across long
sequences of requests while cache-write tokens grew with the conversation.
Request fingerprints also showed the previous temporary tail disappearing.
This supports the planning explanation; it is not evidence that every miss has
the same cause. Cache writes are not free reads: see Anthropic's current pricing
in its [prompt caching guide](https://platform.claude.com/docs/en/build-with-claude/prompt-caching).

## Pcode's fix: append only when the plan changes

[`IdentifiedPlanning`](../src/pcode/planning.py) appends a durable snapshot in
`before_model_request`, whose messages are persisted, and bypasses upstream's
ephemeral planning wrapper for every provider:

```text
Request 1: [history] [plan revision 1]
Request 2: [history] [plan revision 1] [new response + tool results]
Request 3: [history] [plan revision 1] [new response + tool results] [plan revision 2]
```

Earlier content stays in place. Unchanged plans add no reminder; changed plans
append an update that supersedes earlier snapshots. Clearing a previously shown
plan appends an explicit empty-plan update.

Deduplication compares the text of the last reminder actually sent, found by
scanning for a `UserPromptPart` that starts with the tag. Storing a marker in
`ModelRequest.metadata` looks equivalent but is not: Pydantic AI merges
consecutive requests when history is resumed and keeps only its reserved
`__pydantic_ai__` namespace (`_agent_graph.py`), so an application marker
survives within a run and disappears on the next one. Session records showed the
result -- one duplicate reminder per turn, since every turn looked like the first.
Only a part that *starts* with the tag counts, so a tool result quoting the tag
is not mistaken for a sent reminder.

The tradeoff is retaining old plan revisions. Appending only changes limits the
extra context, but the latest plan can become distant during a long tool loop.
The model can use `read_plan` to retrieve it. This fix does not add a new compaction
policy, usage accounting, request budgets, or delegated-agent cache settings.
Other providers' ephemeral limit-warning behavior is also unchanged.

## Codex and verification limits

The old planning mutation also applied to `openai-codex`. Pcode disables explicit
cache breakpoints for that route; reuse is server-managed. Append-only history
removes this source of prefix changes without requiring explicit marker support.
The [OpenAI caching guide](https://developers.openai.com/api/docs/guides/prompt-caching)
explains prefix reuse, but public API pricing and retention rules do not establish
Codex subscription quota accounting or guarantee hits on that endpoint.

Regression coverage checks request structure, not simulated cache-hit counts:

- [`test_prompt_cache.py`](../tests/test_prompt_cache.py) checks Anthropic cache
  controls reach the wire.
- [`test_reminders.py`](../tests/test_reminders.py) checks
  Anthropic message-prefix stability across plan changes, clearing,
  and saved resume. Separate warning cases preserve the existing route-specific
  behavior.
- [`test_planning_cache.py`](../tests/test_planning_cache.py) checks native Codex
  wire-prefix stability, unchanged-plan deduplication, and branch-history handling.

Live confirmation requires observing cache reads advance with the conversation
while writes mostly track new content, rather than repeatedly tracking the whole
tail since the last user prompt. A passing wire test does not prove provider-side
reuse. See [prompt cache notices](../docs/context.md#prompt-cache-notices)
for what the footer and the saved session report, and [reading a cache notice](#reading-a-cache-notice)
for the fingerprint comparison and dumps.

## Delegated runs

A sub-agent inherits the parent's *model object* but is run with
`model_settings=None` (`subagents/_toolset.py`), and cache settings live on the
agent, not the model. A delegated Anthropic run therefore asked for no caching
at all. `ProviderCacheSettings` in [`cache_settings.py`](../src/pcode/cache_settings.py)
applies them per request instead, for the parent's sub-agents and across a
mid-session model switch, deferring to any setting already on the request.
Only Anthropic needs this: Codex caches server-side, so it passes through
untouched.

Three things about delegation are easy to get backwards, and each has a test:

- **Sub-agents do not receive the parent's per-run capabilities**, only
  `shared_capabilities`, so child requests were absent from the step store.
  `AgentRuntime._persist_child_runs` adds `StepPersistence` there per turn, since
  the store changes with `/new`. Child runs are marked by `parent_run_id`; the
  cache report scores them separately, because a sub-agent's history is a
  different conversation and mixing the two reports a rewrite at every hand-off.
- **`SavedSession.recover()` must skip delegated runs.** They share the store,
  and a sub-agent's history is not the conversation to resume.
- **A per-delegation `usage_limits` isolates request counts, not tokens.** Child
  tokens still reach the parent's `result.usage`, so adding
  `DelegationEndEvent.usage` to session totals double-counts every delegated
  token. The budget exists so an unattended child is stopped with a steering
  message instead of aborting the turn -- without it, a child hitting a shared
  limit raises through the parent. `SubAgent(usage_limits=None)` is not "no
  limit": the child shares the parent's counter *and* gets the library's
  50-request default, so `pcode.ext.subagent` fills one in.

## Counting what was actually spent

Usage used to be read from `AgentRunResultEvent`, which never arrives for a turn
that fails, is cancelled, or is retried, so every request the provider had
already billed went unrecorded. No streamed event carries usage, but the
`after_model_request` capability hook fires once per model response, so
[`token_accounting.py`](../src/pcode/token_accounting.py) records each request as
it completes. That hook is a filter, not a listener: it must return the response,
or the run continues with nothing where the model's reply should be.

The same capability is installed on `SubAgents.shared_capabilities`, so a
delegated request is counted where it happens. Nothing may add
`AgentRunResult.usage` or `DelegationEndEvent.usage` on top of it. Two sources
still need explicit handling because they are separate agent runs that never
reach the hook: manual `/compact` and the auto-compaction summarizer.

Totals track cache reads and writes separately, since a write costs more than an
uncached token and a read a fraction of one; `/status` shows the split.
`SessionInfo` gained defaulted fields, so a session written before this still
loads.

## When compaction itself is too large

Nothing upstream bounds the summary request: Harness caps each tool return but
renders text parts and tool-call arguments whole, and never measures the prompt
against a context window. An oversized history can therefore produce an
oversized summary request, and compaction fails at the one moment it has to
succeed.

`summarize()` now runs a `FallbackCompaction` chain: the first attempt keeps
evidence readable (16,000 chars per tool return), and each retry sends less,
pairing a tighter tool-return cap with `ClampOversizedMessages` so one runaway
generation cannot carry the request on its own. The retries use
`TieredCompaction` with `target_tokens=1`, which is never satisfied and is how
both the clamp and summarize tiers are made to run. Provider errors trigger the
fallback; cancellation is not caught, and a genuine outage still surfaces after
every step has been tried.

## Reading a cache notice

Moved from the user reference. Token counts alone cannot say *why* a prefix
stopped matching, so pcode fingerprints every model request of a turn and keeps
a rolling window of the last few. A notice that follows an earlier request in
the same turn gains a one-line comparison, which describes what pcode observed,
not a proven cause:

- `Request fingerprints unchanged`: the tracked instructions, tools, settings,
  and earlier messages match, and new messages were appended. These are
  application-level fingerprints, not the final HTTP payload, so the cause is
  unknown; the measured gap alone does not establish cache expiry.
- `Message N of M changed`: something rewrote history in place, and N is the
  first message that moved. `Instructions changed`, `Tool definitions changed`,
  `Cache settings changed`, and `History shrank` cover changes that sit ahead of,
  or instead of, a message edit.

With `debug` on, the window is dumped to `cache-diagnostics/`. Each entry holds
digests, sizes, part kinds, token counts and breakpoint positions, never prompt
text, which would leak the files and command output the agent read. Compare
consecutive entries to find the message that moved.

The notice itself comes from Harness's
[cache monitor](https://pydantic.dev/docs/ai/harness/warn-on-cache-busts/).

## Reading the provider's verdict from past sessions

Every saved session records `cache_read_tokens` and `cache_write_tokens` per
request, so real traffic supplies the confirmation a wire test cannot, without
credentials or spend. [`scripts/cache_report.py`](../scripts/cache_report.py)
reads those records:

```sh
make cache-report                                  # latest session
uv run python scripts/cache_report.py all -n 10    # recent sessions
uv run python scripts/cache_report.py <id> -v      # per-request tokens
uv run python scripts/cache_report.py all --check  # exit 1 on a regression
```

It reports the share of requests that reused most of the previous request's
input, the pinned-reads signature described above, snapshots that replaced
settled history, and duplicate plan reminders. Output is content-free by
construction: counts, digests, and token totals only.

Two thresholds encode traps rather than preferences. Requests following a prefix
below the provider's minimum cacheable size are excluded, since nothing was
obliged to be cached. Reuse is measured against the previous request's total
input rather than against zero, because the old bug still produced large reads
from a stale prefix -- `read > 0` would have passed throughout.

`tests/test_cache_report.py` builds sessions through the real step store, so a
schema change breaks the test rather than silently producing an empty report.
