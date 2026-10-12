"""Plan mode: think a change through with the user before building it.

`/plan [request]` turns it on and sends the request, if any, as the first
prompt (its opening words also name the plan file). While it is on, each user
turn carries a reminder to investigate, keep a plan in
`.pcode/plans/<slug>.md`, and hold off on implementing until the user approves.
Approval is the model's call: when the user says "go ahead" (in whatever
words), or answers the last open questions without raising new ones, it calls
`exit_plan_mode` and starts building in the same turn.
`/plan off` is the manual way out.

The model is trusted rather than fenced in: nothing blocks edits, the reminder
just says what the user wants. The active plan lives in a marker file beside
the plans, so it survives `/reload` (which re-imports this module) and restarts.

With the `basic_memory` extension on and the workspace in one of its orgs, the
plan is a note in that org's vault instead (`briefs/Plan — <project> — <title>.md`),
in the brief format, and on approval the model marks it approved there. The
marker then holds the note's absolute path rather than a slug.
"""

import re
from pathlib import Path

from pydantic_ai import ModelRetry
from pydantic_ai.messages import ModelRequest, ToolReturnPart, UserPromptPart

from pcode.steering import run_request

PLANS = ".pcode/plans"
ACTIVE = ".active"

REMINDER = """\
<plan-mode>
Plan mode is on. The user wants to agree on a plan before any implementation.
- Investigate freely: read files, search, run read-only commands, research.
- Write the plan to `{path}` and keep it current as the discussion moves on.
  The user may edit that file between turns, so re-read it before revising.
- Do not start implementing. A small throwaway experiment to answer a question
  is fine; making the change itself is not.
- End each turn with the open questions or a clear "ready to proceed?".
- When the user approves, call `exit_plan_mode`, then implement the plan in
  that same turn. Approval can be explicit ("go ahead", "lgtm", "ship it") or
  implicit: if the user's reply answers every remaining open question and
  raises no new concerns, that is the go-ahead; don't ask again for a
  separate confirmation. Approval with changes ("yes, but skip step 3")
  counts too: fold the change in, then exit. Stay in plan mode only while
  something is genuinely unresolved or the user asks to keep planning.{extra}
</plan-mode>"""


def is_worker(ctx) -> bool:
    """A shared delegate runs this extension's hooks too; plan mode is the parent's."""
    return getattr(ctx.agent, "name", None) == "worker"


def slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    if len(slug) > 60:  # Cut at a word boundary when there is one.
        cut = slug[:61]
        slug = cut.rsplit("-", 1)[0] if "-" in cut else slug[:60]
    return slug or "plan"


def topic_slug(request: str) -> str:
    """Name the plan after the request's opening words."""
    return slugify(" ".join(request.split("\n", 1)[0].split()[:8]))


def setup(pcode) -> None:
    if pcode.is_worker:
        return
    plans = pcode.workspace / PLANS
    marker = plans / ACTIVE
    from pcode.extensions.basic_memory import plan_location

    location = plan_location(pcode.workspace)  # None: plans stay in .pcode/plans

    def active() -> str | None:
        try:
            return marker.read_text().strip() or None
        except FileNotFoundError:
            return None

    def is_vault(key: str) -> bool:
        # A vault plan's marker holds its absolute path; a local one, its slug.
        return Path(key).is_absolute()

    def path_of(key: str) -> Path:
        return Path(key) if is_vault(key) else pcode.workspace / PLANS / f"{key}.md"

    def relative(key: str) -> str:
        if is_vault(key):
            path = Path(key)
            return f"~/{path.relative_to(Path.home())}" if Path.home() in path.parents else key
        return f"{PLANS}/{key}.md"

    def latest() -> str | None:
        # Only local plans: a vault holds every repo's plans, so "the newest" could
        # be another project's.
        if location:
            return None
        files = (
            sorted(plans.glob("*.md"), key=lambda p: p.stat().st_mtime) if plans.is_dir() else []
        )
        return files[-1].stem if files else None

    def new_key(request: str) -> str:
        return str(location.path_for(request)) if location else topic_slug(request)

    def start(slug: str) -> None:
        plans.mkdir(parents=True, exist_ok=True)
        ignore = plans / ".gitignore"
        if not ignore.exists():
            ignore.write_text("*\n")
        marker.write_text(slug + "\n")

    def plan_command(argument: str) -> str | None:
        argument = argument.strip()
        current = active()
        if argument == "off":
            if current is None:
                pcode.ui.notify("Plan mode is already off.")
                return
            marker.unlink(missing_ok=True)
            pcode.ui.notify(f"Plan mode off. The plan stays in {relative(current)}.")
            return
        if argument == "status":
            pcode.ui.notify(
                f"Plan mode on: {relative(current)}" if current else "Plan mode is off."
            )
            return
        slug = new_key(argument) if argument else (current or latest() or new_key("plan"))
        start(slug)
        exists = path_of(slug).exists()
        verb = "Resuming" if exists else "Planning in"
        ask = "Say" if argument else "Describe the change; say"
        pcode.ui.notify(
            f"Plan mode on. {verb} {relative(slug)}. {ask} 'go ahead' "
            "(or just answer its questions) when the plan looks right."
        )
        # The text is the request itself, so it goes to the model now rather
        # than making the user send it again.
        return argument or None

    pcode.register_command(
        "/plan",
        "Plan before implementing; the model exits when you approve",
        plan_command,
        arguments=("off", "status"),
        free_arguments=True,  # anything else is the request to plan
        argument_descriptions={
            "off": "Leave plan mode (the plan file is kept)",
            "status": "Show whether plan mode is on and its file",
        },
    )

    @pcode.tool
    def exit_plan_mode() -> str:
        """Leave plan mode once the user has approved the plan, then implement it.

        Call this only when plan mode is on and the user has approved, either by
        saying so or by answering every open question without raising new ones.
        """
        current = active()
        if current is None:
            return "Plan mode was not on; carry on."
        marker.unlink(missing_ok=True)
        pcode.ui.notify("Plan approved; plan mode off.")
        result = (
            f"Plan mode off. Implement the plan in {relative(current)} now, "
            "tracking progress with write_plan, and update the file if the plan changes."
        )
        if is_vault(current):
            result += (
                " First mark it approved, as the brief format says: `state: approved`, "
                "`cssclasses: [approved]`, the APPROVED banner, and ✅ on the brief's Open plans "
                "line. When the work ships, fold it into the brief and mark the plan implemented."
            )
        return result

    @pcode.hooks.on.before_tool_execute
    async def workers_cannot_exit(ctx, *, call, tool_def, args):
        if tool_def.name == "exit_plan_mode" and is_worker(ctx):
            raise ModelRetry("Only the main agent can leave plan mode.")
        return args

    @pcode.hooks.on.before_model_request
    async def remind(ctx, request_context):
        # Once per user turn, persisted in history so the prompt prefix stays
        # cached. A retried turn resends the same request, so skip one that
        # already carries the reminder or is mid-turn (holds tool results).
        current = active()
        if current is None or is_worker(ctx):
            return request_context
        request = run_request(ctx, request_context)
        parts = request.parts if isinstance(request, ModelRequest) else ()
        prompts = [p for p in parts if isinstance(p, UserPromptPart)]
        if (
            prompts
            and not any(isinstance(p, ToolReturnPart) for p in parts)
            and not any(str(p.content).startswith("<plan-mode>") for p in prompts)
        ):
            extra = f"\n{location.guidance}" if location and is_vault(current) else ""
            parts.append(UserPromptPart(REMINDER.format(path=relative(current), extra=extra)))
        return request_context
