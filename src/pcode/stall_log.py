"""Log event-loop stalls in the terminal: the moments typed keys wait to be read.

Keystrokes and model output share one asyncio loop. When synchronous work holds
it, keys queue in the tty and land together once it frees, so typing seems to
freeze and then a whole word pops up. A heartbeat task notes when the loop last
ran; a watcher thread samples the loop thread's stack while the heartbeat is
overdue and appends one JSON line per stall to `stalls.jsonl` in the state
directory. A freeze with no stall logged at that time means the loop was free
and the terminal itself held the frame back.

A stall that holds the GIL throughout (garbage collection, one long C call)
leaves the watcher no chance to sample, so it is logged from the late heartbeat
alone, with no stacks; `gc_ms` says how much of it was collection.

Records hold file names, line numbers, and function names, never arguments or
locals, as `pcode.profiling` does.
"""

import asyncio
import gc
import json
import os
import signal
import sys
import threading
import time
from collections import Counter
from pathlib import Path

THRESHOLD = 0.1
"""Seconds late before a heartbeat counts as a stall; ~100 ms is where typing visibly lags."""
BEAT = 0.05
POLL = 0.02
MAX_BYTES = 1_000_000
"""The log is renamed to `stalls.jsonl.1` past this size, replacing the previous one."""
FRAMES_KEPT = 30
STACKS_KEPT = 3
BUSY_SHARE = 0.1
"""Process CPU per wall second above which an unsampled stall was work, not a suspended
process (Ctrl+Z), which stops the watcher thread as well. Low,
because a busy process on a loaded machine is still a stall the user feels yet may
get only a fraction of a core (a quarter was measured); a suspended one gets none."""


def stall_log_path() -> Path:
    state = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state")
    return state / "pcode" / "stalls.jsonl"


def _where(frame) -> str:
    path = frame.f_code.co_filename
    for marker in ("site-packages/", "/src/"):
        if marker in path:
            path = path.rsplit(marker, 1)[1]
            break
    else:
        path = os.path.basename(path)
    return f"{path}:{frame.f_lineno} {frame.f_code.co_qualname}"


def _idle(frame) -> bool:
    """The event loop waiting for I/O, so it is late only because the process was
    suspended. A blocking `subprocess.run` also waits in a selector, but not one
    the loop called, and that is a stall."""
    caller = frame.f_back
    return (
        frame.f_code.co_filename.endswith("selectors.py")
        and caller is not None
        and caller.f_code.co_name == "_run_once"
    )


def _suspended(before: tuple[float, float], after: tuple[float, float], threshold: float) -> bool:
    """Whether the process was stopped (Ctrl+Z) between two (wall, process CPU) readings.

    The watcher thread is stopped with it, so its wait overran, and nothing ran
    meanwhile. A stall that holds the GIL also delays the watcher, but burns CPU.
    """
    gap = after[0] - before[0]
    return gap >= threshold and after[1] - before[1] < BUSY_SHARE * gap


def _on_main_thread() -> bool:
    return threading.current_thread() is threading.main_thread()


class StallWatch:
    """Run `heartbeat()` as a task on the loop to watch; `stop()` when done."""

    def __init__(self, path: Path | None = None, *, threshold: float = THRESHOLD, context=None):
        self.path = path or stall_log_path()
        self.threshold = threshold
        # Called from the watcher thread when a stall begins; must only read.
        self.context = context
        self._due: float | None = None
        # The latest late beat, (due, how late it ran); beats that late are at
        # least `threshold` apart, so the watcher sees every one.
        self._late: tuple[float, float] | None = None
        self._loop_thread: int | None = None
        self._gc_started = 0.0
        self._gc_seconds = 0.0
        self._stop = threading.Event()
        self._watcher: threading.Thread | None = None
        # When the process last resumed from a stop (Ctrl+Z, then `fg`), as seen
        # by a SIGCONT handler. It runs on the loop thread as soon as that thread
        # runs again, so unlike the watcher's CPU test it does not depend on how
        # soon a loaded machine schedules the watcher after the resume.
        self._continued: float | None = None
        self._sigcont_installed = False
        self._previous_sigcont = None

    async def heartbeat(self) -> None:
        self._loop_thread = threading.get_ident()
        if self._watcher is None:
            self._catch_sigcont()
            gc.callbacks.append(self._gc)
            self._watcher = threading.Thread(target=self._watch, name="stall-watch", daemon=True)
            self._watcher.start()
        try:
            while True:
                now = time.monotonic()
                if self._due is not None and now - self._due >= self.threshold:
                    self._late = (self._due, now - self._due)
                self._due = now + BEAT
                await asyncio.sleep(BEAT)
        finally:
            # Shutdown work after cancellation is not a stall.
            self._due = None

    def stop(self) -> None:
        self._stop.set()
        if self._watcher is not None:
            self._watcher.join(timeout=1)
        if self._gc in gc.callbacks:
            gc.callbacks.remove(self._gc)
        self._release_sigcont()

    def _catch_sigcont(self) -> None:
        # Python only lets the main thread install handlers; elsewhere the
        # watcher falls back to its CPU test alone.
        if not hasattr(signal, "SIGCONT") or not _on_main_thread():
            return
        self._previous_sigcont = signal.getsignal(signal.SIGCONT)
        signal.signal(signal.SIGCONT, self._sigcont)
        # Restart interrupted calls, as with no handler: native threads that do
        # not retry on EINTR must not start failing on every `fg`.
        signal.siginterrupt(signal.SIGCONT, False)
        self._sigcont_installed = True

    def _release_sigcont(self) -> None:
        if not self._sigcont_installed or not _on_main_thread():
            return
        # Left alone if someone has replaced it since: theirs now.
        if signal.getsignal(signal.SIGCONT) == self._sigcont:
            # None: installed outside Python, so it cannot be put back.
            previous = self._previous_sigcont
            signal.signal(signal.SIGCONT, signal.SIG_DFL if previous is None else previous)
        self._sigcont_installed = False

    def _sigcont(self, signum, frame) -> None:
        self._continued = time.monotonic()
        if callable(self._previous_sigcont):
            self._previous_sigcont(signum, frame)

    def _gc(self, phase: str, info: dict) -> None:
        if phase == "start":
            self._gc_started = time.monotonic()
        else:
            self._gc_seconds += time.monotonic() - self._gc_started

    def _watch(self) -> None:
        stalled = None  # When the beat being sampled fell due (or the resume, if later).
        # The clock reading behind the latest sample: a stop can land in the rest
        # of that poll, after the resume marker was read, and is then noticed
        # only on the next poll, whose start is already past it.
        sampled = None
        samples: Counter[tuple[str, ...]] = Counter()
        context: dict = {}
        settled = None  # The late beat last accounted for.
        # (wall, process CPU, GC seconds) at the last poll before the stall.
        mark = (time.monotonic(), time.process_time(), self._gc_seconds)
        # A beat due before this fell due while the whole process was stopped,
        # so it counts as late only from here.
        resumed = float("-inf")
        seen = self._continued  # The last SIGCONT accounted for.
        while True:
            # Taken just before waiting, so the watcher's own work (a write, a
            # stack walk) is not mistaken for time spent stopped.
            waited = (time.monotonic(), time.process_time())
            if self._stop.wait(POLL):
                break
            now = time.monotonic()
            continued = self._continued
            signalled = continued is not None and continued != seen
            seen = continued
            if signalled or _suspended(waited, (now, time.process_time()), self.threshold):
                # The loop may resume mid-callback, so sampling it now would
                # blame that callback for the pause. A stall already under way
                # is written as far as it got.
                if samples:
                    self._write(sampled - stalled, samples, context, mark, ended=sampled)
                # The handler's reading is the resume itself; `now` can be well
                # after it when the resumed loop holds the GIL.
                resumed = max(resumed, continued) if signalled else now
                stalled, samples, context = None, Counter(), {}
            late = self._late
            if late is not None and late[0] != settled:
                settled = late[0]
                # From when it ran, how late it was counting from the resume.
                lateness = late[0] + late[1] - max(late[0], resumed)
                wall, cpu, _ = mark
                busy = time.process_time() - cpu >= BUSY_SHARE * (now - wall)
                if lateness >= self.threshold and (samples or busy):
                    self._write(lateness, samples, context or self._context(), mark)
                stalled, samples, context = None, Counter(), {}
            due = self._due
            start = None if due is None else max(due, resumed)
            overdue = start is not None and now >= start + self.threshold
            if overdue:
                frame = sys._current_frames().get(self._loop_thread)
                if frame is not None and not _idle(frame):
                    if stalled is None:
                        stalled, context = start, self._context()
                    stack = []
                    while frame is not None and len(stack) < FRAMES_KEPT:
                        stack.append(_where(frame))
                        frame = frame.f_back
                    samples[tuple(stack)] += 1
                    sampled = now
            # Hold the mark while overdue: the first poll after a GIL-held stall
            # can run before the heartbeat does, with the loop already idle.
            if stalled is None and not overdue:
                mark = (now, time.process_time(), self._gc_seconds)
        if samples:
            # Stopped mid-stall: the loop is late by at least this much, up to
            # the last sample if the process was suspended since.
            now = time.monotonic()
            suspended = self._continued != seen or _suspended(
                waited, (now, time.process_time()), self.threshold
            )
            ended = sampled if suspended else now
            self._write(ended - stalled, samples, context, mark, ended=ended)

    def _context(self) -> dict:
        if self.context is None:
            return {}
        try:
            return dict(self.context())
        except Exception:
            return {}

    def _write(
        self, late: float, samples: Counter, context: dict, mark: tuple, ended: float | None = None
    ) -> None:
        """`ended` is when the stall ended on the monotonic clock, if not just now."""
        began = time.time() - late - (0.0 if ended is None else time.monotonic() - ended)
        record = {
            "at": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(began)),
            "pid": os.getpid(),
            "stall_ms": round(late * 1000),
            "gc_ms": round((self._gc_seconds - mark[2]) * 1000, 1),
            "samples": sum(samples.values()),
            **context,
            # Innermost frame first: the top line is what was running. Empty
            # when the stall held the GIL throughout.
            "stacks": [
                {"samples": count, "frames": list(stack)}
                for stack, count in samples.most_common(STACKS_KEPT)
            ],
        }
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            if self.path.exists() and self.path.stat().st_size > MAX_BYTES:
                os.replace(self.path, self.path.with_name(self.path.name + ".1"))
            with self.path.open("a") as file:
                file.write(json.dumps(record) + "\n")
        except OSError:
            pass  # Diagnostics must never disturb the session.
