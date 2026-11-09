"""Manager mode: coordinate workers instead of writing the code yourself.

`/manager [on [request]|off|status]` switches it, and so can the model with
`enter_manager_mode(reason)` when a task is large and separable. While it is
on, the main conversation:

- gets a reminder each user turn (the manager's rules, plus the decisions log
  and lessons from `.pcode/manager/`);
- cannot `edit_file`/`write_file` outside `.pcode/` (it delegates instead);
- must give `delegate_task` a `context` or `done_when`;
- cannot complete a plan step linked to a worker until `review_worker`
  accepted that worker;
- spends at most `manager_token_budget` worker tokens, runs workers on
  `manager_worker_model` by default, and isolates workers in worktrees when
  the `worktree` preference is on.

The tools are registered whether or not the mode is on, so the tool list (and
the cached prompt prefix) never changes. State is a marker file, so it
survives `/reload` (which re-imports this module) and restarts. Side
questions and workers are never affected. See dev/manager-mode.md.
"""

from pathlib import Path
from typing import Literal

from pydantic_ai import ModelRetry, RunContext, Tool
from pydantic_ai.capabilities import Capability
from pydantic_ai.messages import ModelRequest, ToolReturnPart, UserPromptPart

import pcode.isolated_delegation as iso
from pcode import worker_channel
from pcode.extensions.plan_mode import is_side
from pcode.steering import run_request

MANAGER = ".pcode/manager"
ACTIVE = ".active"
DECISIONS = "decisions.md"
LESSONS = "lessons.md"
# Each file is cut here in the reminder; the model can read the rest.
FILE_LIMIT = 4000
EDIT_TOOLS = {"edit_file", "write_file"}
PLAN_TOOLS = {"update_task_status", "update_task_statuses", "write_plan"}
DELEGATE = "delegate_task"
OWN_TOOLS = {"enter_manager_mode", "exit_manager_mode"}

INSTRUCTIONS = """\
Manager mode makes you a coordinator: workers do the implementation through \
`delegate_task` and you plan, brief, verify and integrate. Switch it on yourself \
with `enter_manager_mode(reason)` for a large task with several loosely coupled \
pieces, for a long task whose exploration would flood your context, or when the \
user asks for it. Do not use it for small changes or tightly coupled work that \
one agent does better in one pass. When you switch it on, say so and why in \
visible text. Call `exit_manager_mode` when the managed work is done or the user \
asks. `review_worker` records your verdict on a finished worker's result."""

RULES = """\
- You are the manager. Delegate implementation with `delegate_task`; do not \
edit files yourself (writes under `.pcode/` are allowed). Read, search and run \
read-only commands freely to plan and to verify.
- Write briefs a worker can act on alone: the goal in `task`, plus `context` \
(files, earlier decisions), `boundaries` (what not to touch) and `done_when` \
(how it knows it is finished). Pass the relevant decisions below into each brief.
- Track the work with the plan tools and link each worker to its step with \
`plan_step`. Use `run_in_background` for independent pieces, and \
`include_reports` to hand earlier results to a dependent step.
- Verify every worker result before relying on it: read its diff, run a focused \
test, or delegate a reviewer. Then call `review_worker` (accepted or rejected, \
with a note). A linked plan step cannot be completed until its worker is accepted.
- Record choices other workers must stay consistent with in \
`{decisions}`, one line each.
- Scale the effort to the task: one worker for tightly coupled work, several only \
for pieces that do not touch each other.{extra}"""

REMINDER = "<manager-mode>\nManager mode is on.\n{rules}{files}\n</manager-mode>"

ISOLATION = (
    "\n- Workers run in isolated worktrees: commit a checkpoint before delegating, "
    "then bring each accepted worker's branch in with `integrate_task`."
)


def _excerpt(path: Path) -> str:
    try:
        text = path.read_text().strip()
    except OSError, UnicodeDecodeError:
        return ""
    if len(text) > FILE_LIMIT:
        text = text[:FILE_LIMIT] + f"\n[truncated; read {path.name} for the rest]"
    return text


def _field(item, name: str):
    """A plan item's field, whether the validated args hold models or dicts."""
    return item.get(name) if isinstance(item, dict) else getattr(item, name, None)


def _status(value) -> str:
    return str(getattr(value, "value", value) or "")


def setup(pcode) -> None:
    if pcode.is_worker:
        return
    workspace = Path(pcode.workspace)
    directory = workspace / MANAGER
    marker = directory / ACTIVE
    # The hub as last seen by a hook, for `/manager status` (commands get no
    # run context). Workers are memory only, so a reload rightly forgets it.
    seen: dict[str, worker_channel.WorkerHub] = {}
    # The run that already got this turn's reminder; later requests in the same
    # run (tool results, background results, questions, steering) do not.
    reminded: dict[str, str] = {}

    def reason() -> str | None:
        try:
            return marker.read_text().strip() or "on"
        except FileNotFoundError:
            return None

    def active() -> bool:
        return reason() is not None

    def preference(key: str):
        from pcode.preferences import SETTINGS, load_preferences

        return load_preferences().get(key, SETTINGS[key].default)

    def worktree_on() -> bool:
        return preference("worktree") == "on"

    def budget() -> int:
        value = str(preference("manager_token_budget") or "0")
        return int(value) if value.isascii() and value.isdecimal() else 0

    def start(why: str) -> None:
        directory.mkdir(parents=True, exist_ok=True)
        ignore = directory / ".gitignore"
        if not ignore.exists():
            ignore.write_text("*\n")
        marker.write_text((" ".join(why.split()) or "on") + "\n")
        # Harmless with `worktree` off: isolation_enabled() still requires it.
        iso.force_isolation = True

    def stop() -> None:
        marker.unlink(missing_ok=True)
        iso.force_isolation = False

    # Lessons belong to the project, so they live in its main checkout: a
    # session worktree's `.pcode/` goes when the worktree is merged and removed.
    # Decisions are per task and may go with it; lessons may not.
    from pcode.worktree import repo_scope

    lessons_dir = repo_scope(workspace) / MANAGER
    shared_lessons = lessons_dir != directory.resolve()

    def lessons() -> Path:
        return lessons_dir / LESSONS

    def ensure_lessons_dir() -> None:
        lessons_dir.mkdir(parents=True, exist_ok=True)
        if not (ignore := lessons_dir / ".gitignore").exists():
            ignore.write_text("*\n")

    def relative(name: str) -> str:
        if name == LESSONS and shared_lessons:
            return str(lessons())
        return f"{MANAGER}/{name}"

    def rules() -> str:
        extra = ISOLATION if worktree_on() else ""
        return RULES.format(decisions=relative(DECISIONS), extra=extra)

    def reminder() -> str:
        files = ""
        for name, path, heading in (
            (DECISIONS, directory / DECISIONS, "Decisions so far"),
            (LESSONS, lessons(), "Lessons"),
        ):
            if text := _excerpt(path):
                files += f"\n\n{heading} ({relative(name)}):\n{text}"
        return REMINDER.format(rules=rules(), files=files)

    def status() -> str:
        why = reason()
        lines = [f"Manager mode on: {why}" if why else "Manager mode is off."]
        hub = seen.get("hub")
        handles = hub.handles() if hub is not None else []
        if handles:
            lines.append("Workers:")
            for handle in handles:
                review = f", {handle.review}" if handle.review else ""
                step = f", step {handle.plan_step}" if handle.plan_step else ""
                lines.append(
                    f"  {handle.id} {handle.status}{review}{step}, {handle.tokens:,} tokens"
                    f" ({handle.purpose or ' '.join(handle.task.split())[:60]})"
                )
        else:
            lines.append("Workers: none yet.")
        spent = total(hub)
        limit = budget()
        lines.append(
            f"Worker tokens: {spent:,} of {limit:,}"
            if limit
            else f"Worker tokens: {spent:,} (no budget; set manager_token_budget)"
        )
        model = preference("manager_worker_model")
        lines.append(f"Worker model: {model or 'the session model'}")
        if not worktree_on():
            lines.append("Worker isolation: off (the worktree preference is off).")
        else:
            lines.append(
                "Worker isolation: " + ("on" if why and iso.force_isolation else "off") + "."
            )
        return "\n".join(lines)

    def manager_command(argument: str) -> str | None:
        argument = argument.strip()
        word, _, rest = argument.partition(" ")
        if word == "status" and not rest:
            pcode.ui.notify(status())
            return None
        if word == "off" and not rest:
            if not active():
                pcode.ui.notify("Manager mode is already off.")
                return None
            stop()
            pcode.ui.notify("Manager mode off.")
            return None
        request = rest.strip() if word == "on" else argument
        if not active():
            start("turned on by the user")
        pcode.ui.notify(
            "Manager mode on: the agent delegates implementation to workers and reviews "
            "their results. /manager off to leave, /manager status to check on it."
        )
        return request or None

    pcode.register_command(
        "/manager",
        "Coordinate workers instead of editing directly",
        manager_command,
        arguments=("on", "off", "status"),
        free_arguments=True,  # anything else is the request to manage
        argument_descriptions={
            "on": "Turn manager mode on; text after it is sent as the request",
            "off": "Leave manager mode",
            "status": "Show the mode, workers, budget and isolation",
        },
    )

    def enter_manager_mode(reason: str) -> str:
        """Switch to manager mode: delegate implementation to workers and verify their results.

        Use it for a large task with several loosely coupled pieces, a long task
        whose exploration would flood your context, or when the user asks. Not for
        small or tightly coupled changes.

        Args:
            reason: One line telling the user why manager mode suits this task.
        """
        if active():
            return "Manager mode is already on; carry on."
        start(reason)
        pcode.ui.notify(f"Manager mode on: {' '.join(reason.split())}")
        return (
            "Manager mode is on. Tell the user in visible text that you switched to manager "
            "mode and why, then continue under these rules:\n" + rules()
        )

    def exit_manager_mode() -> str:
        """Leave manager mode once the managed work is done or the user asks."""
        if not active():
            return "Manager mode was not on; carry on."
        stop()
        ensure_lessons_dir()
        pcode.ui.notify("Manager mode off.")
        return (
            "Manager mode off; you may edit files directly again. Before moving on, append "
            f"2-4 short lessons from this run to {relative(LESSONS)} (briefs that failed and "
            "why, splits that worked), one bullet each, so the next managed task starts "
            "smarter."
        )

    async def review_worker(
        ctx: RunContext,
        worker_id: str,
        verdict: Literal["accepted", "rejected"],
        note: str = "",
    ) -> str:
        """Record your verdict on a finished worker's result after verifying it.

        Verify first: read its diff, run a focused test, or delegate a reviewer.
        A plan step linked to the worker can be completed only once it is accepted.

        Args:
            worker_id: The worker, such as w3.
            verdict: accepted if the result is right, rejected if it needs rework.
            note: What you checked, or what is wrong.
        """
        hub = worker_channel.hub_for(ctx)
        if hub is None:
            raise ModelRetry("There are no workers to review here.")
        handle = hub.require(worker_id)
        if handle.running:
            raise ModelRetry(f"Worker {handle.id} is still running; review it once it finishes.")
        handle.review = verdict
        handle.review_note = note.strip()
        await worker_channel.notify(handle, ctx, review=verdict)
        if verdict == "accepted":
            step = (
                f" Plan step {handle.plan_step} can now be completed." if handle.plan_step else ""
            )
            return f"Worker {handle.id} accepted.{step}"
        return (
            f"Worker {handle.id} rejected. Send the fix as a follow-up (continue_worker for a "
            "shared worker, or a new delegation with the note in its context), then review again."
        )

    pcode.add_capability(
        Capability(
            id=pcode.id,
            tools=[
                Tool(enter_manager_mode, takes_ctx=False),
                Tool(exit_manager_mode, takes_ctx=False),
                Tool(review_worker, takes_ctx=True),
            ],
            instructions=INSTRUCTIONS,
        )
    )

    def side(ctx) -> bool:
        return is_side(ctx) or worker_channel.current_worker() is not None

    def remember(ctx) -> worker_channel.WorkerHub | None:
        hub = worker_channel.hub_for(ctx)
        if hub is not None:
            seen["hub"] = hub
        return hub

    def total(hub) -> int:
        return hub.total_tokens() if hub is not None else 0

    def under_pcode(path: str) -> bool:
        target = Path(path).expanduser()
        target = (target if target.is_absolute() else workspace / target).resolve()
        return target.is_relative_to((workspace / ".pcode").resolve())

    def check_delegation(hub, args: dict) -> dict:
        limit = budget()
        if limit and (spent := total(hub)) >= limit:
            pcode.ui.notify("Manager token budget spent; new delegations refused.", "warning")
            raise ModelRetry(
                f"The manager token budget ({limit:,} worker tokens) is spent "
                f"({spent:,} used). Do not delegate more: finish with what the "
                "workers produced, and tell the user they can raise manager_token_budget."
            )
        resuming = bool(args.get("continue_worker"))
        brief = any(str(args.get(key) or "").strip() for key in ("context", "done_when"))
        if not resuming and not brief:
            raise ModelRetry(
                "In manager mode every brief needs `context` (relevant files, earlier "
                "decisions, constraints) or `done_when` (how the worker knows it is "
                "finished), ideally both, plus `boundaries` for what it must not touch. "
                "Resend delegate_task with them."
            )
        model = str(preference("manager_worker_model") or "").strip()
        agent = str(args.get("agent_name") or "worker")
        if model and agent == "worker" and not str(args.get("model") or "").strip():
            args = {**args, "model": model}
        return args

    def check_plan(hub, name: str, args: dict) -> None:
        if hub is None:
            return
        if name == "update_task_status":
            changes = [(args.get("task_id"), args.get("status"))]
        elif name == "update_task_statuses":
            changes = [
                (_field(u, "task_id"), _field(u, "status")) for u in args.get("updates") or ()
            ]
        else:
            changes = [(_field(i, "id"), _field(i, "status")) for i in args.get("items") or ()]
        for step, state in changes:
            if not step or _status(state) != "completed":
                continue
            linked = sorted(
                (h for h in hub.handles() if h.plan_step == str(step)), key=lambda h: h.claimed
            )
            # Any linked worker still running blocks the step; otherwise the
            # newest attempt decides, so an accepted retry clears a rejection.
            running = [h for h in linked if h.running]
            worker = running[0] if running else linked[-1] if linked else None
            if worker is not None and (running or worker.review != "accepted"):
                raise ModelRetry(
                    f"Plan step {step} is linked to worker {worker.id}, which is not accepted "
                    f"yet ({worker.status}, review {worker.review or 'pending'}). Verify its "
                    f"result, then call review_worker('{worker.id}', 'accepted', note) before "
                    "completing the step."
                )

    @pcode.hooks.on.before_tool_execute
    async def guard(ctx, *, call, tool_def, args):
        name = tool_def.name
        if side(ctx):
            if name in OWN_TOOLS:
                raise ModelRetry("Only the main agent can switch manager mode.")
            return args
        hub = remember(ctx)
        if not active() or not isinstance(args, dict):
            return args
        if name in EDIT_TOOLS and not under_pcode(str(args.get("path", ""))):
            raise ModelRetry(
                "Manager mode is on: delegate this change with delegate_task instead of "
                "editing it yourself (only files under .pcode/ are yours to write)."
            )
        if name == DELEGATE:
            return check_delegation(hub, args)
        if name in PLAN_TOOLS:
            check_plan(hub, name, args)
        return args

    @pcode.hooks.on.before_model_request
    async def remind(ctx, request_context):
        # Once per user turn, persisted in history so the prompt prefix stays
        # cached; skip mid-turn requests and a retry that already carries it.
        if side(ctx):
            return request_context
        remember(ctx)
        if not active():
            return request_context
        run_id = getattr(ctx, "run_id", None)
        if run_id and reminded.get("run") == run_id:
            return request_context
        reminded["run"] = run_id or ""
        request = run_request(ctx, request_context)
        parts = request.parts if isinstance(request, ModelRequest) else ()
        prompts = [p for p in parts if isinstance(p, UserPromptPart)]
        if (
            prompts
            and not any(isinstance(p, ToolReturnPart) for p in parts)
            and not any(str(p.content).startswith("<manager-mode>") for p in prompts)
        ):
            parts.append(UserPromptPart(reminder()))
        return request_context

    # A reload re-imports this module but not isolated_delegation, so resync.
    iso.force_isolation = active()
