"""The /btw panel's logic: which thread it shows, and where the editor sends."""

import asyncio
from io import StringIO

import pytest
from rich.console import Console

from pcode.app import PreviewApp
from pcode.aside import Aside, SideReply
from pcode.btw_panel import BtwPanel, close, current, next_thread, open_thread, replying
from pcode.preferences import save_preferences
from pcode.ui import Activity


@pytest.fixture(autouse=True)
def panel_view():
    """The panel is opt-in (`btw_view panel`); these tests exercise it."""
    save_preferences(btw_view="panel")


def answered(question: str, answer: str, thread: str = "") -> Aside:
    aside = Aside(question=question, answer=answer, activity="", thread=thread)
    aside.settle("answered")
    aside.reply = SideReply(answer)
    return aside


def seeded_app() -> tuple[PreviewApp, list[tuple[str, str]]]:
    app = PreviewApp(console=Console(file=StringIO(), width=120, color_system=None))
    first = answered("first?", "One.")
    second = answered("second?", "Two.")
    app.controller.asides.items.extend([first, second])
    asked: list[tuple[str, str]] = []

    def follow_up(thread, question):
        app.controller.asides.follows(thread)
        asked.append((thread, question))

    app.controller.follow_up_aside = follow_up
    return app, asked


def test_threads_cycle_newest_first_and_wrap():
    activity = Activity()
    first, second = answered("first?", "One."), answered("second?", "Two.")
    activity.asides = [first, second]
    assert next_thread(activity) == second.thread
    open_thread(activity, second.thread, reply=True)
    assert current(activity) == [second] and replying(activity)
    assert next_thread(activity) == first.thread
    open_thread(activity, first.thread, reply=False)
    assert not replying(activity)
    assert next_thread(activity) == second.thread
    close(activity)
    assert current(activity) == [] and activity.btw_reply is False
    # A thread dropped from the records closes the panel in effect.
    open_thread(activity, "gone", reply=True)
    assert not replying(activity)


@pytest.mark.parametrize(
    ("rows", "lines", "height"),
    [
        (40, 1, 5),  # A short thread takes what it needs, frame included.
        (40, 60, 20),  # A long one, half the screen.
        (16, 60, 8),
        (10, 60, 0),  # Too cramped to draw beside the editor.
    ],
)
def test_the_panel_fits_its_thread_but_never_crowds_out_the_editor(rows, lines, height):
    activity = Activity()
    aside = answered("why?", "\n\n".join(f"Line {n}." for n in range(lines)))
    activity.asides = [aside]
    panel = BtwPanel(activity, None)
    panel.sync(rows, 80)
    assert panel.height() == 0  # Closed.
    open_thread(activity, aside.thread, reply=True)
    panel.sync(rows, 80)
    assert panel.height() == height
    assert "Line 0." in panel.pane.text(60)
    assert aside.read


def test_bare_btw_opens_the_panel_on_the_newest_thread_to_reply():
    app, _ = seeded_app()
    newest = app.controller.asides.items[-1]
    assert asyncio.run(app.read_asides()) is None
    assert (app.activity.btw_thread, app.activity.btw_reply) == (newest.thread, True)
    # The answer that arrives by itself does not take the editor.
    close(app.activity)
    app._command_popup_generation = app._auto_open_generation = 3
    asyncio.run(app.read_asides())
    assert (app.activity.btw_thread, app.activity.btw_reply) == (newest.thread, False)
    # Nor does another one open over it.
    assert not app.auto_open_asides()


def test_the_shortcut_opens_replies_then_shows_the_next_thread():
    app, _ = seeded_app()
    first, second = app.controller.asides.items
    app.btw_key()
    assert (app.activity.btw_thread, app.activity.btw_reply) == (second.thread, True)
    app.btw_key()
    assert app.activity.btw_thread == first.thread
    app.btw_key()
    assert app.activity.btw_thread == second.thread
    app.activity.btw_reply = False
    app.btw_key()
    assert (app.activity.btw_thread, app.activity.btw_reply) == (second.thread, True)
    app.close_btw()
    assert app.activity.btw_thread is None
    app.controller.asides.items.clear()
    app.btw_key()
    assert app.activity.btw_thread is None


def test_the_editor_replies_to_the_thread_while_the_panel_takes_replies():
    app, asked = seeded_app()
    second = app.controller.asides.items[-1]
    # Closed, or only showing, the panel leaves the editor to the conversation.
    assert app.btw_submit("hello") is None
    open_thread(app.activity, second.thread, reply=False)
    assert app.btw_submit("hello") is None

    app.activity.btw_reply = True
    assert app.btw_submit("and then?") is False
    assert asked == [(second.thread, "and then?")]
    # A refused reply keeps the draft.
    second.status = "running"
    assert app.btw_submit("too soon") is True
    second.status = "answered"
    # The conversation's own commands, and /copy and /links, go on as usual.
    assert app.btw_submit("/model") is None
    assert app.btw_submit("/copy") is None
    assert app.btw_submit("/close") is False
    assert app.activity.btw_thread is None


def test_merge_from_the_panel_keeps_the_thread_and_closes_it():
    app, _ = seeded_app()
    second = app.controller.asides.items[-1]
    kept = []

    async def keep_thread(thread, action, instructions=""):
        kept.append((thread, action, instructions))

    app.controller.keep_thread = keep_thread

    async def run():
        open_thread(app.activity, second.thread, reply=True)
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


def test_copy_reads_the_thread_the_panel_replies_to(monkeypatch):
    from pcode import clipboard

    app, _ = seeded_app()
    copied = []
    monkeypatch.setattr(
        clipboard, "copy", lambda text, output=None: (copied.append(text), (True, False))[1]
    )
    first = app.controller.asides.items[0]
    open_thread(app.activity, first.thread, reply=True)
    asyncio.run(app.choose_copy(output=None, session=None))
    assert copied == ["One."]


def test_the_shortcut_opens_the_viewer_when_the_popup_is_the_view():
    save_preferences(btw_view="popup")
    app, _ = seeded_app()
    sent = []
    app._submit_typed = sent.append
    app.btw_key()
    assert sent == ["/btw"]
    assert app.activity.btw_thread is None


def test_the_popup_stays_available_by_setting(monkeypatch):
    save_preferences(btw_view="popup")
    app, _ = seeded_app()
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


def test_a_queued_auto_open_never_takes_over_a_panel_in_use():
    app, _ = seeded_app()
    first = app.controller.asides.items[0]
    open_thread(app.activity, first.thread, reply=True)
    app._command_popup_generation = app._auto_open_generation = 3
    asyncio.run(app.read_asides())
    assert (app.activity.btw_thread, app.activity.btw_reply) == (first.thread, True)


def test_shell_commands_and_paths_while_replying():
    app, asked = seeded_app()
    second = app.controller.asides.items[-1]
    open_thread(app.activity, second.thread, reply=True)
    # A `!command` still runs in the shell; a path is a question, not a command.
    assert app.btw_submit("!ls") is None
    assert app.btw_submit("/etc/hosts has what?") is False
    assert asked == [(second.thread, "/etc/hosts has what?")]


def test_the_panel_reports_what_it_showed_and_fits_its_footer():
    activity = Activity()
    aside = answered("why?", "Because.")
    aside.read = False
    activity.asides = [aside]
    panel = BtwPanel(activity, None)
    seen = []
    panel.on_read = seen.append
    open_thread(activity, aside.thread, reply=True)
    panel.sync(40, 120)
    panel.sync(40, 120)
    assert seen == [[aside.id]]  # Once, as it settles on screen.

    def footer(columns):
        panel.sync(40, columns)
        return "".join(text for _, text in panel.footer())

    assert "/merge /summarize" in footer(120)
    assert "/merge" not in footer(70) and "Esc closes" in footer(70)
    assert footer(40) == "Enter replies here · Esc closes"
