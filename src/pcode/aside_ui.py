"""Popup reader for side questions, with an editor for follow-ups.

The browser never sends a request itself: a follow-up is handed to the `ask`
callback, which starts it the way `/btw` starts a question, and bringing a
thread into the conversation is returned as a `Bridge` for the app to run.
"""

import asyncio
from collections.abc import Callable
from time import monotonic

from prompt_toolkit.application import Application, get_app
from prompt_toolkit.document import Document
from prompt_toolkit.filters import Always, Condition, has_focus
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.key_binding.bindings.focus import focus_next, focus_previous
from prompt_toolkit.layout import (
    ConditionalContainer,
    DynamicContainer,
    Float,
    FloatContainer,
    FormattedTextControl,
    HSplit,
    Layout,
    VSplit,
    Window,
)
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.widgets import Label, TextArea
from rich.markdown import Markdown
from rich.text import Text
from rich.theme import Theme

from pcode.aside import Aside, Asides, Bridge
from pcode.clipboard import copy as copy_to_clipboard
from pcode.conversation_tree import ConversationTree, TurnNode
from pcode.copy_ui import Snippet, snippets
from pcode.frame import Frame
from pcode.links import Link, extract_links, open_link, remember_link
from pcode.links_ui import LinkPicker
from pcode.popup_ui import (
    PopupCommand,
    PopupInput,
    RichPane,
    bind_list_paging,
    focus_overlay,
    list_pane_height,
    popup_container,
    popup_mouse,
    popup_style,
)
from pcode.prefix_keys import PrefixKeys
from pcode.session_ui import literal
from pcode.task_prompt import TaskPrompt
from pcode.text_selection import copy_drags
from pcode.tool_display import plain
from pcode.tree_ui import CopyBrowser
from pcode.ui import StatusLine, status_parts, status_spinner

# A streaming answer should look live without repainting the pane every token.
REFRESH_SECONDS = 0.3


def row(thread: list[Aside], width: int = 90) -> str:
    """One list line: the model if one was named, the first question, then its state."""
    first, newest = thread[0], thread[-1]
    question = plain(" ".join(first.question.split()), width)
    model = f"[{first.label}] " if first.label else ""
    more = len(thread) - 1
    follow_ups = f" · {more} follow-up{'s' if more > 1 else ''}" if more else ""
    bridged = next((aside.bridged for aside in reversed(thread) if aside.bridged), "")
    kept = f" · {bridged}" if bridged else ""
    return f"{model}{question}  ({newest.state()}{follow_ups}{kept})"


def thread_tree(thread: list[Aside]) -> ConversationTree:
    """The thread as a one-branch conversation, for `/copy`'s browser.

    A question with no answer text yet is left out, as `/copy` leaves out an
    empty turn; a partial answer is in, marked with its state.
    """
    tree = ConversationTree()
    for aside in thread:
        if not literal(aside.answer).strip():
            continue
        status = "completed" if aside.status == "answered" else aside.status
        tree.nodes[aside.id] = TurnNode(
            aside.id, tree.active, aside.question, status=status, response=aside.answer
        )
        tree.active = aside.id
    return tree


def question_blocks(aside: Aside, *, first: bool = True) -> list:
    """A question as a thread shows it, and the blank row its answer starts after."""
    blocks: list = [TaskPrompt(literal(aside.question))]
    where = " ".join(
        part
        for part in (
            f"on {aside.model}" if aside.model else "",
            f"at {aside.effort} effort" if aside.effort else "",
        )
        if part
    )
    # A follow-up runs where its thread began, so only the first says where.
    if where and first:
        blocks.append(Text(f"  {where}", style="dim"))
    blocks.append(Text(""))
    return blocks


def outcome_blocks(aside: Aside, answered: bool) -> list:
    """What follows a settled answer: why it stopped, or that there is none."""
    if aside.running:
        return []  # Its progress is the viewer's status row, not text here.
    if aside.error:
        return [Text(f"  {aside.status}: {aside.error}", style="dim")]
    if not answered:
        return [Text(f"  ({aside.status}; no answer)", style="dim")]
    return []


def exchange(aside: Aside, *, code_theme: str, first: bool = True) -> list:
    """One question and whatever of its answer has arrived."""
    blocks = question_blocks(aside, first=first)
    if answer := literal(aside.answer):
        blocks.append(Markdown(answer, code_theme=code_theme))
    if aside.error:
        blocks.append(Text(""))
    return blocks + outcome_blocks(aside, bool(answer))


def details(thread: list[Aside], *, code_theme: str) -> tuple[list, int]:
    """The thread's questions and answers in order, and where the newest begins."""
    if not thread:
        return [Text("Ask one with /btw <question>.", style="dim")], 0
    blocks: list = []
    newest = 0
    for number, aside in enumerate(thread):
        if number:
            blocks.append(Text(""))
        newest = len(blocks)
        blocks.extend(exchange(aside, code_theme=code_theme, first=not number))
    return blocks, newest


def thread_links(thread: list[Aside]) -> list[Link]:
    """URLs in a thread's questions and answers, oldest first, from the raw text."""
    found: dict[str, Link] = {}
    for aside in thread:
        for text, source in ((aside.question, "user"), (aside.answer, "assistant")):
            for link in extract_links(text, source):
                remember_link(found, link)
    return list(found.values())


class AsideBrowser:
    """Read side answers beside a conversation that may still be running.

    The list is one row per thread: a question and the follow-ups asked about
    it here. The pane shows the selected thread, streaming while an answer
    arrives. Opening a thread marks its answers read, which is what clears the
    footer's unread count.

    With `ask`, an editor under the pane sends follow-ups: `ask(thread,
    question)` starts one, or raises `ValueError` saying why it cannot yet.

    With `check_bridge`, prefix `t` merges the selected thread into the
    conversation tree and prefix `s` summarizes it into the conversation, after
    asking for optional instructions in the editor. Either closes the viewer,
    returning a `Bridge` from `run`; `check_bridge(thread)` raises `ValueError`
    when it cannot yet.

    Prefix `y` opens `/copy`'s browser over the thread, in place of the viewer
    until something is copied or it is cancelled, and prefix `o` opens a link
    from the thread with `/links`'s picker over the viewer.
    The editor takes each action as a slash command too: see `commands`.

    `stop` stops every running answer; Ctrl+C does that while one runs, and
    closes the viewer otherwise. It defaults to cancelling `asides` here, which
    only works where the questions run in this process.

    Enter on the list hides it to read the selected thread full width, and Esc
    brings it back; a viewer opened on a single thread starts that way.

    With `edit`, for a viewer the user opened, reading a thread puts the
    cursor in the follow-up editor rather than on the answer, so typing
    replies at once, and Esc from an empty draft does what it would from the
    answer. Leave it off for a viewer that opens by itself: it could arrive
    mid-keystroke at the main prompt, and Enter would send what was typed.
    """

    def __init__(
        self,
        asides: Asides,
        *,
        ask: Callable[[str, str], None] | None = None,
        check_bridge: Callable[[str], object] | None = None,
        stop: Callable[[], object] | None = None,
        selected: str | None = None,
        edit: bool = False,
        rich_theme: Theme | None = None,
        code_theme: str = "ansi_dark",
        color_system: str | None = "truecolor",
        key_prefix: str | None = None,
        **app_options,
    ) -> None:
        self.asides = asides
        self.ask = ask
        self.edit = edit and ask is not None
        self.check_bridge = check_bridge
        self.stop_running = stop or asides.cancel
        self.code_theme = code_theme
        self.rich_theme = rich_theme
        self.color_system = color_system
        self.spinner = status_spinner()
        self.threads = asides.threads()
        # `selected` names a question; the list selects the thread it is in.
        chosen = next((aside for aside in asides.items if aside.id == selected), None)
        if chosen is not None:
            self.selected: str | None = chosen.thread
        else:
            self.selected = self.threads[-1][0].thread if self.threads else None
        self._rendered: tuple | None = None
        self._refreshing = False
        # Enter on the list reads the selected thread full width; Esc brings
        # the list back. A viewer opened on a lone thread starts reading, so
        # one that arrives meanwhile does not pull the list in beside it. One
        # opened to type in (`edit`) starts reading the newest thread too, with
        # the cursor in its editor; Esc shows the list.
        self.reading = len(self.threads) <= 1 or self.edit
        self._ids: list[str] = []
        self.notice = ""
        # The link picker over the viewer, while one is open.
        self.picker: LinkPicker | None = None
        # `/copy`'s browser in place of the viewer, while one is open.
        self.copying: CopyBrowser | None = None
        self.list = TextArea(read_only=True, wrap_lines=False, scrollbar=True)
        copy_drags(self.list, lambda text: setattr(self, "notice", text))
        self.list.window.cursorline = Always()
        self.detail = RichPane(
            theme=rich_theme,
            color_system=color_system,
            notify=lambda text: setattr(self, "notice", text),
        )
        self.list.buffer.on_cursor_position_changed += lambda _: self.select()
        self.prefix_keys = shortcuts = PrefixKeys(key_prefix)
        shortcuts.set_help(self.help)
        self.input = (
            PopupInput(
                self.follow_up,
                home=self.home,
                title=self.input_title,
                placeholder="Ask a follow-up, or / for commands…",
                shortcuts=shortcuts,
                commands=self.commands(),
                back=self.back if self.edit else None,
            )
            if ask is not None or check_bridge is not None
            else None
        )
        keys = KeyBindings()
        self.detail.bind_scrolling(keys, paging=self.input.editing if self.input else None)
        bind_list_paging(keys, self.list, has_focus(self.list))

        @keys.add("enter")
        def enter(event):
            if self.listing():
                self.read(event.app)
            else:
                event.app.exit(result=None)

        @keys.add("escape", eager=True)
        def escape(event):
            self.back(event.app)

        @keys.add("c-c")
        def interrupt(event):
            # As at the main prompt: stop what is running first, then leave.
            if self.asides.running:
                self.stop()
            else:
                event.app.exit(result=None)

        # Shortcuts are inert while a copy browser or link picker is open: one
        # would otherwise act, or move focus, behind it.
        idle = Condition(lambda: self.picker is None and self.copying is None)

        if ask is not None:

            @shortcuts.add("r", "Reply", filter=idle)
            def reply(event):
                self.input.open(event.app)

        # Not `idle`: under the copy browser it copies the highlighted text, as
        # the same key does in `/copy`.
        @shortcuts.add("y", "Copy", filter=Condition(lambda: self.picker is None))
        def copy_answer(event):
            if self.copying is not None:
                self.copying.copy()
            else:
                self.copy(event.app.output)

        @shortcuts.add("o", "Link", filter=idle)
        def open_links(event):
            self.choose_link()

        if self.check_bridge is not None:

            @shortcuts.add("s", "Summarize", filter=idle)
            def summarize(event):
                if self.bridgeable():
                    thread = self.selected
                    self.input.prompt(
                        event.app,
                        title="Summarize into the conversation · optional focus",
                        placeholder="Enter summarizes as is, or say what to keep…",
                        submit=lambda text: self.finish(Bridge(thread, "summary", text)),
                    )

            @shortcuts.add("t", "Merge into conversation", filter=idle)
            def merge(event):
                if self.bridgeable():
                    self.finish(Bridge(self.selected, "merge"))

        # Same key meaning as in /jobs: stop the work, keep the record. Listed
        # only while there is something to stop.
        @shortcuts.add("k", "Stop", filter=idle & Condition(lambda: self.asides.running > 0))
        def stop_running(event):
            self.stop()

        keys.add("tab")(focus_next)
        keys.add("s-tab")(focus_previous)

        # The main prompt's status row, over the editor as it is there.
        status_row = ConditionalContainer(
            Window(FormattedTextControl(self.status_fragments), height=1),
            Condition(lambda: self.running_aside() is not None),
        )
        header = Label(
            lambda: (
                f"Side questions · {len(self.asides.items)} asked · "
                f"{self.asides.running} running · not part of the conversation"
                + (f" · {self.notice}" if self.notice else "")
            )
        )
        listing = Condition(self.listing)
        wide = VSplit(
            [
                ConditionalContainer(
                    Frame(self.list, title="Questions", width=Dimension(weight=2)), listing
                ),
                Frame(self.detail, title="Answer", width=Dimension(weight=3)),
            ]
        )
        narrow = HSplit(
            [
                ConditionalContainer(
                    Frame(
                        self.list,
                        title="Questions",
                        height=lambda: list_pane_height(len(self.threads)),
                    ),
                    listing,
                ),
                Frame(self.detail, title="Answer"),
            ]
        )
        body = DynamicContainer(
            lambda: wide if get_app().output.get_size().columns >= 100 else narrow
        )
        root_container = HSplit(
            [
                header,
                body,
                status_row,
                *([self.input] if self.input else []),
                Label(shortcuts.summary),
            ]
        )
        picker = Frame(
            DynamicContainer(lambda: self.picker.container if self.picker else HSplit([])),
            title="Links",
        )
        overlaid = FloatContainer(
            root_container,
            floats=[
                Float(ConditionalContainer(picker, Condition(lambda: bool(self.picker)))),
                *([self.input.completion_menu()] if self.input else []),
            ],
        )
        self.app = Application(
            # Opens in the editor only with `edit`: the viewer can also open by
            # itself when an answer lands, mid-keystroke at the main prompt.
            layout=Layout(
                popup_container(
                    DynamicContainer(lambda: self.copying.container if self.copying else overlaid),
                    shortcuts,
                ),
                focused_element=self.start(),
            ),
            key_bindings=shortcuts.key_bindings(keys),
            full_screen=True,
            mouse_support=popup_mouse(shortcuts),
            style=popup_style(app_options.pop("style", None)),
            **app_options,
        )
        self.refresh()

    def running_aside(self) -> Aside | None:
        """The selected thread's newest question, while its answer runs."""
        thread = self.current()
        return thread[-1] if thread and thread[-1].running else None

    def status_fragments(self):
        """The running answer as the main status row draws a turn: `⠋ Thinking … 4s`."""
        aside = self.running_aside()
        if aside is None:
            return []
        phase, detail = status_parts(aside.activity)
        line = StatusLine(phase, detail, elapsed=aside.elapsed)
        width = get_app().output.get_size().columns - 1
        return [("", " "), *line.fragments(self.spinner.render(monotonic()).plain, width)]

    def listing(self) -> bool:
        """Whether the question list is on screen: several threads, none opened to read."""
        return not self.reading and len(self.threads) > 1

    def home(self):
        """Where focus rests outside the editor: the list when shown, else the answer."""
        return self.list if self.listing() else self.detail

    def reader(self):
        """Where focus goes to read a thread: the editor with `edit`, else the answer."""
        return self.input.area if self.edit and self.input is not None else self.detail

    def start(self):
        """Where focus opens: the list when shown, else wherever reading puts it."""
        return self.list if self.listing() else self.reader()

    def read(self, app) -> None:
        """Hide the list so the selected thread's answer gets the whole width."""
        self.reading = True
        app.layout.focus(self.reader())

    def going_back_to_list(self) -> bool:
        """Whether Esc from the reader returns to the list rather than closing."""
        return self.reading and len(self.threads) > 1

    def leaving(self) -> str:
        """What Esc from the reader does, for the help."""
        return "return to questions" if self.going_back_to_list() else "close"

    def back(self, app) -> None:
        """Esc from the reader: back to the list when there is one, else close."""
        if self.going_back_to_list():
            self.back_to_list(app)
        else:
            app.exit(result=None)

    def back_to_list(self, app) -> None:
        self.reading = False
        app.layout.focus(self.list)

    def editing(self) -> bool:
        return self.input is not None and self.app.layout.has_focus(self.input.area)

    def help(self) -> list[tuple[str, str]]:
        """Describe the active picker, editor, or reader, including its current focus."""
        if self.copying is not None:
            return self.copying.help_rows()
        if self.picker is not None:
            return self.picker.help()
        interrupt = ("Ctrl+C", "Stop running answers" if self.asides.running else "Close")
        if self.editing():
            prompting = self.input.prompting
            return [
                ("Enter", "Summarize (empty: as is)" if prompting else "Send"),
                ("Ctrl+J", "Newline"),
                ("Tab/Shift+Tab", "Cycle completions, otherwise change focus"),
                (
                    "Esc",
                    "Close completions, otherwise cancel"
                    if prompting
                    else (
                        "Close completions, otherwise return to questions"
                        if self.listing()
                        else f"Close completions, otherwise {self.leaving()}"
                        if self.edit and not self.input.text
                        else "Close completions, otherwise return to reader"
                    ),
                ),
                ("PgUp/PgDn", "Scroll answer"),
                interrupt,
            ]
        return [
            ("↑/↓", "Select question" if self.app.layout.has_focus(self.list) else "Scroll answer"),
            ("PgUp/PgDn", "Page"),
            ("Ctrl+U/D", "Half page"),
            (
                "Enter",
                ("Reply to selected thread" if self.edit else "Read selected thread")
                if self.listing()
                else "Close",
            ),
            ("Esc", "Questions" if self.going_back_to_list() else "Close"),
            ("Tab/Shift+Tab", "Change focus"),
            interrupt,
        ]

    def commands(self) -> list[PopupCommand]:
        """What the editor runs instead of asking: the shortcuts' actions, by name."""

        def bridge(kind: str, focus: str) -> None:
            if not self.bridgeable():
                # Said once, in the editor's title, beside the draft it keeps.
                reason, self.notice = self.notice, ""
                raise ValueError(reason)
            self.finish(Bridge(self.selected, kind, focus))

        commands = [
            PopupCommand("/copy", "Choose what to copy", lambda _: self.copy(self.app.output)),
            PopupCommand("/links", "Open a link from this thread", lambda _: self.choose_link()),
        ]
        if self.check_bridge is not None:
            commands += [
                PopupCommand(
                    "/summarize",
                    "Summarize into the conversation",
                    lambda focus: bridge("summary", focus),
                    argument="[focus]",
                ),
                PopupCommand(
                    "/merge", "Merge into the conversation", lambda _: bridge("merge", "")
                ),
            ]
        commands.append(PopupCommand("/stop", "Stop running answers", lambda _: self.stop()))
        return commands

    def stop(self) -> None:
        """Stop every running answer, keeping what each said so far."""
        running = self.asides.running
        if not running:
            self.notice = "Nothing is running"
            return
        self.stop_running()
        self.notice = f"Stopping {running} running answer{'s' if running > 1 else ''}"

    def bridgeable(self) -> bool:
        """Whether the selected thread can join the conversation now; says why not."""
        if not self.selected:
            self.notice = "No side question to bring into the conversation"
            return False
        try:
            self.check_bridge(self.selected)
        except ValueError as error:
            self.notice = str(error)
            return False
        self.notice = ""
        return True

    def finish(self, request: Bridge) -> None:
        self.app.exit(result=request)

    def input_title(self) -> str:
        thread = self.current()
        label = thread[0].label if thread else ""
        return "Follow up" + (f" on {label}" if label else "")

    def refresh(self, *, force: bool = True) -> None:
        """Rebuild the list from the current records, keeping the selection."""
        self.threads = self.asides.threads()
        self._ids = [aside.id for aside in self.asides.items]
        lines = [row(thread) for thread in self.threads] or ["No side questions yet"]
        index = next(
            (i for i, thread in enumerate(self.threads) if thread[0].thread == self.selected),
            max(0, len(self.threads) - 1),
        )
        position = sum(len(line) + 1 for line in lines[:index])
        self._refreshing = True
        self.list.buffer.set_document(Document("\n".join(lines), position), bypass_readonly=True)
        self._refreshing = False
        self.select(force=force)

    def current(self) -> list[Aside]:
        """The selected thread's questions, oldest first; empty when there is none."""
        return next((thread for thread in self.threads if thread[0].thread == self.selected), [])

    def select(self, force: bool = False) -> None:
        if self._refreshing:
            return
        row_index = self.list.document.cursor_position_row
        if row_index < len(self.threads):
            self.selected = self.threads[row_index][0].thread
        thread = self.current()
        for aside in thread:
            if not aside.running:
                aside.read = True
        state = (
            self.selected,
            tuple(
                # Not `activity`: the status row reads that live, not the pane.
                (aside.id, aside.status, aside.answer, aside.error)
                for aside in thread
            ),
        )
        if state == self._rendered and not force:
            return
        switched = self._rendered is None or state[0] != self._rendered[0]
        grew = not switched and len(state[1]) > len(self._rendered[1])
        self._rendered = state
        blocks, newest = self.details(thread)
        if switched or grew:
            # A thread opens on its newest question, and a follow-up just
            # asked comes into view with its answer streaming below it.
            self.detail.set(blocks, anchor=newest)
        else:
            # A streaming repaint must not yank the reader away from where
            # they scrolled; one reading the tail keeps following it.
            self.detail.follow(blocks)

    def follow_up(self, question: str) -> None:
        """Ask `question` in the selected thread; `ValueError` says why not."""
        if self.ask is None or not self.selected:
            raise ValueError("No side question to follow up on")
        self.ask(self.selected, question)
        self.refresh()

    def copy(self, output=None) -> None:
        """Copy from this thread with `/copy`'s browser, including partial answers.

        As `/copy` does, a lone answer with no quote or code block is copied
        at once, without the browser.
        """
        tree = thread_tree(self.current())
        if not tree.nodes:
            self.notice = "No answer to copy"
            return
        # What is on screen is what leaves the viewer: the sanitized text.
        if (
            len(tree.nodes) == 1
            and len(found := snippets(literal(tree.nodes[tree.active].response))) == 1
        ):
            self.copy_text("answer", found[0].text, output)
            return
        previous = self.app.layout.current_window

        def finish(choice: Snippet | None) -> None:
            self.copying = None
            focus_overlay(self.app, previous)
            if choice is not None:
                name = "answer" if choice.kind == "response" else choice.kind
                self.copy_text(name, choice.text, output)

        self.copying = CopyBrowser(
            tree,
            finish=finish,
            shortcuts=self.prefix_keys,
            rich_theme=self.rich_theme,
            code_theme=self.code_theme,
            color_system=self.color_system,
        )
        focus_overlay(self.app, self.copying.list)

    def copy_text(self, name: str, text: str, output=None) -> None:
        copied, truncated = copy_to_clipboard(text, output)
        limit = " (truncated)" if truncated else ""
        self.notice = f"Copied {name}{limit}" if copied else f"Could not copy {name}"

    def links(self) -> list[Link]:
        """URLs in the selected thread's questions and answers, oldest first.

        From the raw text, as `/links` reads the conversation.
        """
        return thread_links(self.current())

    def choose_link(self) -> None:
        """Pick a URL from the selected thread and open it in the browser, as `/links` does."""
        links = self.links()
        if not links:
            self.notice = "No links in this side thread"
            return

        def pick(close, url: str) -> None:
            close()
            try:
                open_link(url)
            except (OSError, RuntimeError) as error:
                self.notice = f"Could not open {url}: {error}"
                return
            self.notice = f"Opened {url}"

        self.open_picker(
            lambda close: LinkPicker(
                links,
                on_pick=lambda url: pick(close, url),
                on_cancel=close,
                shortcuts=self.prefix_keys,
            )
        )

    def open_picker(self, build) -> None:
        """Show `build(close)` over the viewer; `close` puts focus back where it was."""
        previous = self.app.layout.current_window

        def close() -> None:
            self.picker = None
            self.app.layout.focus(previous)

        self.picker = build(close)
        # Searchable pickers open in their search line, so typing filters at once.
        focus_overlay(self.app, getattr(self.picker, "query", None) or self.picker.list)

    def details(self, thread: list[Aside]) -> tuple[list, int]:
        return details(thread, code_theme=self.code_theme)

    async def run(self) -> Bridge | None:
        """Show the viewer until it closes; the thread to bring into the conversation, if any."""

        async def follow():
            # Poll rather than subscribe: the records are plain data, and a
            # timer keeps elapsed times and the running count moving too.
            while True:
                await asyncio.sleep(REFRESH_SECONDS)
                if [aside.id for aside in self.asides.items] != self._ids:
                    self.refresh()
                elif self.asides.running:
                    # List rows carry elapsed time and state; keep them moving,
                    # repainting the answer only when it changed.
                    self.refresh(force=False)
                else:
                    self.select()
                self.app.invalidate()

        async def spin():
            # The status row turns at the main prompt's pace, apart from the
            # slower refresh of the answer itself.
            while True:
                await asyncio.sleep(self.spinner.interval / 1000)
                if self.running_aside() is not None:
                    self.app.invalidate()

        def start():
            self.app.create_background_task(follow())
            self.app.create_background_task(spin())

        return await self.app.run_async(pre_run=start)


def aside_dialog(asides, **options) -> Application:
    return AsideBrowser(asides, **options).app
