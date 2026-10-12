import asyncio
from io import StringIO
from unittest.mock import Mock, patch

import pytest
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput
from pydantic_ai import Agent
from pydantic_ai.models.function import FunctionModel
from rich.console import Console
from rich.text import Text

from pcode.app import PreviewApp
from pcode.live import AgentRuntime
from pcode.session_ui import SessionBrowser, literal, session_info_dialog
from pcode.sessions import SavedSession, SessionError, Turn, first_prompt, session_turns


def two_sessions(tmp_path):
    """Newest-first: `older` in this workspace, `newer` in another, `newest` here."""
    from pcode.runtime import Message

    root = tmp_path / "sessions"
    ids = {}
    for name, workspace, prompts in [
        ("older", tmp_path, [("Fix the cache warning", "Patched cache_warnings.py")]),
        ("other", tmp_path / "elsewhere", [("Cache question elsewhere", "")]),
        ("newest", tmp_path, [("Add a theme", "Done"), ("Now tests", "Wrote tests for the theme")]),
    ]:
        saved = SavedSession.create("test:local", workspace, root)
        for prompt, response in prompts:
            saved.append("turn_started", prompt=prompt)
            if response:
                saved.event(Message(response))
            saved.append("turn_completed")
        saved.save_info()
        saved.close()
        ids[name] = saved.info.id
    return root, ids


def browser(tmp_path, **options):
    from pcode.sessions import list_sessions

    root, ids = two_sessions(tmp_path)
    records = list_sessions(root)
    assert [info.id for info in records] == [ids["newest"], ids["other"], ids["older"]]
    return SessionBrowser(
        records, root=root, workspace=tmp_path, output=DummyOutput(), **options
    ), ids


@pytest.mark.parametrize(
    "keys,expected",
    [
        ("\r", "newest"),
        ("\x1b[B\r", "older"),
        ("\x1b", None),
        # It opens in the search line, which narrows the list; Enter resumes while typing.
        ("cache warn\r", "older"),
        # Arrows steer the list while typing; Enter does nothing with no match.
        ("a\x1b[B\r", "older"),
        ("nothing-matches\r\x1b", None),
        # Ctrl+F in the list returns to the search.
        ("\t\x06cache warn\r", "older"),
    ],
)
def test_browser_keyboard(tmp_path, keys, expected):
    async def run():
        with create_pipe_input() as pipe:
            app, ids = browser(tmp_path, input=pipe)
            task = asyncio.create_task(app.run())
            await asyncio.sleep(0.05)
            pipe.send_text(keys)
            result = await asyncio.wait_for(task, 2)
            assert result == (ids[expected] if expected else None)

    asyncio.run(run())


@pytest.mark.parametrize(
    "prefix,delete,label",
    [("ctrl", "\x18", "Ctrl+X"), ("ctrl+p", "\x10x", "Ctrl+P x")],
)
def test_browser_deletes_after_confirmation(tmp_path, prefix, delete, label):
    async def run():
        with create_pipe_input() as pipe:
            app, ids = browser(tmp_path, input=pipe, active_id=None, key_prefix=prefix)
            root = app.root
            task = asyncio.create_task(app.run())
            await asyncio.sleep(0.05)
            # Typed on open, `x` searches; the shortcut deletes, even from here.
            pipe.send_text("xx")
            await asyncio.sleep(0.05)
            assert app.query.text == "xx" and not app.status
            # Searching `x` selected the only match; ↑ goes back to the newest.
            pipe.send_text("\x7f\x7f\x1b[A" + delete)
            await asyncio.sleep(0.05)
            assert app.query.text == "" and app.selected.id == ids["newest"]
            assert (root / ids["newest"]).is_dir()
            assert f"Press {label} again" in app.status
            pipe.send_text(delete)
            await asyncio.sleep(0.05)
            assert not (root / ids["newest"]).exists()
            assert [info.id for info in app.visible] == [ids["older"]]
            pipe.send_text("\r")
            assert await asyncio.wait_for(task, 2) == ids["older"]
            assert (root / ids["older"]).is_dir()

    asyncio.run(run())


def test_browser_refuses_to_delete_active_or_open_session(tmp_path):
    with create_pipe_input() as pipe:
        app, ids = browser(tmp_path, input=pipe)
        app.active_id = ids["newest"]
        app.delete_selected()
        assert "active" in app.status
        assert (app.root / ids["newest"]).is_dir()
    held = SavedSession.open(ids["older"], app.root, tmp_path)
    try:
        with pytest.raises(SessionError, match="open in another process"):
            from pcode.sessions import delete_session

            delete_session(ids["older"], app.root)
    finally:
        held.close()
    assert (app.root / ids["older"]).is_dir()


def test_browser_scopes_searches_and_shows_turns(tmp_path):
    with create_pipe_input() as pipe:
        app, ids = browser(tmp_path, input=pipe)
        # Only this workspace, newest first; the detail pane lists every turn.
        assert [info.id for info in app.visible] == [ids["newest"], ids["older"]]
        assert app.list.text.startswith(app.title(app.visible[0]))
        assert "Add a theme" in app.list.text
        shown = app.detail.text()
        assert shown.startswith(f"{ids['newest'][:8]} · test:local · ")
        assert shown.endswith(
            "2 turns\n\n▌ Add a theme\n\n  Done\n\n▌ Now tests\n\n  Wrote tests for the theme"
        )
        # Words are AND-ed against prompts and the detail keeps only matching turns.
        app.query.text = "tests now"
        assert [info.id for info in app.visible] == [ids["newest"]]
        assert app.detail.text().endswith(
            "1 of 2 turns\n\n▌ Now tests\n\n  Wrote tests for the theme"
        )
        # Responses are searched by default; `r` narrows to prompts.
        app.query.text = "patched"
        assert [info.id for info in app.visible] == [ids["older"]]
        app.responses = False
        app.refresh()
        assert app.visible == []
        assert app.detail.text() == "No matching sessions."
        app.responses = True
        # A session ID prefix, or its worktree name, finds every turn of it.
        app.query.text = ids["newest"][:6].upper()
        assert [info.id for info in app.visible] == [ids["newest"]]
        assert app.detail.text().endswith("Wrote tests for the theme")
        assert "2 turns" in app.detail.text()
        app.query.text = f"{ids['newest'][:8]} tests"
        assert [info.id for info in app.visible] == [ids["newest"]]
        assert "1 of 2 turns" in app.detail.text()
        # The list shows the short ID, so there is something to type.
        assert ids["newest"][:8] in app.list.text
        # Widening to every workspace brings the other repo in.
        app.query.text = "cache"
        assert [info.id for info in app.visible] == [ids["older"]]
        app.everywhere = True
        app.refresh()
        assert [info.id for info in app.visible] == [ids["other"], ids["older"]]
        app.detail.set(app.details(app.visible[0]))
        assert app.detail.text().endswith("▌ Cache question elsewhere\n\n  (no response text)")


def test_session_rows_fill_the_pane_and_end_in_an_ellipsis():
    from prompt_toolkit.layout.processors import TransformationInput

    from pcode.popup_ui import EllipsisProcessor

    def shown(text, width):
        ti = TransformationInput(None, None, 0, lambda i: i, [("", text)], width, 1)
        result = EllipsisProcessor().apply_transformation(ti)
        return "".join(text for _, text, *_ in result.fragments), result

    line = "x" * 100 + " tail"
    # Wider than the old 80-character cut, and the end of the row says more follows.
    assert shown(line, 90)[0] == "x" * 89 + "…"
    assert shown(line, 200)[0] == line
    # Double-width characters never push the ellipsis past the edge.
    assert shown("界" * 10, 9)[0] == "界" * 4 + "…"
    _, result = shown(line, 90)
    assert result.source_to_display(95) == 89


def test_browser_scopes_without_git(tmp_path):
    with (
        create_pipe_input() as pipe,
        patch("pcode.worktree.main_checkout", side_effect=FileNotFoundError),
    ):
        app, ids = browser(tmp_path, input=pipe)
        assert [info.id for info in app.visible] == [ids["newest"], ids["older"]]


def test_browser_marks_unreadable_and_empty_sessions(tmp_path):
    with create_pipe_input() as pipe:
        app, ids = browser(tmp_path, input=pipe)
        empty = SavedSession.create("test:local", tmp_path, app.root)
        empty.close()
        gone = empty.info.model_copy(update={"id": "missing"})
        app.detail.set(app.details(empty.info))
        assert app.detail.text().endswith("0 turns\n(No prompt yet)")
        app.detail.set(app.details(gone))
        assert app.detail.text() == "(Transcript unavailable)"


def test_browser_renders_responses_as_markdown(tmp_path):
    from pcode.runtime import Message

    root = tmp_path / "sessions"
    saved = SavedSession.create("test:local", tmp_path, root)
    saved.append("turn_started", prompt="Explain")
    saved.event(Message("## Heading\n\nSome *emphasis* here."))
    saved.append("turn_completed")
    saved.close()
    with create_pipe_input() as pipe:
        app = SessionBrowser(
            [saved.info], root=root, workspace=tmp_path, input=pipe, output=DummyOutput()
        )
        shown = app.detail.text(width=40)
        assert "##" not in shown and "*" not in shown
        assert "Heading" in shown and "emphasis" in shown
        # Styled fragments survive; emphasis is italic in every theme.
        assert any("italic" in style for style, *_ in app.detail.fragments(40))


def browsed_turn(tmp_path, events, prompt="Do the thing"):
    """Render one recorded turn's detail pane."""
    from pcode.runtime import ToolSummary

    root = tmp_path / "sessions"
    saved = SavedSession.create("test:local", tmp_path, root)
    saved.append("turn_started", prompt=prompt)
    for event in events:
        saved.event(event)
    saved.append("turn_completed")
    saved.close()
    with create_pipe_input() as pipe:
        app = SessionBrowser(
            [saved.info], root=root, workspace=tmp_path, input=pipe, output=DummyOutput()
        )
        return app.detail.text(width=100), ToolSummary


def test_tool_calls_appear_between_the_text_they_ran_between(tmp_path):
    from pcode.runtime import Message, ToolSummary

    shown, _ = browsed_turn(
        tmp_path,
        [
            Message("Looking now."),
            ToolSummary("read_file", "src/pcode/ui.py → 40 lines", elapsed_seconds=0.25),
            ToolSummary("shell", "ls → failed", failed=True, command="ls /nope\nsecond line"),
            Message("All done."),
        ],
    )
    body = shown.split("▌ Do the thing\n\n", 1)[1].splitlines()
    # Tool lines stay flush with each other and a blank row brackets the run,
    # the spacing scrollback gives the same sequence.
    assert body == [
        "  Looking now.",
        "",
        "  ✓ Read file · src/pcode/ui.py → 40 lines · 0.2s",
        "  ✗ Run shell · ls → failed · ls /nope … [1 more lines]",
        "",
        "  All done.",
    ]


def test_names_session_needs_four_characters():
    from pcode.session_ui import names_session

    info = Mock(id="add12345-0000-0000-0000-000000000000", workspace="/repo")
    # Too short to be an ID: an ordinary word never matches one by accident.
    assert not names_session("add", info)
    assert names_session("add1", info)
    assert names_session(info.id, info)
    assert not names_session("add2", info) and not names_session("repo", info)
    # A session worktree's whole name, even one that is not the session's ID.
    info.workspace = "/repo/.worktrees/pcode-feature"
    assert names_session("pcode-feature", info)
    assert not names_session("pcode", info) and not names_session("pcode-feat", info)


def test_session_id_finds_a_session_with_no_turns(tmp_path):
    with create_pipe_input() as pipe:
        app, ids = browser(tmp_path, input=pipe)
        empty = SavedSession.create("test:local", tmp_path, app.root)
        empty.close()
        app.records.append(empty.info)
        app.query.text = empty.info.id[:8]
        assert app.visible == [empty.info]
        app.query.text = f"{empty.info.id[:8]} anything"
        assert app.visible == []


def test_search_finds_tool_calls_unless_prompts_only(tmp_path):
    from pcode.runtime import Message, ToolSummary

    root = tmp_path / "sessions"
    saved = SavedSession.create("test:local", tmp_path, root)
    saved.append("turn_started", prompt="Do the thing")
    saved.event(ToolSummary("shell", "ok", command="kubectl rollout restart"))
    saved.event(ToolSummary("edit_file", "src/pcode/frobnicate.py"))
    saved.event(Message("Done."))
    saved.append("turn_completed")
    saved.close()
    with create_pipe_input() as pipe:
        app = SessionBrowser(
            [saved.info], root=root, workspace=tmp_path, input=pipe, output=DummyOutput()
        )
        for query in ("rollout", "frobnicate.py", "edit_file", "done."):
            app.responses = True
            app.query.text = query
            assert app.visible == [saved.info], query
            app.responses = False
            app.refresh()
            assert app.visible == [], query
        app.query.text = "thing"
        assert app.visible == [saved.info]


def test_long_turns_are_shown_whole(tmp_path):
    from pcode.runtime import Message

    prompt = "\n".join(f"prompt line {i}" for i in range(40))
    response = "\n".join(f"response line {i}" for i in range(40))
    shown, _ = browsed_turn(tmp_path, [Message(response)], prompt=prompt)
    assert "prompt line 39" in shown and "response line 39" in shown
    assert "…" not in shown


def test_scrollback_and_browser_share_one_tool_line(tmp_path):
    """Same glyph, label, and style; the browser only passes more of the detail."""
    from pcode.tool_display import tool_summary_lines

    (line,) = tool_summary_lines("read_file", " · src/x.py → 40 lines", elapsed_seconds=0.25)
    assert line.plain == "✓ Read file · src/x.py → 40 lines · 0.2s"
    assert line.style == "pcode.thinking"
    (failed,) = tool_summary_lines("shell", "", failed=True, command="rm -rf /\nmore")
    assert failed.plain == "✗ Run shell · rm -rf / … [1 more lines]"
    # A width truncates rather than wraps, as the scrollback line does.
    (narrow,) = tool_summary_lines("read_file", " · " + "x" * 200, width=20)
    assert len(narrow.plain) == 20 and narrow.plain.endswith("…")


def test_markdown_links_do_not_leak_their_escape_wrapper():
    """Rich writes OSC 8 links; prompt_toolkit's ANSI parser would spill the wrapper."""
    from rich.markdown import Markdown

    from pcode.popup_ui import RichPane

    pane = RichPane()
    pane.set([Markdown("See [docs](https://example.com/page) for more.")])
    shown = pane.text(width=60)
    assert "See docs for more." in shown
    assert "8;id=" not in shown and "example.com" not in shown


def test_rich_pane_renders_current_content_in_one_pass():
    """Layout sizing must not leave the pane showing the previous frame's content."""
    from prompt_toolkit.application import Application
    from prompt_toolkit.application.current import set_app
    from prompt_toolkit.layout import Layout

    from pcode.popup_ui import RichPane

    pane = RichPane()
    pane.set([Text("first")])
    app = Application(layout=Layout(pane.window), output=DummyOutput())
    with set_app(app):
        control = pane.control
        assert control.preferred_width(80) is not None
        pane.set([Text("second")])
        assert control.preferred_width(80) is not None
        content = control.create_content(80, None)
        assert "second" in "".join(t for _, t in content.get_line(0))


def test_literal_keeps_every_line_and_drops_only_the_outer_blanks():
    assert literal("\n\none\n\ntwo\n\n") == "one\n\ntwo"
    # Long lines are not cut: the pane scrolls and wraps instead.
    assert literal("x" * 400) == "x" * 400
    # Terminal controls still cannot survive.
    assert "\x1b" not in literal("a\x1b[31mb")


@pytest.mark.parametrize("keys", ["\x1b", "\r", "q"])
def test_info_popup_closes_on_every_exit_key(keys):
    async def run():
        with create_pipe_input() as pipe:
            dialog = session_info_dialog(
                [("Model", "test:local"), ("Turns", "2")],
                input=pipe,
                output=DummyOutput(),
            )
            task = asyncio.create_task(dialog.run_async())
            await asyncio.sleep(0.05)
            pipe.send_text(keys)
            assert await asyncio.wait_for(task, 2) is None

    asyncio.run(run())


def test_session_overview_reports_storage_and_feeds_context(tmp_path):
    root = tmp_path / "sessions"
    saved = SavedSession.create("test:local", tmp_path, root)
    stream = StringIO()
    app = PreviewApp(workspace=tmp_path, session_dir=root, console=Console(file=stream, width=200))
    app.model = "test:local"
    app.runtime = AgentRuntime(Agent("test"), saved)
    try:
        rows = dict(app.controller.session_overview())
        assert rows["Model"] == "test:local"
        assert rows["Session"] == saved.info.id
        assert rows["Saved in"] == str(saved.directory)
        # Nothing has been sent yet, so there is no resolved prompt to attribute.
        assert rows["Prompt overhead"] == "Measured on the first model request."
        # Without an editor, /status prints exactly what the popup shows.
        app.handle("/status")
        assert f"Saved in: {saved.directory}" in stream.getvalue()
    finally:
        app.runtime.close()


def test_session_overview_attributes_prompt_overhead_after_a_request(tmp_path):
    """The rows come from the last request's parameters, captured on the runtime."""
    from test_context_breakdown import isolated_workspace, request_parameters

    workspace = isolated_workspace()
    (workspace / "AGENTS.md").write_text("Repository rules.\n")
    stream = StringIO()
    app = PreviewApp(workspace=workspace, console=Console(file=stream, width=200))
    app.model = "test:local"
    app.runtime = AgentRuntime(Agent("test"), None)
    try:
        app.runtime.request_parameters = request_parameters(workspace)["parameters"]
        rows = dict(app.controller.session_overview())
        assert "tokens" in rows["Prompt overhead"]
        assert rows["  Instructions"].startswith("~")
        assert "tools" in rows["  Tool schemas"]
        app.handle("/status")
        assert "AGENTS.md" in stream.getvalue()
    finally:
        app.runtime.close()


def test_status_requests_the_popup_only_with_a_live_editor(tmp_path):
    app = PreviewApp(workspace=tmp_path, console=Console(file=StringIO()))
    app.transcript.output = Mock()
    app.handle("/status")
    assert app.session_info_requested
    assert not app.session_requested
    with pytest.raises(ValueError, match="Usage: /status"):
        app.registry.find("/status").handler("extra")


def test_first_prompt_is_not_latest_and_handles_empty_session(tmp_path):
    saved = SavedSession.create("test:local", tmp_path, tmp_path / "sessions")
    try:
        assert first_prompt(saved.info, saved.directory.parent) == "(No prompt yet)"
        saved.append("turn_started", prompt="First question")
        saved.append("turn_started", prompt="Followup")
        assert first_prompt(saved.info, saved.directory.parent) == "First question"
    finally:
        saved.close()


def test_session_turns_pairs_prompts_with_final_responses(tmp_path):
    from pcode.runtime import Message

    saved = SavedSession.create("test:local", tmp_path, tmp_path / "sessions")
    root = saved.directory.parent
    try:
        assert session_turns(saved.info, root) == []
        saved.append("turn_started", prompt="First question", run_id="a")
        saved.event(Message("Interim thoughts"))
        saved.event(Message("Final answer"))
        saved.append("turn_completed", run_id="a")
        saved.append("turn_started", prompt="Second", run_id="b")
        saved.append("turn_failed", run_id="b", error="boom")
        # A /resend retry keeps the turn and replaces its outcome.
        saved.append("turn_started", prompt="Second", run_id="c", continuation=True)
        saved.event(Message("Retry worked"))
        saved.append("turn_completed", run_id="c")
        saved.append("turn_started", prompt="Third", run_id="d")
        saved.append("turn_cancelled", run_id="d")
        with (saved.directory / "transcript.jsonl").open("a") as file:
            file.write('{"kind": "turn_started", "prompt": "torn')  # Torn final record.
        # `response` is the final text block; `blocks` keeps every one, in order.
        assert session_turns(saved.info, root) == [
            Turn(
                "First question", "Final answer", "complete", ["Interim thoughts", "Final answer"]
            ),
            Turn("Second", "Retry worked", "complete", ["Retry worked"]),
            Turn("Third", "", "cancelled", []),
        ]
        assert first_prompt(saved.info, root) == "First question"
    finally:
        saved.close()
    missing = saved.info.model_copy(update={"id": "nope"})
    assert session_turns(missing, root) is None
    assert first_prompt(missing, root) == "(Prompt unavailable)"


def test_resume_continues_a_copy_of_a_session_open_elsewhere(tmp_path):
    async def run():
        root = tmp_path / "sessions"
        saved = SavedSession.create("test:local", tmp_path, root)

        async def model(messages, info):
            yield "Saved answer"

        agent = Agent(FunctionModel(stream_function=model))
        elsewhere = AgentRuntime(agent, saved)  # Still open, as in another process.
        _ = [event async for event in elsewhere.stream("First question")]
        app = PreviewApp(workspace=tmp_path, session_dir=root, console=Console(file=StringIO()))
        with patch("pcode.agent.create_agent", return_value=agent):
            await app.controller.resume_session(saved.info.id)
        try:
            assert app.runtime.session.forked_from == saved.info.id
            assert app.runtime.session.info.id != saved.info.id
            assert app.runtime.history == elsewhere.history
            assert "open in another process" in repr(app.transcript.replay())
        finally:
            app.runtime.close()
            elsewhere.close()

    asyncio.run(run())


def test_resume_restores_before_replacing_runtime(tmp_path):
    async def run():
        root = tmp_path / "sessions"
        saved = SavedSession.create("test:local", tmp_path, root)
        identity = saved.info.id

        async def model(messages, info):
            yield "Saved answer"

        agent = Agent(FunctionModel(stream_function=model))
        previous = AgentRuntime(agent, saved)
        _ = [event async for event in previous.stream("First question")]
        history = previous.history
        previous.close()
        app = PreviewApp(workspace=tmp_path, session_dir=root, console=Console(file=StringIO()))
        app.handle("/resume")
        assert app.session_requested
        with patch("pcode.agent.create_agent", return_value=agent):
            await app.controller.resume_session(identity)
        try:
            assert app.runtime.session.info.id == identity
            assert app.runtime.conversation_id == identity
            assert app.model == "test:local"
            assert app.runtime.history == history
            assert app.runtime.turns == 1
            original = app.runtime
            await app.controller.resume_session(identity)
            assert app.runtime is original
            (tmp_path / "other").mkdir()
            other = SavedSession.create("test:local", tmp_path / "other", root)
            other_id = other.info.id
            other.close()
            with pytest.raises(SessionError, match="cross-repo"):
                await app.controller.resume_session(other_id)
            assert app.runtime is original
            # The failed candidate's lock was released.
            reopened = SavedSession.open(other_id, root)
            reopened.close()
        finally:
            app.runtime.close()

    asyncio.run(run())


def test_failed_recovery_keeps_current_conversation(tmp_path):
    async def run():
        root = tmp_path / "sessions"
        saved = SavedSession.create("test:local", tmp_path, root)
        identity = saved.info.id
        saved.close()
        app = PreviewApp(workspace=tmp_path, session_dir=root, console=Console(file=StringIO()))
        original = app.runtime
        with (
            patch("pcode.agent.create_agent", return_value=Agent("test")),
            patch("pcode.live.AgentRuntime.restore", side_effect=SessionError("broken")),
            pytest.raises(SessionError, match="broken"),
        ):
            await app.controller.resume_session(identity)
        assert app.runtime is original
        reopened = SavedSession.open(identity, root)
        reopened.close()

    asyncio.run(run())


def test_rename_names_the_session_for_resume(tmp_path):
    from pcode.sessions import list_sessions

    root = tmp_path / "sessions"
    app = PreviewApp(workspace=tmp_path, session_dir=root, console=Console(file=StringIO()))
    app.model = "test:local"
    app.runtime = AgentRuntime(Agent("test"), None)
    with pytest.raises(ValueError, match="first prompt"):
        app.controller.rename("Cache work")
    saved = SavedSession.create("test:local", tmp_path, root)
    app.runtime.session = saved
    try:
        updated = saved.info.updated
        app.controller.rename("  Cache \x1b[31m  work ")
        # One clean line, and naming is not activity: the date and order stay.
        assert [info.name for info in list_sessions(root)] == ["Cache [31m work"]
        assert saved.info.updated == updated
        app.controller.rename("Cache work")
        assert dict(app.controller.session_overview())["Name"] == "Cache work"
        app.controller.rename("")
        assert saved.info.name == "Cache work"
        app.controller.rename("-")
        assert [info.name for info in list_sessions(root)] == [None]
    finally:
        saved.close()


def sessions_browser(tmp_path, sessions):
    """A browser over sessions given as lists of (prompt, response), oldest first."""
    from pcode.runtime import Message
    from pcode.sessions import list_sessions

    root = tmp_path / "sessions"
    ids = []
    for turns in sessions:
        saved = SavedSession.create("test:local", tmp_path, root)
        for prompt, response in turns:
            saved.append("turn_started", prompt=prompt)
            saved.event(Message(response))
            saved.append("turn_completed")
        saved.save_info()
        saved.close()
        ids.append(saved.info.id)
    app = SessionBrowser(list_sessions(root), root=root, workspace=tmp_path, output=DummyOutput())
    return app, ids


def test_search_words_can_span_turns_and_rank_matches(tmp_path):
    app, (spread, together, unrelated) = sessions_browser(
        tmp_path,
        [
            [("Set up the redis cache", "Done"), ("Lint it", "ok"), ("Now the queue", "Done")],
            [("Redis queue retries", "Added")],
            [("Theme work", "Done")],
        ],
    )
    # Newest first with no query.
    assert [info.id for info in app.visible] == [unrelated, together, spread]
    app.query.text = "redis queue"
    # Both list, the one with every word in a single turn first; only turns with
    # a word in them are shown, and the words are marked.
    assert [info.id for info in app.visible] == [together, spread]
    app.list.buffer.cursor_down()
    assert app.selected.id == spread
    shown = app.detail.text()
    assert "2 of 3 turns" in shown and "Lint it" not in shown
    marked = "".join(
        text if "search-match" in style else " "
        for line in app.detail.lines(80)
        for style, text, *_ in line
    ).split()
    assert marked == ["redis", "queue"]
    # The first turn's own prompt is the top already; nothing to scroll to.
    assert app.first_match is None
    # More matching turns rank above fewer when neither has every word together.
    app.query.text = "done"
    assert [info.id for info in app.visible] == [spread, unrelated]


def test_browser_scrolls_to_a_match_below_the_top(tmp_path):
    app, _ = sessions_browser(
        tmp_path, [[("First", "Nothing here"), ("Second", "The needle is here")]]
    )
    app.query.text = "needle"
    assert "1 of 2 turns" in app.detail.text()
    # Heading, blank, prompt, blank, then the response holding the match.
    assert app.first_match == 4
    app.query.text = "second"
    assert app.first_match is None


def test_session_name_is_listed_and_searched(tmp_path):
    app, (named, other) = sessions_browser(
        tmp_path, [[("Fix it", "Done"), ("More", "ok")], [("Unrelated", "Done")]]
    )
    app.records[1].name = "Billing outage"
    app._titles.clear()
    app.refresh()
    assert app.records[1].id == named
    assert "Billing outage · Fix it" in app.list.text
    # A name word lists the session with every turn, and ranks it first.
    app.query.text = "billing"
    assert [info.id for info in app.visible] == [named]
    assert "2 turns" in app.detail.text()
    assert app.detail.text().startswith("Billing outage · ")
    app.query.text = "done"
    assert [info.id for info in app.visible] == [other, named]
    app.query.text = "outage done"
    assert [info.id for info in app.visible] == [named]
    # Typing a name word finds it from three letters; shorter stays content.
    app.query.text = "bil"
    assert [info.id for info in app.visible] == [named]
    app.query.text = "it"
    assert [info.id for info in app.visible] == [named]
    assert "1 of 2 turns" in app.detail.text()


def test_session_title_is_listed_searched_and_ranked_like_a_name(tmp_path):
    app, (titled, renamed, other) = sessions_browser(
        tmp_path,
        [
            [("Fix it", "Done")],
            [("Something", "Done")],
            [("Parser cleanup notes", "The parser is fine")],
        ],
    )
    by_id = {info.id: info for info in app.records}
    by_id[titled].title = "Parser crash fix"
    by_id[renamed].title = "Old title"
    by_id[renamed].name = "Chosen name"
    app._titles.clear()
    app.refresh()
    # The /rename name is shown over a title; a title shows where there is none.
    assert "Parser crash fix · Fix it" in app.list.text
    assert "Chosen name · Something" in app.list.text and "Old title" not in app.list.text
    # A title word ranks its session above one that only mentions the word.
    app.query.text = "parser"
    assert [info.id for info in app.visible] == [titled, other]
    assert app.detail.text().startswith("Parser crash fix · ")
    # A title replaced by /rename is still found.
    app.query.text = "old title"
    assert [info.id for info in app.visible] == [renamed]


def test_mark_matches_splits_fragments_case_insensitively():
    from pcode.popup_ui import SEARCH_MATCH, mark_matches

    line = [("bold", "Find the Ne"), ("", "edle here")]
    assert mark_matches(line, ["needle"]) == [
        ("bold", "Find the "),
        (f"bold {SEARCH_MATCH}", "Ne"),
        (f" {SEARCH_MATCH}", "edle"),
        ("", " here"),
    ]
    assert mark_matches(line, ["absent"]) is line
    # Folding that changes length keeps marks on the right characters.
    marked = mark_matches([("", "\u0130stanbul Stra\u00dfe needle")], ["strasse", "needle"])
    assert [text for style, text in marked if SEARCH_MATCH in style] == ["Stra\u00dfe", "needle"]
