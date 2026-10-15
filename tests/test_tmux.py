"""Layout regression in real tmux, including cursor-position reports (CPR)."""

import json
import os
import pathlib
import shlex
import shutil
import signal
import subprocess
import sys
import time
import uuid

import pytest
from conftest import tmux_socket_dir

from pcode.ui import THOUGHT_ICON, THOUGHT_ROWS

pytestmark = pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux is not installed")

# How long a pane gets to reach an expected state. Every wait polls and returns
# the moment the state shows, so this costs time only when a test fails. Keep it
# generous: beside other tests (xdist workers, or another worktree's run) a cold
# pane can spend several seconds importing before its first paint.
TIMEOUT = 20

# Prepended to every pane script. A script that holds a turn open for the test
# to look inside awaits `gate()`, and the test calls `release()` once it has
# looked. A fixed sleep instead is a window that closes before a loaded machine
# gets to look, and makes an idle one wait it out.
GATE = """
import asyncio, os

async def gate(name="release"):
    path = os.path.join(os.environ["PCODE_TEST_GATES"], name)
    while not os.path.exists(path):
        await asyncio.sleep(0.02)
"""


def until(condition, message):
    """Poll ``condition`` until it holds, failing with ``message()`` after TIMEOUT."""
    deadline = time.monotonic() + TIMEOUT
    while not condition():
        if time.monotonic() > deadline:
            pytest.fail(message())
        time.sleep(0.05)


# Only a scrollback replay writes this: it clears history before rebuilding it.
REPLAY = b"\x1b[3J"


def resize(pane, *args):
    """Run a tmux command that resizes the app, then wait for its scrollback replay.

    The live panel repaints at the new size at once, but the debounced replay
    lands a beat later and rebuilds history from the transcript, dropping notes
    already shown. Settle a resize this way before asserting on a note, or a
    loaded machine looks only after the replay has erased it.
    """

    def size():
        return pane("display-message", "-p", "-t", "preview:0.0", "#{pane_width}x#{pane_height}")

    before, replays = size(), pane.output.read_bytes().count(REPLAY)
    pane(*args)
    if size() == before:
        return  # The app saw no resize, so nothing is replayed.
    until(
        lambda: pane.output.read_bytes().count(REPLAY) > replays,
        lambda: f"No scrollback replay after {' '.join(args)}",
    )


@pytest.fixture
def release(tmp_path):
    """Open the pane script's ``gate(name)``."""
    return lambda name="release": (tmp_path / name).touch()


def tmux_reaper(server, owner_pid):
    """Kill `server` once `owner_pid` exits, even if that exit skips teardown.

    A `-L` server detaches from pytest, so the fixture's `finally` is the only
    thing that stops it -- and that never runs under SIGKILL or a hard timeout.
    `start_new_session` keeps this reaper out of pytest's process group so a
    group-wide kill cannot take the reaper down with its owner.
    """
    script = (
        f"while kill -0 {owner_pid} 2>/dev/null; do sleep 2; done; "
        f"tmux -L {shlex.quote(server)} -f /dev/null kill-server 2>/dev/null"
    )
    return subprocess.Popen(
        ["sh", "-c", script],
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


@pytest.fixture
def pane(request, tmp_path):
    # A private server per test: nothing is shared with other tests, xdist
    # workers, or another worktree's run beyond the socket directory.
    server = "pcode-test-" + uuid.uuid4().hex
    base = ["tmux", "-L", server, "-f", "/dev/null"]
    env = {**os.environ, "PCODE_TEST_GATES": str(tmp_path)}
    # These tests exist because a PTY with PROMPT_TOOLKIT_NO_CPR=1 (as in
    # test_terminal.py) never exercises real prompt height: cursor-position
    # reports can stretch the layout into the remaining pane. Keep them on a
    # real tmux.
    env.pop("PROMPT_TOOLKIT_NO_CPR", None)
    # Most panes assert on the widget while the prompt is idle, so opt them out
    # of the default auto-hide; a test that wants it turns it back on itself.
    config = pathlib.Path(env["XDG_CONFIG_HOME"]) / "pcode"
    config.mkdir(parents=True, exist_ok=True)
    # The pane runs from this checkout, which ships .pcode/worktree-setup; trust
    # it up front or the launch prompt blocks the pane. Panes capture the screen
    # the moment a marker shows and expect scrollback to be complete at that
    # instant, so paced scrollback is off; its own test turns it on. Grouping
    # and hints are off as in conftest: these panes assert on per-call summary
    # lines and on the exact footer and task heading. Preserve their explicit
    # Ctrl chords too: subprocesses do not inherit conftest's default override.
    config.joinpath("preferences.json").write_text(
        json.dumps(
            {
                "autohide_tasks": "off",
                "tasks_min_rows": "0",
                "tasks_min_columns": "0",
                "project_extensions": "on",
                "paced_scrollback": "off",
                "group_tools": "off",
                "show_hints": "off",
                # Row counters find the editor by its border glyphs.
                "frame_style": "borders",
                "key_prefix": "ctrl",
            }
        )
    )
    reaper = tmux_reaper(server, os.getpid())

    def command(*args):
        return subprocess.check_output([*base, *args], text=True, env=env)

    try:
        command(
            "new-session",
            "-d",
            "-s",
            "preview",
            "-x",
            "100",
            "-y",
            "32",
            "-c",
            os.getcwd(),
            # This checkout's committed .pcode/preferences.json turns worktrees
            # on, so every main() launch would otherwise check out a real
            # .worktrees/<session> here and wait on its setup script before
            # drawing the prompt. `-c SCRIPT ARG` puts ARG in sys.argv, so this
            # reaches the scripts that call main() too; the rest ignore it.
            shlex.join(
                [sys.executable, "-c", GATE + request.param, "--no-worktree"]
                if hasattr(request, "param")
                else [sys.executable, "-m", "pcode.app", "--no-worktree"]
            ),
        )
        # Mirror everything the app writes, so a test can wait on terminal
        # events no screen capture shows (see `resize`).
        command.output = tmp_path / "pane-output"
        command.output.touch()
        command("pipe-pane", "-t", "preview:0.0", f"cat >> {shlex.quote(str(command.output))}")
        yield command
    finally:
        subprocess.run([*base, "kill-server"], capture_output=True, env=env)
        # kill-server returns before the server is gone, so a surviving server
        # here is wedged rather than merely slow.
        deadline = time.monotonic() + 5
        while subprocess.run([*base, "list-sessions"], capture_output=True).returncode == 0:
            if time.monotonic() > deadline:
                subprocess.run([*base, "kill-server"], capture_output=True, env=env)
                break
            time.sleep(0.05)
        (tmux_socket_dir() / server).unlink(missing_ok=True)
        os.killpg(reaper.pid, signal.SIGTERM)  # The reaper leads its own group.
        reaper.wait(timeout=5)


# The status rides the editor box's top border, led by a frame of the
# default `dots` spinner.
BUSY_FRAMES = tuple(f"┌─ {frame} " for frame in "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏")

# The footer always names the mode the next Enter sends with.
SEND_MODES = ("steering", "queue", "interrupt")


def busy(lines):
    """Whether the spinner rides the editor box's top border."""
    top = max((i for i, line in enumerate(lines) if line.startswith("┌")), default=None)
    return top is not None and lines[top].startswith(BUSY_FRAMES)


def capture(pane, expected, *, running=False, columns=None):
    """Allow asynchronous completion and resize paints to settle, with a deadline."""
    deadline = time.monotonic() + TIMEOUT
    while time.monotonic() < deadline:
        screen = pane("capture-pane", "-p", "-t", "preview:0.0")
        lines = screen.splitlines()
        if (
            expected in screen
            and len(lines) >= 2
            and lines[-2].startswith("└")
            and (columns is None or len(lines[-2]) == columns)
            and any(mode in lines[-1] for mode in SEND_MODES)
            and busy(lines) == running
        ):
            return screen
        time.sleep(0.05)
    pytest.fail(f"Prompt did not settle with {expected!r}:\n{screen}")


def settle(pane, ready, *, running=False):
    """The first settled screen that satisfies ``ready``, else the last one.

    The live status row repaints as soon as an event lands, but scrollback
    commits wait for the next flush handoff, so a status-row marker can show a
    frame or two before the rows written above it. Callers assert on the
    returned screen, so a timeout still fails with the screen in the message.
    """
    deadline = time.monotonic() + TIMEOUT
    while True:
        screen = capture(pane, "", running=running)
        if ready(screen) or time.monotonic() > deadline:
            return screen
        time.sleep(0.05)


# The last row `/theme-preview` writes: everything above it can scroll away.
GALLERY_TAIL = "pcode config set syntax_dark NAME"


def scrollback(pane):
    """History above the visible screen.

    The live panel is not scrollback, so a plain ``-S -`` capture (which
    includes the visible screen) cannot answer "did this reach history?" for
    anything the panel draws, such as an expiring notice.
    """
    return pane("capture-pane", "-p", "-S", "-", "-E", "-1", "-t", "preview:0.0")


# The status row is indented one column so the spinner lines up with the task
# rows inside the frame below it instead of hugging the terminal edge.
SPINNER_ROW = BUSY_FRAMES
# The step in progress turns with the running commands' `tool_spinner` (arc).
TASK_FRAMES = tuple("◜◠◝◞◡◟")


def active_task(text):
    """Every way an in-progress task row can read on a given frame."""
    return tuple(f"{frame}{text}" for frame in TASK_FRAMES)


def _thought_span(lines):
    """Indices of the thinking rows: marked first row, directly above the spinner.

    A blank row always separates the panel from scrollback, and the cap is
    ui.THOUGHT_ROWS, so this never reaches a scrollback line.
    """
    status = next((i for i, line in enumerate(lines) if line.startswith(SPINNER_ROW)), None)
    if status is None:
        return range(0)
    for start in range(status - 1, max(-1, status - 1 - THOUGHT_ROWS), -1):
        if not lines[start].strip():
            break
        if lines[start].startswith(f" {THOUGHT_ICON} "):
            return range(start, status)
    return range(0)


def thought_row(screen):
    """The thinking rows above the status row, without their icon, joined with newlines."""
    lines = screen.splitlines()
    return "\n".join(
        lines[i].strip().removeprefix(THOUGHT_ICON).strip() for i in _thought_span(lines)
    )


def without_status_row(text):
    """A capture minus the status row and the thinking rows above it: scrollback only."""
    lines = text.splitlines()
    thought = set(_thought_span(lines))
    return "\n".join(
        line
        for index, line in enumerate(lines)
        if index not in thought and not line.startswith(SPINNER_ROW)
    )


def input_rows(screen):
    lines = screen.splitlines()
    assert any(mode in lines[-1] for mode in SEND_MODES), screen
    assert lines[-2].startswith("└"), screen
    cursor = next(i for i, line in enumerate(lines) if line.startswith("│❯"))
    # Attached tasks sit above the divider; count only the editor's text rows.
    top = max(i for i, line in enumerate(lines[:cursor]) if line.startswith(("┌", "├")))
    bottom = next(i for i, line in enumerate(lines[top + 1 :], top + 1) if line.startswith("└"))
    return bottom - top - 1


def test_footer_theme_switch_keeps_editor_compact(pane):
    assert input_rows(capture(pane, "❯")) == 1
    for syntax in ("terminal", "gruvbox-dark"):
        pane("send-keys", "-t", "preview:0.0", "-l", f"/syntax {syntax}")
        pane("send-keys", "-t", "preview:0.0", "Enter")
        assert input_rows(capture(pane, f"): {syntax}.")) == 1
        for theme in ("light", "dark", "auto"):
            pane("send-keys", "-t", "preview:0.0", "-l", f"/theme {theme}")
            pane("send-keys", "-t", "preview:0.0", "Enter")
            screen = capture(pane, "Theme: auto (" if theme == "auto" else f"Theme: {theme}.")
            assert input_rows(screen) == 1
            assert "· preview" in screen.splitlines()[-1]
            pane("send-keys", "-t", "preview:0.0", "-l", "/theme-preview")
            pane("send-keys", "-t", "preview:0.0", "Enter")
            # The style gallery scrolls the sample away, so wait on its last row.
            assert input_rows(capture(pane, GALLERY_TAIL)) == 1


def test_terminal_appearance_report_repaints_auto_theme(pane):
    capture(pane, "❯")
    pane("send-keys", "-t", "preview:0.0", "-l", "/theme auto")
    pane("send-keys", "-t", "preview:0.0", "Enter")
    screen = capture(pane, "Theme: auto (")
    assert b"\x1b[?2031h" in pane.output.read_bytes()
    detected = "light" if "Theme: auto (light)" in screen else "dark"
    # Mode 2031 reports 1 for dark and 2 for light: report the other one.
    report = "\x1b[?997;1n" if detected == "light" else "\x1b[?997;2n"

    def colors():
        return pane("capture-pane", "-p", "-e", "-t", "preview:0.0")

    before, replays = colors(), pane.output.read_bytes().count(REPLAY)
    pane("send-keys", "-t", "preview:0.0", "-l", report + "hi")
    until(
        lambda: pane.output.read_bytes().count(REPLAY) > replays,
        lambda: "No repaint after the appearance report",
    )
    screen = capture(pane, "❯ hi")  # Typing around the report is untouched.
    assert "997" not in screen
    pane("send-keys", "-t", "preview:0.0", "C-u")
    capture(pane, "❯")
    until(lambda: colors() != before, lambda: "Palette unchanged after the report")


def test_transcript_uses_terminal_scrollback(pane):
    assert input_rows(capture(pane, "❯")) == 1
    pane("send-keys", "-t", "preview:0.0", "-l", "/theme-preview")
    pane("send-keys", "-t", "preview:0.0", "Enter")
    assert input_rows(capture(pane, GALLERY_TAIL)) == 1
    history = pane("capture-pane", "-p", "-S", "-", "-t", "preview:0.0")
    assert "Hello, world!" in history
    assert "No files were" in history
    assert pane("display-message", "-p", "-t", "preview:0.0", "#{alternate_on}").strip() == "0"
    assert "pcode  /  UI preview" in history


def test_closing_a_menu_that_scrolled_the_screen_restores_the_transcript(pane):
    capture(pane, "❯")
    pane("send-keys", "-t", "preview:0.0", "-l", "/theme-preview")
    pane("send-keys", "-t", "preview:0.0", "Enter")
    capture(pane, GALLERY_TAIL)
    # A full screen of transcript: the menu has no room and scrolls it away.
    pane("send-keys", "-t", "preview:0.0", "-l", "/")
    capture(pane, "List commands")
    pane("send-keys", "-t", "preview:0.0", "C-c")

    def above_editor(screen):
        lines = screen.splitlines()
        return lines[max(i for i, line in enumerate(lines) if line.startswith("┌")) - 1]

    screen = settle(pane, lambda screen: "Input discarded" in above_editor(screen))
    # Before the replay the screen kept the menu's height as blank rows.
    assert "Input discarded" in above_editor(screen), screen
    assert screen.splitlines()[0].strip(), screen


@pytest.mark.parametrize("split", ["-h", "-v"])
def test_input_only_grows_for_text(pane, split):
    assert input_rows(capture(pane, "❯")) == 1
    pane("split-window", split, "-t", "preview:0.0", "cat")
    pane("send-keys", "-t", "preview:0.0", "-l", "hello")
    assert input_rows(capture(pane, "hello")) == 1

    pane("send-keys", "-t", "preview:0.0", "Escape", "Enter")
    pane("send-keys", "-t", "preview:0.0", "-l", "second line")
    assert input_rows(capture(pane, "second line")) == 2

    pane("send-keys", "-t", "preview:0.0", "C-c")
    pane("send-keys", "-t", "preview:0.0", "-l", "/")
    # Match the menu row, not the banner's "/help for keys". The menu can fill
    # a short split to its top row, so there is no newline to anchor on, and a
    # narrow split truncates the description.
    screen = capture(pane, "List commands")
    assert input_rows(screen) == 1
    # Assert on the first command, which is always in view, rather than a
    # lower entry that a new command can push off the list.
    lines = screen.splitlines()
    help_row = next(i for i, line in enumerate(lines) if line.startswith(" /help "))
    top = max(i for i, line in enumerate(lines) if line.startswith("┌"))
    assert help_row < top  # Menu above the fixed frame.

    pane("send-keys", "-t", "preview:0.0", "C-c")
    text = "x" * 120 + "END"
    pane("send-keys", "-t", "preview:0.0", "-l", text)
    screen = capture(pane, "END")
    assert input_rows(screen) > 1  # Wrapped input, not just explicit newlines.

    pane("send-keys", "-t", "preview:0.0", "C-u")
    pane("send-keys", "-t", "preview:0.0", "-l", "short")
    assert input_rows(capture(pane, "short")) == 1
    pane("kill-pane", "-t", "preview:0.1")
    pane("send-keys", "-t", "preview:0.0", "-l", " again")
    assert input_rows(capture(pane, "short again")) == 1


LIVE_SCRIPT = """
from pydantic_ai import Agent
from pydantic_ai.models.function import FunctionModel
from pcode.app import PreviewApp
from pcode.live import AgentRuntime

async def model(messages, info):
    yield "COMMITTED LINE\\n\\nFIRST STREAM CHUNK"
    await gate()
    yield "\\n\\nLIVE ANSWER COMPLETE"

runtime = AgentRuntime(Agent(FunctionModel(stream_function=model)))
PreviewApp(model="test:local", runtime=runtime).run()
"""


@pytest.mark.parametrize("pane", [LIVE_SCRIPT], indirect=True)
def test_stream_keeps_prompt_at_bottom_and_commits_once(pane, release):
    capture(pane, "❯")
    pane("send-keys", "-t", "preview:0.0", "-l", "hello")
    pane("send-keys", "-t", "preview:0.0", "Enter")
    streaming = capture(pane, "COMMITTED LINE", running=True)
    assert input_rows(streaming) == 1
    assert "COMMITTED LINE" in streaming
    # The spinner row reports live work; the prompt itself is already in scrollback.
    assert any(line.startswith(SPINNER_ROW) for line in streaming.splitlines())
    assert not any(
        line.startswith(SPINNER_ROW) and line.endswith(" hello") for line in streaming.splitlines()
    )
    assert "▌ hello" in streaming
    assert "FIRST STREAM CHUNK" not in streaming
    release()
    completed = capture(pane, "LIVE ANSWER COMPLETE")
    assert input_rows(completed) == 1
    assert "✓ hello" not in completed
    assert "FIRST STREAM CHUNK" in completed
    history = pane("capture-pane", "-p", "-S", "-", "-t", "preview:0.0")
    assert history.count("FIRST STREAM CHUNK") == 1
    assert "▌ hello" in history


@pytest.mark.parametrize("pane", [LIVE_SCRIPT], indirect=True)
def test_stream_resize_and_cancellation(pane):
    capture(pane, "❯")
    pane("send-keys", "-t", "preview:0.0", "-l", "hello")
    pane("send-keys", "-t", "preview:0.0", "Enter")
    capture(pane, "COMMITTED LINE", running=True)
    resize(pane, "split-window", "-v", "-t", "preview:0.0", "cat")
    assert input_rows(capture(pane, "❯", running=True)) == 1
    pane("send-keys", "-t", "preview:0.0", "-l", "next input")
    capture(pane, "❯ next input", running=True)
    # Ctrl+C clears the draft first; only an empty prompt cancels the run.
    pane("send-keys", "-t", "preview:0.0", "C-c")
    discarded = capture(pane, "Input discarded", running=True)
    assert "❯ next input" not in discarded
    pane("send-keys", "-t", "preview:0.0", "C-c")
    cancelled = capture(pane, "! Run cancelled")
    assert input_rows(cancelled) == 1


@pytest.mark.parametrize("pane", [LIVE_SCRIPT], indirect=True)
def test_draft_and_cursor_survive_stream_completion_and_width_resize(pane, release):
    capture(pane, "❯")
    pane("send-keys", "-t", "preview:0.0", "h", "Enter")
    capture(pane, "COMMITTED LINE", running=True)
    pane("send-keys", "-t", "preview:0.0", "-l", "draft text")
    pane("send-keys", "-t", "preview:0.0", "Left", "Left", "Left", "Left")
    capture(pane, "❯ draft text", running=True)
    resize(pane, "split-window", "-h", "-t", "preview:0.0", "cat")
    capture(pane, "❯ draft text", running=True)
    release()
    screen = capture(pane, "LIVE ANSWER COMPLETE")
    assert "❯ draft text" in screen
    pane("send-keys", "-t", "preview:0.0", "-l", "my ")
    capture(pane, "❯ draft my text")
    history = pane("capture-pane", "-p", "-S", "-", "-t", "preview:0.0")
    assert history.count("COMMITTED LINE") == 1


@pytest.mark.parametrize("pane", [LIVE_SCRIPT], indirect=True)
def test_shell_draft_swaps_prompt_marker(pane):
    capture(pane, "❯")
    pane("send-keys", "-t", "preview:0.0", "-l", "!ls")
    assert "❯ !ls" not in capture(pane, "$ !ls")
    pane("send-keys", "-t", "preview:0.0", "C-a", "Delete")
    capture(pane, "❯ ls")


@pytest.mark.parametrize("pane", [LIVE_SCRIPT], indirect=True)
def test_immediate_cancellation_unlocks_editor(pane):
    capture(pane, "❯")
    # Deliver submission and cancellation together, before the model task can start.
    pane("send-keys", "-t", "preview:0.0", "h", "Enter", "C-c")
    capture(pane, "! Run cancelled")
    pane("send-keys", "-t", "preview:0.0", "-l", "editable again")
    assert input_rows(capture(pane, "editable again")) == 1


@pytest.mark.parametrize("pane", [LIVE_SCRIPT], indirect=True)
def test_width_resize_does_not_leave_a_copy_of_unfinished_line(pane, release):
    capture(pane, "❯")
    pane("send-keys", "-t", "preview:0.0", "h", "Enter")
    capture(pane, "COMMITTED LINE", running=True)
    pane("split-window", "-h", "-t", "preview:0.0", "cat")
    capture(pane, "❯", running=True, columns=50)
    release()
    capture(pane, "LIVE ANSWER COMPLETE")
    history = pane("capture-pane", "-p", "-S", "-", "-t", "preview:0.0")
    assert history.count("FIRST STREAM CHUNK") == 1


def test_cancel_history_search_discards_draft(pane):
    capture(pane, "❯")
    pane("send-keys", "-t", "preview:0.0", "-l", "draft")
    pane("send-keys", "-t", "preview:0.0", "C-r", "C-c")
    capture(pane, "Input discarded.")
    pane("send-keys", "-t", "preview:0.0", "-l", "fresh")
    screen = capture(pane, "❯ fresh")
    assert "draft" not in screen


LONG_SCRIPT = """
import asyncio
from pydantic_ai import Agent
from pydantic_ai.models.function import FunctionModel
from pcode.app import PreviewApp
from pcode.live import AgentRuntime

async def model(messages, info):
    for i in range(80):
        yield f"LINE_{i:03d}\\n\\n"
        await asyncio.sleep(0.005)
    yield "**FORMATTED MARKDOWN** " + "wide界 " * 60 + "TAIL_MARKER"
    await gate()
    yield " FINISHED"

runtime = AgentRuntime(Agent(FunctionModel(stream_function=model)))
PreviewApp(model="test:local", runtime=runtime).run()
"""


@pytest.mark.parametrize("pane", [LONG_SCRIPT], indirect=True)
def test_long_stream_remains_in_scrollback_without_truncation(pane, release):
    capture(pane, "❯")
    pane("send-keys", "-t", "preview:0.0", "h", "Enter")
    screen = capture(pane, "LINE_079", running=True)
    assert "TAIL_MARKER" not in screen
    pane("send-keys", "-t", "preview:0.0", "-l", "still editable")
    capture(pane, "❯ still editable", running=True)
    release()
    screen = capture(pane, "FINISHED")
    assert "❯ still editable" in screen
    history = pane("capture-pane", "-p", "-S", "-", "-t", "preview:0.0")
    for i in range(80):
        assert history.count(f"LINE_{i:03d}") == 1
    assert history.count("FORMATTED MARKDOWN") == 1
    assert "**FORMATTED MARKDOWN**" not in history
    assert history.count("TAIL_MARKER") == 1


WORD_WRAP_SCRIPT = """
from pydantic_ai import Agent
from pydantic_ai.models.function import FunctionModel
from pcode.app import PreviewApp
from pcode.live import AgentRuntime

async def model(messages, info):
    yield "WRAP_START\\n\\n"
    for char in "streaming boundaries " * 30:
        yield char
    yield "\\n\\nWRAP_END"

runtime = AgentRuntime(Agent(FunctionModel(stream_function=model)))
PreviewApp(model="test:local", runtime=runtime).run()
"""


@pytest.mark.parametrize("split", [False, True])
@pytest.mark.parametrize("pane", [WORD_WRAP_SCRIPT], indirect=True)
def test_words_stay_whole_in_regular_and_split_panes(pane, split):
    capture(pane, "❯")
    if split:
        pane("split-window", "-h", "-t", "preview:0.0", "cat")
        capture(pane, "❯", columns=50)
    pane("send-keys", "-t", "preview:0.0", "h", "Enter")
    capture(pane, "WRAP_END")
    history = pane("capture-pane", "-p", "-S", "-", "-t", "preview:0.0")
    text = history.split("WRAP_START\n", 1)[1].split("WRAP_END", 1)[0]
    assert text.split() == ["streaming", "boundaries"] * 30


CURSOR_SCRIPT = """
import asyncio
import time
from rich.console import Console
from pydantic_ai import Agent
from pydantic_ai.models.function import FunctionModel
from pcode.app import PreviewApp
from pcode.live import AgentRuntime

class SlowConsole(Console):
    def print(self, *objects, **kwargs):
        super().print(*objects, **kwargs)
        if any("CURSOR_LINE_" in getattr(obj, "markup", str(obj)) for obj in objects):
            # Enlarge the handoff window so cursor visibility can be sampled
            # deterministically, even on a fast terminal.
            time.sleep(0.15)

async def model(messages, info):
    await gate()
    for i in range(12):
        yield f"CURSOR_LINE_{i:03d}\\n\\n"
        await asyncio.sleep(0.05)
    yield "CURSOR_STREAM_DONE"

runtime = AgentRuntime(Agent(FunctionModel(stream_function=model)))
PreviewApp(model="test:local", runtime=runtime, console=SlowConsole()).run()
"""


@pytest.mark.parametrize("pane", [CURSOR_SCRIPT], indirect=True)
def test_cursor_is_hidden_while_committing_stream_and_returns_to_draft(pane, release):
    capture(pane, "❯")
    pane("send-keys", "-t", "preview:0.0", "h", "Enter")
    pane("send-keys", "-t", "preview:0.0", "-l", "draft text")
    pane("send-keys", "-t", "preview:0.0", "Left", "Left", "Left", "Left")
    capture(pane, "❯ draft text", running=True)
    release()
    samples = 0
    deadline = time.monotonic() + TIMEOUT
    while time.monotonic() < deadline:
        # These commands run in one tmux invocation, so the screen and cursor
        # mode describe the same terminal state rather than different paints.
        snapshot = pane(
            "capture-pane",
            "-p",
            "-t",
            "preview:0.0",
            ";",
            "display-message",
            "-p",
            "-t",
            "preview:0.0",
            "CURSOR_STATE #{cursor_flag} #{cursor_x} #{cursor_y}",
        )
        screen, state = snapshot.rsplit("CURSOR_STATE ", 1)
        visible, x, y = map(int, state.split())
        lines = screen.splitlines()
        if "CURSOR_LINE_" in screen and not any(line.startswith("│❯") for line in lines):
            samples += 1
            assert not visible, snapshot
        if (
            "CURSOR_STREAM_DONE" in screen
            and any(mode in lines[-1] for mode in SEND_MODES)
            and not busy(lines)
        ):
            assert visible, snapshot
            assert lines[y].startswith("│❯ draft text"), snapshot
            assert x == 9, snapshot  # Three-cell prompt plus 'draft '.
            break
        time.sleep(0.01)
    else:
        pytest.fail(f"Stream did not complete:\n{snapshot}")
    assert samples > 0, "Did not observe a transcript handoff"


MARKDOWN_SCRIPT = """
from pydantic_ai import Agent
from pydantic_ai.models.function import FunctionModel
from pcode.app import PreviewApp
from pcode.live import AgentRuntime

async def model(messages, info):
    yield "**Styled response**\\n\\n```python\\n"
    for i in range(60):
        yield f"value_{i:03d} = {i}\\n"
    yield "# PREVIEW_MARKER"
    await gate()
    yield "\\n```\\n\\nMARKDOWN_DONE"

runtime = AgentRuntime(Agent(FunctionModel(stream_function=model)))
PreviewApp(model="test:local", runtime=runtime).run()
"""


@pytest.mark.parametrize("pane", [MARKDOWN_SCRIPT], indirect=True)
def test_markdown_code_stays_hidden_until_committed_once(pane, release):
    capture(pane, "❯")
    pane("send-keys", "-t", "preview:0.0", "h", "Enter")
    screen = capture(pane, "Styled response", running=True)
    assert "PREVIEW_MARKER" not in screen
    assert input_rows(screen) == 1
    assert "**Styled response**" not in screen
    assert "value_000" not in screen  # Still buffered, not a pane-sized live block.
    pane("send-keys", "-t", "preview:0.0", "-l", "draft survives")
    capture(pane, "❯ draft survives", running=True)
    release()
    assert input_rows(capture(pane, "MARKDOWN_DONE")) == 1
    history = pane("capture-pane", "-p", "-S", "-", "-t", "preview:0.0")
    for i in range(60):
        assert history.count(f"value_{i:03d}") == 1
    assert history.count("PREVIEW_MARKER") == 1
    assert "```" not in history
    assert "❯ draft survives" in history


PACED_SCRIPT = """
import pcode.ui
from pcode.app import PreviewApp
from pcode.preferences import save_preferences
from pcode.runtime import Message, TextDelta

save_preferences(paced_scrollback="rows")
# One row a frame and no catch-up: 600 rows take twenty seconds, so the test
# sees the block half written however loaded the machine is. The second turn
# lifts the limit once the test opens the gate, and the rest lands at once.
pcode.ui.PACED_DRAIN_FRAMES = 10**9

class Runtime:
    session = None
    turns = 0

    async def stream(self, prompt):
        self.turns += 1
        if self.turns == 1:
            block = "\\n".join(f"PACED_ROW_{i:03d}" for i in range(600))
            yield TextDelta("```text\\n" + block + "\\n```\\n\\nBLOCK_SETTLED\\n\\n")
            yield Message("")
        else:
            await gate()
            pcode.ui.PACED_DRAIN_FRAMES = 1
            yield Message("SECOND_TURN_DONE")

PreviewApp(model="test:local", runtime=Runtime()).run()
"""


@pytest.mark.parametrize("pane", [PACED_SCRIPT], indirect=True)
def test_paced_scrollback_rolls_a_block_out_and_keeps_taking_input(pane, release):
    capture(pane, "❯")
    pane("send-keys", "-t", "preview:0.0", "h", "Enter")
    deadline = time.monotonic() + TIMEOUT
    while "PACED_ROW_000" not in (history := scrollback(pane)):
        assert time.monotonic() < deadline, "The block never started to appear"
        time.sleep(0.02)
    # The block is written a row per frame, so the first row shows while the
    # last is still queued.
    assert "PACED_ROW_599" not in history
    # Typing during the roll-out reaches the editor unchanged: the handoffs stay
    # in raw mode, so Return submits rather than landing as a newline.
    pane("send-keys", "-t", "preview:0.0", "-l", "next")
    pane("send-keys", "-t", "preview:0.0", "Enter")
    # The second turn is running (its quote queues behind the block) and the
    # editor is empty again, all while the block is still rolling out.
    screen = capture(pane, "❯", running=True)
    assert "next" not in screen
    assert "PACED_ROW_599" not in scrollback(pane)
    release()
    capture(pane, "SECOND_TURN_DONE")
    history = pane("capture-pane", "-p", "-S", "-", "-t", "preview:0.0")
    positions = [history.index(f"PACED_ROW_{i:03d}") for i in range(600)]
    assert positions == sorted(positions)
    assert history.count("PACED_ROW_599") == 1
    assert positions[-1] < history.index("BLOCK_SETTLED") < history.index("▌ next")
    assert history.index("▌ next") < history.index("SECOND_TURN_DONE")


TYPED_SCRIPT = """
import pcode.ui
from pcode.app import PreviewApp
from pcode.preferences import save_preferences
from pcode.runtime import Message, TextDelta

save_preferences(paced_scrollback="typed")
# One character a step and no catch-up: the paragraph takes over a minute, so
# the test sees it half typed however loaded the machine is, until the gate.
pcode.ui.TYPED_CHARS_PER_STEP = 1
pcode.ui.TYPED_DRAIN_STEPS = 10**9

class Runtime:
    session = None

    async def stream(self, prompt):
        words = " ".join(f"TYPED_W{i:03d}" for i in range(120))
        yield TextDelta(words + "\\n\\nTYPED_DONE\\n\\n")
        await gate()
        pcode.ui.TYPED_CHARS_PER_STEP = 10**6
        yield Message("")

PreviewApp(model="test:local", runtime=Runtime()).run()
"""


@pytest.mark.parametrize("pane", [TYPED_SCRIPT], indirect=True)
def test_typed_scrollback_types_prose_live_and_writes_it_once(pane, release):
    capture(pane, "❯")
    pane("send-keys", "-t", "preview:0.0", "h", "Enter")
    screen = capture(pane, "TYPED_W000", running=True)
    assert "TYPED_W119" not in screen
    release()
    capture(pane, "TYPED_DONE")
    history = pane("capture-pane", "-p", "-S", "-", "-t", "preview:0.0")
    # The live row is erased as its text is written, never left behind.
    for i in range(120):
        assert history.count(f"TYPED_W{i:03d}") == 1
    assert history.index("TYPED_W119") < history.index("TYPED_DONE")


def single_editor_history(pane, marker, *, frames=1):
    """History-inclusive check; a resize erase/repaint is not an atomic write."""
    deadline = time.monotonic() + TIMEOUT
    while time.monotonic() < deadline:
        history = pane("capture-pane", "-p", "-S", "-", "-t", "preview:0.0")
        if history.count("┌") == history.count("└") == frames and history.count(marker) == 1:
            return history
        time.sleep(0.05)
    pytest.fail(
        f"Expected {frames} frames and one {marker!r} across screen and history:\n{history}"
    )


@pytest.mark.parametrize("multiline", [False, True])
@pytest.mark.parametrize("split", ["-h", "-v"])
def test_repeated_resize_keeps_one_editor_frame_and_draft(pane, multiline, split):
    capture(pane, "❯")
    pane("send-keys", "-t", "preview:0.0", "-l", "RESIZE_DRAFT")
    if multiline:
        pane("send-keys", "-t", "preview:0.0", "Escape", "Enter")
        pane("send-keys", "-t", "preview:0.0", "-l", "second line " * 8)
    capture(pane, "RESIZE_DRAFT")
    for _ in range(3):
        pane("split-window", split, "-t", "preview:0.0", "cat")
        width = int(pane("display-message", "-p", "-t", "preview:0.0", "#{pane_width}"))
        capture(pane, "RESIZE_DRAFT", columns=width)
        single_editor_history(pane, "RESIZE_DRAFT")
        pane("kill-pane", "-t", "preview:0.1")
        capture(pane, "RESIZE_DRAFT", columns=100)
        single_editor_history(pane, "RESIZE_DRAFT")
    pane("send-keys", "-t", "preview:0.0", "C-a")
    pane("send-keys", "-t", "preview:0.0", "-l", "inserted ")
    capture(pane, "inserted " + ("second line" if multiline else "RESIZE_DRAFT"))


PLAN_SCRIPT = r"""
from pcode.app import PreviewApp
from pcode.runtime import PlanUpdated, ToolSummary

class Runtime:
    session = None
    async def stream(self, prompt):
        items = [{"id": str(i), "content": f"Task {i}", "status": "pending"} for i in range(12)]
        items[8]["status"] = "in_progress"
        yield PlanUpdated(items)
        yield ToolSummary("write_plan", "Plan updated")
        await gate()
        yield PlanUpdated([dict(item, status="completed") for item in items])
    def reset(self):
        pass

PreviewApp(model="test:local", runtime=Runtime()).run()
"""


@pytest.mark.parametrize("pane", [PLAN_SCRIPT], indirect=True)
@pytest.mark.parametrize("split", ["-h", "-v"])
def test_plan_panel_is_bounded_updates_and_clears(pane, release, split):
    capture(pane, "❯")
    pane("send-keys", "-t", "preview:0.0", "h", "Enter")
    screen = capture(pane, "Task 8", running=True)
    assert any(row in screen for row in active_task(" Task 8"))
    first_frame = next(line[3] for line in screen.splitlines() if line.startswith(SPINNER_ROW))
    deadline = time.monotonic() + TIMEOUT
    while time.monotonic() < deadline:
        # A raw capture can land between erasing the prompt and redrawing it.
        animated = capture(pane, "", running=True)
        assert any(row in animated for row in active_task(" Task 8"))
        if any(
            line.startswith(SPINNER_ROW) and line[3] != first_frame
            for line in animated.splitlines()
        ):
            break
        time.sleep(0.03)
    else:
        pytest.fail("Status spinner did not animate while the plan marker stayed static")
    assert "Tasks ·" not in screen and "Tools" not in screen
    lines = screen.splitlines()
    first_task = next(i for i, line in enumerate(lines) if "Task 6" in line)
    # The status rides the box's top border; the tasks hang straight under it.
    assert lines[first_task - 1].startswith(SPINNER_ROW)
    # The heading's count moves to the status's right-hand side.
    assert "Tasks 0/12 · " in lines[first_task - 1]
    assert screen.count("Tasks 0/12") == 1
    assert all(
        line.startswith("│") and line.endswith("│") for line in lines[first_task : first_task + 5]
    )
    assert lines[first_task + 5].startswith("├")
    assert input_rows(screen) == 1
    assert "Task 0\n" not in screen
    pane("send-keys", "-t", "preview:0.0", "-l", "editable draft")
    resize(pane, "split-window", split, "-t", "preview:0.0", "cat")
    release()
    completed = capture(pane, "✓ Task 0")
    assert any(line.startswith("┌─ Tasks 12/12 · ✓ ─") for line in completed.splitlines())
    assert "│✓ Task 0" in completed
    assert input_rows(completed) == 1
    assert completed.count("┌") == completed.count("└") == 1
    resize(pane, "kill-pane", "-t", "preview:0.1")
    capture(pane, "editable draft", columns=100)
    history = single_editor_history(pane, "editable draft")
    assert "Plan updated" not in history
    pane("send-keys", "-t", "preview:0.0", "C-c")
    pane("send-keys", "-t", "preview:0.0", "-l", "/new")
    pane("send-keys", "-t", "preview:0.0", "Enter")
    screen = capture(pane, "Context reset")
    assert "Tasks" not in screen
    assert "Task 0" not in screen
    # /new zeroes the whole widget: tasks, tools, and the live status row.
    assert "✓ h" not in screen
    assert screen.count("┌") == screen.count("└") == 1
    # The clear reaches scrollback too, not just the visible rows.
    history = pane("capture-pane", "-p", "-S", "-", "-t", "preview:0.0")
    assert "Type / for commands" not in history


PAUSED_STREAM_SCRIPT = """
import asyncio
from pydantic_ai import Agent
from pydantic_ai.models.function import FunctionModel
from pcode.app import PreviewApp
from pcode.live import AgentRuntime

async def model(messages, info):
    yield "COMMITTED MARKER\\n\\n" + "x" * 50 + "PAUSED_TAIL"
    await asyncio.sleep(30)

runtime = AgentRuntime(Agent(FunctionModel(stream_function=model)))
PreviewApp(model="test:local", runtime=runtime).run()
"""


@pytest.mark.parametrize("pane", [PAUSED_STREAM_SCRIPT], indirect=True)
def test_paused_stream_stays_hidden_on_resize_without_more_tokens(pane):
    capture(pane, "❯")
    pane("send-keys", "-t", "preview:0.0", "h", "Enter")
    capture(pane, "COMMITTED MARKER", running=True)
    pane("send-keys", "-t", "preview:0.0", "-l", "keep draft")
    for columns in (40, 100, 35):
        pane("resize-window", "-t", "preview:0", "-x", str(columns))
        screen = capture(pane, "❯", running=True, columns=columns)
        history = pane("capture-pane", "-p", "-S", "-", "-t", "preview:0.0")
        assert "PAUSED_TAIL" not in history
        assert "x" * 20 not in history
        assert "│❯ keep draft" in screen
        assert input_rows(screen) == 1


# The running command's tool row, above the status row that names its verb.
TOOL_ROW = "$ printf FIRST_DETAIL"


TOOLS_SCRIPT = """
import asyncio
from pcode.app import PreviewApp
from pcode.preferences import save_preferences
from pcode.runtime import TextDelta, ToolStarted, ToolSummary

save_preferences(tool_error_scrollback="on")

class Runtime:
    session = None
    turns = 0

    async def stream(self, prompt):
        yield TextDelta("MODEL CONVERSATION ONLY\\n\\n")
        for i in range(1, 13):
            yield ToolStarted("read_file", f"file_{i:02d}.py", str(i))
            await asyncio.sleep(0.02)
            yield ToolSummary(
                "read_file", f"file_{i:02d}.py", call_id=str(i), failed=i == 12,
                error="INSPECTABLE ERROR" if i == 12 else "",
            )
        yield ToolStarted("run_command", "waiting", "13",
                          command="printf FIRST_DETAIL\\nprintf SECOND_DETAIL")
        await asyncio.sleep(30)

app = PreviewApp(model="test:local", runtime=Runtime())
app.activity.plan = [{"id": "one", "content": "A task", "status": "in_progress"}]
app.run()
"""


@pytest.mark.parametrize(
    "pane",
    [
        'from pcode.preferences import save_preferences\nsave_preferences(attach_tasks="off")\n'
        + TOOLS_SCRIPT
    ],
    indirect=True,
)
def test_detached_tasks_have_their_own_frame_and_nested_tools(pane):
    initial = capture(pane, "A task")
    assert "Tools" not in initial and "┌─ Tasks 0/1 ─" in initial
    assert initial.count("┌") == initial.count("└") == 2
    pane("send-keys", "-t", "preview:0.0", "h", "Enter")
    screen = capture(pane, TOOL_ROW, running=True)
    lines = screen.splitlines()
    task = next(i for i, line in enumerate(lines) if "A task" in line)
    # The widget keeps its own frame and holds tasks alone; the running
    # command's verb rides the editor's top border below it, and what it
    # runs sits on the tool row above, with the thoughts.
    assert TOOL_ROW in lines[task - 2]
    assert lines[task - 1].startswith("┌─ Tasks 0/1 ─")
    assert lines[task].startswith(tuple("│" + row for row in active_task("")))
    assert lines[task + 1].startswith("└")
    status = lines[task + 2]
    assert status.startswith(SPINNER_ROW) and "Run shell" in status
    assert "Tools" not in screen and "Tasks ·" not in screen
    history = pane("capture-pane", "-p", "-S", "-", "-t", "preview:0.0")
    # Scrollback holds each call once. The newest calls are also on the live
    # tool rows (`tool_max_lines`), so count one that has left them.
    assert history.count("file_10.py") == 1
    assert history.count("INSPECTABLE ERROR") == 1
    assert "✗ Read file failed" in history

    pane("send-keys", "-t", "preview:0.0", "-l", "keep draft")
    for width, height in ((40, 20), (100, 32), (40, 14)):
        resize(pane, "resize-window", "-t", "preview:0", "-x", str(width), "-y", str(height))
        deadline = time.monotonic() + TIMEOUT
        while True:
            screen = capture(pane, "A task", running=True, columns=width)
            lines = screen.splitlines()
            # Inspect the live widget nearest the editor, not old resize ghosts.
            task = max(i for i, line in enumerate(lines) if "A task" in line)
            if lines[task + 1].startswith("└"):
                break
            assert time.monotonic() < deadline, screen
            time.sleep(0.05)
        assert lines[task].startswith(tuple("│" + row for row in active_task("")))
        assert "keep draft" in screen
        assert input_rows(screen) == 1
    pane("send-keys", "-t", "preview:0.0", "C-c")  # Clears the draft.
    assert "keep draft" not in capture(pane, "Input discarded", running=True)
    pane("send-keys", "-t", "preview:0.0", "C-c")
    screen = capture(pane, "! Run cancelled")
    assert TOOL_ROW not in screen


def attached_box(screen):
    """The editor box's rows: heading, tasks, divider, text, footer, all one frame."""
    lines = screen.splitlines()
    cursor = next(i for i, line in enumerate(lines) if line.startswith("│❯"))
    top = max(i for i, line in enumerate(lines[:cursor]) if line.startswith("┌"))
    divider = max(i for i, line in enumerate(lines[:cursor]) if line.startswith("├"))
    bottom = next(i for i, line in enumerate(lines[cursor:], cursor) if line.startswith("└"))
    assert top < divider < cursor, screen
    assert lines[divider].endswith("┤"), screen
    return lines[top], lines[top + 1 : divider], bottom - divider - 1


@pytest.mark.parametrize("pane", [TOOLS_SCRIPT], indirect=True)
def test_tasks_share_the_editor_box_by_default_and_config_applies_live(pane):
    initial = capture(pane, "A task")
    heading, tasks, text_rows = attached_box(initial)
    assert heading.startswith("┌─ Tasks 0/1 ─")
    assert len(tasks) == 1 and "A task" in tasks[0]
    assert text_rows == 1
    assert initial.count("┌") == initial.count("└") == 1

    pane("send-keys", "-t", "preview:0.0", "-l", "/config set attach_tasks off")
    pane("send-keys", "-t", "preview:0.0", "Enter")
    detached = capture(pane, "attach_tasks = off")
    assert "├" not in detached and detached.count("┌") == 2
    assert input_rows(detached) == 1
    pane("send-keys", "-t", "preview:0.0", "-l", "/config set attach_tasks on")
    pane("send-keys", "-t", "preview:0.0", "Enter")
    attached_box(capture(pane, "attach_tasks = on"))

    pane("send-keys", "-t", "preview:0.0", "h", "Enter")
    pane("send-keys", "-t", "preview:0.0", "-l", "keep draft")
    for width, height in ((40, 14), (100, 32), (40, 20)):
        pane("resize-window", "-t", "preview:0", "-x", str(width), "-y", str(height))
        deadline = time.monotonic() + TIMEOUT
        while True:
            screen = capture(pane, "keep draft", running=True, columns=width)
            if screen.count("├") == 1:
                break
            assert time.monotonic() < deadline, screen
            time.sleep(0.05)
        heading, tasks, text_rows = attached_box(screen)
        # While the turn runs its status takes the heading's place.
        assert heading.startswith(SPINNER_ROW)
        assert len(tasks) == 1 and tasks[0].startswith(tuple("│" + row for row in active_task("")))
        assert text_rows == 1
    pane("send-keys", "-t", "preview:0.0", "C-c")
    pane("send-keys", "-t", "preview:0.0", "C-c")
    capture(pane, "! Run cancelled")


RESIZE_TRANSCRIPT_SCRIPT = TOOLS_SCRIPT.replace(
    'yield TextDelta("MODEL CONVERSATION ONLY\\n\\n")',
    'yield TextDelta("".join(f"RESIZE_TRANSCRIPT_{i:03d}\\n\\n" for i in range(40)))',
)


@pytest.mark.parametrize("pane", [RESIZE_TRANSCRIPT_SCRIPT], indirect=True)
def test_empty_input_resize_preserves_transcript_without_task_ghosts(pane):
    """tmux splits old full-width rows on narrowing; the erase must cover them."""
    capture(pane, "A task")
    pane("resize-window", "-t", "preview:0", "-x", "240", "-y", "40")
    capture(pane, "A task", columns=240)
    pane("send-keys", "-t", "preview:0.0", "h", "Enter")
    capture(pane, TOOL_ROW, running=True, columns=240)

    for width, height in ((120, 24), (240, 40), (80, 24), (240, 40)):
        pane("resize-window", "-t", "preview:0", "-x", str(width), "-y", str(height))
        screen = capture(pane, TOOL_ROW, running=True, columns=width)
        assert input_rows(screen) == 1
        editor = next(line for line in screen.splitlines() if line.startswith("│❯"))
        assert editor[2:-1].strip() == ""  # No multiline draft needed to trigger this.
        history = pane("capture-pane", "-p", "-S", "-", "-t", "preview:0.0")
        # A viewport clear can remove the ghost while destroying real output.
        for i in range(40):
            marker = f"RESIZE_TRANSCRIPT_{i:03d}"
            assert history.count(marker) == 1, f"Lost or duplicated {marker}:\n{history}"
        history = single_editor_history(pane, "A task")
        assert history.count("file_10.py") == 1  # Off the live tool rows.


@pytest.mark.parametrize(
    "pane",
    [
        LIVE_SCRIPT.replace(
            'yield "\\n\\nLIVE ANSWER COMPLETE"', 'raise RuntimeError("model broke")'
        )
    ],
    indirect=True,
)
def test_failure_is_reported_in_scrollback_and_clears_the_status_row(pane, release):
    capture(pane, "❯")
    pane("send-keys", "-t", "preview:0.0", "-l", "hello")
    pane("send-keys", "-t", "preview:0.0", "Enter")
    capture(pane, "COMMITTED LINE", running=True)
    release()
    failed = capture(pane, "✗ Agent failed")
    assert input_rows(failed) == 1
    assert "Run failed" in failed
    # The turn is over, so no spinner row survives above the editor.
    assert not any(line.startswith(SPINNER_ROW) for line in failed.splitlines())
    assert "▌ hello" in failed


@pytest.mark.parametrize("pane", [PAUSED_STREAM_SCRIPT], indirect=True)
def test_prompt_header_stays_one_line_and_truncates_on_resize(pane):
    capture(pane, "❯")
    pane("send-keys", "-t", "preview:0.0", "-l", "LONG PROMPT " * 30)
    pane("send-keys", "-t", "preview:0.0", "Enter")
    capture(pane, "COMMITTED MARKER", running=True)
    for columns in (40, 100, 35):
        pane("resize-window", "-t", "preview:0", "-x", str(columns))
        screen = capture(pane, "❯", running=True, columns=columns)
        lines = screen.splitlines()
        editor_top = max(i for i, line in enumerate(lines) if line.startswith("┌"))
        header = lines[editor_top]
        # One status row, never the echoed prompt, and never wider than the pane.
        assert header.startswith(SPINNER_ROW)
        assert "LONG PROMPT" not in header
        assert len(header) <= columns
        assert screen.count("┌") == screen.count("└") == 1
        assert input_rows(screen) == 1


@pytest.mark.parametrize("pane", [PAUSED_STREAM_SCRIPT], indirect=True)
@pytest.mark.parametrize("mode", ["queue", "steering"])
def test_queued_messages_stay_directly_above_editor(pane, mode):
    capture(pane, "❯")
    label = "Queued" if mode == "queue" else "Steering"
    pane("send-keys", "-t", "preview:0.0", "-l", "active prompt")
    pane("send-keys", "-t", "preview:0.0", "Enter")
    capture(pane, "COMMITTED MARKER", running=True)
    for text in ("first queued message " * 10, "second queued message"):
        if mode == "queue":
            # Ctrl+S picks the mode for one send only.
            pane("send-keys", "-t", "preview:0.0", "C-s")
        pane("send-keys", "-t", "preview:0.0", "-l", text)
        pane("send-keys", "-t", "preview:0.0", "Enter")
    pane("send-keys", "-t", "preview:0.0", "-l", "keep draft")
    for width in (100, 40):
        resize(pane, "resize-window", "-t", "preview:0", "-x", str(width))
        screen = capture(pane, "│❯ keep draft", running=True, columns=width)
        lines = screen.splitlines()
        editor_top = max(i for i, line in enumerate(lines) if line.startswith("┌"))
        assert lines[editor_top - 2].startswith(f" {label}: first")
        assert lines[editor_top - 2].endswith("…")
        assert lines[editor_top - 1].startswith(f" {label}: second")
        assert lines[editor_top].startswith(SPINNER_ROW)
        assert "active prompt" not in lines[editor_top]
        assert "│❯ keep draft" in screen
        assert input_rows(screen) == 1
    pane("send-keys", "-t", "preview:0.0", "C-c")  # Clears the draft.
    assert "keep draft" not in capture(pane, "Input discarded", running=True)
    pane("send-keys", "-t", "preview:0.0", "C-c")
    screen = capture(pane, "! Run cancelled")
    assert f"{label}:" not in screen


@pytest.mark.parametrize(
    "pane",
    [
        TOOLS_SCRIPT.replace(
            'app.activity.plan = [{"id": "one", "content": "A task", "status": "in_progress"}]', ""
        )
    ],
    indirect=True,
)
def test_single_running_tool_needs_no_box_above_the_editor(pane):
    capture(pane, "❯")
    pane("send-keys", "-t", "preview:0.0", "h", "Enter")
    screen = capture(pane, TOOL_ROW, running=True)
    lines = screen.splitlines()
    top = next(i for i, line in enumerate(lines) if line.startswith("┌"))
    # Only the editor is boxed: the lone running call's verb rides its top
    # border, with what it runs on the tool row just above.
    assert screen.count("┌") == screen.count("└") == 1
    assert lines[top].startswith(SPINNER_ROW)
    assert "Run shell" in lines[top] and "FIRST_DETAIL" not in lines[top]
    # Led by the tool spinner's frame.
    assert lines[top - 1].strip().split(" ", 1)[1].startswith(TOOL_ROW)
    assert "Tasks" not in screen and "Tools" not in screen
    assert "✓ Read file" in pane("capture-pane", "-p", "-S", "-", "-t", "preview:0.0")
    assert input_rows(screen) == 1


SPACING_SCRIPT = """
from pcode.app import PreviewApp
from pcode.runtime import ToolStarted, ToolSummary

class Runtime:
    session = None

    async def stream(self, prompt):
        for i in range(1, 31):
            yield ToolStarted("read_file", f"file_{i:02d}.py", str(i))
            yield ToolSummary("read_file", f"file_{i:02d}.py", call_id=str(i))
        yield ToolStarted("read_file", "SLOW_FILE", "31")
        await gate()

PreviewApp(model="test:local", runtime=Runtime()).run()
"""


@pytest.mark.parametrize("pane", [SPACING_SCRIPT], indirect=True)
def test_status_row_keeps_a_blank_line_below_the_last_tool_line(pane):
    capture(pane, "❯")
    pane("send-keys", "-t", "preview:0.0", "h", "Enter")
    capture(pane, "SLOW_FILE", running=True)
    # The live panel can show the filename before its scrollback line is flushed.
    last_summary = "✓ Read file  file_30.py"
    screen = settle(pane, lambda screen: last_summary in screen, running=True)
    assert last_summary in screen, screen
    lines = screen.splitlines()
    tool = next(i for i, line in enumerate(lines) if "SLOW_FILE" in line)
    # Led by the spinner's frame while it runs.
    assert lines[tool].strip().split(" ", 1)[1] == "⎘ SLOW_FILE"
    assert lines[tool + 1].startswith(SPINNER_ROW)
    # The transcript's last tool line, then a blank line, then the live rows:
    # the newest finished calls (while they linger, `tool_max_lines` of them
    # at most) above the running one.
    last = max(i for i, line in enumerate(lines) if last_summary in line)
    assert lines[last + 1].strip() == ""
    live = [line.strip() for line in lines[last + 2 : tool]]
    assert live in ([], ["✓ ⎘ file_30.py"], ["✓ ⎘ file_29.py", "✓ ⎘ file_30.py"]), screen


IMMEDIATE_PROMPT_SCRIPT = """
from pcode.app import PreviewApp
from pcode.runtime import TextDelta, ToolStarted, Message

class Runtime:
    session = None
    async def stream(self, prompt):
        yield ToolStarted("read_file", "WAITING FOR FIRST MESSAGE", "one")
        yield TextDelta("FIRST MODEL")
        await gate("message")
        yield TextDelta(" MESSAGE\\n\\n")
        # Never released: the response is asserted while the turn still runs.
        await gate("finish")
        yield Message("FIRST MODEL MESSAGE")

PreviewApp(model="test:local", runtime=Runtime()).run()
"""


@pytest.mark.parametrize("pane", [IMMEDIATE_PROMPT_SCRIPT], indirect=True)
def test_scrollback_quote_is_committed_on_send_with_a_blank_line_after_it(pane, release):
    capture(pane, "❯")
    pane("send-keys", "-t", "preview:0.0", "-l", "sent prompt")
    pane("send-keys", "-t", "preview:0.0", "Enter")
    capture(pane, "WAITING FOR FIRST MESSAGE", running=True)
    # The runtime withholds the rest of the first message until released, so
    # the quote must land before it.
    waiting = settle(pane, lambda screen: "▌ sent prompt" in screen, running=True)
    assert "WAITING FOR FIRST MESSAGE" in waiting
    assert "▌ sent prompt" in waiting
    assert "FIRST MODEL" not in waiting
    assert input_rows(waiting) == 1
    release("message")
    response = capture(pane, "FIRST MODEL MESSAGE", running=True)
    assert "▌ sent prompt\n\nFIRST MODEL MESSAGE" in response
    assert input_rows(response) == 1
    history = pane("capture-pane", "-p", "-S", "-", "-t", "preview:0.0")
    assert history.count("▌ sent prompt") == 1
    assert history.count("FIRST MODEL MESSAGE") == 1


STREAMED_PLAN_SCRIPT = r"""
import asyncio
from pydantic_ai import Agent
from pydantic_ai.models.function import DeltaToolCall, FunctionModel
from pydantic_ai_harness.planning import Planning
from pcode.app import PreviewApp
from pcode.live import AgentRuntime

async def model(messages, info):
    yield {0: DeltaToolCall(name="write_plan", json_args=
        '{"items":[{"content":"STREAMED_TASK", "status":"in_progress"},')}
    # Neither the arguments nor the model response ever finish before cancellation.
    await asyncio.sleep(60)

runtime = AgentRuntime(Agent(FunctionModel(stream_function=model), capabilities=[Planning()]))
PreviewApp(model="test:local", runtime=runtime).run()
"""


@pytest.mark.parametrize("pane", [STREAMED_PLAN_SCRIPT], indirect=True)
def test_streamed_task_preview_has_real_prompt_height_and_cancels_cleanly(pane):
    capture(pane, "❯")
    pane("send-keys", "-t", "preview:0.0", "h", "Enter")
    screen = capture(pane, "STREAMED_TASK", running=True)
    lines = screen.splitlines()
    top = next(i for i, line in enumerate(lines) if line.startswith("┌"))
    # The preview hangs under the status, which takes the heading's place.
    assert "STREAMED_TASK" in lines[top + 1]
    assert input_rows(screen) == 1
    assert screen.count("┌") == screen.count("└") == 1
    assert screen.count("├") == 1
    pane("send-keys", "-t", "preview:0.0", "C-c")
    screen = capture(pane, "Run cancelled")
    assert "STREAMED_TASK" not in screen
    assert "Tasks 0/1" not in screen
    assert input_rows(screen) == 1
    assert screen.count("┌") == screen.count("└") == 1
    history = pane("capture-pane", "-p", "-S", "-", "-t", "preview:0.0")
    assert "STREAMED_TASK" not in history


MODIFIED_ENTER_SCRIPT = r"""
import json, pathlib, subprocess
from rich.console import Console
from pcode.commands import CommandRegistry
from pcode.ui import Transcript, create_prompt

root = pathlib.Path(os.environ['PCODE_TEST_GATES'])
subprocess.run(['tmux', 'set-option', '-s', 'extended-keys', 'on'], check=True)
subprocess.run(['tmux', 'set-option', '-s', 'extended-keys-format', KEY_FORMAT], check=True)

async def run():
    sent = []
    def submit(kind, text):
        sent.append([kind, text])
        prompt.app.exit()
    prompt = create_prompt(
        CommandRegistry(), transcript=Transcript(Console()),
        on_submit=lambda text: submit('ordinary', text),
        on_interrupt_submit=lambda text: submit('interrupt', text),
    )
    async def ready():
        while not prompt.app.output.output._active:
            await asyncio.sleep(0)
        (root / 'keyboard-ready').touch()
    await prompt.app.run_async(pre_run=lambda: prompt.app.create_background_task(ready()))
    mode = subprocess.check_output(
        ['tmux', 'display-message', '-p', '-t', os.environ['TMUX_PANE'], '#{pane_key_mode}'],
        text=True,
    ).strip()
    (root / 'keyboard-result.tmp').write_text(json.dumps([sent, mode]))
    (root / 'keyboard-result.tmp').replace(root / 'keyboard-result')
    await gate()

asyncio.run(run())
"""


@pytest.mark.parametrize(
    "pane",
    [MODIFIED_ENTER_SCRIPT.replace("KEY_FORMAT", repr(fmt)) for fmt in ("xterm", "csi-u")],
    indirect=True,
)
def test_tmux_generates_modified_enter_and_restores_mode(pane, tmp_path):
    ready, result = tmp_path / "keyboard-ready", tmp_path / "keyboard-result"
    until(ready.exists, lambda: "Editor did not acquire keyboard mode")
    # Named keys make tmux encode according to the mode the app requested.
    # Literal escape-sequence injection would miss broken negotiation.
    pane("send-keys", "-t", "preview:0.0", "-l", "first")
    pane("send-keys", "-t", "preview:0.0", "S-Enter")
    pane("send-keys", "-t", "preview:0.0", "-l", "second")
    pane("send-keys", "-t", "preview:0.0", "C-Enter")
    until(result.exists, lambda: "Modified Enter did not submit")
    assert json.loads(result.read_text()) == [[["interrupt", "first\nsecond"]], "VT10x"]


THINKING_SCRIPT = r"""
import asyncio
from pydantic_ai import Agent
from pydantic_ai.models.function import DeltaThinkingPart, FunctionModel
from pcode.app import PreviewApp
from pcode.live import AgentRuntime

async def model(messages, info):
    yield {0: DeltaThinkingPart(content="SAVED_REASONING_TEXT\n")}
    await gate()
    yield "Public answer while thinking is visible\n\n"
    await asyncio.sleep(60)

runtime = AgentRuntime(Agent(FunctionModel(stream_function=model)))
app = PreviewApp(model="test:local", runtime=runtime)
app.activity.thinking_mode = "off"
app.persist_defaults = lambda **updates: None
app.run()
"""


@pytest.mark.parametrize("pane", [THINKING_SCRIPT], indirect=True)
def test_thinking_modes_cycle_between_row_and_scrollback_without_growing_prompt(pane, release):
    def in_scrollback(screen):
        return "SAVED_REASONING_TEXT" in without_status_row(screen)

    capture(pane, "❯")
    pane("send-keys", "-t", "preview:0.0", "h", "Enter")
    screen = capture(pane, "❯", running=True)
    assert "SAVED_REASONING_TEXT" not in screen  # Off: nowhere.
    # Status line: its own rows above the status row, and not in scrollback.
    pane("send-keys", "-t", "preview:0.0", "C-t", "s")
    screen = settle(
        pane, lambda screen: thought_row(screen) == "SAVED_REASONING_TEXT", running=True
    )
    assert thought_row(screen) == "SAVED_REASONING_TEXT", screen
    assert not in_scrollback(screen)
    assert input_rows(screen) == 1
    # Gone the moment the answer streams: thinking is the lead-up, not a caption.
    release()
    screen = capture(pane, "Public answer while thinking is visible", running=True)
    assert "SAVED_REASONING_TEXT" not in thought_row(screen), screen
    assert not in_scrollback(screen)
    # Scrollback: written above, and the row goes.
    pane("send-keys", "-t", "preview:0.0", "C-t", "b")
    screen = settle(pane, in_scrollback, running=True)
    # The mode's notice sits where a thought would, padded the same: look for
    # the thought itself.
    assert in_scrollback(screen) and "SAVED_REASONING_TEXT" not in thought_row(screen)
    assert input_rows(screen) == 1
    for width, height in ((80, 24), (120, 40)):
        pane("resize-window", "-t", "preview:0", "-x", str(width), "-y", str(height))
        screen = capture(pane, "SAVED_REASONING_TEXT", running=True, columns=width)
        assert input_rows(screen) == 1
    # Off again: gone from both.
    pane("send-keys", "-t", "preview:0.0", "C-t", "o")
    screen = settle(pane, lambda screen: "SAVED_REASONING_TEXT" not in screen, running=True)
    assert "SAVED_REASONING_TEXT" not in screen
    pane("send-keys", "-t", "preview:0.0", "C-t", "b")
    settle(pane, in_scrollback, running=True)
    pane("send-keys", "-t", "preview:0.0", "C-c")
    screen = capture(pane, "Run cancelled")
    assert "SAVED_REASONING_TEXT" in screen
    assert input_rows(screen) == 1
    history = pane("capture-pane", "-p", "-S", "-", "-t", "preview:0.0")
    assert "SAVED_REASONING_TEXT" in history


@pytest.mark.parametrize(
    "pane",
    [
        "from pcode.preferences import save_preferences; "
        "save_preferences(editing_mode='vi'); "
        "import subprocess; "
        "subprocess.run(['tmux', 'set-option', '-s', 'extended-keys', 'on'], check=True); "
        "from pcode.app import main; main()"
    ],
    indirect=True,
)
@pytest.mark.parametrize(
    "newline",
    ["C-j", "\x1b[106;5u", "\x1b[27;5;106~", "\x1b[13;2u", "\x1b[27;2;13~", "S-Enter"],
)
def test_vi_newline_and_escape_keep_editor_compact(pane, newline):
    screen = capture(pane, "INSERT")
    assert screen.splitlines()[-2].endswith(" INSERT ─┘")
    assert "INSERT" not in screen.split("│❯")[0]
    assert input_rows(screen) == 1
    pane("send-keys", "-t", "preview:0.0", "-l", "first")
    # Named Ctrl+J is intentional: tmux in Ext 1 drops literal LF injection.
    literal = [] if newline in {"S-Enter", "C-j"} else ["-l"]
    pane("send-keys", "-t", "preview:0.0", *literal, newline)
    pane("send-keys", "-t", "preview:0.0", "-l", "second")
    assert input_rows(capture(pane, "second")) == 2
    pane("send-keys", "-t", "preview:0.0", "Escape")
    screen = capture(pane, "NORMAL")
    assert screen.splitlines()[-2].endswith(" NORMAL ─┘")
    assert input_rows(screen) == 2
    # Normal-mode dd removes the second line rather than inserting literal 'dd'.
    pane("send-keys", "-t", "preview:0.0", "-l", "dd")
    deadline = time.monotonic() + TIMEOUT
    while time.monotonic() < deadline:
        screen = capture(pane, "first")
        if input_rows(screen) == 1:
            break
        time.sleep(0.05)
    assert input_rows(screen) == 1
    assert "second" not in screen
    pane("send-keys", "-t", "preview:0.0", "-l", "i")
    assert input_rows(capture(pane, "INSERT")) == 1
    for width, height in ((80, 24), (120, 40)):
        pane("resize-window", "-t", "preview:0", "-x", str(width), "-y", str(height))
        screen = capture(pane, "INSERT", columns=width)
        assert screen.splitlines()[-2].endswith(" INSERT ─┘")
        assert input_rows(screen) == 1


@pytest.mark.parametrize(
    "pane",
    [
        "from pcode.preferences import save_preferences; "
        "save_preferences(editing_mode='vi'); "
        "from pcode.app import main; main()"
    ],
    indirect=True,
)
def test_vi_word_motion_and_newline_keep_editor_compact(pane):
    assert input_rows(capture(pane, "❯")) == 1
    pane("send-keys", "-t", "preview:0.0", "-l", "one two three")
    pane("send-keys", "-t", "preview:0.0", "Escape")
    pane("send-keys", "-t", "preview:0.0", "-l", "bbiX")
    assert input_rows(capture(pane, "one Xtwo three")) == 1
    pane("send-keys", "-t", "preview:0.0", "C-j")
    pane("send-keys", "-t", "preview:0.0", "-l", "NEWLINE")
    assert input_rows(capture(pane, "NEWLINEtwo three")) == 2
    pane("send-keys", "-t", "preview:0.0", "C-c")
    assert input_rows(capture(pane, "Input discarded")) == 1


STARTUP_SCRIPT = r"""
import asyncio
import os
import time
from pathlib import Path
from pcode.app import PreviewApp
from pcode.runtime import Message

root = Path(os.environ['XDG_CONFIG_HOME']).parent

class Runtime:
    session = None
    recovery_blocked = ''

    async def refresh_context(self):
        (root / 'metadata-started').touch()
        await asyncio.Event().wait()

    async def stream(self, text):
        yield Message('Startup response received')

    def close(self):
        pass

def create_runtime():
    deadline = time.monotonic() + 60
    while not (root / 'release-startup').exists():
        if time.monotonic() > deadline:
            raise RuntimeError('test did not release initialization')
        time.sleep(0.01)
    return Runtime()

app = PreviewApp(model='test:local')
app.controller._create_runtime = create_runtime
app.run()
"""


@pytest.mark.parametrize("pane", [STARTUP_SCRIPT], indirect=True)
def test_startup_keeps_real_cpr_editor_editable_and_compact(pane, release, tmp_path):
    assert input_rows(capture(pane, "starting")) == 1
    pane("send-keys", "-t", "preview:0.0", "-l", "draft before agent is ready")
    screen = capture(pane, "draft before agent is ready")
    assert "starting" in screen
    assert input_rows(screen) == 1
    pane("resize-window", "-t", "preview:0", "-x", "80", "-y", "40")
    assert input_rows(capture(pane, "draft before agent is ready", columns=80)) == 1
    release("release-startup")
    until((tmp_path / "metadata-started").exists, lambda: "Metadata refresh never started")
    assert input_rows(capture(pane, "draft before agent is ready", columns=80)) == 1
    pane("send-keys", "-t", "preview:0.0", "Enter")
    assert input_rows(capture(pane, "Startup response received", columns=80)) == 1
    # Metadata is still blocked, but further typing and the CPR height are unaffected.
    pane("send-keys", "-t", "preview:0.0", "-l", "next draft")
    assert input_rows(capture(pane, "next draft", columns=80)) == 1
