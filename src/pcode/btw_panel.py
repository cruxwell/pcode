"""The /btw panel: a side thread drawn above the main editor, which replies to it.

The full-screen viewer (`aside_ui`, `btw_view popup`) takes the terminal while
it is open. The panel does not: the conversation keeps streaming into
scrollback above it, and the main editor, with everything it can do, sends
follow-ups to the thread while the panel is open for replies.

What it shows lives on `Activity`: `btw_thread`, the thread on screen or None
while closed, and `btw_reply`, whether Enter goes to that thread. A panel that
opened by itself when an answer arrived starts with replies off, since the
user may be mid-sentence to the conversation; Ctrl+Q turns them on.
"""

from prompt_toolkit.filters import Condition
from prompt_toolkit.layout import ConditionalContainer

from pcode.aside import Aside
from pcode.conversation_tree import excerpt
from pcode.frame import Frame
from pcode.popup_ui import RichPane

# The most of the terminal's rows the panel takes (a short thread takes only
# what it needs), and the fewest body rows worth drawing: on a smaller screen
# it stays closed rather than squeeze the editor.
PANEL_SHARE = 0.5
MIN_BODY_ROWS = 3
# Rows kept for everything else: the editor box (three at least), the footer
# line, and the row the non-full-screen renderer keeps free.
RESERVED_ROWS = 6
# Columns the frame's sides and the pane's scrollbar take from the text.
CHROME_COLUMNS = 3


def threads(activity) -> list[list[Aside]]:
    """Each thread's questions in order, threads in the order they began."""
    grouped: dict[str, list[Aside]] = {}
    for aside in activity.asides:
        grouped.setdefault(aside.thread, []).append(aside)
    return list(grouped.values())


def current(activity) -> list[Aside]:
    """The thread on the panel; empty while closed, or once its thread was dropped."""
    thread = activity.btw_thread
    return [aside for aside in activity.asides if aside.thread == thread] if thread else []


def replying(activity) -> bool:
    """Whether the editor sends to the panel's thread rather than the conversation."""
    return activity.btw_reply and bool(current(activity))


def open_thread(activity, thread: str, *, reply: bool) -> None:
    activity.btw_thread = thread
    activity.btw_reply = reply


def close(activity) -> None:
    activity.btw_thread = None
    activity.btw_reply = False


def next_thread(activity) -> str | None:
    """The thread before the one shown, wrapping to the newest; None with no threads."""
    ordered = [thread[0].thread for thread in threads(activity)]
    if not ordered:
        return None
    if activity.btw_thread not in ordered:
        return ordered[-1]
    return ordered[ordered.index(activity.btw_thread) - 1]


class BtwPanel:
    """The panel's pane and frame, redrawn from the shared records each frame."""

    def __init__(self, activity, transcript, key_label=lambda key: f"Ctrl+{key.upper()}") -> None:
        self.activity = activity
        self.transcript = transcript
        # How to press a prompt shortcut: Ctrl+Q, or a leader and `q`.
        self.key_label = key_label
        # Told the ids of answers the panel shows once they settle, e.g. so a
        # session host stops announcing them to its other terminals.
        self.on_read = None
        # Themed when a thread is first shown (`sync`), as /theme may change it.
        self.pane = RichPane(theme=None)
        self._rendered: tuple | None = None
        self.frame = Frame(
            self.pane,
            title=self.title,
            footer=self.footer,
            height=self.height,
            style="class:btw",
        )
        self._rows = 0
        self._columns = 80

    def sync(self, rows: int, columns: int = 80) -> None:
        """Before a redraw: size for a screen this big, and repaint what changed."""
        self._rows, self._columns = rows, columns
        thread = current(self.activity)
        if not thread:
            self._rendered = None
            return
        seen = [aside for aside in thread if not aside.running and not aside.read]
        for aside in seen:
            # On screen is read: the footer stops announcing it.
            aside.read = True
        if seen and self.on_read is not None:
            self.on_read([aside.id for aside in seen])
        state = (
            thread[0].thread,
            tuple((aside.id, aside.status, aside.answer, aside.error) for aside in thread),
        )
        if state == self._rendered:
            return
        switched = self._rendered is None or state[0] != self._rendered[0]
        grew = not switched and len(state[1]) > len(self._rendered[1])
        self._rendered = state
        # Only once a thread is shown: the prompt's first frame never needs it.
        from pcode.aside_ui import details

        transcript = self.transcript
        if transcript is not None:
            self.pane.theme = transcript.rich_theme
            self.pane.color_system = transcript.console.color_system
        code_theme = transcript.code_theme if transcript is not None else "ansi_dark"
        blocks, newest = details(thread, code_theme=code_theme)
        if switched or grew:
            # A thread opens on its newest question; a follow-up comes into view.
            self.pane.set(blocks, anchor=newest)
        else:
            # A streaming answer keeps a reader's place, and follows its tail.
            self.pane.follow(blocks)

    def height(self) -> int:
        """Rows the whole panel takes, frame included; 0 while closed or too cramped."""
        if not current(self.activity):
            return 0
        rows = self._rows
        content = len(self.pane.rows(max(1, self._columns - CHROME_COLUMNS))) + 2
        height = min(int(rows * PANEL_SHARE), rows - RESERVED_ROWS)
        if height - 2 < MIN_BODY_ROWS:
            return 0
        return max(MIN_BODY_ROWS + 2, min(height, content))

    def title(self):
        thread = current(self.activity)
        if not thread:
            return []
        first, newest = thread[0], thread[-1]
        ordered = [asked[0].thread for asked in threads(self.activity)]
        position = ordered.index(first.thread) + 1 if first.thread in ordered else 0
        count = f" · thread {position}/{len(ordered)}" if len(ordered) > 1 else ""
        model = f" · {first.label}" if first.label else ""
        return [
            ("class:btw.badge", " btw "),
            ("class:btw.title", f" {excerpt(first.question)[:60]}"),
            ("class:btw.meta", f"{model} · {newest.state()}{count}"),
        ]

    def footer(self):
        """The keys that matter now, dropping the least needed until they fit."""
        key = self.key_label("q")
        if replying(self.activity):
            parts = [
                [("Enter", " replies here")],
                [("/merge /summarize /copy /links /stop", "")],
                [(key, " next thread")],
                [("Esc", " closes")],
            ]
            # Narrow, the commands go first, then the next-thread key.
            choices = [parts, [parts[0], parts[2], parts[3]], [parts[0], parts[3]]]
        else:
            choices = [[[(key, " to reply here")], [("Esc", " closes")]]]
        room = self._columns - 8  # Corners, rules and padding around the text.
        for choice in choices:
            fragments = []
            for index, part in enumerate(choice):
                if index:
                    fragments.append(("class:btw.meta", " · "))
                for keys, words in part:
                    fragments += [("class:btw.key", keys), ("class:btw.meta", words)]
            if sum(len(text) for _, text in fragments) <= room:
                break
        return fragments

    def container(self) -> ConditionalContainer:
        return ConditionalContainer(self.frame, filter=Condition(lambda: self.height() > 0))
