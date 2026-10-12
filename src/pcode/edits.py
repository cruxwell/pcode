"""Bounded, sanitized evidence of completed file mutations, not workspace diffs."""

import difflib
import os
import re

from pcode.runtime import EditCompleted
from pcode.tool_display import command_text, plain

MAX_SOURCE = 256 * 1024
MAX_LINES = 4000
MAX_PATCH = 64 * 1024
MAX_PATCH_LINES = 400


def edit_text(text: str) -> str:
    """Edit content as shown: credentials masked and terminal controls removed.

    Masks whole values before diff prefixes or clipping split them.
    """
    return command_text(text)


_HUNK = re.compile(r"@@ -\d+(?:,(\d+))? \+\d+(?:,(\d+))? @@")


def patch_text(patch: str, *, dedent: bool) -> str:
    """A stored patch as it is displayed: redacted, and dedented if asked."""
    text = edit_text(patch)
    return dedent_patch(text) if dedent else text


def dedent_patch(patch: str) -> str:
    """Strip the leading whitespace every line of a hunk shares.

    An edit deep in a nested block otherwise starts its whole diff several
    levels in. Each hunk is dedented on its own, by the whitespace common to
    all its context, added and removed lines; whitespace-only lines do not
    count, and lose only the shared part, so a whitespace change still shows.
    Hunks are walked by their header's line counts, so a removed `--- x` line
    is never mistaken for a file header, and everything outside a hunk is left
    alone; a hunk that does not parse as its header promises is left as it is.
    `edit_text` has already expanded tabs.
    """
    lines = patch.split("\n")
    index = 0
    while index < len(lines):
        match = _HUNK.match(lines[index])
        index += 1
        if not match:
            continue
        old, new = (int(count) if count is not None else 1 for count in match.groups())
        body = []
        malformed = False
        while index < len(lines) and (old > 0 or new > 0):
            marker = lines[index][:1]
            if marker not in (" ", "+", "-", "\\", ""):
                malformed = True
                break
            if marker != "\\":
                body.append(index)
                old -= marker != "+"
                new -= marker != "-"
            index += 1
        # A trailing "\ No newline" note belongs to the hunk just read.
        if index < len(lines) and lines[index].startswith("\\"):
            index += 1
        if not malformed:
            _dedent(lines, body)
    return "\n".join(lines)


def _dedent(lines: list[str], body: list[int]) -> None:
    contents = [lines[i][1:] for i in body]
    indents = [c[: len(c) - len(c.lstrip())] for c in contents if c.strip()]
    if not indents:
        return
    shared = os.path.commonprefix(indents)
    if not shared:
        return
    for i, content in zip(body, contents, strict=True):
        marker = lines[i][:1]
        kept = content.removeprefix(shared) if content.strip() else content[len(shared) :]
        lines[i] = marker + kept


def source_lines(text: str) -> list[str]:
    """Lines as git and diff count them, each keeping its newline.

    `str.splitlines` also breaks at form feeds, U+2028 and other separators
    that are ordinary characters inside a source line, adding lines the file
    does not have and throwing a hunk's line counts off.
    """
    parts = text.split("\n")
    return [part + "\n" for part in parts[:-1]] + ([parts[-1]] if parts[-1] else [])


def change_from_record(record: dict) -> EditCompleted:
    """Rebuild a saved change, ignoring journal fields the dataclass does not own."""
    return EditCompleted(
        **{key: value for key, value in record.items() if key in EditCompleted.__dataclass_fields__}
    )


def completed_change(path, before, after, *, existed=True, call_id="", omitted=""):
    """Build display evidence while raw snapshots are still local to execution."""
    operation = "edited" if existed else "created"
    path = plain(edit_text(path), limit=None)
    if omitted or before is None:
        return EditCompleted(
            call_id, path, operation, omitted=omitted or "Before snapshot unavailable"
        )
    if max(len(before), len(after)) > MAX_SOURCE:
        return EditCompleted(call_id, path, operation, omitted="File exceeds preview size limit")
    if "\x00" in before or "\x00" in after:
        return EditCompleted(call_id, path, operation, omitted="Binary content")
    if max(len(source_lines(before)), len(source_lines(after))) > MAX_LINES:
        return EditCompleted(call_id, path, operation, omitted="File exceeds preview line limit")
    if before == after:
        return EditCompleted(call_id, path, "unchanged" if existed else "created")
    # Count actual changed source lines, even where redaction hides a change.
    raw = list(difflib.unified_diff(source_lines(before), source_lines(after), n=3))
    added = sum(line.startswith("+") for line in raw[2:])
    removed = sum(line.startswith("-") for line in raw[2:])
    lines = []
    for line in difflib.unified_diff(
        source_lines(edit_text(before)),
        source_lines(edit_text(after)),
        fromfile=f"a/{path}" if existed else "/dev/null",
        tofile=f"b/{path}",
        n=3,
    ):
        lines.append(line.rstrip("\n"))
        if not line.endswith("\n"):
            lines.append(r"\ No newline at end of file")
    patch = "\n".join(lines[:MAX_PATCH_LINES])
    truncated = len(lines) > MAX_PATCH_LINES or len(patch) > MAX_PATCH
    patch = patch[:MAX_PATCH]
    if not patch:
        omitted = "Changes hidden by redaction or newline normalization"
    return EditCompleted(call_id, path, operation, patch, added, removed, truncated, omitted)
