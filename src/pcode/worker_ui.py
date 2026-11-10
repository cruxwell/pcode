"""Dashboard popup following delegated agents' own streams.

It never steers a worker itself: answering or messaging one closes the popup
and prefills the main prompt, and the parent model relays it with its
`manage_worker` tool.
"""

import asyncio

from prompt_toolkit.application import Application, get_app
from prompt_toolkit.document import Document
from prompt_toolkit.filters import Always, Condition, has_focus
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.key_binding.bindings.focus import focus_next, focus_previous
from prompt_toolkit.layout import ConditionalContainer, DynamicContainer, HSplit, Layout, VSplit
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.widgets import Label, TextArea
from rich.markdown import Markdown
from rich.text import Text
from rich.theme import Theme

from pcode.frame import Frame
from pcode.popup_ui import (
    RichPane,
    bind_list_paging,
    list_pane_height,
    popup_container,
    popup_mouse,
    popup_style,
)
from pcode.prefix_keys import PrefixKeys
from pcode.runtime import ToolSummary
from pcode.session_ui import literal
from pcode.task_prompt import TaskPrompt
from pcode.text_selection import copy_drags
from pcode.tool_display import label, plain
from pcode.tool_panel import ACTIVE_TASK_ICON, plan_row
from pcode.workers import Worker, Workers

# Streaming should look live without repainting the pane every token.
REFRESH_SECONDS = 0.3


WORKSPACES = {"isolated": "iso", "shared": "shared", "auto": "auto"}


def tokens(count: int) -> str:
    """A compact token count: 950, 182k, 1.2M; — before any is known."""
    if count <= 0:
        return "—"
    if count < 1000:
        return str(count)
    if count < 1_000_000:
        return f"{count // 1000}k"
    return f"{count / 1_000_000:.1f}M"


def cell(value: str, width: int) -> str:
    return plain(" ".join(value.split()) or "—", width).ljust(width)


def row(worker: Worker, width: int = 90) -> str:
    """One dashboard line: id, plan step, model, state, elapsed, tokens, workspace, task."""
    workspace = WORKSPACES.get(worker.workspace_mode, worker.workspace_mode)
    columns = [
        cell(worker.worker_id or worker.agent, 6),
        cell(worker.plan_step, 14),
        cell(worker.model, 14),
        cell(worker.state(), 16),
        f"{worker.elapsed():.0f}s".rjust(5),
        tokens(worker.tokens).rjust(5),
        cell(workspace, 6),
        plain(" ".join(worker.task.split()), width),
    ]
    return " ".join(columns)


def header(workers: Workers) -> str:
    """Counts across the session's workers, for the line above the list."""
    line = f"Agents · {len(workers.items)} this session · {workers.running()} running"
    if waiting := workers.waiting():
        line += f" · {waiting} waiting on answer"
    return line + f" · {tokens(workers.tokens())} tokens"


def question_text(worker: Worker) -> str:
    text = worker.question
    if worker.fallback:
        text += f"\nIf unanswered: {worker.fallback}"
    return text + "\n(a to answer)"


def report_index(blocks: list) -> int | None:
    """Where the worker's last prose starts: its final report once it has finished."""
    return next(
        (i for i in range(len(blocks) - 1, -1, -1) if isinstance(blocks[i], Markdown)), None
    )


def tool_line(event) -> Text:
    if isinstance(event, ToolSummary):
        icon, style = ("✗", "red") if event.failed else ("✓", "dim")
        elapsed = f" · {event.elapsed_seconds:.1f}s" if event.elapsed_seconds is not None else ""
    else:
        icon, style, elapsed = "⟳", "bold", ""
    detail = plain(event.detail, limit=None)
    return Text(f"{icon} {label(event.name)} · {detail}{elapsed}", style=style)


def details(worker: Worker | None, *, code_theme: str, show_thinking: bool) -> list:
    """The worker's assignment, plan, then everything it said and did, in order."""
    if worker is None:
        return [Text("Agents appear here once the model delegates a task.", style="dim")]
    blocks: list = [
        TaskPrompt(literal(worker.task)),
        Text(
            f"  {' · '.join(filter(None, [worker.worker_id, worker.agent]))}"
            f" · {worker.elapsed():.0f}s · {worker.state()}",
            style="dim",
        ),
    ]
    if worker.plan:
        done = sum(item.get("status") == "completed" for item in worker.plan)
        blocks += [Text(""), Text(f"Tasks {done}/{len(worker.plan)}", style="bold")]
        for item in worker.plan:
            style, line = plan_row(item, ACTIVE_TASK_ICON)
            active = style == "class:plan.in_progress"
            blocks.append(Text(f"  {line}", style="bold" if active else "dim"))
    for entry in worker.entries:
        if entry.kind == "tool":
            blocks.append(tool_line(entry.tool))
        elif entry.kind == "thinking":
            if show_thinking and entry.text.strip():
                blocks += [Text(""), Text(entry.text.strip(), style="dim italic"), Text("")]
        elif text := literal(entry.text).strip():
            blocks += [Text(""), Markdown(text, code_theme=code_theme), Text("")]
    if worker.running:
        blocks += [Text(""), Text(f"  ({worker.state()}…)", style="dim")]
    return blocks


class WorkerBrowser:
    """Follow delegated workers beside a conversation that may still be running."""

    def __init__(
        self,
        workers: Workers,
        *,
        rich_theme: Theme | None = None,
        code_theme: str = "ansi_dark",
        color_system: str | None = "truecolor",
        show_thinking: bool = False,
        key_prefix: str | None = None,
        **app_options,
    ) -> None:
        self.workers = workers
        self.code_theme = code_theme
        self.show_thinking = show_thinking
        self.items = list(workers.items)
        latest = workers.latest()
        self.selected = latest.call_id if latest else None
        self._rendered: tuple | None = None
        self._refreshing = False
        self.notice = ""
        self.list = TextArea(read_only=True, wrap_lines=False, scrollbar=True)
        copy_drags(self.list, lambda text: setattr(self, "notice", text))
        self.list.window.cursorline = Always()
        self.detail = RichPane(
            theme=rich_theme,
            color_system=color_system,
            notify=lambda text: setattr(self, "notice", text),
        )
        self.list.buffer.on_cursor_position_changed += lambda _: self.select()
        keys = KeyBindings()
        self.detail.bind_scrolling(keys)
        bind_list_paging(keys, self.list, has_focus(self.list))

        @keys.add("escape", eager=True)
        @keys.add("c-c")
        @keys.add("enter")
        def close(event):
            event.app.exit(result=None)

        @keys.add("a")
        def answer(event):
            if prefill := self.prefill("answer"):
                event.app.exit(result=prefill)

        @keys.add("m")
        def message(event):
            if prefill := self.prefill("message"):
                event.app.exit(result=prefill)

        @keys.add("r")
        def report(event):
            self.show_report()

        self.prefix_keys = shortcuts = PrefixKeys(key_prefix)
        shortcuts.set_help(
            lambda: [
                ("↑/↓", "Select agent / scroll output"),
                ("PgUp/PgDn", "Page"),
                ("Ctrl+U/D", "Half page"),
                ("Tab/Shift+Tab", "Change focus"),
                ("a", "Answer the worker's question"),
                ("m", "Message the selected worker"),
                ("r", "Jump to the final report"),
                ("Enter/Esc/Ctrl+C", "Close"),
            ]
        )

        @shortcuts.add("t", "Thinking")
        def toggle_thinking(event):
            self.show_thinking = not self.show_thinking
            self.select(force=True)

        keys.add("tab")(focus_next)
        keys.add("s-tab")(focus_previous)

        top = Label(
            lambda: (
                header(self.workers)
                + f" · thinking {'on' if self.show_thinking else 'off'}"
                + (f" · {self.notice}" if self.notice else "")
            )
        )
        question = ConditionalContainer(
            Frame(
                Label(lambda: question_text(asking) if (asking := self.asking()) else ""),
                title=lambda: (
                    f"Question from {asking.worker_id or asking.agent}"
                    if (asking := self.asking())
                    else "Question"
                ),
                # A long question must not crowd out the output below it.
                height=Dimension(max=10),
            ),
            filter=Condition(lambda: self.asking() is not None),
        )
        output = HSplit([question, Frame(self.detail, title="Output")])
        wide = VSplit(
            [
                Frame(self.list, title="Agents", width=Dimension(weight=2)),
                HSplit([output], width=Dimension(weight=3)),
            ]
        )
        narrow = HSplit(
            [
                Frame(self.list, title="Agents", height=lambda: list_pane_height(len(self.items))),
                output,
            ]
        )
        body = DynamicContainer(
            lambda: wide if get_app().output.get_size().columns >= 100 else narrow
        )
        root_container = HSplit(
            [
                top,
                body,
                Label(lambda: f"a Answer · m Message · r Report · {shortcuts.summary()}"),
            ]
        )
        self.app = Application(
            layout=Layout(popup_container(root_container, shortcuts), focused_element=self.list),
            key_bindings=shortcuts.key_bindings(keys),
            full_screen=True,
            mouse_support=popup_mouse(shortcuts),
            style=popup_style(app_options.pop("style", None)),
            **app_options,
        )
        self.refresh()

    def refresh(self, *, force: bool = True) -> None:
        """Rebuild the list from the current records, keeping the selection."""
        self.items = list(self.workers.items)
        lines = [row(worker) for worker in self.items] or ["No agents yet"]
        index = next(
            (i for i, worker in enumerate(self.items) if worker.call_id == self.selected),
            max(0, len(self.items) - 1),
        )
        position = sum(len(line) + 1 for line in lines[:index])
        self._refreshing = True
        self.list.buffer.set_document(Document("\n".join(lines), position), bypass_readonly=True)
        self._refreshing = False
        self.select(force=force)

    def current(self) -> Worker | None:
        return next((w for w in self.items if w.call_id == self.selected), None)

    def asking(self) -> Worker | None:
        """The worker whose question the panel shows: the selected one's, else the oldest."""
        worker = self.current()
        if worker is not None and worker.question:
            return worker
        return next((w for w in self.workers.items if w.question), None)

    def prefill(self, kind: str) -> str | None:
        """The prompt text that asks the parent to answer or message a worker, or None."""
        worker = self.asking() if kind == "answer" else self.current()
        if worker is None:
            self.notice = "no question to answer" if kind == "answer" else "no agent selected"
        elif not worker.worker_id:
            self.notice = "this agent has no worker id to address"
        elif kind == "message" and not worker.running:
            self.notice = f"{worker.worker_id} has finished"
        else:
            return f"{kind.capitalize()} for {worker.worker_id}: "
        return None

    def show_report(self) -> None:
        worker = self.current()
        blocks = self.detail.renderables
        index = report_index(blocks) if worker is not None else None
        if index is None:
            self.notice = "no report yet"
            return
        self.detail.scroll_to(index)

    def select(self, force: bool = False) -> None:
        if self._refreshing:
            return
        index = self.list.document.cursor_position_row
        if index < len(self.items):
            self.selected = self.items[index].call_id
        worker = self.current()
        # Elapsed time moves every second even when nothing else changes.
        state = (
            (worker.call_id, worker.version, int(worker.elapsed()), self.show_thinking)
            if worker
            else (None,)
        )
        if state == self._rendered and not force:
            return
        switched = self._rendered is None or state[0] != self._rendered[0]
        self._rendered = state
        blocks = details(worker, code_theme=self.code_theme, show_thinking=self.show_thinking)
        if switched and worker is not None and worker.running:
            # A running worker opens on its newest output, then follows it.
            self.detail.set(blocks, anchor=len(blocks) - 1)
        elif switched:
            self.detail.set(blocks)
        else:
            self.detail.follow(blocks)

    async def run(self) -> str | None:
        """Show the popup; return text to prefill the main prompt with, if any."""

        async def follow():
            while True:
                await asyncio.sleep(REFRESH_SECONDS)
                if [w.call_id for w in self.workers.items] != [w.call_id for w in self.items]:
                    self.refresh()
                elif any(w.running for w in self.items):
                    # List rows carry elapsed time and state; keep them moving too.
                    self.refresh(force=False)
                else:
                    self.select()
                self.app.invalidate()

        def start():
            self.app.create_background_task(follow())

        return await self.app.run_async(pre_run=start)
