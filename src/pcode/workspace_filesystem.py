"""Unconfined file paths with a stable workspace-relative base.

Harness resolves relative paths from the workspace's working directory and
reports walked paths relative to it; a `root_dir` of `/` lifts the boundary,
so the workspace stays the base without being a sandbox. See
dev/dependencies.md before upgrading the filesystem integration.
"""

from dataclasses import fields
from pathlib import Path

from pydantic_ai_harness.filesystem import FileSystem, FileSystemToolset


def _any_depth(pattern: str) -> str:
    """Match a protected-file pattern at any depth, since the root is `/`."""
    return pattern if pattern.startswith("**/") else f"**/{pattern}"


class WorkspaceFileSystem(FileSystem):
    """Keep workspace-relative paths without using the workspace as a boundary."""

    def __post_init__(self) -> None:
        super().__post_init__()
        # Workspace paths are POSIX, so `/` is the whole filesystem on any host.
        self.root_dir = "/"
        # Protected-file rules still apply, including to paths outside the workspace.
        self.read_only_patterns = [_any_depth(pattern) for pattern in self.read_only_patterns]

    @classmethod
    def from_filesystem(cls, filesystem: FileSystem) -> "WorkspaceFileSystem":
        values = {f.name: getattr(filesystem, f.name) for f in fields(FileSystem) if f.init}
        # Deprecated upstream aliases; passing them only re-triggers the warning.
        values.pop("cwd", None)
        values.pop("protected_patterns", None)
        return cls(**values)

    def get_instructions(self):
        async def instructions(ctx) -> str:
            base = await ctx.workspace.working_dir()
            return (
                f"File tools accept absolute paths anywhere on the host and relative paths "
                f"based on the workspace {base!r}, including '..'. "
                "Shell cd does not change that file-tool base. File tools are not a sandbox. "
                "Search and listing tools default to the workspace. "
                "Returned relative paths use that same workspace base. "
                "Existing protected-file write rules still apply to file tools, not shell commands."
                " read_file shows an image (PNG, JPEG, GIF, WebP) as the picture itself when "
                "the model accepts images, so look at one rather than guessing from its name."
            )

        return instructions

    def _toolset_type(self) -> type[FileSystemToolset]:
        return FileSystemToolset

    def _file_system_toolset(self) -> FileSystemToolset:
        if self._toolset is None:
            self._toolset = self._toolset_type()(
                root_dir=Path(self.root_dir),
                allowed_patterns=self.allowed_patterns,
                denied_patterns=self.denied_patterns,
                read_only_patterns=self.read_only_patterns,
                max_read_lines=self.max_read_lines,
                max_read_chars=self.max_read_chars,
                max_list_results=self.max_list_results,
                max_search_results=self.max_search_results,
                max_find_results=self.max_find_results,
                id=self.id or "file_system",
                content_hashes=self.content_hashes,
                tools=self.tools,
                max_retries=self.max_retries,
            )
        return self._toolset
