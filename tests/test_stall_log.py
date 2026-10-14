import asyncio
import gc
import json
import os
import signal
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

from pcode import stall_log
from pcode.stall_log import StallWatch


def frame(filename, name, caller=None):
    return SimpleNamespace(
        f_code=SimpleNamespace(co_filename=filename, co_name=name, co_qualname=name),
        f_lineno=1,
        f_back=caller,
    )


class ScriptedWatch:
    """Run the real watcher synchronously, controlling what happens between polls.

    Wall time and process CPU are independent: sleeping and scheduler starvation
    must not accidentally stand in for CPU-bound work or a process suspension.
    Patch module references, not the shared time/sys modules used by pytest.
    """

    def __init__(self, tmp_path, monkeypatch):
        self.wall = 10.0
        self.cpu = 1.0
        self.frame = frame("test_stall_log.py", "block_the_loop")
        self.watch = StallWatch(tmp_path / "stalls.jsonl")
        self.watch._loop_thread = 123
        self.monkeypatch = monkeypatch
        monkeypatch.setattr(
            stall_log,
            "time",
            SimpleNamespace(
                monotonic=lambda: self.wall,
                process_time=lambda: self.cpu,
                time=time.time,
                strftime=time.strftime,
                localtime=time.localtime,
            ),
        )
        monkeypatch.setattr(
            stall_log, "sys", SimpleNamespace(_current_frames=lambda: {123: self.frame})
        )

    def advance(self, wall, cpu=0):
        self.wall += wall
        self.cpu += cpu

    def run(self, *steps):
        steps = iter(steps)

        def wait(timeout):
            assert timeout == stall_log.POLL
            step = next(steps, None)
            if step is None:
                return True
            step()
            return False

        self.monkeypatch.setattr(self.watch._stop, "wait", wait)
        self.watch._watch()
        return records(self.watch.path)


@pytest.fixture
def scripted_watch(tmp_path, monkeypatch):
    return ScriptedWatch(tmp_path, monkeypatch)


def block_the_loop(seconds):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        pass


async def watched(watch, body, *, settle=0.15):
    task = asyncio.create_task(watch.heartbeat())
    try:
        await asyncio.sleep(0.1)
        await body()
        await asyncio.sleep(settle)
    finally:
        task.cancel()
        watch.stop()


def records(path):
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def test_heartbeat_registers_gc_callback_until_stopped(tmp_path):
    watch = StallWatch(tmp_path / "stalls.jsonl")

    async def run():
        task = asyncio.create_task(watch.heartbeat())
        try:
            await asyncio.sleep(0)  # Let heartbeat reach its first await.
            assert watch._gc in gc.callbacks
        finally:
            task.cancel()
            try:
                with pytest.raises(asyncio.CancelledError):
                    await task
            finally:
                watch.stop()
        assert watch._gc not in gc.callbacks

    asyncio.run(run())


def test_a_blocked_loop_logs_one_stall_naming_the_blocker(tmp_path):
    path = tmp_path / "stalls.jsonl"
    watch = StallWatch(path, context=lambda: {"busy": True})

    async def body():
        block_the_loop(0.4)

    asyncio.run(watched(watch, body))
    [record] = records(path)
    assert 250 <= record["stall_ms"] < 1000
    assert record["busy"] is True
    top = record["stacks"][0]["frames"][0]
    assert "block_the_loop" in top and "test_stall_log.py" in top


@pytest.mark.parametrize("caller", ["subprocess", "event_loop"])
def test_a_blocking_subprocess_is_a_stall_though_it_waits_in_a_selector(scripted_watch, caller):
    driver = scripted_watch
    watch = driver.watch
    driver.frame = frame(
        "selectors.py",
        "select",
        frame("subprocess.py", "_communicate")
        if caller == "subprocess"
        else frame("base_events.py", "_run_once"),
    )
    watch._due = driver.wall

    def poll():
        # Regular polls can sample a blocking selector even with no CPU use.
        driver.advance(0.02)

    def heartbeat():
        watch._late = (watch._due, driver.wall - watch._due)
        watch._due = driver.wall + stall_log.BEAT
        driver.advance(0.02)

    result = driver.run(*([poll] * 10), heartbeat)
    if caller == "event_loop":
        assert result == []
    else:
        [record] = result
        assert record["stall_ms"] == 200
        assert record["samples"] > 0
        assert record["stacks"][0]["frames"] == [
            "selectors.py:1 select",
            "subprocess.py:1 _communicate",
        ]


def test_a_slow_log_write_is_not_mistaken_for_a_suspension(scripted_watch):
    """Writing one stall can take a while on a slow disk; the next stall, already
    under way, must still be logged."""
    driver = scripted_watch
    watch = driver.watch
    watch._due = driver.wall
    write = watch._write

    def slow_write(*args, **kwargs):
        driver.advance(0.15)  # Slow disk: wall time passes, but no CPU is used.
        write(*args, **kwargs)

    driver.monkeypatch.setattr(watch, "_write", slow_write)

    def poll():
        driver.advance(0.02)

    def heartbeat():
        watch._late = (watch._due, driver.wall - watch._due)
        watch._due = driver.wall + stall_log.BEAT
        driver.advance(0.02)

    first, second = driver.run(*([poll] * 10), heartbeat, poll, heartbeat)
    assert [first["stall_ms"], second["stall_ms"]] == [200, 140]
    assert first["samples"] > 0 and second["samples"] > 0
    assert first["stacks"][0]["frames"] == second["stacks"][0]["frames"]


@pytest.mark.parametrize("watcher_first", [False, True])
@pytest.mark.parametrize("busy", [False, True], ids=["suspended", "gil_held"])
def test_a_stall_holding_the_gil_is_logged_without_stacks(scripted_watch, watcher_first, busy):
    driver = scripted_watch
    watch = driver.watch
    watch._due = driver.wall + stall_log.BEAT
    driver.frame = frame("selectors.py", "select", frame("base_events.py", "_run_once"))

    def heartbeat():
        watch._late = (watch._due, driver.wall - watch._due)
        watch._due = driver.wall + stall_log.BEAT

    def delayed_poll():
        if busy:
            watch._gc("start", {})
            driver.advance(0.04, cpu=0.04)
            watch._gc("stop", {})
            driver.advance(0.31, cpu=0.26)
        else:
            driver.advance(0.35)
        if not watcher_first:
            heartbeat()

    def next_poll():
        if watcher_first:
            heartbeat()
        driver.advance(0.02)

    result = driver.run(delayed_poll, next_poll)
    if not busy:
        assert result == []
    else:
        [record] = result
        assert record["stacks"] == []
        assert record["samples"] == 0
        assert record["stall_ms"] == 300
        assert record["gc_ms"] == 40


def test_a_resume_the_watcher_wakes_late_for_is_still_a_suspension(scripted_watch):
    """On a loaded machine the watcher can get its first poll after `fg` only once
    the resumed loop has burned enough CPU to look busy; SIGCONT still marks the
    resume, so the stalls on either side are logged apart and without the stop."""
    driver = scripted_watch
    watch = driver.watch
    watch._due = driver.wall

    def spinning_poll():
        driver.advance(0.02, cpu=0.02)

    def stopped_then_starved():
        driver.advance(0.6)  # Stopped: no CPU at all.
        watch._sigcont(signal.SIGCONT, None)
        driver.advance(0.2, cpu=0.2)  # The loop spins before the watcher runs.

    def heartbeat():
        watch._late = (watch._due, driver.wall - watch._due)
        watch._due = driver.wall + stall_log.BEAT
        driver.advance(0.02)

    before, after = driver.run(
        *([spinning_poll] * 15), stopped_then_starved, *([spinning_poll] * 40), heartbeat
    )
    assert [before["stall_ms"], after["stall_ms"]] == [300, 1000]


def test_a_stop_landing_while_the_watcher_samples_is_not_part_of_the_stall(scripted_watch):
    """The stop can land mid-poll, after the watcher read the clock and the resume
    marker: it only sees the SIGCONT on its next poll, whose start is already past
    the stop, so the stall must end at the last sample taken before it."""
    driver = scripted_watch
    watch = driver.watch
    watch._due = driver.wall
    stop_while_sampling = False

    def current_frames():
        nonlocal stop_while_sampling
        if stop_while_sampling:
            stop_while_sampling = False
            driver.advance(0.6)  # Stopped: no CPU at all.
            watch._sigcont(signal.SIGCONT, None)
        return {123: driver.frame}

    driver.monkeypatch.setattr(stall_log, "sys", SimpleNamespace(_current_frames=current_frames))

    def spinning_poll():
        driver.advance(0.02, cpu=0.02)

    def poll_then_stop():
        nonlocal stop_while_sampling
        spinning_poll()
        stop_while_sampling = True

    def heartbeat():
        watch._late = (watch._due, driver.wall - watch._due)
        watch._due = driver.wall + stall_log.BEAT
        driver.advance(0.02)

    before, after = driver.run(
        *([spinning_poll] * 15), poll_then_stop, *([spinning_poll] * 40), heartbeat
    )
    assert [before["stall_ms"], after["stall_ms"]] == [320, 800]


def test_sigcont_handler_chains_to_the_previous_one_until_stopped(tmp_path):
    original = signal.getsignal(signal.SIGCONT)
    seen = []

    def previous(signum, frame):
        seen.append(signum)

    watch = StallWatch(tmp_path / "stalls.jsonl")

    async def run():
        task = asyncio.create_task(watch.heartbeat())
        await asyncio.sleep(0)
        assert signal.getsignal(signal.SIGCONT) == watch._sigcont
        os.kill(os.getpid(), signal.SIGCONT)
        await asyncio.sleep(0)  # Python runs handlers between bytecodes.
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    signal.signal(signal.SIGCONT, previous)
    try:
        asyncio.run(run())
        watch.stop()
        assert watch._continued is not None
        assert seen == [signal.SIGCONT]
        assert signal.getsignal(signal.SIGCONT) == previous
    finally:
        watch.stop()
        signal.signal(signal.SIGCONT, original)


def test_a_suspended_process_logs_no_stall(tmp_path):
    """Ctrl+Z stops the watcher with the loop: a late beat with no CPU behind it is not a stall."""
    path = tmp_path / "stalls.jsonl"
    script = f"""
import asyncio, sys
from pathlib import Path
from pcode.stall_log import StallWatch

async def main():
    watch = StallWatch(Path({str(path)!r}))
    task = asyncio.create_task(watch.heartbeat())
    resumed = asyncio.Event()
    asyncio.get_running_loop().add_reader(sys.stdin, resumed.set)
    await asyncio.sleep(0.1)
    print("ready", flush=True)
    # Idle in the loop's own selector, as a session waiting for keys is.
    await resumed.wait()
    await asyncio.sleep(0.3)
    task.cancel()
    watch.stop()

asyncio.run(main())
"""
    child = subprocess.Popen(
        [sys.executable, "-c", script], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True
    )
    try:
        assert child.stdout.readline() == "ready\n"
        child.send_signal(signal.SIGSTOP)
        time.sleep(0.6)
        child.send_signal(signal.SIGCONT)
        child.stdin.write("\n")
        child.stdin.close()
        assert child.wait(timeout=30) == 0
    finally:
        child.kill()
        child.wait()
        child.stdout.close()
    assert records(path) == []


def test_a_process_suspended_mid_callback_logs_no_stall(tmp_path):
    """Ctrl+Z can land while a callback runs, not only while the loop idles: the
    loop is then busy when both threads resume, which is no stall either."""
    path = tmp_path / "stalls.jsonl"
    script = f"""
import asyncio, os, signal, time
from pathlib import Path
from pcode.stall_log import StallWatch

async def main():
    watch = StallWatch(Path({str(path)!r}))
    task = asyncio.create_task(watch.heartbeat())
    await asyncio.sleep(0.1)
    os.kill(os.getpid(), signal.SIGSTOP)
    # Resumed mid-callback: work on, briefly enough not to be a stall itself.
    end = time.monotonic() + 0.06
    while time.monotonic() < end:
        pass
    await asyncio.sleep(0.3)
    task.cancel()
    watch.stop()

asyncio.run(main())
"""
    child = subprocess.Popen([sys.executable, "-c", script])
    try:
        _, status = os.waitpid(child.pid, os.WUNTRACED)
        assert os.WIFSTOPPED(status)
        time.sleep(0.6)
        child.send_signal(signal.SIGCONT)
        assert child.wait(timeout=30) == 0
    finally:
        child.kill()
        child.wait()
    assert records(path) == []


def test_a_stall_on_both_sides_of_a_suspension_is_logged_without_it(tmp_path):
    """Ctrl+Z pressed because the terminal froze, then `fg`: the freeze is still
    there, before and after, but the time spent stopped is not part of it."""
    path = tmp_path / "stalls.jsonl"
    script = f"""
import asyncio, os, signal, time
from pathlib import Path
from pcode.stall_log import StallWatch

def spin(seconds):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        pass

async def main():
    watch = StallWatch(Path({str(path)!r}))
    task = asyncio.create_task(watch.heartbeat())
    await asyncio.sleep(0.1)
    spin(0.5)
    os.kill(os.getpid(), signal.SIGSTOP)
    spin(1.0)
    await asyncio.sleep(0.3)
    task.cancel()
    watch.stop()

asyncio.run(main())
"""
    child = subprocess.Popen([sys.executable, "-c", script])
    try:
        _, status = os.waitpid(child.pid, os.WUNTRACED)
        assert os.WIFSTOPPED(status)
        time.sleep(0.6)
        child.send_signal(signal.SIGCONT)
        assert child.wait(timeout=30) == 0
    finally:
        child.kill()
        child.wait()
    stalls = sorted(record["stall_ms"] for record in records(path))
    # Before the stop, then after it: neither spans the 600 ms spent stopped.
    # The first is measured from the next beat's due time to the watcher's last
    # poll before the stop, both GIL-bound against the spin, so a loaded runner
    # reads well under the 500 ms spun (146 ms of a 300 ms spin was seen).
    # Including the stop would read over 1,100 ms.
    assert len(stalls) == 2, stalls
    assert 150 <= stalls[0] < 600 and 800 <= stalls[1] < 1500, stalls


def test_a_stall_still_open_at_stop_is_written(tmp_path):
    path = tmp_path / "stalls.jsonl"

    async def body():
        block_the_loop(0.3)

    # Stop the moment the block ends, before the heartbeat can run late.
    asyncio.run(watched(StallWatch(path), body, settle=0))
    [record] = records(path)
    assert record["stall_ms"] >= 100
    assert "block_the_loop" in record["stacks"][0]["frames"][0]


def test_an_idle_loop_logs_nothing(tmp_path):
    path = tmp_path / "stalls.jsonl"

    async def body():
        await asyncio.sleep(0.4)

    # A generous threshold: a loaded test machine can starve the process briefly.
    asyncio.run(watched(StallWatch(path, threshold=0.3), body))
    assert records(path) == []


def test_a_short_block_under_the_threshold_is_ignored(tmp_path):
    path = tmp_path / "stalls.jsonl"

    async def body():
        block_the_loop(0.03)

    asyncio.run(watched(StallWatch(path, threshold=0.3), body))
    assert records(path) == []
