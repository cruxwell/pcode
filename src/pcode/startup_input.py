"""Return startup probe bytes through prompt_toolkit's normal input callback."""

import asyncio
import re
import time
from collections.abc import Callable
from contextlib import contextmanager

from prompt_toolkit.input.vt100 import Vt100Input
from prompt_toolkit.input.vt100_parser import Vt100Parser

# An OSC 11 background reply, or a mode 2031 color-scheme report (1 dark, 2 light).
_REPLY = re.compile(r"\x1b\]11;[^\x07\x1b]{0,64}(?:\x07|\x1b\\)|\x1b\[\?997;([12])n")
# A tail that may still become one of those: held until the next read. Only
# characters a color reply contains, so typing after a truncated reply is
# released at its first keystroke instead of being held for the flush to drop.
_OPEN = re.compile(r"\x1b\]11;[0-9A-Fa-f:/rgb]{0,40}\x1b?|\x1b\[\?997;[12]?")
_HEADERS = ("\x1b]11;", "\x1b[?997;")
# A cursor position report, and a tail that may still become one.
_CPR = re.compile(r"\x1b\[\d+;\d+R")
_OPEN_CPR = re.compile(r"\x1b\[\d*;?\d*")
LATE_CPR_SECONDS = 3


class TerminalReplyParser(Vt100Parser):
    """Take terminal appearance replies out of the key stream, even split across reads.

    The startup probe's reply can arrive after the editor starts, and later
    queries (see `pcode.appearance`) always do. A terminated OSC 11 reply is
    dropped even when its color can't be read; anything else, including an
    overlong or unterminated reply, reaches prompt_toolkit as ordinary input.
    """

    def __init__(self, callback, on_theme: Callable[[str], None] | None = None, late_cprs: int = 0):
        super().__init__(callback)
        self.pending = ""
        self.on_theme = on_theme
        # The startup width probe's position reports still on their way
        # (`pcode.theme`). Reports come in the order asked, so they are the
        # first ones here, ahead of the renderer's own, and must not answer it.
        # A terminal that never answers must not keep swallowing input that
        # looks like one (Shift+F3), so the wait is bounded.
        self.late_cprs = late_cprs
        self.late_deadline = time.monotonic() + LATE_CPR_SECONDS

    def feed(self, data: str) -> None:
        text, self.pending = self.pending + data, ""
        if self.late_cprs and time.monotonic() > self.late_deadline:
            self.late_cprs = 0
        while self.late_cprs and (match := _CPR.search(text)):
            text = text[: match.start()] + text[match.end() :]
            self.late_cprs -= 1
        while match := _REPLY.search(text):
            super().feed(text[: match.start()])
            text = text[match.end() :]
            self._report(match)
        hold = _held(text, self.late_cprs > 0)
        super().feed(text[:hold])
        self.pending = text[hold:]

    def _report(self, match: re.Match) -> None:
        from pcode.theme import background_theme

        if match.group(1):
            detected = "dark" if match.group(1) == "1" else "light"
        else:
            detected = background_theme(match.group().encode())
        if detected is not None and self.on_theme is not None:
            self.on_theme(detected)

    def flush(self) -> None:
        text, self.pending = self.pending, ""
        # An incomplete terminal reply must not hold the next prompt hostage.
        # Anything shorter than a whole header (Escape, Alt-], Alt-[ then ?)
        # is more likely typing, and must reach the parser.
        if not text.startswith(_HEADERS):
            super().feed(text)
        super().flush()


def _held(text: str, cpr: bool = False) -> int:
    """Where an unfinished reply may start in `text`; len(text) when none can.

    `cpr` also holds what may become a position report the probe awaits.
    """
    start = text.find("\x1b")
    while start >= 0:
        tail = text[start:]
        if (
            any(header.startswith(tail) for header in _HEADERS)
            or _OPEN.fullmatch(tail)
            or (cpr and _OPEN_CPR.fullmatch(tail))
        ):
            return start
        start = text.find("\x1b", start + 1)
    return len(text)


class StartupInput(Vt100Input):
    def __init__(
        self,
        stdin,
        pending: bytes,
        on_theme: Callable[[str], None] | None = None,
        *,
        late_cprs: int = 0,
    ):
        super().__init__(stdin)
        self.pending = pending
        self.vt100_parser = TerminalReplyParser(
            lambda key: self._buffer.append(key), on_theme, late_cprs
        )

    @contextmanager
    def attach(self, input_ready_callback):
        with super().attach(input_ready_callback):
            if self.pending:
                # Use the application's callback so it also schedules its normal
                # escape timeout. Feeding keys in pre_run would bypass that timer.
                asyncio.get_running_loop().call_soon(input_ready_callback)
            yield

    def read_keys(self):
        if self.pending:
            # Keep the decoder shared with later reads, including split UTF-8.
            text = self.stdin_reader._stdin_decoder.decode(self.pending)
            self.pending = b""
            self.vt100_parser.feed(text)
        return super().read_keys()
