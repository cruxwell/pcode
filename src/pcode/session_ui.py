"""Temporary session popups; the editor is suspended while one owns the terminal."""

import re
from pathlib import Path

from prompt_toolkit.application import Application
from prompt_toolkit.document import Document
from prompt_toolkit.filters import Always, has_focus
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.key_binding.bindings.focus import focus_next, focus_previous
from prompt_toolkit.layout import HSplit, Layout
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.widgets import Label, TextArea
from rich.markdown import Markdown
from rich.padding import Padding
from rich.text import Text
from rich.theme import Theme

from pcode.diagnostics import redact
from pcode.frame import Dialog, Frame
from pcode.popup_ui import (
    EllipsisProcessor,
    RichPane,
    bind_list_paging,
    fit_width,
    popup_container,
    popup_mouse,
    popup_style,
    steer_list_from_query,
)
from pcode.prefix_keys import PrefixKeys
from pcode.sessions import (
    SessionError,
    SessionInfo,
    ToolCall,
    Turn,
    delete_session,
    first_prompt,
    session_turns,
)
from pcode.task_prompt import TaskPrompt
from pcode.text_selection import copy_drags
from pcode.tool_display import plain, tool_summary_lines
from pcode.worktree import SESSION_WORKTREE_PREFIX, repo_scope


def literal(text: str) -> str:
    """Every line of a prompt or response, redacted and safe for the terminal.

    Nothing is dropped: the pane scrolls, and a turn read here should say what
    the turn said. Blank lines inside are structure, so only the surrounding
    ones go, which would otherwise draw an empty quote rail.
    """
    lines = [plain(line, limit=None) for line in redact(text).splitlines()]
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    return "\n".join(lines)


def names_session(word: str, info: SessionInfo) -> bool:
    """Whether a casefolded query word names this session itself, not its content.

    An ID prefix of four characters at least, so an ordinary short word ("add",
    "fix") does not pull in every session whose random ID starts with it; or the
    exact name of the ``pcode-*`` worktree it ran in, which `--worktree NAME`
    and later sessions in the same worktree do not share with the ID.
    """
    if len(word) >= 4 and info.id.casefold().startswith(word):
        return True
    worktree = Path(info.workspace).name.casefold()
    return worktree.startswith(SESSION_WORKTREE_PREFIX) and word == worktree


def display_name(info: SessionInfo) -> str:
    """The session's /rename name, else the title its model gave it."""
    return info.name or info.title or ""


def in_name(word: str, info: SessionInfo) -> bool:
    """Whether a casefolded query word is a word of the session's name or title.

    Or the start of one, from three characters, so typing "bil" already finds
    "Billing outage" but a lone "a" or "in" does not name every named session.
    Both count: an asked-for title is still what the list shows when there is
    no /rename name, and a name replaced by /rename is still worth finding.
    """
    text = f"{info.name or ''} {info.title or ''}".casefold()
    return any(
        part == word or (len(word) >= 3 and part.startswith(word))
        for part in re.findall(r"\w+", text)
    )


class SessionBrowser:
    """Full-screen browser over saved sessions: list, per-turn detail, and search.

    Turns are read lazily from each transcript and cached. Typing a query reads
    every session in scope once; a session stays listed only if a turn matches
    every query word somewhere in it, not necessarily in one turn, searching
    prompts, responses, and tool details (prompts alone with the ``r``
    shortcut). A word naming the session itself (see `names_session` and
    `in_name`) matches every turn of it.
    """

    def __init__(
        self,
        records: list[SessionInfo],
        *,
        root: Path,
        workspace: Path,
        active_id: str | None = None,
        rich_theme: Theme | None = None,
        code_theme: str = "ansi_dark",
        color_system: str | None = "truecolor",
        key_prefix: str | None = None,
        **app_options,
    ) -> None:
        self.records = records
        self.root = root
        self._workspace_scopes: dict[Path, Path] = {}
        self.workspace = self.workspace_scope(workspace)
        self.active_id = active_id
        self.code_theme = code_theme
        self.everywhere = False
        self.responses = True
        self.visible: list[SessionInfo] = []
        self.selected: SessionInfo | None = None
        self._turns: dict[str, list[Turn] | None] = {}
        # Each turn's casefolded search text, per session and search mode, so a
        # keystroke does not rebuild it from every transcript in scope.
        self._haystacks: dict[tuple[str, bool], list[str]] = {}
        self.first_match: int | None = None
        self._titles: dict[str, str] = {}
        self._refreshing = False
        self.pending_delete: str | None = None
        self.status = ""
        self.query = TextArea(height=1, prompt="Search: ", multiline=False)
        # Rows hold the whole first prompt; the processor cuts each at the pane's edge.
        self.list = TextArea(
            read_only=True,
            wrap_lines=False,
            scrollbar=True,
            input_processors=[EllipsisProcessor()],
        )
        copy_drags(self.list, lambda text: setattr(self, "status", text))
        self.list.window.cursorline = Always()
        self.detail = RichPane(
            theme=rich_theme,
            color_system=color_system,
            notify=lambda text: setattr(self, "status", text),
        )
        self.query.buffer.on_text_changed += lambda _: self.refresh(keep_selection=False)
        self.list.buffer.on_cursor_position_changed += lambda _: self.select()
        keys = KeyBindings()
        self.detail.bind_scrolling(keys)

        @keys.add("escape", eager=True)
        @keys.add("c-c")
        def close(event):
            event.app.exit(result=None)

        keys.add("tab")(focus_next)
        keys.add("s-tab")(focus_previous)
        steer_list_from_query(keys, self.query, self.list)
        bind_list_paging(keys, self.list, has_focus(self.list) | has_focus(self.query))

        @keys.add("enter")
        def resume(event):
            # Like the model picker: Enter while typing accepts the selection.
            if self.selected is not None:
                event.app.exit(result=self.selected.id)

        self.prefix_keys = shortcuts = PrefixKeys(key_prefix)
        shortcuts.set_help(
            lambda: [
                ("Type", "Search in the search field"),
                ("↑/↓", "Select session / scroll turns"),
                ("PgUp/PgDn", "Page"),
                ("Ctrl+U/D", "Half page"),
                ("Enter", "Resume selected session"),
                ("Delete", "Delete selected session (twice; list focused)"),
                ("Tab/Shift+Tab", "Change focus"),
                ("Esc/Ctrl+C", "Cancel"),
            ]
        )

        @shortcuts.add("f", "Search")
        def search(event):
            event.app.layout.focus(self.query)

        @shortcuts.add("r", "Prompts only/all")
        def responses(event):
            self.responses = not self.responses
            self.refresh()

        @shortcuts.add("g", "All workspaces")
        def workspaces(event):
            self.everywhere = not self.everywhere
            self.refresh()

        @shortcuts.add("x", "Delete (twice)")
        @keys.add("delete", filter=has_focus(self.list))
        def delete(event):
            self.delete_selected()

        header = Label(
            lambda: (
                f"Sessions · {len(self.visible)}/{len(self.in_scope())} · "
                f"Workspace: {'all' if self.everywhere else self.workspace.name} · "
                f"Search: {'prompts + responses + tools' if self.responses else 'prompts'}"
                + (f" · {self.status}" if self.status else "")
            )
        )
        # Stacked at every width so a long session name gets the full row:
        # turns fill the top two thirds, the session list the bottom third.
        body = HSplit(
            [
                Frame(self.detail, title="Turns", height=Dimension(weight=2)),
                Frame(self.list, title="Sessions", height=Dimension(weight=1)),
            ]
        )
        root_container = HSplit(
            [
                header,
                self.query,
                body,
                Label(shortcuts.summary),
            ]
        )
        self.app = Application(
            # Open in the search line, so typing filters straight away.
            layout=Layout(popup_container(root_container, shortcuts), focused_element=self.query),
            key_bindings=shortcuts.key_bindings(keys),
            full_screen=True,
            mouse_support=popup_mouse(shortcuts),
            style=popup_style(app_options.pop("style", None)),
            **app_options,
        )
        self.refresh()

    def delete_selected(self) -> None:
        """Delete the selected session on the second press; the first only asks."""
        info = self.selected
        if info is None:
            return
        if info.id == self.active_id:
            self.status = "Can't delete the active session"
            return
        if self.pending_delete != info.id:
            self.pending_delete = info.id
            self.status = f"Press {self.prefix_keys.label('x')} again to delete {info.id[:8]}"
            return
        self.pending_delete = None
        try:
            delete_session(info.id, self.root)
        except SessionError as error:
            self.status = str(error)
            return
        self.records = [record for record in self.records if record.id != info.id]
        self._turns.pop(info.id, None)
        self._titles.pop(info.id, None)
        for responses in (True, False):
            self._haystacks.pop((info.id, responses), None)
        self.status = f"Deleted {info.id[:8]}"
        # Keep the cursor on the same row so repeated deletes walk down the list.
        row = self.list.document.cursor_position_row
        self.selected = None
        self.refresh()
        if self.visible:
            row = min(row, len(self.visible) - 1)
            position = self.list.document.translate_row_col_to_index(row, 0)
            self.list.buffer.cursor_position = position

    def turns(self, info: SessionInfo) -> list[Turn]:
        if info.id not in self._turns:
            self._turns[info.id] = session_turns(info, self.root)
        return self._turns[info.id] or []

    def workspace_scope(self, workspace: Path) -> Path:
        """Group linked checkouts, resolving each workspace only once per browser."""
        workspace = workspace.resolve()
        if workspace not in self._workspace_scopes:
            self._workspace_scopes[workspace] = repo_scope(workspace)
        return self._workspace_scopes[workspace]

    def in_scope(self) -> list[SessionInfo]:
        if self.everywhere:
            return self.records
        return [
            info
            for info in self.records
            if self.workspace_scope(Path(info.workspace)) == self.workspace
        ]

    def search_text(self, turn: Turn) -> str:
        if not self.responses:
            return turn.prompt.casefold()
        # Every block, not just the final text: a turn's answer is often split
        # by tool calls, and a remembered file or command lives in a call.
        parts = [turn.prompt] + [
            block if isinstance(block, str) else f"{block.name} {block.detail} {block.command}"
            for block in turn.blocks
        ]
        return "\n".join(parts).casefold()

    def haystacks(self, info: SessionInfo) -> list[str]:
        key = (info.id, self.responses)
        if key not in self._haystacks:
            self._haystacks[key] = [self.search_text(turn) for turn in self.turns(info)]
        return self._haystacks[key]

    def words(self) -> list[str]:
        return self.query.text.casefold().split()

    def content_words(self, info: SessionInfo) -> list[str]:
        """The query words left to find in turns once those naming the session go."""
        return [
            word
            for word in self.words()
            if not names_session(word, info) and not in_name(word, info)
        ]

    def matching_turns(self, info: SessionInfo) -> list[Turn]:
        """Turns holding any query word: the words of a match can span turns."""
        words = self.content_words(info)
        turns = self.turns(info)
        if not words:
            return turns
        return [
            turn
            for turn, haystack in zip(turns, self.haystacks(info), strict=True)
            if any(word in haystack for word in words)
        ]

    def listed(self, info: SessionInfo) -> bool:
        """Every content word is somewhere in the session, not necessarily one turn.

        Named outright, by ID, worktree, or name, a session is listed even with
        no turns to match.
        """
        words = self.content_words(info)
        if not words:
            return True
        haystacks = self.haystacks(info)
        return all(any(word in haystack for haystack in haystacks) for word in words)

    def rank(self, info: SessionInfo) -> tuple[int, bool, int]:
        """Higher is better: words naming the session, all words in one turn, matching turns.

        Ties keep the newest-first order.
        """
        words = self.content_words(info)
        haystacks = self.haystacks(info) if words else []
        together = any(all(word in haystack for word in words) for haystack in haystacks)
        hits = sum(any(word in haystack for word in words) for haystack in haystacks)
        return len(self.words()) - len(words), together or not words, hits

    def highlights(self, info: SessionInfo) -> list[str]:
        """Words to mark and scroll to: those that listed the session's turns.

        Name words only when nothing else is left, and never a single character,
        which would mark half the pane while the first letter is typed.
        """
        words = self.content_words(info) or [
            word for word in self.words() if not names_session(word, info)
        ]
        return [word for word in words if len(word) >= 2]

    def title(self, info: SessionInfo) -> str:
        # Listing reads only the first prompt; the full transcript loads on select/search.
        if info.id not in self._titles:
            marker = "* " if info.id == self.active_id else "  "
            first = redact(first_prompt(info, self.root))
            if name := display_name(info):
                first = f"{name} · {first}"
            when = info.updated[5:16].replace("T", " ")
            self._titles[info.id] = f"{marker}{when}  {info.id[:8]}  {plain(first, limit=None)}"
        return self._titles[info.id]

    def heading(self, info: SessionInfo, shown: int, total: int) -> str:
        parts = [info.id[:8], plain(info.model, 40), info.updated[:16].replace("T", " ")]
        if name := display_name(info):
            parts.insert(0, plain(name, 60))
        if info.id == self.active_id:
            parts.append("active")
        noun = "turn" if total == 1 else "turns"
        parts.append(f"{shown} of {total} {noun}" if shown < total else f"{total} {noun}")
        return " · ".join(parts)

    def refresh(self, *, keep_selection: bool = True) -> None:
        """Re-filter the list; a new query selects its best match instead of the old row."""
        previous = self.selected if keep_selection else None
        self.visible = [info for info in self.in_scope() if self.listed(info)]
        if self.words():
            # sorted() is stable, so equally good matches stay newest first.
            self.visible.sort(key=self.rank, reverse=True)
        selected = next((i for i, info in enumerate(self.visible) if info is previous), 0)
        lines = [self.title(info) for info in self.visible]
        text = "\n".join(lines) or "No matching sessions."
        position = sum(len(line) + 1 for line in lines[:selected])
        self._refreshing = True
        self.list.buffer.set_document(Document(text, position), bypass_readonly=True)
        self._refreshing = False
        self.select(force=True)

    def select(self, force: bool = False) -> None:
        if self._refreshing:
            return
        row = self.list.document.cursor_position_row
        info = self.visible[row] if row < len(self.visible) else None
        if info is self.selected and info is not None and not force:
            return
        if info is not self.selected and self.pending_delete:
            # Moving off a session cancels its pending delete.
            self.pending_delete = None
            self.status = ""
        self.selected = info
        blocks = self.details(info)
        self.detail.set(
            blocks,
            anchor=self.first_match,
            highlight=self.highlights(info) if info is not None else (),
        )

    def details(self, info: SessionInfo | None) -> list:
        """Rich renderables for the Turns pane: prompts verbatim, responses as Markdown.

        Sets `first_match` to the renderable holding the first query word, or
        None when that is already at the top, for the pane to scroll to.
        """
        self.first_match = None
        words = self.highlights(info) if info is not None else []

        def found(text: str) -> bool:
            return bool(words) and any(word in text.casefold() for word in words)

        if info is None:
            return [Text("No matching sessions.")]
        total = len(self.turns(info))
        if self._turns[info.id] is None:
            return [Text("(Transcript unavailable)")]
        turns = self.matching_turns(info)
        blocks: list = [Text(self.heading(info, len(turns), total), style="dim")]
        if not turns:
            blocks.append(Text("(No prompt yet)"))
        for turn in turns:
            blocks.append(Text(""))
            if self.first_match is None and found(turn.prompt):
                self.first_match = len(blocks)
            # The same quote rail scrollback draws, so a remembered turn looks
            # here the way it looked when it was live, blank line included.
            blocks.append(TaskPrompt(literal(turn.prompt)))
            blocks.append(Text(""))
            # Consecutive tool lines stay flush and a blank row marks entering or
            # leaving that run, the rule Transcript.print applies to scrollback.
            previous = "blank"
            for block in turn.blocks:
                if isinstance(block, ToolCall):
                    # The whole detail, unlike scrollback: there is no live tool
                    # panel here to have already named what the call worked on.
                    kind = "tools"
                    rendered = [
                        Padding(line, (0, 0, 0, 2))
                        for line in tool_summary_lines(
                            block.name,
                            " · " + plain(block.detail, limit=None) if block.detail else "",
                            failed=block.failed,
                            elapsed_seconds=block.elapsed_seconds,
                            command=block.command,
                        )
                    ]
                elif text := literal(block):
                    kind = "text"
                    rendered = [Padding(Markdown(text, code_theme=self.code_theme), (0, 0, 0, 2))]
                else:
                    continue
                if previous not in ("blank", kind):
                    blocks.append(Text(""))
                source = (
                    f"{block.name} {block.detail} {block.command}"
                    if isinstance(block, ToolCall)
                    else block
                )
                if self.first_match is None and self.responses and found(source):
                    self.first_match = len(blocks)
                blocks.extend(rendered)
                previous = kind
            if not turn.blocks:
                state = turn.status if turn.status != "complete" else "no response text"
                blocks.append(Text(f"  ({state})", style="dim"))
            elif turn.status not in ("complete", "running"):
                blocks.append(Text(f"  ({turn.status})", style="dim"))
        if self.first_match == 2:
            # The first turn's own prompt: the top already shows it, heading too.
            self.first_match = None
        return blocks

    async def run(self) -> str | None:
        return await self.app.run_async()


def session_info_dialog(
    rows, *, input=None, output=None, style=None, key_prefix: str | None = None
):
    """Read-only view of the live session; every key that closes it exits the same way."""
    width = max((len(label) for label, _ in rows), default=0)
    lines = [f"{label.ljust(width)}  {value}" for label, value in rows]
    body = TextArea(
        text="\n".join(lines),
        read_only=True,
        scrollbar=True,
        focus_on_click=True,
        width=fit_width(lines),
    )
    notice = ""

    def copied(text: str) -> None:
        nonlocal notice
        notice = text

    copy_drags(body, copied)
    bindings = KeyBindings()

    @bindings.add("escape", eager=True)
    @bindings.add("enter", eager=True)
    @bindings.add("q", eager=True)
    @bindings.add("c-c")
    def close(event):
        event.app.exit(result=None)

    bind_list_paging(bindings, body, has_focus(body))
    shortcuts = PrefixKeys(key_prefix)
    shortcuts.set_help(
        lambda: [
            ("↑/↓", "Scroll"),
            ("PgUp/PgDn", "Page"),
            ("Ctrl+U/D", "Half page"),
            ("Enter/q/Esc/Ctrl+C", "Close"),
        ]
    )

    dialog = Dialog(
        title="Session",
        body=HSplit(
            [
                Label(lambda: notice or "/resume switches session", dont_extend_height=True),
                body,
                Label(shortcuts.summary),
            ],
            padding=1,
        ),
    )
    return Application(
        layout=Layout(popup_container(dialog, shortcuts), focused_element=body),
        key_bindings=shortcuts.key_bindings(bindings),
        full_screen=True,
        mouse_support=popup_mouse(shortcuts),
        input=input,
        output=output,
        style=popup_style(style),
    )
