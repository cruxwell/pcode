#!/usr/bin/env python3
"""Report how the model used the shell tool in saved pcode sessions.

The transcript only keeps a display projection of shell results, so this reads
the step store, which holds what the model actually saw. It answers the
questions that come up when judging the job model: did the model poll with
`sleep`, did it background long commands and wait for them, how many results
were spilled, and how often a `| tail` hid a failing exit status.

Default output is content-free: counts and durations only. `-v` lists each
call with a clipped, redacted command line. See docs/tools.md.
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from pcode.diagnostics import redact  # noqa: E402
from pcode.sessions import (  # noqa: E402
    SessionError,
    list_sessions,
    read_info,
    resolve_session,
    session_root,
    snapshot_messages_json,
)
from pcode.shell import REDUCED_SHELL_OUTPUT  # noqa: E402
from pcode.tool_display import command_text  # noqa: E402

JOB_TOOLS = ("shell", "wait_for_job", "job_output", "stop_job", "list_jobs")
# A foreground wait this close to the tool's ceiling (shell_tools.MAX_WAIT_SECONDS)
# was one slow run away from handing back a handle mid-command.
NEAR_CAP_SECONDS = 240.0

_MARKER = re.compile(r"\[(j\d+) · (running|stopped|exit (-?\d+)) · ([^\]]*)\]")
_DURATION = re.compile(r"^(?:(\d+)h(\d+)m|(\d+)m(\d+)s|([\d.]+)(ms|s))$")
_CD_PREFIX = re.compile(r"^cd\s+(?P<dir>'[^']*'|\"[^\"]*\"|[^\s;&|]+)\s*(?:&&|;)\s*")
# The command's own status is hidden when its output is piped into a filter.
_PIPED_TO_FILTER = re.compile(r"\|\s*(?:tail|head|grep|rg|wc|sed|awk|cut|sort|uniq|less|cat)\b")
_FAILURE_TEXT = re.compile(r"Error \d+|\bFAILED\b|\bTraceback\b|\d+ failed\b|\berror:", re.I)
# Polling is a `sleep` that is the whole command or is followed by a read of
# something else's progress. A `sleep` that paces the command's own steps
# (`pkill ...; sleep 2; pytest`) or sits inside a loop is the script's business.
_SLEEP = re.compile(r"^sleep\s+\d+\s*(?:$|[;&]+\s*(?:cat|tail|ls|test|\[|grep|curl)\b)")
_LOOP = re.compile(r"\b(?:while|for|until)\b")
_READ_ONLY = re.compile(r"^(?:cat|sed\s+-n|head|tail|grep|rg|ls|find|wc)\b")
_NOTICE = re.compile(r"^\[j\d+\] .* → ")


@dataclass
class Call:
    tool: str
    command: str
    background: bool
    purpose: str
    result: str
    job: str = ""
    outcome: str = ""
    exit_code: int | None = None
    elapsed: float | None = None

    @property
    def running(self) -> bool:
        return self.outcome == "running"

    @property
    def failed(self) -> bool:
        return self.exit_code not in (None, 0)

    @property
    def body(self) -> str:
        """The command without a leading `cd`, for classifying what it does."""
        return _CD_PREFIX.sub("", self.command.strip())


@dataclass
class Finding:
    level: str
    message: str


@dataclass
class Report:
    session: str
    model: str
    updated: str
    workspace: str
    calls: list[Call]
    notices: int
    findings: list[Finding] = field(default_factory=list)

    def of(self, tool: str) -> list[Call]:
        return [call for call in self.calls if call.tool == tool]


def _duration(text: str) -> float | None:
    match = _DURATION.match(text.strip())
    if match is None:
        return None
    hours, minutes, minutes2, seconds2, number, unit = match.groups()
    if hours is not None:
        return int(hours) * 3600 + int(minutes) * 60
    if minutes2 is not None:
        return int(minutes2) * 60 + int(seconds2)
    return float(number) / (1000 if unit == "ms" else 1)


def _parse_marker(call: Call) -> None:
    match = None
    for match in _MARKER.finditer(call.result):
        pass  # The last marker belongs to this call.
    if match is None:
        return
    call.job, outcome, code, elapsed = match.groups()
    call.outcome = (
        "running" if outcome == "running" else "stopped" if outcome == "stopped" else "exit"
    )
    call.exit_code = int(code) if code is not None else None
    call.elapsed = _duration(elapsed)


def _args(part: dict) -> dict:
    args = part.get("args")
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except ValueError:
            return {}
    return args if isinstance(args, dict) else {}


def _content(part: dict) -> str:
    content = part.get("content")
    return content if isinstance(content, str) else json.dumps(content)


def _messages(database: Path):
    """Yield every parent-run snapshot's history, in order.

    Snapshots are cumulative, so calls repeat across them; callers deduplicate
    by tool call id. Delegated runs are a different conversation and skipped.
    """
    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    try:
        query = "SELECT messages FROM snapshots WHERE parent_run_id IS NULL ORDER BY seq"
        for (raw,) in connection.execute(query):
            yield json.loads(snapshot_messages_json(connection, raw))
    except sqlite3.DatabaseError as error:
        raise SessionError(f"Unreadable step store: {error}") from None
    finally:
        connection.close()


def collect(database: Path) -> tuple[list[Call], int]:
    calls: dict[str, Call] = {}
    results: dict[str, str] = {}
    notices: set[str] = set()
    for messages in _messages(database):
        for message in messages:
            for part in message.get("parts", []):
                kind = part.get("part_kind")
                if kind == "tool-call" and part.get("tool_name") in JOB_TOOLS:
                    args = _args(part)
                    calls.setdefault(
                        part["tool_call_id"],
                        Call(
                            tool=part["tool_name"],
                            command=str(args.get("command", "")),
                            background=bool(args.get("background")),
                            purpose=str(args.get("purpose") or ""),
                            result="",
                        ),
                    )
                elif kind in ("tool-return", "retry-prompt") and part.get("tool_call_id"):
                    results.setdefault(part["tool_call_id"], _content(part))
                elif kind == "user-prompt":
                    content = part.get("content")
                    if isinstance(content, str) and _NOTICE.match(content):
                        notices.add(content)
    for identity, call in calls.items():
        call.result = results.get(identity, "")
        _parse_marker(call)
    return list(calls.values()), len(notices)


def analyze(directory: Path) -> Report:
    info = read_info(directory)
    database = directory / "steps.sqlite3"
    if not database.is_file():
        raise SessionError("No step store; the session never ran a turn.")
    calls, notices = collect(database)
    report = Report(info.id, info.model, info.updated[:19], info.workspace, calls, notices)
    report.findings = findings(report)
    return report


def findings(report: Report) -> list[Finding]:
    shell = report.of("shell")
    found: list[Finding] = []
    if not shell:
        return [Finding("ok", "no shell calls")]

    polls = [c for c in shell if _SLEEP.search(c.body) and not _LOOP.search(c.body)]
    if polls:
        found.append(
            Finding(
                "warn", f"{len(polls)} command(s) sleep between turns instead of waiting on a job"
            )
        )

    masked = [
        c
        for c in shell
        if c.exit_code == 0 and _PIPED_TO_FILTER.search(c.body) and _FAILURE_TEXT.search(c.result)
    ]
    if masked:
        found.append(
            Finding(
                "warn",
                f"{len(masked)} result(s) exit 0 with failure text; a pipe hid the status",
            )
        )

    near_cap = [c for c in report.calls if c.running and (c.elapsed or 0) >= NEAR_CAP_SECONDS]
    if near_cap:
        found.append(
            Finding("warn", f"{len(near_cap)} wait(s) hit the ceiling and handed back a handle")
        )
    close = [
        c
        for c in shell
        if not c.running and not c.background and (c.elapsed or 0) >= NEAR_CAP_SECONDS
    ]
    if close:
        found.append(
            Finding(
                "info", f"{len(close)} foreground run(s) finished within 30s of the wait ceiling"
            )
        )

    prefixed = [c for c in shell if _cd_into(c.command, report.workspace)]
    if prefixed and len(prefixed) > len(shell) // 2:
        found.append(
            Finding(
                "info", f"{len(prefixed)}/{len(shell)} commands start with cd into the workspace"
            )
        )

    reads = [c for c in shell if _READ_ONLY.match(c.body)]
    if reads:
        found.append(
            Finding("info", f"{len(reads)} read-only command(s) that the file tools could serve")
        )

    spilled = [c for c in report.calls if c.result.startswith(REDUCED_SHELL_OUTPUT)]
    if spilled:
        found.append(
            Finding("info", f"{len(spilled)} result(s) were reduced or spilled to a handle")
        )

    unlabeled = [c for c in shell if c.background and not c.purpose]
    if unlabeled:
        found.append(
            Finding("info", f"{len(unlabeled)} background job(s) started without a purpose")
        )
    if not found:
        found.append(Finding("ok", "no shell-usage issues found"))
    return found


def _cd_into(command: str, workspace: str) -> bool:
    match = _CD_PREFIX.match(command.strip())
    if match is None or not workspace:
        return False
    target = match["dir"].strip("'\"")
    return target == workspace or target.rstrip("/") == workspace.rstrip("/")


def render(report: Report, *, verbose: bool = False) -> str:
    shell = report.of("shell")
    background = [c for c in shell if c.background]
    handles = [c for c in report.calls if c.running]
    failed = [c for c in shell if c.failed]
    elapsed = [c.elapsed for c in shell if c.elapsed is not None]
    lines = [
        f"session {report.session[:8]}  {report.model}  updated {report.updated}",
        f"  shell={len(shell)}  background={len(background)}  failed={len(failed)}  "
        f"handles returned={len(handles)}  notices={report.notices}",
        "  "
        + "  ".join(f"{tool}={len(report.of(tool))}" for tool in JOB_TOOLS[1:])
        + f"  result chars={sum(len(c.result) for c in report.calls):,}",
    ]
    if elapsed:
        lines.append(
            f"  wall time={sum(elapsed):.0f}s  longest={max(elapsed):.0f}s  "
            f"over 10s={sum(1 for e in elapsed if e >= 10)}"
        )
    if verbose:
        lines.append("   job    tool           elapsed  status   command")
        for call in report.calls:
            status = (
                "running"
                if call.running
                else call.outcome
                if call.outcome != "exit"
                else f"exit {call.exit_code}"
            )
            duration = "" if call.elapsed is None else f"{call.elapsed:.1f}s"
            flags = ("bg " if call.background else "") + (
                f"[{call.purpose}] " if call.purpose else ""
            )
            command = " ".join(command_text(redact(call.command)).split())
            lines.append(
                f"  {call.job:>5}  {call.tool:<14} {duration:>7}  {status:<8} {flags}{command[:90]}"
            )
    lines.extend(f"  [{finding.level}] {finding.message}" for finding in report.findings)
    return "\n".join(lines)


def _directories(selector: str, limit: int, root: Path | None) -> list[Path]:
    root = root or session_root()
    if selector != "all":
        return [resolve_session(selector, root)]
    return [root / info.id for info in list_sessions(root)[:limit]]


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "session",
        nargs="?",
        default="latest",
        help="session id prefix, 'latest' (default), or 'all'",
    )
    parser.add_argument("-n", "--limit", type=int, default=5, help="sessions to read for 'all'")
    parser.add_argument("-v", "--verbose", action="store_true", help="list every call")
    parser.add_argument("--root", type=Path, default=None, help="session directory root")
    arguments = parser.parse_args(argv)

    try:
        directories = _directories(arguments.session, arguments.limit, arguments.root)
    except SessionError as error:
        print(error, file=sys.stderr)
        return 2
    if not directories:
        print("No saved sessions found.", file=sys.stderr)
        return 2

    for directory in directories:
        try:
            report = analyze(directory)
        except SessionError as error:
            print(f"session {directory.name[:8]}: {error}", file=sys.stderr)
            continue
        print(render(report, verbose=arguments.verbose))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
