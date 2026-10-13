import asyncio
import os
import pty
import select
import sys
from io import StringIO

import pytest
from rich.console import Console

from pcode import appearance
from pcode.app import PreviewApp


def _drain(fd) -> bytes:
    data = b""
    while select.select([fd], [], [], 0)[0]:
        data += os.read(fd, 1024)
    return data


def test_desktop_change_queries_the_terminal_and_reports_toggle(monkeypatch):
    monkeypatch.setattr(appearance, "POLL_SECONDS", 0.01)
    monkeypatch.setattr(appearance, "QUERY_DELAYS", (0.01,))
    monkeypatch.setenv("TERM", "xterm-256color")
    values = iter(["dark", "dark", None, "light"])
    master, slave = pty.openpty()

    async def run():
        watch = appearance.AppearanceWatch(slave, reader=lambda: next(values, "light"))
        task = asyncio.create_task(watch.run())
        await asyncio.sleep(0.3)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    try:
        asyncio.run(run())
        written = _drain(master)
    finally:
        os.close(master)
        os.close(slave)
    assert written.startswith(appearance.ENABLE_REPORTS + appearance.QUERY_BACKGROUND)
    assert written.endswith(appearance.DISABLE_REPORTS)
    # One at startup, then one flip (an unreadable poll in between is not a
    # flip), so one more query.
    assert written.count(appearance.QUERY_BACKGROUND) == 2


def test_desktop_is_only_polled_for_auto_and_a_missed_flip_still_counts(monkeypatch):
    monkeypatch.setattr(appearance, "POLL_SECONDS", 0.01)
    monkeypatch.setattr(appearance, "QUERY_DELAYS", (0.01,))
    monkeypatch.setenv("TERM", "xterm-256color")
    state = {"auto": False, "desktop": "dark", "reads": 0}

    def reader():
        state["reads"] += 1
        return state["desktop"]

    master, slave = pty.openpty()

    async def run():
        watch = appearance.AppearanceWatch(slave, reader=reader, active=lambda: state["auto"])
        task = asyncio.create_task(watch.run())
        await asyncio.sleep(0.1)
        assert _drain(master).count(appearance.QUERY_BACKGROUND) == 1  # Startup.
        reads = state["reads"]
        state["desktop"] = "light"  # Flipped while the theme was pinned.
        await asyncio.sleep(0.1)
        assert state["reads"] == reads == 1
        assert appearance.QUERY_BACKGROUND not in _drain(master)
        state["auto"] = True
        await asyncio.sleep(0.2)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    try:
        asyncio.run(run())
        assert _drain(master).count(appearance.QUERY_BACKGROUND) == 1
    finally:
        os.close(master)
        os.close(slave)


def test_paused_hands_the_terminal_over_without_reports(monkeypatch):
    master, slave = pty.openpty()
    try:
        watch = appearance.AppearanceWatch(slave)
        watch.enabled = True
        with watch.paused():
            assert _drain(master) == appearance.DISABLE_REPORTS
            assert watch.suspended
            watch.refresh()  # Nothing reaches the other program.
            assert _drain(master) == b""
        # A switch during the handoff went unseen, so ask on the way back.
        assert _drain(master) == appearance.ENABLE_REPORTS + appearance.QUERY_BACKGROUND
        assert watch.enabled and not watch.suspended
    finally:
        os.close(master)
        os.close(slave)


def test_not_a_terminal_writes_nothing():
    read, write = os.pipe()
    try:
        asyncio.run(appearance.AppearanceWatch(write, reader=lambda: "dark").run())
        assert not select.select([read], [], [], 0)[0]
    finally:
        os.close(read)
        os.close(write)


def test_no_desktop_polling_over_ssh(monkeypatch):
    monkeypatch.setenv("SSH_CONNECTION", "10.0.0.1 1 10.0.0.2 22")
    assert appearance.desktop_reader() is None


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS appearance setting")
def test_macos_reader_reads_a_value(monkeypatch):
    monkeypatch.delenv("SSH_CONNECTION", raising=False)
    monkeypatch.delenv("SSH_TTY", raising=False)
    reader = appearance.desktop_reader()
    assert reader is not None and reader() in ("dark", "light")


def test_theme_auto_asks_the_terminal_again():
    app = PreviewApp(theme="dark", console=Console(file=StringIO()))
    asked = []
    app._appearance = type("Watch", (), {"refresh": lambda self: asked.append(1)})()
    app.handle("/theme light")
    assert asked == []
    app.handle("/theme auto")
    assert asked == [1]


def test_reported_appearance_repaints_auto_only():
    app = PreviewApp(theme="auto", console=Console(file=StringIO()))
    app.transcript.detected_theme = "dark"
    regenerated = []
    app.transcript.regenerate = lambda: regenerated.append(app.transcript.resolved_theme)
    app.follow_appearance("dark")
    assert regenerated == []
    app.follow_appearance("light")
    assert regenerated == ["light"]
    app.transcript.theme = "dark"
    app.follow_appearance("dark")
    assert regenerated == ["light"]
    # Remembered for when the user goes back to auto.
    assert app.transcript.detected_theme == "dark"
