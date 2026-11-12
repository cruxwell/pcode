"""Unsent editor text, kept recoverable.

The editor loses text in two ways. A reset (sending, or Ctrl+C) rebuilds its
lines from the prompt history, which drops whatever was typed and not sent,
including edits made to a recalled prompt and then left behind with ↑ or ↓.
And the history itself lives in memory, so a closed or killed session takes
any unsent text with it.

`Drafts` covers both. Before a reset it adds each unsent text to the history,
where ↑ finds it again. While the editor runs it writes each one to its own
file every `INTERVAL` seconds, and the next session loads those files into its
history. Sending a draft deletes its file.
"""

import asyncio
import os
import time
from collections.abc import Callable
from itertools import count
from pathlib import Path

from prompt_toolkit.buffer import Buffer

# Seconds between saves of whatever is unsent.
INTERVAL = 10.0
# Saved drafts a new session loads; older files are deleted then.
KEEP = 50
# Seconds after which a write that never finished is litter.
STALE_WRITE = 60


def default_dir() -> Path:
    state = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state")
    return state / "pcode" / "drafts"


def origins(buffer: Buffer) -> list[str | None]:
    """For each working line, the history entry it started as a copy of.

    The new prompt at the end started empty. A reset refills the other lines
    in the background; until that is done theirs is unknown, None.
    """
    lines = len(buffer._working_lines)
    task = buffer._load_history_task
    if task is None or not task.done():
        return [None] * (lines - 1) + [""]
    # Entries added since the lines were filled sit past the end of them.
    return [*buffer.history.get_strings()[: lines - 1], ""]


def edited(buffer: Buffer, skip: int | None = None) -> dict[int, str]:
    """The buffer's unsent texts by working line, leaving out line ``skip``."""
    result = {}
    for index, (line, origin) in enumerate(zip(buffer._working_lines, origins(buffer))):
        if index != skip and origin is not None and line.strip() and line != origin:
            result[index] = line
    return result


class Drafts:
    """Keeps a buffer's unsent text, in its history and under ``root``.

    Without a ``root`` nothing is written; resets still keep their text.
    """

    def __init__(self, root: Path | None = None) -> None:
        self.root = root
        # The buffer `run` saves, set once the editor exists.
        self.buffer: Buffer | None = None
        # Saved text -> its file, for every draft loaded or written here.
        self._files: dict[str, Path] = {}
        # Working line -> the text last written for it.
        self._slots: dict[int, str] = {}
        self._serial = count()

    def load(self) -> list[str]:
        """The saved drafts, oldest first, deleting all but the newest `KEEP`."""
        if self.root is None:
            return []
        found = []
        try:
            for path in self.root.iterdir():
                try:
                    modified = path.stat().st_mtime
                except OSError:
                    continue
                if path.suffix == ".txt":
                    found.append((modified, path))
                elif path.suffix == ".tmp" and time.time() - modified > STALE_WRITE:
                    _unlink(path)
        except OSError:
            return []
        found.sort()
        for _, path in found[: max(0, len(found) - KEEP)]:
            _unlink(path)
        texts = []
        for _, path in found[-KEEP:]:
            try:
                text = path.read_text(encoding="utf-8")
            except OSError, ValueError:
                continue
            if not text.strip():
                continue
            if text in self._files:
                # The same text saved twice: the newer file stands for both.
                _unlink(self._files[text])
                texts.remove(text)
            self._files[text] = path
            texts.append(text)
        return texts

    def save(self, buffer: Buffer | None = None, skip: int | None = None) -> None:
        """Write every unsent text that changed since it was last written."""
        buffer = buffer or self.buffer
        if buffer is None or self.root is None:
            return
        for index, text in edited(buffer, skip).items():
            previous = self._slots.get(index)
            path = self._files.get(text)
            # Another session may have deleted the file: loading trims the directory.
            if text == previous and path is not None and path.exists():
                continue
            # The same draft, further along, rewrites its file.
            path = self._files.get(previous) or path or self._new_path()
            if self._write(path, text):
                self._files.pop(previous, None)
                if (other := self._files.get(text)) not in (None, path):
                    _unlink(other)
                self._files[text] = path
                self._slots[index] = text

    def remember(self, buffer: Buffer, skip: int | None = None) -> None:
        """Add the unsent texts to the history, where a reset cannot drop them.

        Safe to repeat: a send that is then refused resets nothing, and its
        lines are still unsent the next time.
        """
        known = set(buffer.history.get_strings())
        for text in edited(buffer, skip).values():
            if text not in known:
                buffer.history.append_string(text)
                known.add(text)

    def keep(self, buffer: Buffer) -> None:
        """Call before the buffer is reset unsent: everything in it is kept."""
        self.save(buffer)
        self.remember(buffer)
        # The reset renumbers the lines.
        self._slots.clear()

    def accept(self, buffer: Buffer, submit: Callable[[str], object]) -> bool:
        """Send the buffer's text through ``submit``, as its accept handler.

        ``submit`` returns true to refuse the text, which keeps it in the
        editor. Otherwise the buffer resets: the other unsent lines are
        already in the history, under the prompt sent, and the files that
        held this one are deleted.
        """
        text, index = buffer.text, buffer.working_index
        origin = origins(buffer)[index]
        self.remember(buffer, skip=index)
        buffer.append_to_history()
        if submit(text):
            return True
        # What was sent, the earlier state of it that a save caught, and the
        # saved draft it was recalled from.
        for saved in (text, self._slots.get(index), origin):
            path = self._files.pop(saved, None)
            if path is not None:
                _unlink(path)
        self.save(buffer, skip=index)
        self._slots.clear()
        return False

    async def run(self) -> None:
        while True:
            await asyncio.sleep(INTERVAL)
            self.save()

    def _new_path(self) -> Path:
        return self.root / f"{time.time_ns()}-{os.getpid()}-{next(self._serial)}.txt"

    def _write(self, path: Path, text: str) -> bool:
        temporary = path.with_suffix(".tmp")
        try:
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as file:
                file.write(text)
            os.replace(temporary, path)
        except OSError, ValueError:
            # Never the editor's problem: a full disk only costs the backup.
            _unlink(temporary)
            return False
        return True


def _unlink(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass
