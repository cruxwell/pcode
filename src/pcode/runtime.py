"""Application events and the offline fixture runtime; no terminal imports."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Message:
    markdown: str


@dataclass(frozen=True)
class ToolStarted:
    name: str
    detail: str
    call_id: str
    command: str = ""
    arguments: str | None = None
    run_id: str = ""
    started_at: str = ""
    process_id: str = ""
    parent_call_id: str = ""
    activity: str = ""
    # What the model said it is for. Kept beside `command` rather than folded
    # into it: `command` is rendered as a shell invocation, so it has to stay
    # literal enough to copy and run.
    purpose: str = ""
    # "background" or "foreground" for command tools, "" for everything else:
    # whether the model asked for a job handle or waited on the command.
    execution: str = ""
    # A delegation's sub-agent and its assignment (its stated purpose when it
    # gave one), "" for every other tool.
    # Sessions saved before these existed restore without them.
    agent: str = ""
    task: str = ""


@dataclass(frozen=True)
class ToolSummary:
    name: str
    detail: str
    failed: bool = False
    call_id: str = ""
    elapsed_seconds: float | None = None
    error: str = ""
    command: str = ""
    result: str | None = None
    run_id: str = ""
    outcome: str = ""
    process_id: str = ""
    parent_call_id: str = ""
    purpose: str = ""
    # Background completion notices share the normal command-result rendering.
    execution: str = ""


@dataclass(frozen=True)
class JobFinished(ToolSummary):
    """A delayed command completion, not another tool invocation."""

    execution: str = "background"


@dataclass(frozen=True)
class EditCompleted:
    """Sanitized historical change; raw file snapshots are never journaled."""

    call_id: str
    path: str
    operation: str
    patch: str = ""
    added: int = 0
    removed: int = 0
    truncated: bool = False
    omitted: str = ""


@dataclass(frozen=True)
class EditPreview:
    """Proposed content only, never a committed change or a journal entry."""

    call_id: str
    path: str = ""
    text: str = ""
    # "edit" carries +/- lines for a file; "code" carries a sandboxed snippet
    # that has not run yet. Both are pending tool arguments, never results.
    kind: str = "edit"


@dataclass(frozen=True)
class CommandOutput:
    """Transient sanitized snapshot, never saved in the transcript journal."""

    call_id: str
    command: str
    output: str


@dataclass(frozen=True)
class TextDelta:
    text: str


@dataclass(frozen=True)
class ThinkingDelta:
    """Provider-exposed reasoning text, persisted even when hidden."""

    text: str


@dataclass(frozen=True)
class Thinking:
    """Completion marker for one readable thinking block (never a signature)."""

    text: str


@dataclass(frozen=True)
class CacheBust:
    """An observed cache collapse, not a diagnosis of what changed the prompt."""

    text: str


@dataclass(frozen=True)
class RunStatus:
    text: str


@dataclass(frozen=True)
class PlanUpdated:
    """Authoritative Harness plan snapshot, not a parsed tool summary."""

    items: list[dict]


@dataclass(frozen=True)
class PlanPreview:
    """Display-only streamed plan; None restores the authoritative snapshot."""

    items: list[dict] | None


@dataclass(frozen=True)
class ChildPlan:
    """Display-only snapshot of a running sub-agent's plan, keyed by its delegate call.

    Never journaled: the child's plan lives only as long as its run, and the
    conversation's own plan is `PlanUpdated`.
    """

    call_id: str
    items: list[dict]


@dataclass(frozen=True)
class ChildText:
    """A running sub-agent's own prose or reasoning, keyed by its delegate call.

    Display-only and never journaled: it feeds the worker viewer, never the
    conversation's transcript. `start` opens a new part; otherwise the text
    continues the previous part of the same kind.
    """

    call_id: str
    text: str
    thinking: bool = False
    start: bool = False


@dataclass(frozen=True)
class WorkerStatus:
    """Display-only change to a delegated worker's dashboard row, keyed by its delegate call.

    Never journaled, like `ChildPlan`. Fields left at None did not change.
    """

    call_id: str
    worker_id: str
    question: str | None = None
    fallback: str | None = None
    answered: bool | None = None
    tokens: int | None = None
    review: str | None = None
    status: str | None = None


Event = (
    Message
    | ToolStarted
    | ToolSummary
    | TextDelta
    | ThinkingDelta
    | Thinking
    | RunStatus
    | CacheBust
    | PlanUpdated
    | PlanPreview
    | ChildPlan
    | ChildText
    | WorkerStatus
    | CommandOutput
    | EditCompleted
    | EditPreview
)


class PreviewRuntime:
    def __init__(self) -> None:
        self.turns = 0

    def reset(self) -> None:
        self.turns = 0

    def reply(self, prompt: str) -> tuple[Event, ...]:
        self.turns += 1
        return (
            Message(
                "This is a **local UI preview**, not a connected model. "
                f"Your {len(prompt.splitlines())}-line message arrived intact.\n\n"
                "Try `/theme-preview` for a sample coding response, "
                "`/theme light` to change the palette, "
                "or type `/` to browse commands. Nothing is sent anywhere."
            ),
        )

    def demo(self) -> tuple[Event, ...]:
        self.turns += 1
        return (
            Message("Here’s a **sample coding response**. Tool activity below is fictional."),
            ToolSummary("Read", "example/greeting.py · 8 lines · preview only"),
            ToolSummary("Edit", "example/greeting.py · +2 −1 · preview only"),
            Message(
                "## Theme preview\n\n"
                "The greeting now handles an empty name with `greet(name)`. "
                "Here is **bold**, *italic*, and a "
                "[Rich documentation link](https://rich.readthedocs.io/).\n\n"
                "> Quotes, inline code, and links follow the selected color style.\n\n"
                "```python\n"
                "def greet(name: str) -> str:\n"
                '    name = name.strip() or "world"\n'
                '    return f"Hello, {name}!"\n'
                "```\n\n"
                "```diff\n"
                '-    return f"Hello, {name}!"\n'
                '+    name = name.strip() or "world"\n'
                '+    return f"Hello, {name}!"\n'
                "```\n\n"
                "| Input | Result |\n"
                "| --- | --- |\n"
                "| `Ada` | Hello, Ada! |\n"
                "| empty | Hello, world! |\n"
                "| `世界` | Hello, 世界! |\n\n"
                "- Markdown, code, and this table reflow when the terminal is resized.\n"
                "- Try `/theme light` or `/theme dark`, "
                "then `/theme-preview` again to compare syntax colors.\n"
                "- `/syntax monokai` restyles code and chrome for the palette in use, "
                "`/syntax terminal` hands every color back to the terminal; "
                "the gallery below samples every style.\n"
                "- Use terminal/tmux scrollback to compare previous output.\n\n"
                "**No files were read or changed, and no tests were executed.**"
            ),
        )
