"""Mutation evidence layered on Harness's filesystem operations.

Harness announces each write and edit through `_request` while it holds the
file's lock and before it writes, so the before snapshot is taken there, inside
the operation rather than from a later reread. The after side is what the write
put there: the written content, or the same replacements applied to the snapshot.

It also lets `read_file` show an image. Harness 0.56 answers every binary file
with a note; its next release returns a PNG, JPEG, GIF or WebP as the file
(pydantic/pydantic-ai#9596), and `_viewed` steps aside for that, so delete it
when the pin moves past it. Whether the model can see the picture is not decided
here: `pcode.images.ImageGate` does that on each request.
"""

import posixpath
from contextvars import ContextVar
from dataclasses import dataclass, field

from pydantic_ai import CapabilityEvent
from pydantic_ai.messages import BinaryImage
from pydantic_ai_harness.filesystem._toolset import (
    FileSystemToolset,
    _apply_replacements,
    _is_binary,
)

from pcode.edits import MAX_SOURCE, completed_change
from pcode.images import (
    IMAGE_SUFFIXES,
    MAX_IMAGE_BYTES,
    MAX_IMAGE_SIDE,
    image_pixels,
    image_type,
)
from pcode.runtime import EditCompleted
from pcode.workspace_filesystem import WorkspaceFileSystem

# What Harness 0.56 returns for a file its null-byte check calls binary.
BINARY_NOTE = "[Binary file: "


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
    async def _emit_change(self, ctx, path, before, after, **kwargs):
        change = completed_change(path, before, after, call_id=ctx.tool_call_id or "", **kwargs)
        await ctx.emit(FileChangeEvent(change=change))

    @staticmethod
    def _display_path(scope, resolved: str) -> str:
        """Workspace-relative inside the workspace, absolute outside it."""
        relative = posixpath.relpath(resolved, scope.cwd)
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
        elif _is_binary(raw):
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
        # A newer Harness returns a list for an image; this only fills the gap
        # while it answers with text. A name alone catches a GIF the null-byte
        # check took for text, a note catches an image with another name.
        if isinstance(result, str) and (
            result.startswith(BINARY_NOTE) or posixpath.splitext(path)[1].lower() in IMAGE_SUFFIXES
        ):
            return await self._viewed(scope, path, result)
        return result

    async def _viewed(self, scope, path, text: str):
        """The image at `path` as the file, or `text` when it is not one.

        The file is read again: Harness has already judged it and kept only its
        verdict. One over the limit is not, so a huge file named like an image
        costs no second read. A file that vanishes in between is whatever `text`
        said.
        """
        try:
            resolved = await self._safe_resolve(scope, path)
            size = (await scope.workspace.stat(resolved)).size
            if size is not None and size > MAX_IMAGE_BYTES:
                if posixpath.splitext(path)[1].lower() not in IMAGE_SUFFIXES:
                    return text
                return (
                    f"[{path} | {size} bytes]\n{too_large(f'the limit is {MAX_IMAGE_BYTES} bytes')}"
                )
            raw = await scope.workspace.read_bytes(resolved)
        except OSError:
            return text
        media_type = image_type(raw)
        if media_type is None:
            return text
        header = f"[{path} | {media_type} | {len(raw)} bytes]\n"
        if len(raw) > MAX_IMAGE_BYTES:
            return header + too_large(f"the limit is {MAX_IMAGE_BYTES} bytes")
        pixels = image_pixels(raw)
        if pixels and max(pixels) > MAX_IMAGE_SIDE:
            width, height = pixels
            return header + too_large(
                f"{width}x{height} pixels, and the limit is {MAX_IMAGE_SIDE} on a side"
            )
        # The type a saved history comes back as, so reopening a session finds it equal.
        return [header, BinaryImage(data=raw, media_type=media_type)]

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
            lambda before: _apply_replacements(before, replacements, path),
        )
