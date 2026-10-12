"""Net git diffs for /diffs: what a session's work adds up to, one entry per file.

A linked worktree belongs to its session, so its whole branch is compared with
the merge-base on the mainline branch. That is exactly what a merge would bring
in, and it stays right after `/worktree merge` merges mainline into the branch,
where a remembered starting commit would claim mainline's changes too. A shared
checkout has no branch of its own, so it compares the working tree with the
commit the session started from, but only for files this session's tools edited
or its commits touched, since anything else there may be the user's. A commit
counts as the session's when it is new since the start, was made after the
session began, and by this checkout's git identity; a pulled commit is neither.
The same load also cuts out what is not yet committed, and what changed since
the tree last marked reviewed, kept under the session's refs (`SESSION_REFS`)
with the tree it was compared against.

`git diff` never shows untracked files, and staging them would change the
user's index. So the working tree is recorded into a throwaway copy of the index
(`GIT_INDEX_FILE`), written out as a tree, and diffed tree to tree; the real
index is never written. The copy sits beside the real one because a split index
finds its shared half there.
"""

import os
import re
import shutil
import subprocess
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from pcode import worktree
from pcode.edits import edit_text, source_lines
from pcode.runtime import EditCompleted
from pcode.tool_display import plain

# One file's raw diff beyond this is counted but not shown; a lockfile rewrite
# would otherwise stall redaction and the browser's search.
MAX_FILE_DIFF = 1024 * 1024
MAX_DIFF_LINES = 2000
OPERATIONS = {"A": "created", "C": "copied", "D": "deleted", "R": "renamed"}
# Pin everything user config can change about the output this module parses.
DIFF_OPTIONS = (
    "--no-color",
    "--no-ext-diff",
    "--no-textconv",
    "--no-relative",
    "--find-renames",
    "--src-prefix=a/",
    "--dst-prefix=b/",
)


class GitDiffError(Exception):
    """Git could not produce the review."""


# Each session's refs live under one prefix, so one sweep removes them with the
# session or its worktree: a ref keeps what it names from garbage collection,
# and no session file has to be written for it. The review checkpoint is
# `reviewed` (the tree last marked reviewed) and `review-base` (the base tree
# it was compared against then, since merging mainline moves the base).
SESSION_REFS = "refs/pcode/sessions/"


def session_ref(key: str, name: str) -> str:
    """Session `key`'s ref called `name`; GitDiffError for a key git would misread."""
    if not _valid_key(key):
        raise GitDiffError("invalid session id")
    return f"{SESSION_REFS}{key}/{name}"


def forget_session(workspace: Path, key: str) -> None:
    """Delete every ref session `key` holds, releasing what they kept."""
    prefix = session_ref(key, "")
    listed = _git(workspace, "for-each-ref", "--format=%(refname)", prefix).decode().split()
    if listed:
        commands = "".join(f"delete {name}\n" for name in listed)
        _git(workspace, "update-ref", "--stdin", input=commands.encode())


@dataclass(frozen=True)
class Checkpoint:
    """What was marked reviewed: the working tree, and the base it was compared with."""

    tree: str
    # None for a checkpoint whose base was not recorded; the current one stands in.
    base: str | None = None


@dataclass(frozen=True)
class Review:
    """One load of /diffs: the session's net work, and two narrower cuts of it.

    `changes` is the net work against its base (`title` says which).
    `uncommitted` is what the next commit would take in, and `since_review` what
    changed since the tree last marked reviewed (None when there is none). All
    three describe `tree`, the working tree as recorded for this load, so marking
    it reviewed records exactly what was shown. Paths are relative to `root`.
    """

    title: str
    changes: list[EditCompleted]
    empty: str
    root: Path | None = None
    uncommitted: list[EditCompleted] = field(default_factory=list)
    since_review: list[EditCompleted] | None = None
    tree: str | None = None
    # The base's tree, recorded with `tree` when it is marked reviewed.
    base_tree: str | None = None

    @property
    def checkpoint(self) -> Checkpoint | None:
        return Checkpoint(self.tree, self.base_tree) if self.tree else None


def load_review(
    workspace: Path,
    edited: Iterable[str],
    start: str | None = None,
    since: str | None = None,
    reviewed: Checkpoint | None = None,
) -> Review | None:
    """The review of `workspace`, or None outside a git repository.

    `edited` holds the paths this session's file tools changed, relative to
    the workspace; `start` is the commit the session began at and `since` when
    (ISO 8601). Only a shared checkout uses them. `reviewed` is the checkpoint
    last marked reviewed. Raises GitDiffError when git cannot produce the review.
    """
    try:
        if worktree.project_checkout(workspace) is None:
            return None
        linked = worktree.describe(workspace)
    except worktree.WorktreeError as error:
        raise GitDiffError(str(error)) from error
    if linked is not None:
        top = _toplevel(linked.path)
        base, title, empty = _branch_base(linked)
        only = None
    else:
        top = _toplevel(workspace)
        base, title, empty, only = _session_base(workspace, top, edited, start, since)
    if only is not None and not only:
        return Review(title, [], empty, root=top)
    tree = _snapshot(top, only)
    changes = _tree_diff(top, base, tree, only)
    uncommitted = changes if base == "HEAD" else _tree_diff(top, "HEAD", tree, only)
    since_review = None
    if reviewed is not None:
        try:
            # Only the session's work: files that differ from the base now, or
            # differed from the base of the time when reviewed. A file
            # mainline's merge (or a pull) brought in differs from neither, so
            # it is not news.
            then = reviewed.base or base
            ours = _names(top, base, tree, only) | _names(top, then, reviewed.tree, only)
            since_review = _tree_diff(top, reviewed.tree, tree, ours) if ours else []
        except GitDiffError:
            since_review = None  # a tree git no longer has: review everything
    base_tree = _git(top, "rev-parse", f"{base}^{{tree}}").decode().strip()
    return Review(
        title,
        sorted(changes, key=_path),
        empty,
        root=top,
        uncommitted=sorted(uncommitted, key=_path),
        since_review=since_review,
        tree=tree,
        base_tree=base_tree,
    )


def _path(change: EditCompleted) -> str:
    return change.path


def reviewed(workspace: Path, key: str) -> Checkpoint | None:
    """The checkpoint last marked reviewed for session `key`, if git still has it."""
    if not _valid_key(key):
        return None
    found = []
    for name in ("reviewed", "review-base"):
        try:
            spec = f"{session_ref(key, name)}^{{tree}}"
            out = _git(workspace, "rev-parse", "--verify", "--quiet", spec, check=False)
        except GitDiffError:
            return None
        found.append(out.decode().strip() or None)
    tree, base = found
    return Checkpoint(tree, base) if tree else None


def mark_reviewed(workspace: Path, key: str, checkpoint: Checkpoint) -> None:
    """Record `checkpoint` as session `key`'s reviewed state, both refs at once."""
    names = [checkpoint.tree, *([checkpoint.base] if checkpoint.base else [])]
    if not _valid_key(key) or not all(_is_object_name(name) for name in names):
        raise GitDiffError("invalid review checkpoint")
    commands = f"update {session_ref(key, 'reviewed')} {checkpoint.tree}\n"
    base = session_ref(key, "review-base")
    commands += f"update {base} {checkpoint.base}\n" if checkpoint.base else f"delete {base}\n"
    _git(workspace, "update-ref", "--stdin", input=commands.encode())


def _is_object_name(name: str) -> bool:
    return bool(re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", name))


def _valid_key(key: str) -> bool:
    # Stricter than git's ref rules: one plain path component, no "..", no
    # ".lock" ending, so the key can never reach outside its prefix.
    return bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]*", key))


def _branch_base(linked: worktree.Worktree) -> tuple[str, str, str]:
    """A linked worktree's whole branch, against its merge-base with mainline."""
    try:
        mainline = worktree.mainline_branch(linked.main)
    except worktree.WorktreeError as error:
        raise GitDiffError(str(error)) from error
    base = _git(linked.path, "merge-base", "HEAD", mainline).decode().strip()
    branch, mainline = plain(linked.branch), plain(mainline)
    return (
        base,
        f"{branch} vs {mainline} (merge-base {_short(linked.path, base)})"
        " · includes uncommitted and new files",
        f"{branch} has nothing that {mainline} does not already have.",
    )


def _session_base(
    workspace: Path, top: Path, edited: Iterable[str], start: str | None, since: str | None
) -> tuple[str, str, str, set[str]]:
    """A shared checkout's session work: the working tree against the starting commit.

    Limited to files this session edited or committed. Falls back to
    uncommitted changes against HEAD, in edited files only, when no start was
    recorded or HEAD no longer descends from it (a rebase, a branch switch).
    """
    edited_paths = _top_relative(workspace, top, edited)
    if start is None or not _is_ancestor(top, start):
        why = "no starting commit recorded" if start is None else "HEAD moved off the start"
        return (
            "HEAD",
            f"uncommitted changes vs HEAD ({_short(top, 'HEAD')})"
            f" · only files edited this session · {why}",
            "No uncommitted changes in files edited this session.",
            edited_paths,
        )
    return (
        start,
        f"since the session started ({_short(top, start)})"
        " · files this session edited or committed, including uncommitted and new files",
        "No changes since the session started in files it edited or committed.",
        edited_paths | _committed(top, start, since),
    )


def _toplevel(workspace: Path) -> Path:
    top = Path(_git(workspace, "rev-parse", "--show-toplevel").decode().strip()).resolve()
    if not _git(workspace, "rev-parse", "--verify", "--quiet", "HEAD^{commit}", check=False):
        raise GitDiffError("the repository has no commits yet")
    return top


def _top_relative(workspace: Path, top: Path, edited: Iterable[str]) -> set[str]:
    paths = set()
    for path in edited:
        # Tool paths are relative to the workspace, which may sit below the top level.
        location = Path(os.path.normpath(workspace / path))
        if location.is_relative_to(top):
            paths.add(location.relative_to(top).as_posix())
    return paths


def _is_ancestor(top: Path, start: str) -> bool:
    """Whether HEAD still descends from `start`; a commit git lost is not an ancestor."""
    # The hash comes from session.json; never let it reach git as an option.
    if not re.fullmatch(r"[0-9a-f]{40,64}", start):
        return False
    try:
        _git(top, "merge-base", "--is-ancestor", start, "HEAD")
    except GitDiffError:
        return False
    return True


def _committed(top: Path, start: str, since: str | None) -> set[str]:
    """Paths touched by the session's own commits after `start`."""
    args = ["log", "--format=", "--name-only", "-z", "--no-renames", "--no-merges"]
    args.append("--no-show-signature")
    if since:
        args.append(f"--since={since}")
    email = _git(top, "config", "user.email", check=False).decode().strip()
    if email:
        args += ["--fixed-strings", f"--committer=<{email}>"]
    return _paths(_git(top, *args, f"{start}..HEAD", "--"))


def _snapshot(top: Path, only: set[str] | None = None) -> str:
    """Record the working tree as a tree object.

    Untracked files are included, limited to `only` where given; the real
    index is never written.
    """
    index = Path(
        _git(top, "rev-parse", "--path-format=absolute", "--git-path", "index").decode().strip()
    )
    descriptor, scratch = tempfile.mkstemp(prefix="pcode-diff-index-", dir=index.parent)
    os.close(descriptor)
    env = {**os.environ, "GIT_INDEX_FILE": scratch, "GIT_OPTIONAL_LOCKS": "0"}
    try:
        # Starting from the real index keeps staged work and its stat cache, so
        # only files that differ from it are hashed below. copy2 keeps the
        # index's mtime: git rechecks the content of any entry modified in the
        # same second the index was written, and a fresh mtime would make a
        # same-size edit from that second look unchanged.
        if index.exists():
            shutil.copy2(index, scratch)
        else:
            os.unlink(scratch)
        untracked = _paths(_git(top, "ls-files", "-z", "--others", "--exclude-standard", env=env))
        changed = _paths(_git(top, "ls-files", "-z", "--modified", "--deleted", env=env))
        if only is not None:
            untracked &= only
            changed &= only
        # A tree cannot hold a conflict, so a merge in progress outside `only`
        # is recorded as its files stand; the diffs never look outside `only`.
        unmerged = _paths(_git(top, "diff", "--name-only", "-z", "--diff-filter=U", env=env))
        stage = sorted(changed | unmerged | untracked)
        if stage:
            listing = b"\0".join(path.encode("utf-8", "surrogateescape") for path in stage)
            _git(
                top,
                "add",
                "--all",
                "--pathspec-from-file=-",
                "--pathspec-file-nul",
                env={**env, "GIT_LITERAL_PATHSPECS": "1"},
                input=listing,
            )
        tree = _git(top, "write-tree", env=env).decode().strip()
    finally:
        Path(scratch).unlink(missing_ok=True)
    return tree


DIFF_ENV = {"GIT_LITERAL_PATHSPECS": "1", "GIT_OPTIONAL_LOCKS": "0"}


def _names(top: Path, base: str, tree: str, only: set[str] | None) -> set[str]:
    """Every path, either side of a rename, that differs between `base` and `tree`."""
    paths = ["--", *sorted(only)] if only is not None else []
    names = _git(
        top,
        "diff",
        *DIFF_OPTIONS,
        "--name-status",
        "-z",
        base,
        tree,
        *paths,
        env={**os.environ, **DIFF_ENV},
    )
    found = {path for _, old, new in _entries(names) for path in (old, new)}
    return found if only is None else found & only


def _tree_diff(top: Path, base: str, tree: str, only: set[str] | None) -> list[EditCompleted]:
    """`tree` against `base`, one change per file, limited to `only` where given."""
    env = {**os.environ, **DIFF_ENV}
    # Limit git itself, not just the result: against a start commit, a pull
    # would otherwise patch every file it brought in. A rename with one side
    # outside `only` shows as one side.
    paths = ["--", *sorted(only)] if only is not None else []
    names = _git(top, "diff", *DIFF_OPTIONS, "--name-status", "-z", base, tree, *paths, env=env)
    patch = _git(top, "diff", *DIFF_OPTIONS, "--patch", base, tree, *paths, env=env)
    return [
        _change(status, old, new, chunk)
        for (status, old, new), chunk in _pair(_entries(names), patch)
        if only is None or old in only or new in only
    ]


def _pair(entries: list[tuple[str, str, str]], patch: bytes):
    """Match each name-status entry with its patch text, both in git's file order."""
    chunks = iter(chunk for chunk in re.split(rb"^(?=diff --git )", patch, flags=re.M) if chunk)
    paired = []
    for entry in entries:
        # A type change (file to symlink) is printed as a deletion and a creation.
        taken = [next(chunks, None) for _ in range(2 if entry[0] == "T" else 1)]
        if None in taken:
            raise GitDiffError("git listed more files than patches")
        paired.append((entry, b"".join(taken)))
    if next(chunks, None) is not None:
        raise GitDiffError("git listed more patches than files")
    return paired


def _change(status: str, old: str, new: str, chunk: bytes) -> EditCompleted:
    path = plain(edit_text(new if old == new else f"{old} → {new}"), limit=None)
    operation = OPERATIONS.get(status, "edited")
    # Only "\n" ends a line, as in git's hunk counts; a CRLF file's "\r" goes.
    text = chunk.decode("utf-8", "replace")
    lines = [line.rstrip("\n").removesuffix("\r") for line in source_lines(text)]
    added, removed = _counts(lines)
    if len(chunk) > MAX_FILE_DIFF:
        return EditCompleted(
            "", path, operation, "", added, removed, omitted="Diff exceeds preview size limit"
        )
    # `diff --git` repeats the path and `index` only names blobs. Hunk lines
    # always carry a prefix, so neither can be file content.
    body = [line for line in lines if not line.startswith(("diff --git ", "index "))]
    shown = edit_text("\n".join(body)).split("\n")
    truncated = len(shown) > MAX_DIFF_LINES
    patch = "\n".join(shown[:MAX_DIFF_LINES])
    return EditCompleted("", path, operation, patch, added, removed, truncated)


def _counts(lines: list[str]) -> tuple[int, int]:
    """Changed lines inside hunks; each file's `---`/`+++` headers come before its first.

    A type change is two patches in one chunk, so a second `diff --git` header
    leaves the first patch's hunks.
    """
    added = removed = 0
    in_hunk = False
    for line in lines:
        if line.startswith("diff --git "):
            in_hunk = False
        elif line.startswith("@@"):
            in_hunk = True
        elif in_hunk and line.startswith("+"):
            added += 1
        elif in_hunk and line.startswith("-"):
            removed += 1
    return added, removed


def _entries(raw: bytes) -> list[tuple[str, str, str]]:
    """`--name-status -z` records as (status, old path, new path)."""
    fields = raw.split(b"\0")
    entries = []
    position = 0
    while position < len(fields) and fields[position]:
        status = fields[position].decode()[0]
        if status in "RC":
            old, new = fields[position + 1], fields[position + 2]
            position += 3
        else:
            old = new = fields[position + 1]
            position += 2
        entries.append((status, _decode(old), _decode(new)))
    return entries


def _paths(raw: bytes) -> set[str]:
    return {_decode(path) for path in raw.split(b"\0") if path}


def _decode(path: bytes) -> str:
    # Surrogate escapes round-trip a non-UTF-8 name back to git as a pathspec.
    return path.decode("utf-8", "surrogateescape")


def _short(top: Path, revision: str) -> str:
    return _git(top, "rev-parse", "--short", revision).decode().strip()


def _git(
    cwd: Path, *args: str, env: dict | None = None, input: bytes | None = None, check: bool = True
) -> bytes:
    try:
        result = subprocess.run(
            ["git", "-C", str(cwd), "-c", "core.quotePath=false", *args],
            capture_output=True,
            env=env,
            input=input,
            check=False,
        )
    except OSError as error:
        raise GitDiffError(f"git could not run: {error}") from error
    if result.returncode:
        if not check:
            return b""
        detail = result.stderr.decode("utf-8", "replace").strip() or f"exit {result.returncode}"
        raise GitDiffError(f"git {args[0]} failed: {detail}")
    return result.stdout
