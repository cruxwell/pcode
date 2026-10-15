"""Best-effort startup detection of the terminal's default background.

OSC 11 is an xterm query, not a command to change the terminal's colors.
Never query while prompt_toolkit owns input; cache the result at startup.
"""

import os
import re
import select
import sys
import time

THEMES = ("dark", "light", "auto")
_pending_input = bytearray()
_BACKGROUND = re.compile(
    rb"\x1b\]11;rgb:([0-9a-fA-F]{1,4})/([0-9a-fA-F]{1,4})/([0-9a-fA-F]{1,4})(?:\x07|\x1b\\)"
)


# The last background the terminal reported, as `#rrggbb` with the theme it
# implies; None until one does. Shaded frames mix their surfaces from it, so
# they suit any theme. The theme rides along because a mode 2031 report can
# flip light/dark without a color: a stale dark color must not be read as the
# new light terminal's.
_background: tuple[str, str] | None = None


def terminal_background() -> tuple[str, str] | None:
    """The last reported background, `(#rrggbb, "light" | "dark")`, or None."""
    return _background


def background_theme(response: bytes) -> str | None:
    """Classify an OSC 11 RGB response (components may use 1–4 hex digits).

    Also records the color for `terminal_background`.
    """
    global _background
    match = _BACKGROUND.search(response)
    if match is None:
        return None
    rgb = [int(value, 16) / (16 ** len(value) - 1) for value in match.groups()]
    # Perceived brightness, rather than the unweighted RGB average.
    brightness = sum(value * weight for value, weight in zip(rgb, (0.299, 0.587, 0.114)))
    theme = "light" if brightness >= 0.5 else "dark"
    _background = ("#" + "".join(f"{round(value * 255):02x}" for value in rgb), theme)
    return theme


def _terminal():
    """Descriptors to read the reply on and write the query to, plus any handle to close.

    Standard streams first, so an ordinary session queries exactly what it
    talks to. `--print` can be fed its prompt on stdin and still render to a
    terminal, so a redirected stream falls back to the controlling terminal
    instead of costing detection. Reading and writing are separate because
    stdin is not always opened for writing.
    """
    if sys.stdin.isatty() and sys.stdout.isatty():
        return sys.stdin.fileno(), sys.stdout.fileno(), None
    try:
        handle = open("/dev/tty", "r+b", buffering=0)
    except OSError:  # No controlling terminal: a daemon, or a CI runner.
        return None, None, None
    return handle.fileno(), handle.fileno(), handle


def _query_background(timeout: float = 0.15) -> str | None:
    # Nothing will be colored, so there is no palette to match: stay quiet
    # rather than write an escape sequence into whatever stdout is.
    if os.environ.get("TERM") == "dumb" or not (sys.stdout.isatty() or sys.stderr.isatty()):
        return None
    try:
        import termios
    except ImportError:
        return None
    fd, query_fd, handle = _terminal()
    if fd is None:
        return None
    response = bytearray()
    try:
        # Don't steal already queued input, or flush it on entering/leaving cbreak.
        if select.select([fd], [], [], 0)[0]:
            return None
        original = termios.tcgetattr(fd)
        mode = termios.tcgetattr(fd)
        mode[3] &= ~(termios.ICANON | termios.ECHO)
        mode[6][termios.VMIN] = 0
        mode[6][termios.VTIME] = 0
        try:
            termios.tcsetattr(fd, termios.TCSANOW, mode)
            # Canonical mode hides an unfinished line from select(). Check again
            # now that every queued character is visible, without consuming it.
            if select.select([fd], [], [], 0)[0]:
                return None
            if handle is None:
                # Keep the query behind any text already queued on stdout.
                sys.stdout.flush()
            os.write(query_fd, b"\x1b]11;?\x1b\\")
            deadline = time.monotonic() + timeout
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not select.select([fd], [], [], remaining)[0]:
                    break
                chunk = os.read(fd, 1)
                if not chunk:
                    break
                response.extend(chunk)
                result = background_theme(response)
                if result is not None:
                    response[:] = _BACKGROUND.sub(b"", response)
                    return result
        finally:
            termios.tcsetattr(fd, termios.TCSANOW, original)
    except OSError, ValueError, termios.error:
        pass
    finally:
        if handle is not None:
            handle.close()
        else:
            # Typing can race the query. Only the OSC reply belongs to us;
            # replay everything else through the editor's normal input parser.
            _pending_input.extend(response)
    return None


def replay_pending_input(app, on_theme=None) -> None:
    """Return bytes read during the probe before the editor reads newer input.

    Also keeps filtering appearance replies out of typing for the whole
    session, passing each to `on_theme`: a late answer to the startup probe,
    and every later one `pcode.appearance` asks for.
    """
    from prompt_toolkit.input.vt100 import Vt100Input

    from pcode.startup_input import StartupInput

    if not isinstance(app.input, Vt100Input):
        return
    app.input = StartupInput(app.input.stdin, bytes(_pending_input), on_theme)
    _pending_input.clear()


def detect_theme() -> str:
    """Use OSC 11, then COLORFGBG (when supplied by the terminal), then dark."""
    detected = _query_background()
    if detected is not None:
        return detected
    try:
        background = int(os.environ.get("COLORFGBG", "").split(";")[-1])
    except ValueError:
        return "dark"
    # COLORFGBG describes ANSI palette indices, not RGB values.
    return "light" if background in (7, 15) else "dark"
