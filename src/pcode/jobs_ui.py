"""Popup over the shell jobs this session knows about: what runs, what it printed."""

import asyncio
import time
from collections.abc import Callable

from prompt_toolkit.application import Application, get_app
from prompt_toolkit.document import Document
from prompt_toolkit.filters import Always, has_focus
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.key_binding.bindings.focus import focus_next, focus_previous
from prompt_toolkit.layout import DynamicContainer, HSplit, Layout, VSplit
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.widgets import Label, TextArea
from rich.text import Text
from rich.theme import Theme

from pcode.frame import Frame
from pcode.inspector_ui import code_block, format_command
from pcode.jobs import Job, JobRegistry, format_duration
from pcode.popup_ui import (
    RichPane,
    bind_list_paging,
    list_pane_height,
    popup_container,
    popup_mouse,
    popup_style,
)
from pcode.prefix_keys import PrefixKeys
from pcode.shell import preview_text
from pcode.text_selection import copy_drags
from pcode.tool_display import command_text

REFRESH_SECONDS = 0.5

# More than the model's tail, since this is where a person reads the whole log,
# but no more than `preview_text` keeps: it clips to its last 128 KiB anyway.
LOG_BYTES = 128 * 1024


def icon(job: Job) -> str:
    if job.running:
        return "⟳"
    return "✗" if job.stopped or job.exit_code != 0 else "✓"


def ordered(registry: JobRegistry) -> list[Job]:
    """Running jobs first, since those are the ones you open this to act on."""
    return sorted(registry.jobs.values(), key=lambda job: (not job.running, job.started_at))


def row(job: Job, watched: str = "") -> str:
    line = f"{icon(job)} {job.id} · {job.outcome()} · {format_duration(job.elapsed)}"
    line += f" · {command_text(job.label())}"
    if job.id == watched:
        line += " · watching"
    return line


def log_size(job: Job) -> int:
    try:
        return job.output_path.stat().st_size
    except OSError:
        return -1


def details(registry: JobRegistry, job: Job | None, *, code_theme: str, watched: str = "") -> list:
    """The job's purpose, command, and state, then the tail of what it printed.

    Nothing here moves with the clock, so an idle job never re-renders its log.
    """
    if job is None:
        return [Text("Jobs appear here once the model runs a shell command.", style="dim")]
    blocks: list = []
    if job.purpose:
        blocks.append(Text(command_text(job.purpose), style="bold"))
    blocks.append(code_block(format_command(command_text(job.command)), "bash", code_theme))
    started = time.strftime("%H:%M:%S", time.localtime(job.started_at))
    state = f"started {started}"
    if not job.running:
        state = f"{job.outcome()} · {state} · took {format_duration(job.elapsed)}"
    notes = [state]
    if job.background:
        notes.append("background")
    if job.adopted:
        notes.append("adopted from an earlier pcode")
    if job.id == watched:
        notes.append("watching in the preview")
    blocks += [Text("  " + " · ".join(notes), style="dim"), Text("")]
    if not job.output_path.exists():
        return [*blocks, Text("The log has been removed.", style="dim")]
    output, truncated = registry.read_output(job, max_bytes=LOG_BYTES)
    output = preview_text(output, final=True)
    if truncated:
        blocks.append(Text(f"… earlier output is in {job.output_path}", style="dim"))
    blocks.append(Text(output) if output else Text("(no output yet)", style="dim"))
    # A one-line last block, so anchoring to it scrolls to the very end.
    return [*blocks, Text("")]


class JobBrowser:
    """List jobs, follow the selected one's log, and stop or watch it."""

    def __init__(
        self,
        registry: JobRegistry,
        *,
        stop: Callable[[Job], None],
        watch: Callable[[Job | None], None],
        watched: Callable[[], str],
        rich_theme: Theme | None = None,
        code_theme: str = "ansi_dark",
        color_system: str | None = "truecolor",
        key_prefix: str | None = None,
        **app_options,
    ) -> None:
        self.registry = registry
        self.stop = stop
        self.watch = watch
        self.watched = watched
        self.code_theme = code_theme
        self.items = ordered(registry)
        self.selected = self.items[0].id if self.items else None
        self.notice = ""
        self._rendered: tuple | None = None
        self._refreshing = False
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
                ("↑/↓", "Select job / scroll output"),
                ("PgUp/PgDn", "Page"),
                ("Ctrl+U/D", "Half page"),
                ("Tab/Shift+Tab", "Change focus"),
                ("Enter/Esc/Ctrl+C", "Close"),
            ]
        )

        @shortcuts.add("w", "Watch in preview")
        def toggle_watch(event):
            job = self.current()
            if job is not None and job.id == self.watched():
                self.watch(None)
                self.notice = f"stopped watching {job.id}"
            elif job is None or not job.running:
                self.notice = "only a running job can be watched"
                return
            else:
                self.watch(job)
                self.notice = f"watching {job.id} in the preview"
            self.refresh()

        @shortcuts.add("k", "Stop")
        def stop_selected(event):
            # Same key meaning as in /btw: stop the work, keep the record.
            job = self.current()
            if job is None or not job.running:
                self.notice = "nothing running to stop"
                return
            self.stop(job)
            self.notice = f"stopped {job.id}"
            self.refresh()

        keys.add("tab")(focus_next)
        keys.add("s-tab")(focus_previous)

        def header() -> str:
            running = sum(job.running for job in self.items)
            line = f"Jobs · {len(self.items)} this session · {running} running"
            return line + (f" · {self.notice}" if self.notice else "")

        wide = VSplit(
            [
                Frame(self.list, title="Jobs", width=Dimension(weight=2)),
                Frame(self.detail, title="Output", width=Dimension(weight=3)),
            ]
        )
        narrow = HSplit(
            [
                Frame(self.list, title="Jobs", height=lambda: list_pane_height(len(self.items))),
                Frame(self.detail, title="Output"),
            ]
        )
        body = DynamicContainer(
            lambda: wide if get_app().output.get_size().columns >= 100 else narrow
        )
        root_container = HSplit(
            [
                Label(header),
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

    def refresh(self) -> None:
        """Re-read statuses and rebuild the list, keeping the selection."""
        self.registry.refresh()
        self.items = ordered(self.registry)
        watched = self.watched()
        lines = [row(job, watched) for job in self.items] or ["No jobs yet"]
        index = next((i for i, job in enumerate(self.items) if job.id == self.selected), 0)
        position = sum(len(line) + 1 for line in lines[:index])
        self._refreshing = True
        self.list.buffer.set_document(Document("\n".join(lines), position), bypass_readonly=True)
        self._refreshing = False
        self.select()

    def current(self) -> Job | None:
        return next((job for job in self.items if job.id == self.selected), None)

    def select(self) -> None:
        if self._refreshing:
            return
        index = self.list.document.cursor_position_row
        if index < len(self.items):
            self.selected = self.items[index].id
        job = self.current()
        watched = self.watched()
        state = (job.id, job.outcome(), log_size(job), job.id == watched) if job else (None,)
        if state == self._rendered:
            return
        switched = self._rendered is None or state[0] != self._rendered[0]
        self._rendered = state
        blocks = details(self.registry, job, code_theme=self.code_theme, watched=watched)
        if switched:
            # Open on the newest output: that is what a log is read for. The
            # anchor is the log's last block, and the window clamps a scroll
            # past the bottom, so this lands on its final line.
            self.detail.set(blocks, anchor=len(blocks) - 1)
        else:
            self.detail.follow(blocks)

    async def run(self) -> None:
        async def follow():
            while True:
                await asyncio.sleep(REFRESH_SECONDS)
                # Rows carry elapsed time, so the list moves even when no log does.
                self.refresh()
                self.app.invalidate()

        def start():
            self.app.create_background_task(follow())

        await self.app.run_async(pre_run=start)
