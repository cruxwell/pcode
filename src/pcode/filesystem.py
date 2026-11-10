"""Mutation evidence layered on Harness's filesystem operations.

Harness announces each write and edit through `_request` while it holds the
file's lock and before it writes, so the before snapshot is taken there, inside
the operation rather than from a later reread. The after side is what the write
put there: the written content, or the same replacements applied to the snapshot.

A `grep` too large to return inline comes back as match counts per file instead
of a spilled transcript; see `grep_summary`.

`read_file` returns an image as the file only within what every provider
accepts; see `viewable`.
"""

import posixpath
import re
from collections import Counter
from contextvars import ContextVar
from dataclasses import dataclass, field

from pydantic_ai import CapabilityEvent
from pydantic_ai.messages import BinaryContent, BinaryImage
from pydantic_ai_harness.filesystem import FileSystemToolset

# Private, but the edit preview must apply replacements exactly as the edit did.
from pydantic_ai_harness.filesystem._content import apply_replacements

from pcode.edits import MAX_SOURCE, completed_change
from pcode.images import unsendable
from pcode.runtime import EditCompleted
from pcode.tool_output_limits import inline_result_limit
from pcode.workspace_filesystem import WorkspaceFileSystem

# Matching lines a grep needs before an oversized result is summarized. Fewer
# are left to the output limits: they are long lines or wide context, which
# narrowing cannot shrink, and their line numbers matter more than counts.
GREP_SUMMARY_MIN_MATCHES = 50
# Files a grep summary names; the rest are folded into one line.
GREP_SUMMARY_FILES = 50
# Harness's note on paths a search could not read, kept under a summary.
_UNREADABLE_NOTE = re.compile(r"^\[\d+ paths? could not be read", re.MULTILINE)
_TRUNCATED_NOTE = re.compile(r"^\[\.\.\. truncated at \d+ lines\]$", re.MULTILINE)

# The (path, is_match) of each line one grep call kept, in output order.
_grep_hits: ContextVar[list[tuple[str, bool]] | None] = ContextVar("pcode_grep_hits", default=None)


def grep_summary(
    hits: list[tuple[str, bool]], *, complete: bool, limit: int, context: int, notes: list[str]
) -> str:
    """Match counts per file, in place of a result too long to show inline.

    Shown inline rather than spilled: an agent pages a spilled listing hit by
    hit, and counts plus a way to narrow get it to the right file sooner (the
    SWE-agent ablations, arXiv 2405.15793).
    """
    counts = Counter(path for path, is_match in hits if is_match)
    total = sum(counts.values())
    lines_word = "line" if total == 1 else "lines"
    files_word = "file" if len(counts) == 1 else "files"
    found = f"{total:,} matching {lines_word} in {len(counts):,} {files_word}"
    if not complete:
        found = f"At least {found} (the search stopped after {len(hits):,} lines, in path order)"
    head = (
        f"{found}: too many to show within the {limit:,}-character result limit. "
        "Matches per file, most first:"
    )
    narrow = "a more specific pattern, a narrower `path`, a `glob` or `file_type`"
    if context:
        narrow += ", a smaller `context`"
    tail = [f"Narrow the search and grep again: {narrow}, or one of the files above.", *notes]
    ranked = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    width = len(f"{ranked[0][1]:,}")
    # Stay under the limit, or the summary would be spilled in turn; the
    # allowance covers the line that folds in the rest.
    room = limit - len(head) - sum(len(line) + 1 for line in tail) - 80
    rows: list[str] = []
    for path, count in ranked[:GREP_SUMMARY_FILES]:
        row = f"{count:>{width},}  {path}"
        room -= len(row) + 1
        if room < 0:
            break
        rows.append(row)
    if rest := ranked[len(rows) :]:
        rows.append(f"… {len(rest):,} more files with {sum(n for _, n in rest):,} matches")
    return "\n".join([head, *rows, *tail])


# Harness's whole note, after its header line, for an image or PDF over its cap.
HARNESS_TOO_LARGE = re.compile(r"\]\nToo large to view: the limit is \d+ bytes\.\n\Z")


def too_large(why: str) -> str:
    """The text in place of an image no provider would take; it says how to see it anyway."""
    return f"Too large to view: {why}. Make a smaller copy (shell) and read that.\n"


@dataclass(kw_only=True)
class FileChangeEvent(CapabilityEvent, namespace="pcode_files", name="change"):
    change: EditCompleted


@dataclass
class _Snapshot:
    """What `_request` saw of one write or edit, for the event sent after it."""

    display_path: str = ""
    before: str | None = None
    existed: bool = True
    omitted: str = ""
    taken: bool = field(default=False)


# One per tool call: concurrent calls run in separate tasks with their own copy.
_snapshot: ContextVar[_Snapshot | None] = ContextVar("pcode_file_snapshot", default=None)


class DisplayFileSystem(WorkspaceFileSystem):
    def _toolset_type(self):
        return DisplayFileSystemToolset


class DisplayFileSystemToolset(FileSystemToolset):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Snapshotted like the output limits themselves: settings apply on restart.
        self._inline_limit = inline_result_limit()

    async def _grep(self, scope, ctx, pattern, **kwargs):
        hits: list[tuple[str, bool]] = []
        token = _grep_hits.set(hits)
        try:
            result = await super()._grep(scope, ctx, pattern, **kwargs)
        finally:
            _grep_hits.reset(token)
        limit = self._inline_limit
        if limit is None or not isinstance(result, str) or len(result) < limit:
            return result
        # One record past the cap is accepted to detect it, then dropped.
        hits = hits[: self._max_search_results]
        # No hits means the search ran without ripgrep's records (a workspace
        # that cannot run commands); like a few long matches, it is left to the
        # output limits.
        if sum(is_match for _, is_match in hits) <= GREP_SUMMARY_MIN_MATCHES:
            return result
        return grep_summary(
            hits,
            # Harness marks both its line cap and ripgrep's output byte cap.
            complete=not _TRUNCATED_NOTE.search(result),
            limit=limit,
            context=kwargs.get("context", 0),
            notes=[line for line in result.splitlines() if _UNREADABLE_NOTE.match(line)],
        )

    def _match_line(self, scope, cwd, record, *, include_hidden=False):
        line = super()._match_line(scope, cwd, record, include_hidden=include_hidden)
        hits = _grep_hits.get()
        if line is not None and hits is not None:
            # Rebuilt as `<entry><sep><digits><sep><text>`; `:` marks a match, `-` context.
            digits = len(record.text) - len(record.text.lstrip("0123456789"))
            hits.append(
                (line[: len(line) - len(record.text) - 1], record.text[digits : digits + 1] == ":")
            )
        return line

    async def _emit_change(self, ctx, path, before, after, **kwargs):
        change = completed_change(path, before, after, call_id=ctx.tool_call_id or "", **kwargs)
        await ctx.emit(FileChangeEvent(change=change))

    @staticmethod
    def _display_path(scope, resolved: str) -> str:
        """Workspace-relative inside the workspace, absolute outside it."""
        relative = posixpath.relpath(resolved, scope.bounds.cwd)
        return resolved if relative == ".." or relative.startswith("../") else relative

    async def _request(self, scope, ctx, change, *, path, resolved):
        snapshot = _snapshot.get()
        if ctx is not None and snapshot is not None:
            await self._take(snapshot, scope, resolved)
        return await super()._request(scope, ctx, change, path=path, resolved=resolved)

    async def _take(self, snapshot: _Snapshot, scope, resolved: str) -> None:
        snapshot.taken = True
        # Harness writes through the path as written; name the file it reaches.
        try:
            real = await scope.workspace.realpath(resolved)
        except OSError:
            real = resolved
        snapshot.display_path = self._display_path(scope, real)
        try:
            raw = await scope.workspace.read_bytes(real)
        except FileNotFoundError:
            snapshot.before, snapshot.existed = "", False
            return
        except PermissionError:
            # Display evidence must not require permissions the write itself
            # doesn't need; leave the diff out instead.
            snapshot.omitted = "Before snapshot unavailable"
            return
        if len(raw) > MAX_SOURCE * 4:
            snapshot.omitted = "File exceeds preview size limit"
        elif b"\x00" in raw[:8192]:  # Harness's own binary check
            snapshot.omitted = "Binary content"
        else:
            try:
                snapshot.before = raw.decode("utf-8")
            except UnicodeDecodeError:
                snapshot.omitted = "Binary or non-UTF-8 content"

    async def _recorded(self, ctx, operation, after) -> str:
        """Run `operation`, then report the change it made, if it made one."""
        snapshot = _Snapshot()
        token = _snapshot.set(snapshot)
        try:
            result = await operation()
        finally:
            _snapshot.reset(token)
        succeeded = isinstance(result, str) and result.startswith(("Wrote ", "Edited "))
        if ctx is not None and snapshot.taken and succeeded:
            await self._emit_change(
                ctx,
                snapshot.display_path,
                snapshot.before,
                after(snapshot.before) if snapshot.before is not None else "",
                existed=snapshot.existed,
                omitted=snapshot.omitted,
            )
        return result

    async def _read_file(self, scope, ctx, path, *, offset=0, limit=None):
        result = await super()._read_file(scope, ctx, path, offset=offset, limit=limit)
        if isinstance(result, list):
            return viewable(result)
        if isinstance(result, str) and HARNESS_TOO_LARGE.search(result):
            # Harness's own note for a file over its 5 MB cap; say how to see it.
            return result.removesuffix("\n") + " Make a smaller copy (shell) and read that.\n"
        return result

    async def _write_file(self, scope, ctx, path, content, *, expected_hash=None):
        return await self._recorded(
            ctx,
            lambda: super(DisplayFileSystemToolset, self)._write_file(
                scope, ctx, path, content, expected_hash=expected_hash
            ),
            lambda before: content,
        )

    async def _edit_file(self, scope, ctx, path, replacements, *, expected_hash=None):
        return await self._recorded(
            ctx,
            lambda: super(DisplayFileSystemToolset, self)._edit_file(
                scope, ctx, path, replacements, expected_hash=expected_hash
            ),
            lambda before: apply_replacements(before, replacements, path),
        )


def viewable(result: list):
    """Harness's file content for `read_file`, held to what every provider accepts.

    Harness returns an image or PDF up to 5,000,000 bytes as a `BinaryContent`
    after its header. A larger or wider image than Anthropic takes is described
    instead (see `MAX_IMAGE_BYTES`), since history keeps it and every later
    request would fail. So is a PDF, which not every model reads. Whether the
    model can see a picture is `ImageGate`'s call on each request.
    """
    match result:
        case [str() as header, BinaryContent(data=raw, media_type=media_type)]:
            pass
        case _:
            return result
    if not media_type.startswith("image/"):
        return f"{header}{media_type} files are not shown; read their text another way.\n"
    if why := unsendable(raw):
        return header + too_large(why)
    # The type a saved history comes back as, so reopening a session finds it equal.
    return [header, BinaryImage(data=raw, media_type=media_type)]
