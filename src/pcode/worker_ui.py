"""Read-only popup following delegated agents' own streams; never steers them."""

import asyncio

from prompt_toolkit.application import Application, get_app
from prompt_toolkit.document import Document
from prompt_toolkit.filters import Always, has_focus
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.key_binding.bindings.focus import focus_next, focus_previous
from prompt_toolkit.layout import DynamicContainer, HSplit, Layout, VSplit
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
from pcode.tool_panel import ACTIVE_TASK_ICON, AGENT_ICON, plan_row
from pcode.workers import Worker, Workers

# Streaming should look live without repainting the pane every token.
REFRESH_SECONDS = 0.3


def row(worker: Worker, width: int = 90) -> str:
    """One list line: who, how long, what state, and the assignment."""
    name = worker.agent[:1].upper() + worker.agent[1:]
    task = plain(" ".join(worker.task.split()), width)
    return f"{AGENT_ICON} {name} · {worker.elapsed():.0f}s · {worker.state()} · {task}"


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
        Text(f"  {worker.agent} · {worker.elapsed():.0f}s · {worker.state()}", style="dim"),
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

        self.prefix_keys = shortcuts = PrefixKeys(key_prefix)
        shortcuts.set_help(
            lambda: [
                ("↑/↓", "Select agent / scroll output"),
                ("PgUp/PgDn", "Page"),
                ("Ctrl+U/D", "Half page"),
                ("Tab/Shift+Tab", "Change focus"),
                ("Enter/Esc/Ctrl+C", "Close"),
            ]
        )

        @shortcuts.add("t", "Thinking")
        def toggle_thinking(event):
            self.show_thinking = not self.show_thinking
            self.select(force=True)

        keys.add("tab")(focus_next)
        keys.add("s-tab")(focus_previous)

        header = Label(
            lambda: (
                f"Agents · {len(self.items)} this session · {self.workers.running()} running"
                f" · read-only · thinking {'on' if self.show_thinking else 'off'}"
                + (f" · {self.notice}" if self.notice else "")
            )
        )
        wide = VSplit(
            [
                Frame(self.list, title="Agents", width=Dimension(weight=2)),
                Frame(self.detail, title="Output", width=Dimension(weight=3)),
            ]
        )
        narrow = HSplit(
            [
                Frame(self.list, title="Agents", height=lambda: list_pane_height(len(self.items))),
                Frame(self.detail, title="Output"),
            ]
        )
        body = DynamicContainer(
            lambda: wide if get_app().output.get_size().columns >= 100 else narrow
        )
        root_container = HSplit(
            [
                header,
                body,
                Label(shortcuts.summary),
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

    async def run(self) -> None:
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

        await self.app.run_async(pre_run=start)
