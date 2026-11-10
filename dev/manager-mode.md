# Worker channel and manager mode

Design notes for the delegation improvements and `/manager`. The rule that
shaped them: anything that helps any delegating session lives in core
delegation; `/manager` is only a preset plus enforcement on top.

## Why (research summary)

- Orchestration fails mostly at the brief, at verification, and at merging
  results (MAST, arXiv 2503.13657; SwarmBench, arXiv 2608.30661).
- A central coordinator that checks work contains error amplification
  (17.2x independent vs 4.4x centralized; arXiv 2512.08296).
- Splitting only pays on loosely coupled work ("When More Is Less",
  arXiv 2609.19759); information carried between dependent steps matters more
  than agent count (OrchBench, arXiv 2607.25656).
- Delegation's main win on big tasks is context hygiene: compaction loses
  information at arbitrary points, delegation at boundaries the parent picks.

## Core delegation (every session)

Workers are one level deep: a worker cannot delegate, so a worker's parent is
always the top-level session.

`pcode.worker_channel` holds:

- `WorkerHub`: per parent agent (owned by `WorkspaceSubAgents`), memory only.
  Maps short worker ids (`w1`, `w2`, ...) to `WorkerHandle`s and delegate call
  ids to worker ids. Keeps the last `HISTORY` handles.
- `WorkerHandle`: `id`, `call_id`, `agent`, `task`, `purpose`, `plan_step`,
  `workspace_mode`, `status` (`running|done|failed|stopped`), `report`,
  `messages` (the child's history, for follow-ups), `inbox` (parent messages),
  `question` (pending `Question`), `review` (`""|accepted|rejected`),
  `tokens`, `cancel` (set by `BackgroundDelegation` while detached).
- `current_worker()`: the handle of the delegation the caller runs inside
  (a ContextVar set by `delegate_task`), `None` in the parent.
- `hub_for(ctx)`: the parent's hub from its capabilities, or `None`.
- `ChildChannel`: shared capability on every delegate. Tool `ask_parent`,
  delivery of parent messages before each child model request, and the fixed
  report format in its instructions.
- `WorkerUpdate` capability event (`namespace="pcode_workers"`): explicit
  `call_id` (the delegate call), `worker_id`, and whichever of `question`,
  `answered`, `tokens`, `review`, `status` changed. `stream_events` turns it
  into the display-only runtime event `WorkerStatus` (never journaled).

`delegate_task` gains (all optional, schema static so the prompt cache holds):

| Argument | Effect |
| --- | --- |
| `context`, `boundaries`, `done_when` | Rendered into the brief under headings |
| `include_reports: list[str]` | Earlier workers' reports appended to the brief |
| `plan_step` | Parent plan step id; shown on the dashboard row, stored on the handle |
| `run_in_background` | Return at once with the worker id; result arrives later |
| `continue_worker` | Resume a finished shared-mode worker with its own history |

(`background` and `resume` are Harness names it strips without a
`DelegationTasks` owner, hence the different names.)

Every result names its worker id. New parent tools beside `integrate_task`:
`manage_worker(action, worker_id, text)` with action `answer`, `message`,
`stop` or `list`: one tool rather than four, so the rarely used controls cost
one schema. (Deferring them with Pydantic AI's `ToolSearch` was considered: on
`claude:` a discovery changes the tool list, which forks the CLI process and
rewrites the whole prompt cache.)

### How a question reaches the parent

The parent may be (a) waiting in a foreground `delegate_task`, (b) doing other
work, or (c) waiting at the end of its response for detached children.

- (a) `BackgroundDelegation`'s wait loop treats an undelivered question like
  steering: it detaches every waiting delegation, so the parent can make a
  model request.
- (a, b) `WorkspaceSubAgents.before_model_request` appends undelivered
  questions to the next request and marks them delivered.
- (c) `BackgroundDelegation._wait_step` wakes on one and enqueues it.

`ask_parent` waits up to `wait_seconds` (default 600) for `manage_worker(action='answer')`,
else returns "no answer; proceed with your fallback".

The dashboard does not talk to the runtime directly (it may be across a
socket). Its answer/message keys prefill the prompt with
`Answer for w3: ` / `Message for w3: `; the parent's instructions say to relay
those with `manage_worker`.

## /agents dashboard

`Workers` folds `WorkerStatus`; rows show id, plan step, model, state
(running, `? asks parent`, done, done · reviewed, failed), elapsed, tokens and
workspace mode, with a pending-question panel. Keys: enter transcript,
`a` answer, `m` message (both prefill the prompt), `r` report.

## /manager (bundled extension `extensions/manager.py`)

- Opt-in (`DEFAULT_ENABLED = False`): its tools and instructions would sit in
  every session's prompt otherwise.
- `/manager [on|off|status]`; tools `enter_manager_mode(reason)` and
  `exit_manager_mode()` are always registered (stable tool list), so the model
  can switch the mode on itself for large, separable work and tell the user.
- State in `.pcode/manager/.active`, survives `/reload` and restarts.
- Reminder once per run (like plan mode, but not repeated on the run's later
  requests: background results and questions arrive as prompts too).
  No edit guardrail and no required brief fields: what to delegate and what
  to edit itself is the model's call (an earlier version blocked edits and
  forced `context`/`done_when`; that cost prompt and judgement for little gain).
- Review gate: a plan step linked to a worker cannot be completed until
  `review_worker(worker_id, verdict, note)` accepted it.
- Preset: `manager_worker_model` default for `delegate_task.model`, worker
  isolation forced on for the session when `worktree` is on, a token budget
  (`manager_token_budget`) that refuses new delegations once spent.
- Decisions live in the workspace's `.pcode/manager/` (per task); lessons in
  the main checkout's (`repo_scope`), so they outlive a session worktree. Both
  are loaded into the reminder.
- `force_isolation` is reset by `load_extensions` and set again by the
  extension's setup, so `/extensions off manager` + reload clears it.
- Review gate: any running worker linked to the step blocks it; otherwise the
  newest attempt (by `WorkerHandle.claimed`) must be accepted.
