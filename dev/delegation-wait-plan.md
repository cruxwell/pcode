# Background delegation: plan

Working notes for letting `delegate_task` run in the background, and for keeping
a parent's prompt cache warm while it waits on sub-agents. Update the checklist in
the same commit as the change.

Built so far: `pcode.background_delegation.BackgroundDelegation` detaches a
foreground `delegate_task` when the user steers (not yet at a deadline, and with
no `run_in_background` flag). The run-end wait it uses is unbounded but also wakes
on steering, and runs inside the ending node's event stream rather than in
`after_node_run`: between nodes Pydantic AI flushes no `ctx.emit` events, so the
child's live activity would freeze on screen. A deadline detach can reuse the same
path by adding a second trigger beside `steering_pending`.

## Why

`anthropic:` and `claude:` both write five-minute cache entries (see
[prompt caching](prompt-caching.md) and
[anthropic-providers](anthropic-providers.md#what-shipped)). An entry lives five
minutes from the *start* of the request that last read or wrote it, so the next
request has to start inside that window, generation time included.

Tool waits already fit. `shell` and `wait_for_job` hand back a job handle after at
most 270 seconds, and every saved `anthropic:` case (eight, 246–275 s apart)
reused the whole prefix. `delegate_task` has no such cap: Harness runs the child
inside the tool call and returns only when it finishes. Every tool-loop gap over
five minutes in the saved sessions is a delegation, 5 to 22 minutes long, and on
`anthropic:` the parent's next request reused 0%, 0% and 39% of its prefix. Before
the 5-minute switch, `claude:` hid this behind its one-hour writes.

A keep-alive ping (`max_tokens: 0`) cannot help `claude:`, because the CLI builds
those requests. The fix has to produce a real parent request inside the window,
which works on every provider.

The same change answers a second question: whether delegation should block at
all. `shell` already lets the model choose foreground or background, and a long
foreground wait turns into a handle. Delegation should work the same way.

## What Harness already provides

`BackgroundTools` (`pydantic_ai_harness/background_tools`, present at the pinned
commit) runs selected tools in the background:

- A tool marked `metadata={'background': 'optional'}` gets a `run_in_background`
  argument, stripped before validation, so the model decides per call.
- A backgrounded call returns at once: "Tool '…' is running in background (task
  `<tool_call_id>`). This call is pending. Do not repeat or poll it; its result will
  arrive automatically." Its instructions tell the model to continue with
  independent work or end its response.
- The result arrives through `ctx.enqueue`, prefixed "Background tool '…' (task …)
  completed. Result:", at the next model request, or as a redirect when the model
  has ended its response.
- `wrap_run` opens an anyio task group around the run, so no task outlives it and
  cancelling the run cancels them.
- `after_node_run` delivers finished results, and when the model ends its response
  with tasks still live, waits for the next one so the pending-message drain turns
  it into another model turn.

That covers detaching, delivery, run end and cancellation. The task id is the tool
call id, and Harness emits `DelegationEndEvent` through the tool's `RunContext`,
which stamps that same id, so the end of a background child lands on the right
delegate row. `ctx.emit` writes into the run's event buffer, which is alive for as
long as the task group is.

## Decisions

- **Foreground stays the default.** Most delegations are "do this and report
  back", and the model needs the result to continue. A shared-mode worker edits
  the parent's live files, so a parent working beside it is how edits collide.
  `shell` makes the same choice.
- **`run_in_background` is the opt-in**, through `BackgroundTools`' optional mode.
  It suits isolated workers and read-only delegates (research, review) while the
  parent does independent work.
- **A foreground wait is bounded by the cache window.** If the child is still
  running 270 seconds after the parent's last request started, the call returns
  the same "running in background" message and the child carries on as a
  background task. This is the `shell` rule, measured from the request start
  instead of the tool's start.
- **The run-end wait is bounded the same way.** Upstream waits for the next result
  indefinitely. pcode waits until the deadline, then enqueues a short status note,
  so the model gets a cheap turn (it ends its response again) and the cache stays
  warm. The model never polls; the rounds come from pcode.
- **No new tools and no `d` ids.** Results arrive by themselves. `wait_for_job` and
  `stop_job` stay about processes.
- **Every provider.** The extra rounds are cheap everywhere, and `claude:` needs no
  special handling (below).
- **The same deadline bounds `shell` and `wait_for_job`**, replacing their fixed 270
  seconds from the tool's own start, so a long generation before the call cannot
  push the next request past the window.

## What the model sees

`delegate_task` gains `run_in_background` (boolean, default false), described by
upstream as "Set to true to keep working and get the result later as a follow-up
message." pcode's delegate instructions add when to use it: independent work only,
and never beside a shared-mode worker on the same files.

A foreground call that outlives the window returns upstream's background message
unchanged, so both paths read the same. A run that ends with children still
running gets, every four and a half minutes until they finish:

```text
Still running in background: delegate_task (task toolu_01…, worker "fixing the
/btw cache prefix", 9m 40s). Its result will arrive automatically; end your
response to keep waiting.
```

## How it works

**Deadline.** A small capability records `monotonic()` in `before_model_request`,
keyed by run id. `wait_budget(ctx)` returns `request_start + 270 s - now`, floored
at about 20 seconds so a long generation does not turn into an instant empty
round. 270 leaves 30 seconds for sibling tools, hooks and the network before the
next request starts. For `claude:` the recorded start is when pcode hands the turn
to the CLI, which is close enough. `JobShellToolset._wait_seconds` takes
`min(timeout, wait_budget(ctx))`.

**`BackgroundDelegation(BackgroundTools)`**, installed on the main agent (not on
`shared_capabilities`; sub-agents do not delegate):

- Selects `delegate_task` as `'optional'`, either by name in `_background_mode` or
  by adding `metadata={'background': 'optional'}` in the delegate tool's prepare
  hook.
- Overrides `wrap_tool_execute` for foreground `delegate_task` calls. It starts
  the handler in the run's task group, exactly as upstream's `_run` does, but the
  task first offers its outcome to the waiting call. Finished within
  `wait_budget(ctx)`: the call returns the raw result or raises the raw exception,
  so hooks and the UI see a normal tool call. Otherwise the call returns the
  background message, and the task later sends its formatted outcome down
  upstream's stream, with upstream's tool-call reservation and `_live` count.
- Overrides `after_node_run`: at `End` with live tasks and nothing pending, wait for
  an outcome for `wait_budget(ctx)`; on timeout, enqueue the status note. It keeps
  its own map of live delegations (call id, agent, purpose, start) for the note.
- `for_run` already copies the capability per run, so this state is per run.

**UI.** `live.py` settles a delegate row on its `FunctionToolResultEvent`, and a
result with no `DelegationEndEvent` currently reads as "Not started". A background
result leaves the row running, with its child activity still arriving, and the
later `DelegationEndEvent` for that call id settles it with the usual
`delegation_detail`. `Workers` does the same (it marks a worker settled on the
delegate's `ToolSummary`). While a run waits at its end, the status line reads
"Waiting for workers", not "Waiting for model". `Workers.end_turn()` needs no
change, since a turn no longer ends under a running child.

**`claude:`.** Nothing provider-specific. The background message releases the
parked MCP handler, so the CLI sends its next request. An enqueued result beside
tool results is queued input the CLI sends with them. A run-end redirect is a new
user message on the live process.

## Cost

Each extra round is a real request: a cache read of the prefix plus a short
reply. On Opus 5.5 with a 150k-token prefix that is about 0.05 × 150k = 7.5k
input-token units plus a few hundred output tokens at 5x, against 1.25 × 150k ≈
190k to rewrite the prefix after an expired wait. A 20-minute delegation costs
four rounds, roughly a sixth of one rewrite.

## Upstream candidates

Both pcode additions reach into `BackgroundTools`' private task group, stream and
counters. Each would be a small, general option upstream, and would remove that
coupling:

- A foreground timeout: run normally, detach if still running after N seconds
  (for example `metadata={'background': 'optional', 'detach_after': 270}` or a
  callable, since pcode's deadline depends on the request start).
- A bound on the run-end wait that enqueues a status message on expiry, for any
  caller whose model cache has a TTL.

Until then, pin the private behavior with tests so a Harness upgrade fails loudly.

## Risks and open questions

- **Model behavior.** A model may background a shared-mode worker and then edit
  the same files, or background work it needs straight away. Check on Opus 5.5
  and a Codex model before shipping, and consider refusing `run_in_background` for
  shared-mode workers if it goes wrong.
- **Hooks skipped.** Upstream's later result "does not pass through tool-result or
  tool-error hooks". Check which of pcode's `after_tool_execute` /
  `wrap_tool_execute` capabilities matter for a delegate result (output limits,
  inspection capture, token accounting) and apply them in the task instead.
- **Error detail.** Upstream shows only the exception type when a background tool
  fails. Harness's delegation already turns child failures into steering text or
  `ModelRetry` (which upstream keeps), so this should rarely bite; confirm with a
  crashing child.
- **Resume.** A pcode crash or `/continue` mid-delegation loses the child, as today,
  but the history now holds a "result will arrive automatically" message that never
  gets one. A resumed session may need a note saying background work from the
  previous run was lost.
- **Side questions and compaction** run against settled prefixes and summaries;
  check that a pending background message in either does not confuse them.

## Tests

- A scripted child held open with `gate()`/`release()`, never `sleep`, and a
  deadline shrunk for the test: a foreground call finishing in time returns the
  raw result; one outliving the deadline returns the background message, and its
  result arrives with the next request.
- `run_in_background=true` returns at once and delivers later, through pcode's
  subclass.
- A model that ends its response under a live child is redirected with the status
  note at the deadline, and again with the result when it finishes.
- Ctrl+C with a background child cancels it and leaves no pending task.
- The delegate row stays running across the background message and settles on the
  end event, in-process and over `make test-socket`.
- The deadline shortens `shell` waits after a long generation.
- Pins on the upstream internals the subclass relies on.

## Verification

After it lands, `cache_report.py` on a session with a delegation over five minutes
should show the parent's post-delegation request reading at least 75% of the
previous input on `anthropic:` and `claude:`, with the extra rounds visible as
short requests.

## Checklist

- [ ] Wait deadline capability, used by `shell` and `wait_for_job`
- [x] `BackgroundDelegation`: foreground detach on steering
- [ ] `BackgroundDelegation`: optional background, foreground detach at the deadline
- [x] Run-end wait that wakes on steering and keeps child events flowing
- [ ] Bounded run-end wait with status notes
- [x] Delegate rows and status line for detached children
- [ ] `/agents` for background children
- [ ] Hooks the background path skips, applied where they matter
- [ ] Live check of model behavior on Opus 5.5 and Codex
- [ ] Cache verification on a real long delegation
- [ ] Propose the foreground timeout and bounded run-end wait upstream
