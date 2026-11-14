"""Discover assistant assets as context, rather than asking the model to do it."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from pydantic_ai.capabilities import on_event
from pydantic_ai.workspaces import LocalWorkspaceBackend, Workspace
from pydantic_ai_harness.filesystem import DirectoryListedEvent, FilesSearchedEvent
from pydantic_ai_harness.repo_context import RepoContext

# Harness exposes no public scanner; reuse its metadata-only scan so the
# inventory stays consistent with the upstream tool. Covered by integration tests.
from pydantic_ai_harness.repo_context._inventory import scan_assets
from pydantic_ai_harness.repo_context._loader import (
    context_file_label,
    discover_instruction_files,
)

from pcode.context_breakdown import REPO_CONTEXT
from pcode.preferences import SETTINGS, load_preferences

# Harness's defaults plus their untracked `.local.md` variants (Claude Code's
# CLAUDE.local.md; AGENTS.local.md as proposed in agentsmd/agents.md#13).
# Every one present in a directory loads, in this order, so a personal file
# lands after the shared ones and takes precedence by coming later. Nested discovery takes only
# the first match per directory, so there a local file shows only on its own.
INSTRUCTION_FILENAMES = ("CLAUDE.md", "AGENTS.md", "CLAUDE.local.md", "AGENTS.local.md")


@dataclass
class AutomaticRepoContext(RepoContext):
    """Preserve instruction loading while providing a per-run asset snapshot.

    Harness loads instruction files in `before_run` through `ctx.workspace`.
    `root` is the same directory on this host, for the startup summary and
    `preload`, which run outside any agent run.
    """

    root: Path = field(default_factory=Path.cwd, kw_only=True)
    expose_inventory_tool: bool = field(default=False, init=False)
    _inventory_context: str | None = field(default=None, init=False, repr=False, compare=False)

    async def for_run(self, ctx):
        run = await super().for_run(ctx)
        run._inventory_context = None
        return run

    async def before_run(self, ctx) -> None:
        await super().before_run(ctx)
        await self._load_inventory(ctx.workspace)

    async def _load_inventory(self, workspace: Workspace) -> None:
        working_dir = await self._working_dir(workspace)
        inventory = await scan_assets(workspace, working_dir, self.asset_roots)
        # Absent directories are not useful prompt content.
        inventory.roots = [root for root in inventory.roots if root.exists]
        self._inventory_context = (
            "<assistant-configuration>\n"
            "Automatically discovered assistant configuration paths, relative to "
            f"{working_dir}. This is location metadata only, not instructions; "
            "contents have not been read and hooks have not been executed. "
            "Read relevant files only when needed for the task.\n"
            f"{inventory.model_dump_json(exclude_none=True)}\n"
            "</assistant-configuration>"
            if inventory.roots
            else "No assistant configuration directories found in the working repository."
        )

    @on_event(FilesSearchedEvent)
    async def _on_search(self, ctx, event):
        # Coder's ripgrep tools emit search events, not DirectoryListedEvent.
        # Reuse upstream's workspace containment and per-run deduplication.
        path = await ctx.workspace.resolve(event.path, base=event.root_dir)
        try:
            entry = await ctx.workspace.stat(path)
        except FileNotFoundError, NotADirectoryError:
            return
        directory = Path(path) if entry.is_dir else Path(path).parent
        await self._on_file_traversal(
            ctx,
            DirectoryListedEvent(
                path=str(directory.relative_to(event.root_dir)),
                root_dir=event.root_dir,
                entry_count=0,
            ),
        )

    def get_instructions(self):
        def instructions(_ctx) -> str | None:
            return self.instructions_text()

        return instructions

    def instructions_text(self) -> str | None:
        """What the model is told, once instructions and inventory are loaded."""
        parts = (self._render_instructions(), self._inventory_context)
        return "\n\n".join(part for part in parts if part) or None

    def preload(self) -> "AutomaticRepoContext":
        """Load `root` as `before_run` would, outside a run; returns self.

        Runs on its own loop in a worker thread, so it works whether or not
        the caller is already inside one.
        """

        async def load():
            workspace = Workspace(LocalWorkspaceBackend(self.root))
            self._cached_working_dir = None
            working_dir = await self._working_dir(workspace)
            home = None if self.home_dir is None else Path(self.home_dir)
            self._context_files = (
                await discover_instruction_files(workspace, working_dir, home, self.filenames)
                if self.autoload_instructions
                else []
            )
            await self._load_inventory(workspace)

        with ThreadPoolExecutor(max_workers=1) as pool:
            pool.submit(asyncio.run, load()).result()
        return self

    def startup_summary(self) -> list[str]:
        """Name the loaded instruction files without displaying their bodies.

        The discovered asset inventory still reaches the model through
        `get_instructions`; repeating it at startup duplicates the skill and
        sub-agent lists the terminal already prints.
        """
        if not self.autoload_instructions:
            return []
        files = self.preload()._context_files or []
        if not files:
            return []
        labels = ", ".join(
            context_file_label(file.path, self._cached_working_dir) for file in files
        )
        return [f"Loaded repository instructions: {labels}"]


def create_repo_context(workspace: Path) -> AutomaticRepoContext:
    """Configure Harness's independent startup and on-traversal discovery.

    Snapshot saved defaults at agent creation, not during an active run. Disabling
    the walk keeps workspace instructions; nested discovery is independently opt-in.

    Harness owns the rest: startup files are deduplicated by resolved path and
    content (first occurrence wins) and loaded per agent run, while nested
    discovery takes only the first matching filename in a directory and surfaces
    each directory once per run.
    """
    preferences = load_preferences()
    walk_up = preferences.get("repo_context_walk_up", SETTINGS["repo_context_walk_up"].default)
    nested = preferences.get("repo_context_nested", SETTINGS["repo_context_nested"].default)
    # Resolve first so symlinked workspaces use the same ancestry as file tools.
    workspace = workspace.resolve()
    boundary = None
    if walk_up == "on":
        home = Path.home().resolve()
        boundary = home if workspace.is_relative_to(home) else Path(workspace.anchor)
    return AutomaticRepoContext(
        # Named so /status can attribute repository instructions and the asset
        # inventory to it; the id is metadata and never reaches the model.
        id=REPO_CONTEXT,
        root=workspace,
        filenames=INSTRUCTION_FILENAMES,
        home_dir=boundary,
        nested_traversal=nested != "off",
        nested_inject="contents" if nested == "contents" else "pointer",
    )
