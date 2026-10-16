import os
import pty
import select
import sys
import termios
import threading
import time
from io import StringIO

import pytest
from rich.console import Console

from pcode import theme
from pcode.app import PreviewApp
from pcode.preferences import SETTINGS, load_preferences, save_preferences
from pcode.ui import PALETTES, Transcript


@pytest.mark.parametrize(
    "rgb, expected",
    [
        (b"ffff/ffff/ffff", "light"),
        (b"0000/0000/0000", "dark"),
        (b"ff/ff/ff", "light"),
        (b"0/0/0", "dark"),
        (b"0/ffff/0", "light"),
        (b"ffff/0/0", "dark"),
    ],
)
@pytest.mark.parametrize("ending", [b"\x07", b"\x1b\\"])
def test_background_response(rgb, expected, ending):
    assert theme.background_theme(b"\x1b]11;rgb:" + rgb + ending) == expected


@pytest.mark.parametrize(
    "response", [b"", b"\x1b]11;rgb:ff/ff/ff", b"rgb:ff/ff/ff\x07", b"\x1b]11;rgb:gg/00/00\x07"]
)
def test_invalid_response(response):
    assert theme.background_theme(response) is None


@pytest.mark.parametrize(
    "value, expected",
    [("0;7", "light"), ("15;0", "dark"), ("0;default;15", "light"), ("bad", "dark")],
)
def test_environment_fallback(monkeypatch, value, expected):
    monkeypatch.setattr(theme, "_query_background", lambda **_: None)
    monkeypatch.setenv("COLORFGBG", value)
    assert theme.detect_theme() == expected
    monkeypatch.setattr(theme, "_query_background", lambda **_: "dark")
    assert theme.detect_theme() == "dark"


def test_no_terminal_at_all_does_not_query(monkeypatch):
    for name in ("stdin", "stdout", "stderr"):
        monkeypatch.setattr(sys, name, StringIO())
    monkeypatch.delenv("COLORFGBG", raising=False)
    assert theme.detect_theme() == "dark"
    assert sys.stdout.getvalue() == ""


def _detect_with_redirected_streams(timeout: float) -> str:
    """In a child owning the pty: prompt on stdin, reply on stdout, both pipes."""
    prompt_read, prompt_write = os.pipe()
    os.write(prompt_write, b"summarize this")
    os.close(prompt_write)
    reply_read, reply_write = os.pipe()
    sys.stdin = os.fdopen(prompt_read)
    sys.stdout = os.fdopen(reply_write, "w")
    sys.stderr = os.fdopen(2, "w", closefd=False)  # Undo pytest's capture: stderr is the pty.
    os.environ["TERM"] = "xterm-256color"
    detected = theme._query_background(timeout)
    # The query must never land in the reply stream a pipe is reading.
    leaked = bool(select.select([reply_read], [], [], 0)[0])
    return f"DETECTED={detected} LEAKED={leaked}\n"


def test_redirected_streams_still_query_the_controlling_terminal():
    """`--print` takes its prompt on stdin and its reply on stdout, and still has a terminal."""
    pid, fd = pty.fork()
    if pid == 0:  # The child's controlling terminal is the pty.
        report = "DETECTED=crashed LEAKED=?\n"
        try:
            report = _detect_with_redirected_streams(2)
        finally:
            os.write(2, report.encode())
            os._exit(0)
    seen = bytearray()
    deadline = time.monotonic() + 10
    try:
        while b"DETECTED" not in seen and time.monotonic() < deadline:
            if not select.select([fd], [], [], deadline - time.monotonic())[0]:
                break
            chunk = os.read(fd, 1024)
            if not chunk:
                break
            seen.extend(chunk)
            if b"\x1b]11;?" in seen:
                seen.clear()  # Answer with a light background: a default would hide a failure.
                os.write(fd, b"\x1b]11;rgb:ffff/ffff/ffff\x1b\\")
    finally:
        os.close(fd)
        os.waitpid(pid, 0)
    assert "DETECTED=light LEAKED=False" in seen.decode(errors="replace")


@pytest.mark.parametrize("respond", [True, False])
def test_query_restores_terminal(monkeypatch, respond):
    master, slave = pty.openpty()
    original = termios.tcgetattr(slave)
    monkeypatch.setenv("TERM", "xterm-256color")
    received = []

    def terminal():
        if select.select([master], [], [], 2)[0]:
            received.append(os.read(master, 1024))
            if respond:
                os.write(master, b"\x1b]11;rgb:ffff/ffff/ffff\x1b\\")

    worker = threading.Thread(target=terminal)
    try:
        with os.fdopen(os.dup(slave), "r") as stdin, os.fdopen(os.dup(slave), "w") as stdout:
            monkeypatch.setattr(sys, "stdin", stdin)
            monkeypatch.setattr(sys, "stdout", stdout)
            worker.start()
            assert theme._query_background(0.1) == ("light" if respond else None)
            worker.join(2)
        restored = termios.tcgetattr(slave)
        # macOS sets PENDIN when returning to canonical mode.
        restored[3] &= ~getattr(termios, "PENDIN", 0)
        original[3] &= ~getattr(termios, "PENDIN", 0)
        assert restored == original
        assert received == [b"\x1b]11;?\x1b\\"]
    finally:
        worker.join(2)
        os.close(master)
        os.close(slave)


def test_query_does_not_consume_an_unfinished_canonical_line(monkeypatch):
    master, slave = pty.openpty()
    monkeypatch.setenv("TERM", "xterm-256color")
    try:
        with os.fdopen(os.dup(slave), "r") as stdin, os.fdopen(os.dup(slave), "w") as stdout:
            monkeypatch.setattr(sys, "stdin", stdin)
            monkeypatch.setattr(sys, "stdout", stdout)
            os.write(master, b"early draft")
            assert not select.select([slave], [], [], 0)[0]  # No newline yet.
            assert theme._query_background() is None
            from prompt_toolkit.input.vt100 import raw_mode

            with raw_mode(slave):
                assert select.select([slave], [], [], 1)[0]
                assert os.read(slave, 1024) == b"early draft"
    finally:
        os.close(master)
        os.close(slave)


@pytest.mark.parametrize("reply", [b"", b"\x1b]11;rgb:ffff/ffff/ffff\x1b\\"])
@pytest.mark.parametrize("draft", [b"early ", b"x" * 256])
def test_typing_during_query_is_replayed_with_split_utf8(monkeypatch, reply, draft):
    from types import SimpleNamespace

    from prompt_toolkit.input.vt100 import Vt100Input

    master, slave = pty.openpty()
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.setattr(theme, "_pending_input", bytearray())

    def terminal():
        if select.select([master], [], [], 2)[0]:
            os.read(master, 1024)
            os.write(master, draft + reply + b"\xc3")

    worker = threading.Thread(target=terminal)
    try:
        with os.fdopen(os.dup(slave), "r") as stdin, os.fdopen(os.dup(slave), "w") as stdout:
            monkeypatch.setattr(sys, "stdin", stdin)
            monkeypatch.setattr(sys, "stdout", stdout)
            worker.start()
            assert theme._query_background(0.2) == ("light" if reply else None)
            worker.join(2)
            keys = []
            terminal_input = Vt100Input(stdin)
            app = SimpleNamespace(input=terminal_input)
            theme.replay_pending_input(app)
            with app.input.raw_mode():
                keys.extend(app.input.read_keys())
                os.write(master, b"\xa9")
                assert select.select([slave], [], [], 1)[0]
                keys.extend(app.input.read_keys())
            assert "".join(key.data for key in keys) == draft.decode() + "é"
            assert not theme._pending_input
    finally:
        worker.join(2)
        os.close(master)
        os.close(slave)


def test_auto_palette_and_syntax(monkeypatch):
    monkeypatch.setattr("pcode.ui.detect_theme", lambda **_: "light")
    transcript = Transcript(Console(file=StringIO()), "auto")
    assert transcript.theme == "auto"
    assert transcript.palette == PALETTES["light"]
    # The default `terminal` syntax renders code with the ANSI style for the palette.
    assert SETTINGS["syntax_light"].default == "terminal"
    assert transcript.code_theme == "ansi_light"
    transcript.theme = "dark"
    assert transcript.palette == PALETTES["dark"]
    assert transcript.code_theme == "ansi_dark"
    transcript.syntax_themes["dark"] = "gruvbox-dark"
    assert transcript.code_theme == "gruvbox-dark"


def test_auto_setting_persists_and_toggle_uses_resolved_theme(monkeypatch):
    monkeypatch.setattr("pcode.ui.detect_theme", lambda **_: "light")
    save_preferences(theme="auto")
    app = PreviewApp(console=Console(file=StringIO()))
    assert app.transcript.theme == "auto"
    app.theme("")
    assert app.transcript.theme == "dark"
    app.theme("auto")
    assert load_preferences()["theme"] == "auto"
    assert "auto (light)" in app.transcript.console.file.getvalue()


def test_saved_syntax_themes_apply_per_palette(monkeypatch):
    monkeypatch.setattr("pcode.ui.detect_theme", lambda **_: "dark")
    save_preferences(syntax_dark="monokai", syntax_light="tango")
    transcript = Transcript(Console(file=StringIO()), "auto")
    assert transcript.code_theme == "monokai"
    transcript.theme = "light"
    assert transcript.code_theme == "tango"


def test_invalid_saved_syntax_theme_falls_back_to_default():
    save_preferences(syntax_dark="no-such-style")
    transcript = Transcript(Console(file=StringIO()), "dark")
    assert transcript.syntax_themes["dark"] == SETTINGS["syntax_dark"].default
    assert transcript.code_theme == "ansi_dark"


def test_syntax_command_persists_the_resolved_palette_only():
    app = PreviewApp(theme="dark", console=Console(file=StringIO()))
    app.syntax("dracula")
    assert app.transcript.code_theme == "dracula"
    assert load_preferences() == {"syntax_dark": "dracula"}
    app.transcript.theme = "light"
    assert app.transcript.syntax_themes["light"] == SETTINGS["syntax_light"].default
    assert app.transcript.code_theme == "ansi_light"
    app.syntax("")
    assert "Syntax (light)" in app.transcript.console.file.getvalue()


def test_syntax_command_rejects_an_unknown_style():
    app = PreviewApp(theme="dark", console=Console(file=StringIO()))
    with pytest.raises(ValueError, match="must be one of"):
        app.syntax("no-such-style")
    assert load_preferences() == {}
