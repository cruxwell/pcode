"""Send library log records and warnings to a file instead of the terminal.

With no handler configured, Python prints any WARNING or above to stderr, which
is the terminal the UI is drawing on: an SDK's warning (google-genai logs one on
every request that mixes function tools with a native tool) lands in the middle
of the editor row. `warnings.warn` prints there too.
"""

import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path

MAX_BYTES = 1_000_000
"""The log is renamed to `library.log.1` past this size, replacing the previous one."""


def library_log_path() -> Path:
    state = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state")
    return state / "pcode" / "library.log"


class _LibraryLog(RotatingFileHandler):
    def handleError(self, record: logging.LogRecord) -> None:
        # The default prints a traceback to stderr, the very thing this avoids.
        pass


class _Discard(logging.NullHandler):
    """Stands in for the file when the state directory cannot be written."""


def install() -> None:
    """Route what libraries log, and the warnings they raise, to `library_log_path()`.

    That is WARNING and above, the root logger's level, plus whatever a
    library was asked to log in more detail (`ANTHROPIC_LOG=debug`).
    """
    # fastmcp otherwise attaches its own stderr handlers on import and stops
    # its records from reaching the root logger.
    os.environ.setdefault("FASTMCP_LOG_ENABLED", "false")
    # `warnings.warn` becomes a record on the `py.warnings` logger.
    logging.captureWarnings(True)
    root = logging.getLogger()
    if any(isinstance(handler, (_LibraryLog, _Discard)) for handler in root.handlers):
        return
    path = library_log_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        # Any handler keeps Python's last-resort stderr handler out of it.
        root.addHandler(_Discard())
        return
    # `delay` leaves the file uncreated until something is logged.
    handler = _LibraryLog(path, maxBytes=MAX_BYTES, backupCount=1, delay=True, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root.addHandler(handler)
