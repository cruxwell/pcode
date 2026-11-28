"""Editor availability is independent of backend and optional metadata readiness."""

import asyncio
import subprocess
import sys
import threading
from io import StringIO
from unittest.mock import patch

import pytest
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput
from rich.console import Console

from pcode.app import PreviewApp
from pcode.runtime import Message
from pcode.ui import create_prompt


def test_cli_constructor_and_toolbar_do_not_import_agent_stack():
    # A fresh interpreter is essential: the suite itself imports the backend.
    subprocess.run(
        [
            sys.executable,
            "-c",
            """
import sys
from io import StringIO
from rich.console import Console
from pcode.app import PreviewApp, main
app = PreviewApp(model='test:local', console=Console(file=StringIO()))
app.toolbar()
PreviewApp(model='antigravity:gemini-4-neon', console=Console(file=StringIO())).toolbar()
# Stop at the boundary where the CLI would start the interactive event loop.
PreviewApp.run = lambda self: None
sys.stdin.isatty = lambda: True
sys.stdout.isatty = lambda: True
sys.argv = ['pcode', '-m', 'test:local', '--no-host', '--no-worktree']
main()
for name in ('pcode.sessions', 'pcode.agent', 'pcode.live', 'pcode.antigravity', 'pydantic_ai'):
    assert name not in sys.modules, name
""",
        ],
        check=True,
        capture_output=True,
        text=True,
    )


def test_provider_shutdown_wrapper_does_not_import_agent_stack():
    # `PreviewApp.run` wraps the editor in this; importing the providers up
    # front held the first frame back by a second or more.
    subprocess.run(
        [
            sys.executable,
            "-c",
            """
import asyncio, sys
from pcode.app import _stopping_provider_processes

async def editor():
    for name in ('pcode.claude_sdk', 'pcode.antigravity', 'pydantic_ai'):
        assert name not in sys.modules, name
    return 'done'

assert asyncio.run(_stopping_provider_processes(editor())) == 'done'
""",
        ],
        check=True,
        capture_output=True,
        text=True,
    )


def test_agent_stack_does_not_import_unused_provider_sdks():
    # Every session host loads these; the Anthropic SDK alone is ~80 MB per
    # host, and the OpenAI one ~50 MB, for sessions that never use them.
    subprocess.run(
        [
            sys.executable,
            "-c",
            """
import sys
import pcode.agent, pcode.live, pcode.output_limits, pcode.claude_sdk
from pcode.models import active_providers
active_providers('antigravity:gemini-4-neon')
for name in ('anthropic', 'openai', 'pydantic_ai.models.anthropic', 'pcode.claude_sdk.model'):
    assert name not in sys.modules, name
""",
        ],
        check=True,
        capture_output=True,
        text=True,
    )


def test_provider_shutdown_wrapper_stops_loaded_providers(monkeypatch):
    from types import SimpleNamespace

    from pcode.app import _stopping_provider_processes

    stopped = []

    async def stop(name):
        stopped.append(name)

    for name in ("pcode.claude_sdk.session_pool", "pcode.antigravity"):
        monkeypatch.setitem(
            sys.modules, name, SimpleNamespace(shutdown=lambda name=name: stop(name))
        )

    async def editor():
        raise RuntimeError("editor failed")

    with pytest.raises(RuntimeError):
        asyncio.run(_stopping_provider_processes(editor()))
    assert stopped == ["pcode.claude_sdk.session_pool", "pcode.antigravity"]


@pytest.mark.parametrize("outcome", ["success", "cancel", "failure", "quit", "restore", "commands"])
def test_editor_accepts_input_while_backend_starts(outcome):
    async def run():
        building = threading.Event()
        release = threading.Event()
        closed = threading.Event()
        metadata_started = asyncio.Event()
        metadata_cancelled = asyncio.Event()
        restore_started = asyncio.Event()
        restore_release = asyncio.Event()
        calls = []
        output = StringIO()

        class Runtime:
            session = None
            recovery_blocked = ""

            async def restore(self):
                restore_started.set()
                await restore_release.wait()

            async def refresh_context(self):
                metadata_started.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    metadata_cancelled.set()

            async def stream(self, text):
                calls.append(text)
                yield Message("response")

            def close(self):
                closed.set()

        def build():
            assert threading.current_thread() is not threading.main_thread()
            building.set()
            assert release.wait(8)
            if outcome == "failure":
                raise ValueError("api_key=sk-synthetic-startup-secret")
            return Runtime()

        app = PreviewApp(
            model="test:local", console=Console(file=output), resume=outcome == "restore"
        )
        app.controller._create_runtime = build
        # Resume replay needs a real saved session; recovery ordering is what
        # this test exercises. Existing session tests cover transcript replay.
        app.replay = lambda: None
        with create_pipe_input() as pipe:
            session = None

            def prompt(*args, **kwargs):
                nonlocal session
                session = create_prompt(*args, input=pipe, output=DummyOutput(), **kwargs)
                return session

            async def wait_for(predicate):
                for _ in range(500):
                    if predicate():
                        return
                    await asyncio.sleep(0.01)
                raise AssertionError(f"Timed out: {output.getvalue()}")

            async def drive():
                try:
                    await wait_for(lambda: building.is_set())
                    assert session.app.is_running
                    if outcome == "commands":
                        pipe.send_text("/effort high\r/theme light\r")
                        await wait_for(lambda: app.transcript.theme == "light")
                        # The frontend command must not wait behind /effort.
                        assert not release.is_set()
                    pipe.send_text("early draft")
                    await wait_for(lambda: session.default_buffer.text == "early draft")
                    assert not calls
                    if outcome == "quit":
                        pipe.send_text("\x03/status\r/quit\r")
                        await wait_for(lambda: not session.app.is_running)
                        release.set()
                        return
                    pipe.send_text("\r")
                    await wait_for(lambda: app.activity.queued == 1)
                    if outcome == "cancel":
                        pipe.send_text("\x03")
                        await wait_for(lambda: app.activity.queued == 0)
                    release.set()
                    if outcome == "restore":
                        await restore_started.wait()
                        assert not calls
                        pipe.send_text("typing during recovery")
                        await wait_for(
                            lambda: session.default_buffer.text == "typing during recovery"
                        )
                        restore_release.set()
                    await wait_for(lambda: not app._startup_pending)
                    if outcome == "failure":
                        await wait_for(lambda: "Agent startup failed" in output.getvalue())
                        assert not calls
                        assert "sk-synthetic-startup-secret" not in output.getvalue()
                        assert not app.activity.queued_prompts
                        pipe.send_text("retry\r")
                        await wait_for(lambda: "restart pcode" in output.getvalue())
                    else:
                        await metadata_started.wait()
                        if outcome != "cancel":
                            await wait_for(lambda: calls == ["early draft"])
                            await wait_for(lambda: not app.activity.busy)
                        else:
                            assert not calls
                        # A hung optional metadata request does not own input.
                        pipe.send_text("\x03still editable")
                        await wait_for(lambda: session.default_buffer.text == "still editable")
                    pipe.send_text("\x03/quit\r")
                finally:
                    release.set()
                    restore_release.set()

            with patch("pcode.app.create_prompt", prompt):
                await asyncio.wait_for(asyncio.gather(app.run_async(), drive()), timeout=10)
        if outcome == "quit":
            assert closed.is_set(), "late-created runtimes must be cleaned up on exit"
        elif outcome != "failure":
            assert metadata_cancelled.is_set()

    asyncio.run(run())


def test_startup_border_animation_lifecycle():
    from pcode.ui import STARTUP_SETTLE_SECONDS, Activity, PromptLayout

    activity = Activity()
    assert not activity.startup_animating
    activity.begin_startup("Starting session")
    assert activity.startup_animating
    assert activity.startup_label == "Starting session"

    # Success transitions into the brief settle sweep; failure clears at once.
    activity.finish_startup(ok=True)
    assert not activity.startup_label
    assert activity.startup_animating
    activity.startup_done_at -= STARTUP_SETTLE_SECONDS + 0.05
    assert not activity.startup_animating

    activity.begin_startup("Starting agent")
    activity.finish_startup(ok=False)
    assert not activity.startup_animating

    # Startup border renders cleanly in bordered mode at normal and narrow widths.
    app = PreviewApp(console=Console(file=StringIO()))
    with create_pipe_input() as pipe:
        session = create_prompt(
            app.registry,
            activity=activity,
            transcript=app.transcript,
            input=pipe,
            output=DummyOutput(),
        )
        layout = PromptLayout(session, activity, app.transcript, session.shortcuts)
        activity.begin_startup("Starting pcode")
        border = layout.startup_border()
        fragments = border.content.text()
        rendered = "".join(text for _, text in fragments)
        assert rendered.startswith("┌─ ")
        assert "Starting pcode" in rendered
        assert rendered.endswith("┐")
        assert len(rendered) == 80

        # Narrow width truncates cleanly without overflowing the pane width.
        layout.size = lambda: type("Size", (), {"columns": 14, "rows": 24})()
        narrow = "".join(text for _, text in border.content.text())
        assert narrow.startswith("┌─ ") and narrow.endswith("┐")
        assert len(narrow) == 14

        # Settle sweep keeps identical border geometry, and skips shaded mode.
        layout.size = lambda: type("Size", (), {"columns": 80, "rows": 24})()
        activity.finish_startup(ok=True)
        assert layout.startup_border_active()
        settle = "".join(text for _, text in border.content.text())
        assert settle.startswith("┌") and settle.endswith("┐")
        assert set(settle[1:-1]) == {"─"}

        activity.frame_style = "shaded"
        app.transcript.frame_shade = lambda: "#222222"
        assert not layout.startup_border_active()
        activity.begin_startup("Starting pcode")
        assert layout.startup_border_active()
        shaded = "".join(text for _, text in border.content.text())
        assert "Starting pcode" in shaded


def test_theme_cursor_probe_records_initial_row():
    from pcode import theme

    buf = bytearray(b"\x1b[12;1R\x1b[12;2R")
    assert theme._measure_width(buf)
    assert theme.take_initial_cursor_row() == 12
    assert theme.take_initial_cursor_row() is None

    buf = bytearray(b"\x1b[5;1R")
    assert theme._measure_cursor(buf)
    assert theme.take_initial_cursor_row() == 5
    assert theme.take_initial_cursor_row() is None


def test_welcome_returns_rendered_row_count():
    from pcode.ui import Transcript

    transcript = Transcript(Console(file=StringIO(), width=100), detected_theme="dark")
    rows = transcript.welcome("test:model", "/tmp/ws")
    assert rows == 6
    narrow = Transcript(Console(file=StringIO(), width=30), detected_theme="dark")
    assert narrow.welcome("test:model", "/tmp/a/very/long/workspace/path") > 6
