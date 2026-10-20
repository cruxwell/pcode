"""Workspace file references: type `@` in the prompt to insert a relative path.

Completing `@ui.py` inserts `./src/pcode/ui.py`, the same workspace-relative
form the file tools accept, so the model can read or search it without guessing
where the file lives. The path is all pcode sends; whether the file is worth
reading is the model's call. An image is the exception: `pcode.prompt_images`
sends the picture of any image file a prompt names, however the path got there.
"""

import os
import re
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from time import monotonic

from prompt_toolkit.completion import CompleteEvent, Completer, Completion
from prompt_toolkit.document import Document
from prompt_toolkit.lexers import Lexer

# Directories that never hold source worth referencing, used only when the
# workspace is not a Git checkout (Git supplies its own ignore rules).
# Ripgrep skips the hidden ones on its own; the rest it would happily list.
IGNORED_DIRECTORIES = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".tox",
        ".venv",
        "__pycache__",
        "build",
        "dist",
        "node_modules",
        "venv",
    }
)
MAX_FILES = 20000
# Paths read from a listing before the menu's own cap applies; see _ripgrep_files.
LISTING_CEILING = MAX_FILES * 10
MAX_COMPLETIONS = 50
CACHE_SECONDS = 10.0

# A reference is a whitespace-delimited token: an unfinished `@fragment`, a
# `./` or `../` path, or a quoted path whose name contains spaces.
REFERENCE_PATTERN = re.compile(r'(?<!\S)(?:"\.{1,2}/[^"\n]+"|@\S*|\.{1,2}/\S+)')


def _git_files(root: Path) -> list[str] | None:
    """Tracked and untracked files below root, or None outside a Git checkout."""
    try:
        result = subprocess.run(
            ["git", "-C", str(root), "ls-files", "--cached", "--others", "--exclude-standard"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except OSError, subprocess.SubprocessError:
        return None
    if result.returncode != 0:
        return None
    # A file staged for deletion is still cached; listing it would complete to a
    # path that no longer exists, so drop duplicates and keep insertion order.
    return list(dict.fromkeys(result.stdout.splitlines()))


def _ripgrep_files(root: Path) -> list[str] | None:
    """Files below root via ripgrep, or None when `rg` cannot be run.

    Used where there is no Git listing to borrow. Ripgrep walks in parallel and
    skips hidden entries itself, which beats `os.walk` several times over on a
    large tree; completion runs on the UI event loop, so that time is a visible
    stall between keystrokes.

    Ripgrep's default `.gitignore` handling is left alone: outside a checkout it
    reads those files only under `--no-require-git`, and tool-managed trees such
    as a uv cache or a virtualenv carry a `.gitignore` of `*` that would leave
    the menu empty. `.ignore` files still apply, as they do everywhere.

    Output is read as it arrives and the process is stopped at LISTING_CEILING,
    which bounds an unreasonable tree without truncating a merely large one:
    ripgrep's parallel walk emits in a different order each run, so a cut below
    the caller's own cap would reshuffle the menu between refreshes.
    """
    excluded = ",".join(sorted(IGNORED_DIRECTORIES))
    arguments = ["rg", "--files", "--glob", f"!{{{excluded}}}"]
    paths: list[str] = []
    try:
        with subprocess.Popen(
            arguments, cwd=root, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True
        ) as process:
            for line in process.stdout:
                paths.append(line.rstrip("\n"))
                if len(paths) >= LISTING_CEILING:
                    process.terminate()
                    break
            # A stopped run is killed mid-write, so only judge a complete one.
            if process.wait() not in (0, 1) and len(paths) < LISTING_CEILING:
                return None
    except OSError, subprocess.SubprocessError:
        return None
    # Ripgrep's parallel walk emits in thread completion order; sorting restores
    # the stable menu `os.walk` gave, and costs far less than `--sort path`,
    # which would make ripgrep search serially.
    return sorted(paths)


def _walked_files(root: Path) -> list[str]:
    """Files below root in pure Python, for a machine without ripgrep."""
    paths: list[str] = []
    for directory, subdirectories, names in os.walk(root):
        subdirectories[:] = sorted(
            name
            for name in subdirectories
            if name not in IGNORED_DIRECTORIES and not name.startswith(".")
        )
        base = Path(directory)
        for name in sorted(names):
            if name.startswith("."):
                continue
            paths.append(str((base / name).relative_to(root)))
            if len(paths) >= MAX_FILES:
                return paths
    return paths


def human_size(size: int) -> str:
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"
    return f"{size / (1024 * 1024):.1f} MB"


@dataclass
class WorkspaceFiles:
    """Workspace-relative file paths, refreshed at most every CACHE_SECONDS.

    Completion runs on every keystroke, so the listing is cached rather than
    re-read; new files show up within the refresh window.
    """

    root: Path
    cache_seconds: float = CACHE_SECONDS
    _paths: list[str] = field(default_factory=list)
    _loaded_at: float | None = None

    def paths(self) -> list[str]:
        now = monotonic()
        if self._loaded_at is None or now - self._loaded_at >= self.cache_seconds:
            # An empty listing falls through as a missing one: a workspace that
            # Git or ripgrep ignores wholesale (a gitignored directory, say) is
            # still worth walking rather than offering nothing.
            listed = _git_files(self.root) or _ripgrep_files(self.root)
            self._paths = (listed or _walked_files(self.root))[:MAX_FILES]
            self._loaded_at = now
        return self._paths

    def matches(self, fragment: str, limit: int = MAX_COMPLETIONS) -> list[str]:
        """Paths containing fragment, closest match first.

        A basename hit outranks a directory hit, and an earlier hit outranks a
        later one, so `@ui.py` offers `src/pcode/ui.py` before `tests/test_ui.py`.
        """
        needle = fragment.lstrip("@").removeprefix("./").lower()
        if not needle:
            # Shallowest first: the interesting top-level files, not build output.
            return sorted(self.paths(), key=lambda path: (path.count("/"), path))[:limit]
        ranked = []
        for path in self.paths():
            position = path.lower().find(needle)
            if position < 0:
                continue
            name_position = os.path.basename(path).lower().find(needle)
            ranked.append(((0, name_position) if name_position >= 0 else (1, position), path))
        ranked.sort(key=lambda item: (item[0], len(item[1]), item[1]))
        return [path for _, path in ranked[:limit]]

    def describe(self, path: str) -> str:
        """The file's size, so a reference's cost to read is visible before picking."""
        try:
            return human_size((self.root / path).stat().st_size)
        except OSError:
            return "file"


def reference_fragment(text: str) -> str | None:
    """The path fragment of a trailing `@` reference, or None when absent.

    `@` only starts a reference at a word boundary, so emails and decorators in
    pasted text never open the menu.
    """
    token = text.rsplit(maxsplit=1)[-1] if text.split() else ""
    if not text or text[-1].isspace() or not token.startswith("@"):
        return None
    return token[1:]


class ReferenceLexer(Lexer):
    """Style file references in the editor so they read as paths, not prose."""

    def __init__(self, extra: list[tuple[re.Pattern[str], str]] = ()) -> None:
        self.patterns = [(REFERENCE_PATTERN, "class:reference"), *extra]

    def lex_document(self, document: Document):
        def line_fragments(number: int):
            line = document.lines[number]
            matches = sorted(
                (match.start(), match.end(), style)
                for pattern, style in self.patterns
                for match in pattern.finditer(line)
            )
            fragments = []
            position = 0
            for start, end, style in matches:
                if start < position:
                    continue
                fragments.append(("", line[position:start]))
                fragments.append((style, line[start:end]))
                position = end
            fragments.append(("", line[position:]))
            return fragments

        return line_fragments


class FileReferenceCompleter(Completer):
    """Replace a trailing `@fragment` with a `./` workspace-relative path.

    ``workspace`` may be a callable, read on every completion: a hosted session
    learns its worktree from the host only after the prompt exists, and /attach
    can move the terminal to a session in another checkout.
    """

    def __init__(self, workspace: Path | Callable[[], Path] | None = None) -> None:
        self._workspace = workspace if callable(workspace) else (lambda: workspace)
        self.files = WorkspaceFiles(self._root())

    def _root(self) -> Path:
        return (self._workspace() or Path.cwd()).resolve()

    def get_completions(self, document: Document, complete_event: CompleteEvent):
        fragment = reference_fragment(document.text_before_cursor)
        if fragment is None:
            return
        if (root := self._root()) != self.files.root:
            self.files = WorkspaceFiles(root)
        for path in self.files.matches(fragment):
            # The `@` is a trigger, not part of the reference: it is replaced so
            # the model receives a path its file tools accept verbatim. The
            # leading './' is what marks it as a path; only a name containing
            # whitespace needs quotes to keep its end unambiguous in prose.
            reference = f"./{path}"
            yield Completion(
                f'"{reference}" ' if any(char.isspace() for char in reference) else f"{reference} ",
                start_position=-(len(fragment) + 1),
                display=path,
                display_meta=self.files.describe(path),
            )
