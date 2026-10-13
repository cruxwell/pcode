"""Read-only inspection projection; never used to replay or execute tools.

Saved payloads stay in the existing private UI journal and are loaded on selection.
Unsaved payloads have a global memory budget; call metadata is retained on eviction.
File changes are held in memory either way: each patch is already capped when made.
"""

import json
import math
from collections import deque
from dataclasses import asdict, dataclass, field
from pathlib import Path

from pcode.edits import change_from_record
from pcode.runtime import EditCompleted, ToolStarted, ToolSummary
from pcode.tool_display import command_text

PAYLOAD_LIMIT = 128 * 1024  # characters per arguments/result field
MEMORY_LIMIT = 8 * 1024 * 1024  # UTF-8 bytes of unsaved payload text
MISSING = "Details were not captured for this call (older session or preview)."


def capture(value: object) -> str:
    """Render a bounded text projection, not arbitrary object reprs."""
    text = (
        value
        if isinstance(value, str)
        else json.dumps(
            value,
            ensure_ascii=False,
            indent=2,
            default=lambda obj: f"<{type(obj).__name__} omitted>",
        )
    )
    text = command_text(text)
    if len(text) > PAYLOAD_LIMIT:
        text = text[:PAYLOAD_LIMIT] + "\n[Inspector truncated this payload at 128 Ki characters.]"
    return text


@dataclass
class Payload:
    text: str | None = None
    path: Path | None = None
    offset: int = 0
    key: str = ""

    def read(self) -> str:
        if self.path is None:
            return self.text if self.text is not None else MISSING
        try:
            with self.path.open("rb") as file:
                file.seek(self.offset)
                record = json.loads(file.readline())
            if not isinstance(record, dict):
                return "Saved details are unavailable: damaged journal record."
            value = record.get(self.key)
            if value is None and self.key == "result" and record.get("error"):
                return capture(record["error"]) + "\n\n" + MISSING
            return capture(value) if value is not None else MISSING
        except OSError, ValueError:
            return "Saved details are unavailable or the journal record is damaged."


@dataclass
class InspectedCall:
    call_id: str
    name: str
    run_id: str
    started_at: str = "unavailable"
    state: str = "running"
    detail: str = ""
    summary: str = ""
    elapsed: float | None = None
    outcome: str = ""
    process_id: str = ""
    execution: str = ""
    arguments: Payload = field(default_factory=Payload)
    result: Payload = field(default_factory=Payload)
    # The file changes this call made: already sanitized and size-capped.
    changes: list[EditCompleted] = field(default_factory=list)

    def title(self) -> str:
        # Only "background" is worth a row tag: waiting is the default, and the
        # detail pane names the mode either way.
        tag = " (background)" if self.execution == "background" else ""
        return f"{self.state:11} {self.name}{tag} · {self.detail}"

    def metadata(self, calls: list["InspectedCall"]) -> list[tuple[str, str]]:
        """Label/value rows describing the call, shared by text and Rich renderings."""
        timing = f"{self.elapsed:.2f}s" if self.elapsed is not None else "unavailable"
        rows = [
            ("Call", self.call_id),
            ("Run", self.run_id),
            ("Started", self.started_at),
            ("Duration", timing),
            *([("Execution", self.execution)] if self.execution else []),
            ("Framework outcome", self.outcome or "unavailable"),
            ("Summary", self.summary),
        ]
        if self.process_id:
            related = [
                c.call_id for c in calls if c is not self and c.process_id == self.process_id
            ]
            rows.append(("Background process", self.process_id))
            rows.append(("Related calls", ", ".join(related) or "none"))
        return rows

    def details(self, calls: list["InspectedCall"]) -> str:
        metadata = f"{self.name} · {self.state}\n" + "".join(
            f"{label}: {value}\n" for label, value in self.metadata(calls)
        )
        return command_text(
            metadata
            + "\nArguments\n─────────\n"
            + self.arguments.read()
            + "\n\nReturned result / error\n───────────────────────\n"
            + self.result.read()
            + "\n\nOnly captured tool output is shown; tool-side truncation cannot be recovered."
        )


class ToolArchive:
    def __init__(self) -> None:
        self.calls: list[InspectedCall] = []
        self._running: dict[tuple[str, str], InspectedCall] = {}
        self._payloads: deque[tuple[Payload, int]] = deque()
        self._bytes = 0
        self.run_id = "unavailable"
        self._path: Path | None = None
        self._file_id: tuple[int, int] | None = None
        self._offset = 0

    def settle(self, state: str) -> None:
        for call in self._running.values():
            call.state = state
        self._running.clear()

    def _payload(self, record: dict, key: str, path: Path | None, offset: int) -> Payload:
        if path is not None:
            return Payload(path=path, offset=offset, key=key)
        value = record.get(key)
        if value is None and key == "result" and record.get("error"):
            value = capture(record["error"]) + "\n\n" + MISSING
        payload = Payload(text=capture(value) if value is not None else None)
        size = len((payload.text or "").encode("utf-8"))
        self._payloads.append((payload, size))
        self._bytes += size
        while self._bytes > MEMORY_LIMIT:
            old, count = self._payloads.popleft()
            old.text = "[Payload evicted from the unsaved session's 8 MiB inspection budget.]"
            self._bytes -= count
        return payload

    def record(self, record: dict, *, path: Path | None = None, offset: int = 0) -> None:
        kind = record.get("kind")
        if not isinstance(kind, str):
            return
        # Ignore malformed metadata without losing healthy surrounding records.
        for key in (
            "name",
            "call_id",
            "run_id",
            "detail",
            "command",
            "purpose",
            "process_id",
            "execution",
            "started_at",
            "time",
            "outcome",
        ):
            if key in record and not isinstance(record[key], str):
                return
        if kind == "turn_started":
            self.settle("unknown")
            self.run_id = record.get("run_id", str(offset))
            return
        if kind in {"turn_completed", "turn_failed", "turn_cancelled"}:
            self.settle("interrupted" if kind == "turn_cancelled" else "unknown")
            return
        if kind == "EditCompleted":
            self._change(record)
            return
        if kind not in {"ToolStarted", "ToolSummary"}:
            return
        if not record.get("name"):
            return
        call_id = record.get("call_id") or f"unavailable-{len(self.calls) + 1}"
        run_id = record.get("run_id") or self.run_id
        key = (run_id, call_id)
        call = self._running.get(key)
        if call is None:
            call = InspectedCall(call_id, capture(record["name"]), run_id)
            self.calls.append(call)
        # Purpose leads the row, command follows it: this list is the inventory
        # of what ran, so it must keep the command a stated intention can't prove.
        invocation = record.get("command") or record.get("detail", "")
        purpose = record.get("purpose") or ""
        if purpose and record.get("command"):
            invocation = f"{purpose} · {invocation}"
        call.detail = command_text(invocation)[:300]
        call.summary = command_text(record.get("detail", ""))[:1000]
        call.process_id = command_text(record.get("process_id") or call.process_id)
        call.execution = record.get("execution") or call.execution
        if kind == "ToolStarted":
            call.started_at = record.get("started_at") or record.get("time", "unavailable")
            call.arguments = self._payload(record, "arguments", path, offset)
            self._running[key] = call
        else:
            call.result = self._payload(record, "result", path, offset)
            elapsed = record.get("elapsed_seconds")
            call.elapsed = (
                elapsed
                if (type(elapsed) in (int, float) and math.isfinite(elapsed) and elapsed >= 0)
                else None
            )
            call.state = "failed" if record.get("failed") else "succeeded"
            call.outcome = record.get("outcome", "")
            self._running.pop(key, None)

    def _change(self, record: dict) -> None:
        """Attach a change to the call in this turn that made it."""
        call_id = record.get("call_id")
        if not (
            call_id
            and isinstance(record.get("path"), str)
            and isinstance(record.get("operation"), str)
            and all(isinstance(record.get(key, ""), str) for key in ("patch", "omitted"))
            and all(type(record.get(key, 0)) is int for key in ("added", "removed"))
            and type(record.get("truncated", False)) is bool
        ):
            return
        # Retries and resumed sessions can reuse a call id, so the turn must match too.
        run_id = record.get("run_id") or self.run_id
        call = next(
            (c for c in reversed(self.calls) if (c.run_id, c.call_id) == (run_id, call_id)), None
        )
        if call is not None:
            call.changes.append(change_from_record(record))

    def event(self, event: ToolStarted | ToolSummary | EditCompleted) -> None:
        self.record({"kind": type(event).__name__, **asdict(event)})

    def update(self, path: Path) -> None:
        """Incrementally index complete appends; payloads remain file-offset references."""
        stat = path.stat()
        identity = (stat.st_dev, stat.st_ino)
        if path != self._path or identity != self._file_id or stat.st_size < self._offset:
            self.__init__()
            self._path, self._file_id = path, identity
        with path.open("rb") as file:
            file.seek(self._offset)
            while True:
                offset = file.tell()
                line = file.readline()
                if not line or not line.endswith(b"\n"):
                    break  # Retry an incomplete append on the next open.
                self._offset = file.tell()
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                if isinstance(record, dict):
                    self.record(record, path=path, offset=offset)
        for call in self._running.values():
            call.state = "unknown"

    @classmethod
    def load(cls, path: Path) -> "ToolArchive":
        archive = cls()
        archive.update(path)
        return archive
