"""Notice the terminal switching between light and dark while pcode runs.

Two triggers, both answered through `TerminalReplyParser` in the input stream:

- Mode 2031: terminals that support it (Ghostty, kitty, iTerm2, VTE, foot, tmux)
  report `CSI ? 997 ; 1|2 n` whenever their color scheme changes. Works over
  SSH and on any OS; terminals without it ignore the request.
- The desktop's own setting (macOS, or GNOME through `gsettings`), polled.
  A change only prompts a fresh OSC 11 background query: what matters is the
  terminal's background, and a terminal pinned to dark stays dark when the
  desktop goes light. Skipped over SSH, where the desktop is someone else's.

Polling is a cheap in-process read on macOS, so the slower GNOME path is the
only one that spawns anything.
"""

import asyncio
import os
import shutil
import subprocess
import sys
from collections.abc import Callable
from contextlib import contextmanager

from pcode.terminal_notify import _isatty, _write_all

POLL_SECONDS = 2.0
# The desktop setting can flip before the terminal repaints; ask again later
# too. Repeated identical answers change nothing.
QUERY_DELAYS = (0.3, 1.5)
ENABLE_REPORTS = b"\x1b[?2031h"
DISABLE_REPORTS = b"\x1b[?2031l"
QUERY_BACKGROUND = b"\x1b]11;?\x1b\\"


def _macos_reader():
    """A function reading the global AppleInterfaceStyle, or None without CoreFoundation."""
    import ctypes
    import ctypes.util

    path = ctypes.util.find_library("CoreFoundation")
    if path is None:
        return None
    cf = ctypes.CDLL(path)
    utf8 = 0x08000100
    cf.CFStringCreateWithCString.restype = ctypes.c_void_p
    cf.CFStringCreateWithCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_uint32]
    cf.CFPreferencesCopyAppValue.restype = ctypes.c_void_p
    cf.CFPreferencesCopyAppValue.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    cf.CFPreferencesAppSynchronize.argtypes = [ctypes.c_void_p]
    cf.CFStringGetCString.restype = ctypes.c_bool
    cf.CFGetTypeID.restype = ctypes.c_ulong
    cf.CFGetTypeID.argtypes = [ctypes.c_void_p]
    cf.CFStringGetTypeID.restype = ctypes.c_ulong
    string_type = cf.CFStringGetTypeID()
    cf.CFStringGetCString.argtypes = [
        ctypes.c_void_p,
        ctypes.c_char_p,
        ctypes.c_long,
        ctypes.c_uint32,
    ]
    cf.CFRelease.argtypes = [ctypes.c_void_p]
    key = cf.CFStringCreateWithCString(None, b"AppleInterfaceStyle", utf8)
    domain = ctypes.c_void_p.in_dll(cf, "kCFPreferencesAnyApplication")

    def read() -> str:
        # Without the sync the process keeps its first cached answer.
        cf.CFPreferencesAppSynchronize(domain)
        value = cf.CFPreferencesCopyAppValue(key, domain)
        if not value:  # The key only exists while dark.
            return "light"
        buffer = ctypes.create_string_buffer(16)
        # Any property list type can sit under the key; only a string is ours.
        ok = cf.CFGetTypeID(value) == string_type and cf.CFStringGetCString(
            value, buffer, len(buffer), utf8
        )
        cf.CFRelease(value)
        return "dark" if ok and buffer.value == b"Dark" else "light"

    return read


def _gnome_read() -> str | None:
    try:
        result = subprocess.run(
            ["gsettings", "get", "org.gnome.desktop.interface", "color-scheme"],
            capture_output=True,
            text=True,
            timeout=2,
        )
    except OSError, subprocess.SubprocessError:
        return None
    if result.returncode != 0:
        return None
    return "dark" if "dark" in result.stdout else "light"


def desktop_reader():
    """How to read the desktop's light/dark setting here, or None to not poll."""
    if os.environ.get("SSH_CONNECTION") or os.environ.get("SSH_TTY"):
        return None
    try:
        if sys.platform == "darwin":
            return _macos_reader()
    except OSError, AttributeError, ValueError:
        return None
    if sys.platform.startswith("linux") and shutil.which("gsettings"):
        if os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"):
            return _gnome_read
    return None


class AppearanceWatch:
    """Ask the terminal for its appearance whenever it may have changed.

    Answers arrive as input and are handled by the editor's input parser, so
    this only writes requests and never reads.
    """

    def __init__(
        self, fd: int | None, reader=None, active: Callable[[], bool] = lambda: True
    ) -> None:
        self.fd = fd
        self.reader = reader
        # Desktop polling only matters while the theme is `auto`; mode 2031
        # reports stay on regardless, so switching back to auto starts current.
        self.active = active
        self.enabled = False
        self.suspended = False

    def close(self) -> None:
        if self.enabled and _write_all(self.fd, DISABLE_REPORTS):
            self.enabled = False

    @contextmanager
    def paused(self):
        """Hand the terminal to another program: no reports or replies land in its input."""
        enabled = self.enabled
        self.close()
        self.suspended = True
        try:
            yield
        finally:
            self.suspended = False
            if enabled:
                self.enabled = _write_all(self.fd, ENABLE_REPORTS)
                # A switch while the other program had the terminal went unseen.
                self.refresh()

    def refresh(self) -> None:
        """Ask for the background now; the answer arrives as input like any other."""
        if self.enabled and not self.suspended:
            _write_all(self.fd, QUERY_BACKGROUND)

    async def query(self) -> None:
        for delay in QUERY_DELAYS:
            await asyncio.sleep(delay)
            if not self.suspended:
                _write_all(self.fd, QUERY_BACKGROUND)

    async def run(self) -> None:
        if self.fd is None or not _isatty(self.fd) or os.environ.get("TERM") == "dumb":
            return
        self.enabled = _write_all(self.fd, ENABLE_REPORTS)
        # The startup probe is skipped when input is already waiting, leaving a
        # guess from COLORFGBG that only a later switch would otherwise correct.
        self.refresh()
        reader = self.reader if self.reader is not None else desktop_reader()
        pending = None
        try:
            if reader is None:
                await asyncio.Future()  # Mode 2031 only; disable it on the way out.
            last = await asyncio.to_thread(reader)
            while True:
                await asyncio.sleep(POLL_SECONDS)
                if not self.active():
                    # Keep `last`: a flip made meanwhile is still asked about
                    # on the first poll after switching back to auto.
                    continue
                current = await asyncio.to_thread(reader)
                if current is None:
                    continue
                if last is not None and current != last:
                    if pending is not None:
                        pending.cancel()
                    pending = asyncio.create_task(self.query())
                last = current
        finally:
            if pending is not None:
                pending.cancel()
            self.close()
