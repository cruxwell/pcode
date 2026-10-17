"""Read-only replay of journaled display events, never of model/tool execution."""

import hashlib
import json
import os
import stat
import time
from collections import Counter, deque
from dataclasses import dataclass
from functools import partial
from pathlib import Path

from pydantic import TypeAdapter, ValidationError

from pcode import runtime
from pcode.conversation_tree import side_record
from pcode.stream_display import present_events, present_stream_event
from pcode.ui import Activity, TerminalOutput, Transcript

MAX_RECORD_BYTES = 8 * 1024 * 1024
EVENT_TYPES = (
    runtime.Message,
    runtime.TextDelta,
    runtime.Thinking,
    runtime.ThinkingDelta,
    runtime.ToolStarted,
    runtime.ToolSummary,
    runtime.EditCompleted,
    runtime.CacheBust,
    runtime.RunStatus,
    runtime.PlanUpdated,
)
ADAPTERS = {cls.__name__: TypeAdapter(cls) for cls in EVENT_TYPES}
BOUNDARIES = {"turn_started", "turn_completed", "turn_cancelled", "turn_failed"}
METADATA = {"tree_selected", "compaction_checkpoint"}


class EndOnlyOutput(TerminalOutput):
    """Experimental comparison only: parse at semantic finish boundaries, not per line."""

    def _commit_blocks(self, *, thinking: bool = False) -> None:
        pass


class JournalSnapshot:
    """Freeze the readable byte prefix without opening/locking/modifying a SavedSession."""

    def __init__(self, path: Path):
        if path.parent.is_symlink() or path.is_symlink():
            raise ValueError("symlinked journals are not supported")
        fd = os.open(
            path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        )
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                raise ValueError("journal must be a regular file")
            self.size = info.st_size
            self.file = os.fdopen(fd, "rb")
        except BaseException:
            os.close(fd)
            raise
        self.expected_digest = None

    def close(self):
        self.file.close()

    def records(self):
        # A buffered seek within the previous read buffer can return stale bytes
        # after an in-place edit. SEEK_END invalidates that buffer before rereading.
        self.file.seek(0, os.SEEK_END)
        self.file.seek(0)
        remaining = self.size
        digest = hashlib.sha256()
        self.skipped = Counter()
        while remaining:
            line = self.file.readline(min(remaining, MAX_RECORD_BYTES + 1))
            if not line:
                raise ValueError("journal was truncated during replay")
            remaining -= len(line)
            digest.update(line)
            if len(line) > MAX_RECORD_BYTES:
                # Drain one oversized record in bounded chunks without decoding it.
                while remaining and not line.endswith(b"\n"):
                    line = self.file.readline(min(remaining, MAX_RECORD_BYTES + 1))
                    if not line:
                        raise ValueError("journal was truncated during replay")
                    remaining -= len(line)
                    digest.update(line)
                self.skipped["oversized"] += 1
                continue
            try:
                record = json.loads(line)
            except ValueError, UnicodeDecodeError:
                self.skipped["malformed"] += 1
                continue
            if not isinstance(record, dict):
                self.skipped["malformed"] += 1
            elif record.get("version", 1) != 1:
                self.skipped["unsupported_version"] += 1
            else:
                yield record
        self.digest = digest.hexdigest()
        if self.expected_digest is not None and self.digest != self.expected_digest:
            raise ValueError("journal changed between replay passes")
        self.expected_digest = self.digest


@dataclass
class ReplaySettings:
    show_thinking: bool = True
    command_scrollback: bool = False
    start_turn: int = 1
    max_turns: int | None = None


async def replay_journal(
    snapshot: JournalSnapshot, output: TerminalOutput, settings: ReplaySettings
):
    """Measure shared live presentation code; no prompt app, backend, or session writer."""
    activity = Activity(thinking_mode="scrollback" if settings.show_thinking else "off")
    transcript = Transcript(
        output.console,
        activity=activity,
        preferences={"show_commands": "on" if settings.command_scrollback else "off"},
        detected_theme="dark",
    )
    transcript.output = output
    # Edits still render and are retained by Transcript; no diff-browser history is needed.
    present = partial(
        present_events, activity=activity, transcript=transcript, edits=deque(maxlen=0)
    )
    counts = Counter()
    skipped = Counter()
    turns = []
    current = None
    turn_index = 0
    open_turn = False
    render_cpu = render_wall = 0.0
    started, cpu_started = time.perf_counter(), time.process_time()

    def selected(index):
        return index >= settings.start_turn and (
            settings.max_turns is None or index < settings.start_turn + settings.max_turns
        )

    async def finish_turn():
        nonlocal open_turn, render_cpu, render_wall
        if not open_turn:
            return
        before, cpu_before = time.perf_counter(), time.process_time()
        output.end_turn()
        activity.tools.clear()
        activity.status = ""
        await output.flush()
        render_wall += time.perf_counter() - before
        cpu = time.process_time() - cpu_before
        render_cpu += cpu
        if current is not None:
            current["render_cpu_seconds"] += cpu
        open_turn = False

    side_runs: set = set()
    for record in snapshot.records():
        kind = record.get("kind")
        if not isinstance(kind, str):
            skipped["malformed"] += 1
            continue
        if side_record(record):
            side_runs.add(record.get("run_id"))
        if record.get("run_id") in side_runs:
            # A side question, journaled beside the turn: never main output.
            skipped["side_question"] += 1
            continue
        if kind in METADATA:
            skipped["metadata"] += 1
            continue
        if kind not in ADAPTERS and kind not in BOUNDARIES:
            skipped["unknown_kind"] += 1
            continue
        if kind == "turn_started":
            # Also settles an interrupted attempt without a closing journal record.
            await finish_turn()
            turn_index += 1
            current = None
            if selected(turn_index):
                current = dict(turn=turn_index, events=0, characters=0, render_cpu_seconds=0.0)
                turns.append(current)
        elif turn_index == 0:
            # Legacy journals can contain display events before a turn marker.
            turn_index = 1
            if selected(turn_index):
                current = dict(turn=turn_index, events=0, characters=0, render_cpu_seconds=0.0)
                turns.append(current)
        if not selected(turn_index):
            skipped["outside_turn_range"] += 1
            continue
        event = None
        if kind in ADAPTERS:
            try:
                event = ADAPTERS[kind].validate_python(record)
            except ValidationError:
                skipped["invalid_event"] += 1
                continue
        elif kind == "turn_started" and not isinstance(record.get("prompt", ""), str):
            skipped["invalid_event"] += 1
            continue
        counts[kind] += 1
        if current is not None:
            current["events"] += 1
            if kind in {"TextDelta", "ThinkingDelta", "Message", "Thinking"}:
                current["characters"] += len(getattr(event, "text", getattr(event, "markdown", "")))
        if kind in BOUNDARIES and kind != "turn_started":
            await finish_turn()
            continue
        open_turn = True
        before, cpu_before = time.perf_counter(), time.process_time()
        if kind == "turn_started":
            output.begin_turn(record.get("prompt", ""))
        else:
            present_stream_event(
                event, output=output, transcript=transcript, activity=activity, present=present
            )
        await output.flush()
        elapsed, cpu = time.perf_counter() - before, time.process_time() - cpu_before
        render_wall += elapsed
        render_cpu += cpu
        if current is not None:
            current["render_cpu_seconds"] += cpu
    await finish_turn()
    return dict(
        snapshot_bytes=snapshot.size,
        journal_sha256=snapshot.digest,
        turns=len(turns),
        event_counts=dict(counts),
        skipped_records=dict(snapshot.skipped + skipped),
        render_cpu_seconds=render_cpu,
        render_wall_seconds=render_wall,
        cpu_seconds=time.process_time() - cpu_started,
        wall_seconds=time.perf_counter() - started,
        top_turns=sorted(turns, key=lambda item: item["render_cpu_seconds"], reverse=True)[:10],
    )


# Real journals pause for minutes while a tool runs or the user types; a live
# replay is about render cost, so no single gap waits longer than this.
MAX_GAP_SECONDS = 2.0


@dataclass
class JournalTurn:
    prompt: str
    events: list  # (seconds since the previous event, display event)


def journal_turns(snapshot: JournalSnapshot) -> list[JournalTurn]:
    """Split a journal into prompts and their timestamped display events."""
    turns: list[JournalTurn] = []
    previous = None
    side_runs: set = set()
    for record in snapshot.records():
        kind = record.get("kind")
        if side_record(record):
            side_runs.add(record.get("run_id"))
        if record.get("run_id") in side_runs:
            continue
        if kind == "turn_started":
            prompt = record.get("prompt", "")
            turns.append(JournalTurn(prompt if isinstance(prompt, str) else "", []))
            previous = _record_time(record)
            continue
        if kind not in ADAPTERS:
            continue
        if not turns:
            turns.append(JournalTurn("", []))
        try:
            event = ADAPTERS[kind].validate_python(record)
        except ValidationError:
            continue
        stamp = _record_time(record)
        gap = 0.0
        if stamp is not None and previous is not None:
            gap = min(max(0.0, (stamp - previous).total_seconds()), MAX_GAP_SECONDS)
        if stamp is not None:
            previous = stamp
        turns[-1].events.append((gap, event))
    return [turn for turn in turns if turn.events]


def _record_time(record):
    from datetime import datetime

    value = record.get("time")
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


class JournalRuntime:
    """A runtime whose every ``stream`` call plays back the next journaled turn.

    Only what ``PreviewApp.run_live`` reads is here; nothing contacts a model.
    """

    session = None
    recovery_blocked = None
    agent = None
    inspections = None

    def __init__(self, turns: list[JournalTurn], speed: float = 1.0):
        self.turns = list(turns)
        self.speed = speed
        self.events_played = 0

    async def stream(self, text):
        import asyncio

        turn = self.turns.pop(0)
        for gap, event in turn.events:
            if self.speed > 0 and gap > 0:
                await asyncio.sleep(gap / self.speed)
            else:
                # Keep the editor responsive even at full speed.
                await asyncio.sleep(0)
            self.events_played += 1
            yield event

    def close(self) -> None:
        pass
