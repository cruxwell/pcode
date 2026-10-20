"""The `/attach` chooser over running session hosts, and stopped ones nobody has seen."""

import time
from pathlib import Path

from prompt_toolkit.application import Application
from prompt_toolkit.document import Document
from prompt_toolkit.filters import Always, has_focus
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import Layout
from prompt_toolkit.layout.containers import HSplit
from prompt_toolkit.widgets import Label, TextArea

from pcode.frame import Dialog
from pcode.host_protocol import HostEntry, code_fingerprint
from pcode.popup_ui import (
    bind_list_paging,
    fit_width,
    nest_forks,
    popup_container,
    popup_mouse,
    popup_style,
    steer_list_from_query,
)
from pcode.prefix_keys import PrefixKeys

_TITLE_WIDTH = 40
STATES = {"working": "● working", "idle": "○ idle   ", "starting": "◌ starting"}
NEW = "✓ new    "


def unseen(entry: HostEntry) -> bool:
    """Finished while no terminal was showing it, and not looked at since."""
    return entry.unseen and entry.state != "working"


def ordered(entries: list[HostEntry]) -> list[HostEntry]:
    """Finished-and-unseen first, then working, then the rest; newest activity first in each."""

    def rank(entry: HostEntry) -> tuple[int, float]:
        group = 0 if unseen(entry) else 1 if entry.state == "working" else 2
        return group, -entry.updated

    return sorted(entries, key=rank)


def age(seconds: float) -> str:
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{int(seconds // 60)}m ago"
    if seconds < 86400:
        return f"{int(seconds // 3600)}h ago"
    return f"{int(seconds // 86400)}d ago"


def host_row(
    entry: HostEntry,
    current: str | None,
    now: float | None = None,
    *,
    code: str | None = None,
    guide: str = "",
) -> str:
    """One line per host; `code` is the installed fingerprint, to flag hosts on older code.

    `guide` draws a fork under its original (`nested`), within the title's width.
    """
    now = time.time() if now is None else now
    title = guide + " ".join(entry.label().split())
    if len(title) > _TITLE_WIDTH:
        title = title[: _TITLE_WIDTH - 1] + "…"
    marker = "▸" if entry.id == current else " "
    state = NEW if unseen(entry) else STATES.get(entry.state, entry.state)
    where = Path(entry.workspace).name
    if entry.state == "stopped":
        old = " · stopped"  # Enter starts it again on the code installed now.
    else:
        old = " · old code" if code is not None and entry.stale(code) else ""
    return f"{marker} {state}  {title:<{_TITLE_WIDTH}}  {where} · {age(now - entry.updated)}{old}"


def nested(entries: list[HostEntry]) -> list[tuple[HostEntry, str]]:
    """`entries`, in order, with each fork under the session it was copied from."""
    return nest_forks(
        entries,
        # A host with no session yet is nobody's original: its own ID keeps it apart.
        key=lambda entry: entry.session_id or entry.id,
        parent=lambda entry: entry.forked_from,
    )


def status_rows(entry: HostEntry, now: float | None = None, *, code: str | None = None):
    """`/status` rows about the host itself, read from its status file."""
    now = time.time() if now is None else now
    terminals = f"{entry.attached} terminal{'s' if entry.attached != 1 else ''} attached"
    rows = [("Host", f"{entry.id} · pid {entry.pid} · started {age(now - entry.started)}")]
    rows.append(("Attached", terminals))
    if entry.log:
        rows.append(("Host log", entry.log))
    if entry.stale(code):
        rows.append(("Host code", "older than the pcode installed now; /restart updates it"))
    return rows


def hosts_dialog(
    entries: list[HostEntry],
    *,
    current: str | None,
    input=None,
    output=None,
    style=None,
    key_prefix: str | None = None,
):
    """Returns ("attach", id), ("stop", id), ("new", None), or None when cancelled.

    For a stopped entry "attach" means resume it, and "stop" forget it.
    """
    entries = ordered(entries)
    code = code_fingerprint()
    visible: list[HostEntry] = []
    tree = nested(entries)
    query = TextArea(height=1, prompt="Search: ", multiline=False)
    choices = TextArea(
        read_only=True,
        wrap_lines=False,
        scrollbar=True,
        # Fit every row, not just the filtered ones, so searching does not resize the box.
        width=fit_width([host_row(entry, current, code=code, guide=g) for entry, g in tree]),
    )
    choices.window.cursorline = Always()
    status = [""]
    armed: list[str | None] = [None]
    bindings = KeyBindings()

    def selected() -> HostEntry | None:
        row = choices.document.cursor_position_row
        return visible[row] if row < len(visible) else None

    def refresh():
        previous = selected()
        term = query.text.casefold()
        # Searching lists matches flat, as /resume does; otherwise forks nest.
        rows = (
            [
                (entry, "")
                for entry in entries
                if term
                in f"{entry.title} {entry.last_prompt} {entry.workspace} {entry.id}".casefold()
            ]
            if term
            else tree
        )
        visible[:] = [entry for entry, _ in rows]
        index = next((i for i, entry in enumerate(visible) if entry is previous), 0)
        lines = [host_row(entry, current, code=code, guide=guide) for entry, guide in rows]
        position = sum(len(line) + 1 for line in lines[:index])
        choices.buffer.set_document(
            Document("\n".join(lines) or "No matching sessions.", position), bypass_readonly=True
        )

    query.buffer.on_text_changed += lambda _: refresh()
    steer_list_from_query(bindings, query, choices)
    bind_list_paging(bindings, choices, has_focus(choices) | has_focus(query))

    @bindings.add("enter", eager=True)
    def accept(event):
        entry = selected()
        if entry is not None:
            event.app.exit(result=("attach", entry.id))

    shortcuts = PrefixKeys(key_prefix)
    shortcuts.set_help(
        lambda: [
            ("Type", "Search in the search field"),
            ("↑/↓", "Select session"),
            ("PgUp/PgDn", "Page"),
            ("Ctrl+U/D", "Half page"),
            ("Enter", "Switch session"),
            ("Tab/Shift+Tab", "Switch search / list"),
            ("Delete", "Stop (or forget a stopped) session (twice; list focused)"),
            ("Esc", "Clear search when focused, otherwise cancel"),
            ("Ctrl+C", "Cancel"),
        ]
    )

    @shortcuts.add("f", "Search")
    def search(event):
        event.app.layout.focus(query)

    @shortcuts.add("n", "New session")
    def new(event):
        event.app.exit(result=("new", None))

    @shortcuts.add("x", "Stop (twice)")
    @bindings.add("delete", filter=has_focus(choices))
    def stop(event):
        entry = selected()
        if entry is None:
            return
        if armed[0] == entry.id:
            event.app.exit(result=("stop", entry.id))
            return
        armed[0] = entry.id
        status[0] = (
            f"Press {shortcuts.label('x')} again to remove {entry.label()[:40]} from this list. "
            "/resume still opens it."
            if entry.state == "stopped"
            else f"Press {shortcuts.label('x')} again to stop {entry.id} ({entry.label()[:40]}). "
            "Its turn is cancelled."
        )

    @bindings.add("escape", eager=True)
    def escape(event):
        if event.app.layout.has_focus(query) and query.text:
            query.text = ""
        else:
            event.app.exit(result=None)

    @bindings.add("c-c")
    def cancel(event):
        event.app.exit(result=None)

    running = sum(entry.state != "stopped" for entry in entries)
    working = sum(entry.state == "working" for entry in entries)
    fresh = sum(unseen(entry) for entry in entries)
    dialog = Dialog(
        title="Sessions",
        body=HSplit(
            [
                Label(
                    f"{running} running · {working} working · {fresh} new · ▸ this terminal",
                    dont_extend_height=True,
                ),
                query,
                choices,
                Label(lambda: status[0], dont_extend_height=True),
                Label(shortcuts.summary),
            ],
            padding=1,
        ),
    )
    refresh()
    return Application(
        layout=Layout(popup_container(dialog, shortcuts), focused_element=query),
        key_bindings=shortcuts.key_bindings(bindings),
        full_screen=True,
        mouse_support=popup_mouse(shortcuts),
        input=input,
        output=output,
        style=popup_style(style),
    )
