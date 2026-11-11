#!/usr/bin/env python3
"""Report prompt-cache behavior from saved pcode sessions.

Every session already records the provider's own verdict for each request, so
past traffic is the cheapest regression signal available for caching work: no
credentials, no spend, and real tool loops rather than a synthetic script.

Output is content-free by construction. Prompts carry file contents and command
output, so this prints counts, digests, and token totals only -- never message
text. See dev/prompt-caching.md for what the numbers mean.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from hashlib import sha256
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from pcode.sessions import (  # noqa: E402
    SessionError,
    list_sessions,
    resolve_session,
    session_root,
    snapshot_messages_json,
)

PLAN_TAG = "<plan-reminder>"
# Request parts Pydantic AI hoists ahead of the rest when it merges requests.
TOOL_RESULT_KINDS = frozenset({"tool-return", "retry-prompt"})

# A prefix below the provider's minimum cacheable size is never stored, so a
# short session reports no reads without anything being wrong.
MIN_CACHEABLE_INPUT = 1024
# Reads land on block boundaries, so the previous request's total is an upper
# bound this can only approach. Below this share of it, the prefix moved.
REUSE_RATIO = 0.75
# A prefix that never advances while the conversation grows is the signature of
# a rewritten tail: reads pin to one value for request after request.
MAX_PINNED_READS = 8


@dataclass
class Request:
    """One model request, as the provider reported it."""

    index: int
    timestamp: str
    model: str
    read: int
    write: int
    uncached: int
    total_input: int
    output: int


@dataclass
class Finding:
    level: str
    message: str


@dataclass
class Report:
    session: str
    model: str
    updated: str
    requests: list[Request]
    reminders: list[str]
    prefix_rewrites: int
    delegated: list[Request] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)

    @property
    def totals(self) -> dict[str, int]:
        keys = ("read", "write", "uncached", "total_input", "output")
        return {key: sum(getattr(r, key) for r in self.requests) for key in keys}

    @property
    def read_share(self) -> float:
        totals = self.totals
        return totals["read"] / totals["total_input"] if totals["total_input"] else 0.0


def _responses(database: Path, *, delegated: bool | None = None) -> Iterator[dict]:
    """Yield each model response once, in order.

    Snapshots hold cumulative history, so the same response appears in many of
    them; `provider_response_id` deduplicates without reading any content.
    """
    seen: dict[object, dict] = {}
    for messages in _snapshots(database, delegated=delegated):
        for message in messages:
            if message.get("kind") == "response" and message.get("model_name"):
                key = message.get("provider_response_id") or (
                    message.get("run_id"),
                    message.get("timestamp"),
                )
                seen.setdefault(key, message)
    yield from sorted(seen.values(), key=lambda m: m.get("timestamp") or "")


def _snapshots(database: Path, *, delegated: bool | None = None) -> Iterator[list[dict]]:
    """Yield snapshot histories, optionally only parent or only delegated runs.

    Sub-agent runs share the session's store and are marked by `parent_run_id`.
    Their history is a different conversation, so mixing the two would report a
    prefix rewrite on every hand-off.
    """
    query = "SELECT seq, messages FROM snapshots"
    if delegated is not None:
        query += f" WHERE parent_run_id IS {'NOT NULL' if delegated else 'NULL'}"
    connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
    try:
        for seq, raw in connection.execute(f"{query} ORDER BY seq"):
            yield json.loads(snapshot_messages_json(connection, seq, raw))
    except sqlite3.DatabaseError as error:
        raise SessionError(f"Unreadable step store: {error}") from None
    finally:
        connection.close()


def _digest(value: object) -> str:
    """Fingerprint by shape and content hash, never by content."""
    return sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()[:12]


def _turns(messages: list[dict]) -> list[list[str]]:
    """Each turn as its kind followed by one digest per part re-sent to the provider.

    A settled message still gains bookkeeping fields after it is sent --
    `run_id`, `timestamp`, `instructions`, `conversation_id` -- so hashing the
    whole record reports a rewrite on every healthy session.

    Consecutive same-kind messages are one turn, tool results first: Pydantic AI
    normalizes a loaded history that way (`_merge_consecutive_messages`), so a
    stored tool return and the plan reminder after it become one request on the
    next turn without anything the provider sees changing.
    """
    turns: list[tuple[str, list[dict]]] = []
    for message in messages:
        parts = list(message.get("parts", []))
        if turns and turns[-1][0] == message.get("kind"):
            turns[-1][1].extend(parts)
        else:
            turns.append((message.get("kind") or "", parts))
    return [
        [
            kind,
            *(
                _digest([part.get("part_kind"), part.get("content")])
                for part in sorted(parts, key=lambda p: p.get("part_kind") not in TOOL_RESULT_KINDS)
            ),
        ]
        for kind, parts in turns
    ]


def _count_prefix_rewrites(database: Path, *, delegated: bool | None = None) -> int:
    """Count snapshots that changed settled history instead of appending to it.

    Append-only history is what keeps a provider's cached prefix reusable, and
    it is the property pcode's plan reminders are built to preserve. Parts are
    compared in sequence, as a prefix cache matches blocks: a part appended to
    the end of a settled turn (a note sent after a tool result) extends the
    prefix rather than replacing it.

    The last two turns are excluded rather than one: the trailing turn is still
    being assembled when a snapshot is written, and the response before it can
    be discarded by a retry or interruption. Compaction and branch switches
    legitimately replace history, so callers report this as a warning.
    """
    rewrites = 0
    previous: list[list[str]] = []
    for messages in _snapshots(database, delegated=delegated):
        turns = _turns(messages)
        current = [token for turn in turns for token in turn]
        settled = [token for turn in previous[:-2] for token in turn]
        if settled and len(current) >= len(settled) and current[: len(settled)] != settled:
            rewrites += 1
        previous = turns
    return rewrites


def _plan_reminders(database: Path) -> list[str]:
    """Digest each plan reminder in the final history, so duplicates show up."""
    reminders: list[str] = []
    for messages in _snapshots(database):
        reminders = [
            _digest(content)
            for message in messages
            for part in message.get("parts", [])
            if part.get("part_kind") == "user-prompt"
            and isinstance(content := part.get("content"), str)
            and content.startswith(PLAN_TAG)
        ]
    return reminders


def _requests(database: Path, *, delegated: bool | None = None) -> list[Request]:
    requests = []
    for index, message in enumerate(_responses(database, delegated=delegated)):
        usage = message.get("usage") or {}
        details = usage.get("details") or {}
        requests.append(
            Request(
                index=index,
                timestamp=(message.get("timestamp") or "")[11:19],
                model=message.get("model_name") or "",
                read=usage.get("cache_read_tokens", 0),
                write=usage.get("cache_write_tokens", 0),
                uncached=details.get("input_tokens", 0),
                total_input=usage.get("input_tokens", 0),
                output=usage.get("output_tokens", 0),
            )
        )
    return requests


def _pinned_streak(requests: Sequence[Request]) -> tuple[int, int]:
    """Return the longest run of identical non-zero reads, and that read value."""
    best_length, best_value, length = 0, 0, 0
    for previous, current in zip(requests, requests[1:]):
        length = length + 1 if current.read and current.read == previous.read else 0
        if length > best_length:
            best_length, best_value = length, current.read
    return best_length + 1 if best_length else 0, best_value


def _reuse_findings(requests: Sequence[Request]) -> list[Finding]:
    eligible = [
        (previous, current)
        for previous, current in zip(requests, requests[1:])
        if previous.total_input >= MIN_CACHEABLE_INPUT and current.model == previous.model
    ]
    if not eligible:
        return [
            Finding(
                "info",
                f"No request followed a same-model prefix of at least {MIN_CACHEABLE_INPUT} "
                "tokens, so the provider had nothing it was obliged to cache.",
            )
        ]
    reused = sum(
        1 for previous, current in eligible if current.read >= previous.total_input * REUSE_RATIO
    )
    share = reused / len(eligible)
    level = "ok" if share >= 0.9 else "warn" if share >= 0.5 else "fail"
    return [
        Finding(
            level,
            f"{reused}/{len(eligible)} eligible requests reused at least "
            f"{REUSE_RATIO:.0%} of the previous request's input.",
        )
    ]


def analyze(directory: Path) -> Report:
    try:
        info = json.loads((directory / "session.json").read_text())
    except OSError, ValueError:
        raise SessionError("Unreadable session metadata.") from None
    database = directory / "steps.sqlite3"
    if not database.exists():
        raise SessionError("No step store in this session.")
    # Delegated runs share the store but are a separate conversation: scoring
    # them together would report a prefix rewrite at every hand-off, and a
    # sub-agent's caching is worth judging on its own.
    delegated = _requests(database, delegated=True)
    requests = _requests(database, delegated=False)
    reminders = _plan_reminders(database)
    report = Report(
        session=info.get("id", directory.name),
        model=info.get("model", "?"),
        updated=info.get("updated", "")[:19],
        requests=requests,
        reminders=reminders,
        prefix_rewrites=_count_prefix_rewrites(database, delegated=False),
        delegated=delegated,
    )
    if not requests:
        report.findings.append(Finding("info", "No model requests recorded."))
        return report

    report.findings.extend(_reuse_findings(requests))

    streak, value = _pinned_streak(requests)
    if streak > MAX_PINNED_READS:
        report.findings.append(
            Finding(
                "fail",
                f"Reads pinned at {value:,} tokens for {streak} consecutive requests while the "
                "conversation grew: the tail was rewritten instead of reused.",
            )
        )
    if report.prefix_rewrites:
        report.findings.append(
            Finding(
                "warn",
                f"{report.prefix_rewrites} snapshot(s) replaced settled history instead of "
                "appending to it. Compaction, retries and branch switches do this legitimately; "
                "anything else invalidates the cached prefix from that point.",
            )
        )
    duplicates = len(reminders) - len(set(reminders))
    if duplicates:
        report.findings.append(
            Finding(
                "warn",
                f"{duplicates} duplicate plan reminder(s) of {len(reminders)} in the final "
                "history: deduplication is not matching previously sent text.",
            )
        )
    if delegated:
        # A sub-agent inherits the parent's model but not its settings, so this
        # is the check that a delegated run is asking for caching at all.
        report.findings.extend(
            Finding(finding.level, f"delegated: {finding.message}")
            for finding in _reuse_findings(delegated)
        )
    return report


def render(report: Report, *, verbose: bool = False) -> str:
    totals = report.totals
    lines = [
        f"session {report.session[:8]}  {report.model}  updated {report.updated}",
        f"  requests={len(report.requests)}  input={totals['total_input']:,}  "
        f"read={totals['read']:,}  write={totals['write']:,}  out={totals['output']:,}  "
        f"read share={report.read_share:.1%}",
    ]
    if report.delegated:
        child = Report("", "", "", report.delegated, [], 0)
        child_totals = child.totals
        lines.append(
            f"  delegated: requests={len(report.delegated)}  "
            f"input={child_totals['total_input']:,}  read={child_totals['read']:,}  "
            f"write={child_totals['write']:,}  read share={child.read_share:.1%}"
        )
    if report.reminders:
        lines.append(
            f"  plan reminders={len(report.reminders)} distinct={len(set(report.reminders))}"
        )
    if verbose and report.requests:
        lines.append("   idx  time           read        write     uncached     total in")
        for request in report.requests:
            lines.append(
                f"  {request.index:>4}  {request.timestamp:<8} {request.read:>11,} "
                f"{request.write:>12,} {request.uncached:>12,} {request.total_input:>12,}"
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
    parser.add_argument("-v", "--verbose", action="store_true", help="print per-request tokens")
    parser.add_argument(
        "--check",
        action="store_true",
        help="exit non-zero when a session shows a caching regression",
    )
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

    regressed = False
    for directory in directories:
        try:
            report = analyze(directory)
        except SessionError as error:
            print(f"session {directory.name[:8]}: {error}", file=sys.stderr)
            continue
        print(render(report, verbose=arguments.verbose))
        regressed = regressed or any(finding.level == "fail" for finding in report.findings)
    return 1 if regressed and arguments.check else 0


if __name__ == "__main__":
    raise SystemExit(main())
