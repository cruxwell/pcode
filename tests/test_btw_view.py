"""The /btw thread view: a thread drawn in the conversation's place, and where Enter sends."""

import asyncio
from contextlib import asynccontextmanager
from io import StringIO
from types import SimpleNamespace

import pytest
from prompt_toolkit.data_structures import Size
from prompt_toolkit.output import DummyOutput
from rich.console import Console
from rich.text import Text

from pcode.app import PreviewApp
from pcode.aside import Aside, SideReply
from pcode.btw_view import ThreadView, close, current, entry, next_thread, open_thread
from pcode.preferences import save_preferences
from pcode.ui import Activity, CursorSafeOutput, Handoff, TerminalOutput, Transcript


@pytest.fixture(autouse=True)
def swap_view():
    """The swap is opt-in (`btw_view swap`); these tests exercise it."""
    save_preferences(btw_view="swap")


def answered(question: str, answer: str, thread: str = "") -> Aside:
    aside = Aside(question=question, answer=answer, activity="", thread=thread)
    aside.settle("answered")
    aside.reply = SideReply(answer)
    return aside


def seeded_app(*, terminal: bool = True) -> tuple[PreviewApp, list[tuple[str, str]]]:
    console = Console(
        file=StringIO(), width=120, color_system=None, force_terminal=terminal or None
    )
    app = PreviewApp(console=console)
    first = answered("first?", "One.")
    second = answered("second?", "Two.")
    app.controller.asides.items.extend([first, second])
    asked: list[tuple[str, str]] = []

    def follow_up(thread, question):
        app.controller.asides.follows(thread)
        asked.append((thread, question))

    app.controller.follow_up_aside = follow_up
    return app, asked


class Screen:
    """A transcript writing to a terminal stand-in, read one flush at a time."""

    def __init__(self, monkeypatch, *, paced: bool = False):
        activity = Activity()
        console = Console(file=StringIO(), width=80, color_system=None, force_terminal=True)
        self.transcript = Transcript(console, activity=activity)
        terminal = DummyOutput()
        terminal.get_size = lambda: Size(rows=40, columns=80)
        self.output = TerminalOutput(
            console, SimpleNamespace(output=CursorSafeOutput(terminal), invalidate=lambda: None)
        )
        self.transcript.output = self.output
        self.output.paced = paced
        self.activity = activity
        self._read = 0

        async def handoff(app, **kwargs):
            yield Handoff(11)

        monkeypatch.setattr("pcode.ui.suspended_editor", asynccontextmanager(handoff))

    def flush(self) -> str:
        """What the next frames write, until the queue drains."""

        async def drain():
            await self.output.flush()
            while self.output.rows:
                await self.output.flush()

        asyncio.run(drain())
        text = self.transcript.console.file.getvalue()
        new, self._read = text[self._read :], len(text)
        return new


def test_a_view_holds_the_conversation_until_it_closes(monkeypatch):
    screen = Screen(monkeypatch)
    transcript, output = screen.transcript, screen.output
    transcript.message("BEFORE")
    assert "BEFORE" in screen.flush()
    transcript.note("NOTE_QUEUED")
    output.open_view(lambda: [entry(Text("THREAD"))])
    shown = screen.flush()
    assert "THREAD" in shown and "BEFORE" not in shown and "NOTE_QUEUED" not in shown
    # A running turn keeps writing: nothing reaches the screen.
    transcript.message("DURING")
    assert screen.flush() == ""
    output.view_print([entry(Text("MORE"))])
    assert "MORE" in screen.flush()
    # A resize (or theme, or menu) rebuild redraws the view, not the conversation.
    output.regenerate(transcript.replay)
    shown = screen.flush()
    assert "THREAD" in shown and "BEFORE" not in shown and "DURING" not in shown
    output.close_view(transcript.replay)
    back = screen.flush()
    assert "THREAD" not in back and "MORE" not in back
    assert back.index("BEFORE") < back.index("DURING")
    # The note never reached the screen, so it comes back with the conversation.
    assert back.count("NOTE_QUEUED") == 1
    output.view_print([entry(Text("LATE"))])  # Closed: nowhere to write it.
    assert screen.flush() == ""


def test_queued_rows_and_their_notes_come_back_once(monkeypatch):
    screen = Screen(monkeypatch, paced=True)
    transcript, output = screen.transcript, screen.output
    transcript.message("\n\n".join(f"ROW_{n}" for n in range(30)))
    transcript.note("NOTE_IN_ROWS")
    asyncio.run(output.flush())  # A paced frame: most rows still queued.
    assert output.rows
    output.open_view(lambda: [entry(Text("THREAD"))])
    assert "NOTE_IN_ROWS" not in screen.flush()
    output.close_view(transcript.replay)
    back = screen.flush()
    assert "ROW_29" in back and back.count("NOTE_IN_ROWS") == 1


def test_notes_while_a_thread_is_shown_flash_instead(monkeypatch):
    screen = Screen(monkeypatch)
    screen.output.open_view(lambda: [])
    screen.flush()
    screen.transcript.note("Copied reply.")
    assert screen.activity.notice == "Copied reply."
    # A warning flashes too, and is the conversation's once it is back.
    screen.transcript.warning("A turn is running.\nMore detail.")
    assert screen.activity.notice == "Warning: A turn is running."
    screen.output.close_view(screen.transcript.replay)
    back = screen.flush()
    assert "Copied reply." not in back and "A turn is running." in back
    # Redrawing the conversation never flashes its old warnings again.
    screen.activity.notice = ""
    screen.output.regenerate(screen.transcript.replay)
    screen.flush()
    assert screen.activity.notice == ""


def test_the_conversations_newest_rows_stay_in_reach_under_the_view(monkeypatch):
    from pcode.ui import ESCAPES

    def plain(rows):
        return [ESCAPES.sub("", row).strip() for row in rows]

    screen = Screen(monkeypatch)
    transcript, output = screen.transcript, screen.output
    transcript.message("OLD")
    screen.flush()
    assert output.held_tail(80, 3) == []  # Nothing hidden, nothing to show.
    output.open_view(lambda: [])
    screen.flush()
    for n in range(4):
        transcript.message(f"HELD_{n}")
    # Blank separators never take a row; the newest three do.
    assert plain(output.held_tail(80, 3)) == ["HELD_1", "HELD_2", "HELD_3"]
    # Text still streaming, not yet a block, is the newest of all.
    output.delta("half a sente")
    assert plain(output.held_tail(80, 3)) == ["HELD_2", "HELD_3", "half a sente"]
    # A long line still streaming wraps, and its newest words are the ones shown.
    output.delta(" ".join(f"word{n}" for n in range(60)))
    newest = plain(output.held_tail(40, 3))
    assert len(newest) == 3 and newest[-1].endswith("word59")
    assert all(len(row) <= 40 for row in newest)
    output.close_view(transcript.replay)
    assert output.held_tail(80, 3) == []


def test_reopening_shows_only_what_came_since(monkeypatch):
    from pcode.ui import ESCAPES

    screen = Screen(monkeypatch)
    transcript, output = screen.transcript, screen.output
    output.open_view(lambda: [])
    screen.flush()
    transcript.message("FIRST_VISIT")
    assert "FIRST_VISIT" in ESCAPES.sub("", "".join(output.held_tail(80, 3)))
    output.close_view(transcript.replay)
    screen.flush()
    output.open_view(lambda: [])
    screen.flush()
    # The same count of held writes as last time, but none of them.
    transcript.message("SECOND_VISIT")
    shown = ESCAPES.sub("", "".join(output.held_tail(80, 3)))
    assert "SECOND_VISIT" in shown and "FIRST_VISIT" not in shown


def test_the_divider_points_both_ways_and_names_the_keys(monkeypatch):
    from prompt_toolkit.formatted_text.utils import fragment_list_to_text

    from pcode.ui import PromptLayout

    screen = Screen(monkeypatch)
    aside = answered("why is the parser LL(1)?", "Because.")
    screen.activity.asides = [aside]
    layout = SimpleNamespace(
        transcript=screen.transcript,
        activity=screen.activity,
        shortcuts=None,
        render_cache=None,
        size=lambda: Size(rows=40, columns=100),
    )
    rows = PromptLayout.conversation_rows.__wrapped__
    assert rows(layout) == []  # The conversation is on screen.
    open_thread(screen.activity, aside.thread)
    screen.output.open_view(lambda: [])
    assert rows(layout) == []  # Not until the thread is drawn.
    screen.flush()
    screen.transcript.message("A link: [docs](https://example.com/docs)")
    divider, *tail = (fragment_list_to_text(row) for row in rows(layout))
    assert "↑ btw: why is the parser LL(1)?" in divider and "↓ conversation" in divider
    assert divider.endswith("Esc back · Ctrl+Q next thread ─") and len(divider) == 99
    # A hyperlink's escape never reaches the screen as text.
    assert [row.rstrip() for row in tail] == ["  A link: docs"]


def test_opening_and_closing_before_a_frame_leaves_the_conversation(monkeypatch):
    screen = Screen(monkeypatch)
    transcript, output = screen.transcript, screen.output
    transcript.message("KEPT")
    transcript.note("NOTE")
    output.open_view(lambda: [entry(Text("THREAD"))])
    output.close_view(transcript.replay)
    back = screen.flush()
    assert "KEPT" in back and back.count("NOTE") == 1 and "THREAD" not in back


def test_a_rebuild_asked_for_during_the_handoff_still_draws_the_view(monkeypatch):
    screen = Screen(monkeypatch, paced=True)
    transcript, output = screen.transcript, screen.output
    transcript.message("CONVERSATION")
    transcript.note("NOTE_Y")
    requested = []

    async def handoff(app, **kwargs):
        # A resize settles while the handoff waits for its cursor report.
        if not requested:
            requested.append(True)
            output.open_view(lambda: [entry(Text("THREAD"))])
        yield Handoff(11)

    monkeypatch.setattr("pcode.ui.suspended_editor", asynccontextmanager(handoff))
    shown = screen.flush()
    assert "THREAD" in shown
    output.close_view(transcript.replay)
    back = screen.flush()
    assert "CONVERSATION" in back and back.count("NOTE_Y") + shown.count("NOTE_Y") == 1


def test_the_thread_streams_in_block_by_block_and_closes_to_the_conversation(monkeypatch):
    screen = Screen(monkeypatch)
    transcript, activity = screen.transcript, screen.activity
    transcript.message("CONVERSATION")
    screen.flush()
    aside = Aside(question="why?", activity="")
    activity.asides = [aside]
    view = ThreadView(activity, transcript)
    seen = []
    view.on_read = seen.append
    open_thread(activity, aside.thread)
    view.sync()
    shown = screen.flush()
    assert "why?" in shown and "Esc back to the conversation" in shown
    assert "CONVERSATION" not in shown
    # Half a paragraph waits; a finished one lands, as the conversation's do.
    aside.answer = "First paragraph"
    view.sync()
    assert screen.flush() == ""
    aside.answer = "First paragraph.\n\nSecond"
    view.sync()
    assert "First paragraph." in (written := screen.flush()) and "Second" not in written
    # Half an indent is not yet a blank line: the code block keeps it.
    aside.answer = "First paragraph.\n\nSecond paragraph.\n\n  "
    view.sync()
    assert "Second paragraph." in screen.flush()
    aside.answer = "First paragraph.\n\nSecond paragraph.\n\n    INDENTED_CODE\n"
    aside.settle("answered")
    view.sync()
    code = next(line for line in screen.flush().splitlines() if "INDENTED_CODE" in line)
    assert code.startswith(" ")  # A code block, padded; a paragraph would start flush.
    assert seen == [[aside.id]] and aside.read
    view.sync()
    assert screen.flush() == "" and seen == [[aside.id]]
    # A follow-up comes in below.
    follow = Aside(question="and then?", answer="Then this.", activity="", thread=aside.thread)
    follow.settle("answered")
    activity.asides.append(follow)
    view.sync()
    assert "and then?" in (written := screen.flush()) and "Then this." in written
    # Text already written that changes is drawn again from the top.
    follow.answer = "Something else."
    view.sync()
    redrawn = screen.flush()
    assert "why?" in redrawn and "Something else." in redrawn and "Then this." not in redrawn
    close(activity)
    view.sync()
    back = screen.flush()
    assert "CONVERSATION" in back and "why?" not in back


def test_an_error_and_an_empty_answer_say_so():
    activity = Activity()
    console = Console(file=StringIO(), width=80, color_system=None, force_terminal=True)
    view = ThreadView(activity, Transcript(console, activity=activity))
    failed = Aside(question="why?", activity="")
    failed.settle("failed", error="no network")
    empty = Aside(question="how?", activity="", thread=failed.thread)
    empty.settle("cancelled")
    activity.asides = [failed, empty]
    open_thread(activity, failed.thread)
    text = " ".join(
        str(getattr(obj, "plain", obj)) for objects, _, _ in view.replay() for obj in objects
    )
    assert "failed: no network" in text and "(cancelled; no answer)" in text


def test_threads_cycle_newest_first_and_wrap():
    activity = Activity()
    first, second = answered("first?", "One."), answered("second?", "Two.")
    activity.asides = [first, second]
    assert next_thread(activity) == second.thread
    open_thread(activity, second.thread)
    assert current(activity) == [second]
    assert next_thread(activity) == first.thread
    open_thread(activity, first.thread)
    assert next_thread(activity) == second.thread
    close(activity)
    assert current(activity) == []
    # A thread dropped from the records shows the conversation in effect.
    open_thread(activity, "gone")
    assert current(activity) == []


def test_bare_btw_shows_the_newest_thread():
    app, _ = seeded_app()
    newest = app.controller.asides.items[-1]
    assert asyncio.run(app.read_asides()) is None
    assert app.activity.btw_thread == newest.thread


def test_an_answer_that_opens_by_itself_never_takes_a_thread_or_a_draft():
    app, _ = seeded_app()
    first, second = app.controller.asides.items
    app._command_popup_generation = app._auto_open_generation = 3
    open_thread(app.activity, first.thread)
    assert not app.auto_open_asides()
    asyncio.run(app.read_asides())
    assert app.activity.btw_thread == first.thread
    # Mid-sentence to the conversation, the swap would send it to the thread.
    close(app.activity)
    app.prompt_session = SimpleNamespace(default_buffer=SimpleNamespace(text="half a messa"))
    assert not app.auto_open_asides()
    asyncio.run(app.read_asides())
    assert app.activity.btw_thread is None
    app.prompt_session.default_buffer.text = ""
    assert app.auto_open_asides()
    asyncio.run(app.read_asides())
    assert app.activity.btw_thread == second.thread


def test_the_shortcut_shows_the_newest_thread_then_the_one_before():
    app, _ = seeded_app()
    first, second = app.controller.asides.items
    app.btw_key()
    assert app.activity.btw_thread == second.thread
    app.btw_key()
    assert app.activity.btw_thread == first.thread
    app.btw_key()
    assert app.activity.btw_thread == second.thread
    app.close_btw()
    assert app.activity.btw_thread is None
    app.controller.asides.items.clear()
    app.btw_key()
    assert app.activity.btw_thread is None


def test_the_editor_replies_to_the_thread_on_screen():
    app, asked = seeded_app()
    second = app.controller.asides.items[-1]
    # With the conversation shown, the editor is the conversation's.
    assert app.btw_submit("hello") is None
    open_thread(app.activity, second.thread)
    assert app.btw_submit("and then?") is False
    assert asked == [(second.thread, "and then?")]
    # A refused reply keeps the draft.
    second.status = "running"
    assert app.btw_submit("too soon") is True
    second.status = "answered"
    # /copy and /links read the thread, and /btw asks beside it: it stays.
    for command in ("/copy", "/links", "/btw why?"):
        assert app.btw_submit(command) is None
        assert app.activity.btw_thread == second.thread
    # The conversation's own commands bring it back, where their output goes.
    assert app.btw_submit("/help") is None
    assert app.activity.btw_thread is None
    open_thread(app.activity, second.thread)
    assert app.btw_submit("/close") is False
    assert app.activity.btw_thread is None


def test_merge_from_the_thread_keeps_it_and_goes_back():
    app, _ = seeded_app()
    second = app.controller.asides.items[-1]
    kept = []

    async def keep_thread(thread, action, instructions=""):
        kept.append((thread, action, instructions))

    app.controller.keep_thread = keep_thread

    async def run():
        open_thread(app.activity, second.thread)
        app.activity.busy = True
        assert app.btw_submit("/merge") is True  # Refused mid-turn, draft kept.
        app.activity.busy = False
        # A preview has no tree to keep it in; pretend it does.
        app.controller.check_bridge = lambda thread: second
        assert app.btw_submit("/summarize keep decisions") is False
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert kept == [(second.thread, "summary", "keep decisions")]
        assert app.activity.btw_thread is None

    asyncio.run(run())


def test_copy_reads_the_thread_on_screen(monkeypatch):
    from pcode import clipboard

    app, _ = seeded_app()
    copied = []
    monkeypatch.setattr(
        clipboard, "copy", lambda text, output=None: (copied.append(text), (True, False))[1]
    )
    first = app.controller.asides.items[0]
    open_thread(app.activity, first.thread)
    asyncio.run(app.choose_copy(output=None, session=None))
    assert copied == ["One."]


def test_paths_are_questions_and_shell_commands_go_back_to_the_conversation():
    app, asked = seeded_app()
    second = app.controller.asides.items[-1]
    open_thread(app.activity, second.thread)
    # A path is a question, not a command.
    assert app.btw_submit("/etc/hosts has what?") is False
    assert asked == [(second.thread, "/etc/hosts has what?")]
    # A `!command` still runs in the shell, its output in the conversation.
    assert app.btw_submit("!ls") is None
    assert app.activity.btw_thread is None


def test_the_shortcut_opens_the_viewer_when_the_popup_is_the_view():
    save_preferences(btw_view="popup")
    app, _ = seeded_app()
    sent = []
    app._submit_typed = sent.append
    app.btw_key()
    assert sent == ["/btw"]
    assert app.activity.btw_thread is None


@pytest.mark.parametrize("terminal", [True, False])
def test_the_popup_shows_threads_by_default_and_off_a_terminal(monkeypatch, terminal):
    if terminal:
        save_preferences(btw_view="popup")
    app, _ = seeded_app(terminal=terminal)
    opened = []

    class Browser:
        def __init__(self, *args, **kwargs):
            opened.append(kwargs.get("selected"))

        async def run(self):
            return None

    monkeypatch.setattr("pcode.aside_ui.AsideBrowser", Browser)

    class Popup:
        async def __aenter__(self):
            return None

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(app, "popup", lambda output, session: Popup())
    app.prompt_session = type(
        "Session", (), {"app": type("App", (), {"output": None, "style": None})()}
    )()
    assert asyncio.run(app.read_asides()) is None
    assert opened == [app.controller.asides.items[-1].id]
    assert app.activity.btw_thread is None


def test_keep_thread_is_a_host_query():
    from pcode.controller import QUERIES

    assert "keep_thread" in QUERIES
