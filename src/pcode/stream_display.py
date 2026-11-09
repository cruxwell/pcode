"""Presentation-only event routing, shared by live streaming and offline benchmarks."""

from rich.console import Console
from rich.markdown import Markdown

from pcode.runtime import (
    CacheBust,
    ChildPlan,
    ChildText,
    CommandOutput,
    EditCompleted,
    EditPreview,
    Message,
    PlanPreview,
    PlanUpdated,
    RunStatus,
    TextDelta,
    Thinking,
    ThinkingDelta,
    ToolStarted,
    ToolSummary,
    WorkerStatus,
)
from pcode.terminal_notify import TabProgress, terminal_fd
from pcode.terminal_text import safe_text


def present_events(events, *, activity, transcript, edits) -> None:
    """Route live tool activity separately from permanent transcript writes."""
    for event in events:
        if isinstance(event, EditPreview):
            activity.edit_previews.pop(event.call_id, None)
            if event.path:
                activity.edit_previews[event.call_id] = event
        elif isinstance(event, EditCompleted):
            edits.append(event)
            activity.tools.record_edit(event)
            transcript.edit(event)
        elif isinstance(event, CommandOutput):
            activity.command_outputs.pop(event.call_id, None)
            activity.command_outputs[event.call_id] = event
        elif isinstance(event, (ToolStarted, ToolSummary)):
            activity.tools.record(event)
            activity.workers.record(event)
            if transcript.output is not None:
                transcript.output.app.invalidate()
            # The adapter's failed flag includes non-zero exits and tool retries.
            if isinstance(event, ToolSummary):
                activity.command_outputs.pop(event.call_id, None)
                transcript.tool_result(event)
        else:
            transcript.events((event,))


class PrintedReply:
    """`--print`'s output: the reply on `stdout`, every other event through `present`.

    The transcript is expected to be on stderr, so a pipe reading stdout sees
    the reply alone. A terminal gets rendered Markdown; a pipe gets its source,
    which is what a reader downstream can work with.
    """

    # Live-panel state, with no live panel to show it.
    SKIPPED = (
        ThinkingDelta,
        RunStatus,
        PlanPreview,
        PlanUpdated,
        ChildPlan,
        ChildText,
        WorkerStatus,
    )

    def __init__(self, stdout, *, transcript, present) -> None:
        self.stdout = stdout
        self.transcript = transcript
        self.present = present
        reply = Console(file=stdout, theme=transcript.rich_theme)
        # Rendering replaces token-by-token output with settled blocks, so it
        # must not be chosen for a destination that cannot display it.
        self.console = reply if reply.is_terminal else None
        # Text streamed since the last settled message, so a turn that ends
        # mid-block still prints what arrived.
        self.block = ""

    def tab_progress(self, activity) -> TabProgress:
        """The terminal's tab progress, following `activity`, as the editor shows it.

        Sent to the transcript's stream first: a reply piped elsewhere leaves
        that one on the terminal. Neither being one sends nothing.
        """
        from pcode.preferences import load_preferences

        fd = terminal_fd(self.transcript.console.file, self.stdout)
        return TabProgress(activity, fd, load_preferences().get("terminal_progress", "auto"))

    def write(self, markdown: str, *, streamed: bool = False) -> None:
        """Settle one block of reply text; `streamed` means its source is already out."""
        # The reply goes elsewhere, so close the run of calls it follows first.
        self.transcript.settle_tools()
        if self.console is not None:
            self.console.print(Markdown(safe_text(markdown), code_theme=self.transcript.code_theme))
            self.console.print()
            return
        if not streamed:
            self.stdout.write(markdown)
        if not markdown.endswith("\n"):
            self.stdout.write("\n")
        self.stdout.write("\n")
        self.stdout.flush()

    def event(self, event) -> None:
        if isinstance(event, TextDelta):
            self.block += event.text
            if self.console is None:
                self.stdout.write(event.text)
                self.stdout.flush()
        elif isinstance(event, Message):
            # Deltas usually carried this text already; a message without them
            # (a structured result) is written whole.
            self.write(
                event.markdown or self.block,
                streamed=self.console is None and bool(self.block),
            )
            self.block = ""
        elif isinstance(event, Thinking):
            self.transcript.events((event,))
        elif not isinstance(event, self.SKIPPED):
            self.present((event,))

    def settle(self) -> None:
        """Write out a block the turn ended (or failed, or retried) in the middle of."""
        self.transcript.settle_tools()
        if self.block:
            self.write(self.block, streamed=self.console is None)
            self.block = ""


def present_stream_event(event, *, output, transcript, activity, present) -> None:
    """Apply one display event without contacting a model or executing a tool."""
    if isinstance(event, ThinkingDelta):
        output.finish()
        output.thinking_delta(event.text)
        activity.think(event.text)
    elif isinstance(event, Thinking):
        output.finish_thinking(event.text)
        if activity.thought_done and event.text:
            # A block that arrived whole, with no deltas before it.
            activity.think(event.text)
        # Held on the thinking rows through the tool calls it explains; the
        # next block, or the answer, replaces it.
        activity.thought_done = True
    elif isinstance(event, TextDelta):
        output.finish_thinking()
        output.delta(event.text)
        activity.status = "Responding…"
        if event.text.strip():  # An empty part start says nothing yet.
            activity.forget_thought()
    elif isinstance(event, CacheBust):
        # A footer note, not a scrollback line: the session journal keeps the
        # full notice (and its cause) for anyone diagnosing it later.
        from pcode.cache_warnings import footer_label

        activity.cache_note = footer_label(event.text)
    elif isinstance(event, EditCompleted):
        output.finish_thinking()
        output.finish()
        present((event,))
    elif isinstance(event, (CommandOutput, EditPreview)):
        present((event,))
    elif isinstance(event, RunStatus):
        activity.status = event.text
    elif isinstance(event, PlanUpdated):
        activity.plan = event.items
    elif isinstance(event, PlanPreview):
        activity.plan_preview = event.items
    elif isinstance(event, ChildPlan):
        activity.tools.record_plan(event.call_id, event.items)
        activity.workers.record(event)
    elif isinstance(event, ChildText | WorkerStatus):
        # A worker's own prose and status feed `/agents`; the transcript never shows them.
        activity.workers.record(event)
        return
    elif isinstance(event, (ToolStarted, ToolSummary)):
        output.finish_thinking()
        # Settled calls land in scrollback, so prose must be committed first.
        # Suppressed calls, such as successful planning, do not interrupt it.
        if transcript.writes_tool_result(event):
            output.finish()
        present((event,))
    elif isinstance(event, Message):
        output.finish(event.markdown)
        if event.markdown.strip():  # An answer that arrived whole, with no deltas before it.
            activity.forget_thought()
    else:
        output.finish()
        transcript.events((event,))
    output.app.invalidate()
