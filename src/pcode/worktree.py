"""Throwaway git worktrees, one per session, so concurrent sessions never share a tree.

The workspace *is* the worktree: file tools, the shell, repo context, and the
saved session all point at it, so the model needs no instructions and relative
paths cannot land in the mainline checkout by mistake.

Setup that git cannot do (installing dependencies, copying untracked config,
symlinking shared caches) lives in two optional scripts run after checkout:
`~/.config/pcode/worktree-setup` for every repo, then `<repo>/.pcode/worktree-setup`
for this one. The project script is arbitrary code shipped with the repo, so it
only runs for a trusted repository, like `.pcode/extensions`.
"""

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

from pcode.preferences import preferences_path

WORKTREES_DIR = ".worktrees"
DETACHED = "(detached)"
SETUP_SCRIPT = "worktree-setup"
PROJECT_SETUP = Path(".pcode") / SETUP_SCRIPT


class WorktreeError(ValueError):
    """A ValueError so slash-command dispatch reports it as a plain message."""


@dataclass(frozen=True)
class Worktree:
    path: Path
    branch: str
    main: Path


def _git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    result = subprocess.run(
        ["git", "-C", str(cwd), *args], capture_output=True, text=True, check=False
    )
    if check and result.returncode:
        detail = (result.stderr or result.stdout).strip() or f"exit {result.returncode}"
        raise WorktreeError(f"git {args[0]} failed: {detail}")
    return result


def main_checkout(path: Path) -> Path | None:
    """The repository's primary worktree, or None when `path` is not in a git repo."""
    result = _git(path, "worktree", "list", "--porcelain", check=False)
    if result.returncode:
        return None
    first = result.stdout.splitlines()[0]
    return Path(first.removeprefix("worktree ")).resolve()


def project_checkout(path: Path) -> Path | None:
    """`main_checkout`, but None when git is missing rather than an error.

    What a caller labelling or grouping a workspace wants: saving a session or
    listing history must keep working on a machine without git.
    """
    try:
        return main_checkout(path)
    except OSError:
        return None


def head_commit(path: Path) -> str | None:
    """The full hash `HEAD` names at `path`; None outside git or before the first commit."""
    try:
        result = _git(path, "rev-parse", "--verify", "--quiet", "HEAD^{commit}", check=False)
    except OSError:
        return None
    if result.returncode:
        return None
    return result.stdout.strip() or None


def repo_scope(path: Path) -> Path:
    """The identity to group a workspace by: its main checkout, or itself.

    Linked worktrees and the mainline checkout share a main checkout, so
    comparing this value (rather than the raw workspace) groups sessions from
    the same repository regardless of which worktree created them.
    """
    path = path.resolve()
    return project_checkout(path) or path


def is_linked(path: Path) -> bool:
    """True inside a secondary worktree (created by `git worktree add`)."""
    main = main_checkout(path)
    if main is None:
        return False
    toplevel = _git(path, "rev-parse", "--show-toplevel").stdout.strip()
    return Path(toplevel).resolve() != main


def mainline_branch(main: Path) -> str:
    result = _git(main, "symbolic-ref", "--quiet", "--short", "HEAD", check=False)
    if result.returncode:
        raise WorktreeError(f"mainline checkout {main} is not on a branch")
    return result.stdout.strip()


def validate_name(name: str) -> str:
    if not name or "/" in name or name.startswith(".") or any(c.isspace() for c in name):
        raise WorktreeError(f"worktree name must be a plain name, got {name!r}")
    return name


def create(repo: Path, name: str, base: str | None = None) -> Worktree:
    """Add `.worktrees/<name>` on branch `<name>`, reusing the branch if it exists."""
    validate_name(name)
    main = main_checkout(repo)
    if main is None:
        raise WorktreeError(f"{repo} is not inside a git repository")
    path = main / WORKTREES_DIR / name
    if path.exists():
        raise WorktreeError(f"{path} already exists")
    base = base or mainline_branch(main)
    path.parent.mkdir(exist_ok=True)
    _exclude_worktrees_dir(main)
    exists = _git(main, "show-ref", "--quiet", "--verify", f"refs/heads/{name}", check=False)
    if exists.returncode == 0:
        _git(main, "worktree", "add", str(path), name)
    else:
        _git(main, "worktree", "add", "-b", name, str(path), base)
    return Worktree(path=path, branch=name, main=main)


def _exclude_worktrees_dir(main: Path) -> None:
    """Keep `.worktrees/` out of `git status` without touching the tracked .gitignore."""
    common = _git(main, "rev-parse", "--path-format=absolute", "--git-common-dir").stdout
    exclude = Path(common.strip()) / "info" / "exclude"
    entry = f"/{WORKTREES_DIR}/"
    try:
        lines = exclude.read_text().splitlines() if exclude.exists() else []
        if entry not in lines and f"{WORKTREES_DIR}/" not in lines:
            exclude.parent.mkdir(exist_ok=True)
            with exclude.open("a") as file:
                file.write(f"{entry}\n")
    except OSError:
        pass  # cosmetic; git status noise is not worth failing creation


def describe(path: Path) -> Worktree | None:
    """The worktree `path` lives in, or None when it is the mainline or not a repo."""
    main = main_checkout(path)
    if main is None:
        return None
    toplevel = Path(_git(path, "rev-parse", "--show-toplevel").stdout.strip()).resolve()
    if toplevel == main:
        return None
    branch = _git(toplevel, "symbolic-ref", "--quiet", "--short", "HEAD", check=False)
    return Worktree(path=toplevel, branch=branch.stdout.strip() or DETACHED, main=main)


def setup_scripts(worktree: Worktree, project: bool | None = None) -> list[Path]:
    """Existing setup scripts in run order: user-level first, then the project's.

    The project script is taken from the new checkout when its branch has one
    (so it matches the code being set up), else from the mainline, where an
    untracked local copy may live. `project` None defers to whether the
    repository is trusted; True runs it regardless, for an explicit invocation
    inside that repo.
    """
    from pcode.project_trust import is_trusted

    scripts = [preferences_path().parent / SETUP_SCRIPT]
    if project is None:
        project = is_trusted(worktree.main)
    if project:
        scripts.append(
            next(
                (
                    p
                    for p in (worktree.path / PROJECT_SETUP, worktree.main / PROJECT_SETUP)
                    if p.is_file()
                ),
                worktree.main / PROJECT_SETUP,
            )
        )
    return [script for script in scripts if script.is_file()]


def run_setup(worktree: Worktree, stream=None, project: bool | None = None) -> None:
    """Run each setup script inside the worktree, echoing its output to `stream`.

    Executable scripts run directly (so they need a shebang, like git hooks);
    anything else goes through `sh`. A failing script aborts creation.
    """
    env = {
        **os.environ,
        "PCODE_MAIN": str(worktree.main),
        "PCODE_WORKTREE": str(worktree.path),
        "PCODE_BRANCH": worktree.branch,
    }
    for script in setup_scripts(worktree, project):
        if stream is not None:
            print(f"worktree: running {script}", file=stream)
        command = [str(script)] if os.access(script, os.X_OK) else ["sh", str(script)]
        result = subprocess.run(
            command,
            cwd=worktree.path,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        if stream is not None and result.stdout:
            stream.write(result.stdout)
        if result.returncode:
            raise WorktreeError(f"{script} exited {result.returncode}")


def is_dirty(path: Path) -> bool:
    return bool(_git(path, "status", "--porcelain", "--untracked-files=no").stdout.strip())


def unmerged_commits(worktree: Worktree) -> int:
    """Commits on the worktree's branch that the mainline branch does not have."""
    mainline = mainline_branch(worktree.main)
    result = _git(worktree.path, "rev-list", "--count", f"{mainline}..HEAD", check=False)
    return int(result.stdout.strip() or 0) if result.returncode == 0 else 0


def conflicted_files(path: Path) -> list[str]:
    """Paths still holding conflict markers from an in-progress merge, or empty."""
    if _git(path, "rev-parse", "-q", "--verify", "MERGE_HEAD", check=False).returncode:
        return []
    out = _git(path, "diff", "--name-only", "--diff-filter=U").stdout
    return [line for line in out.splitlines() if line]


def resolve_prompt(worktree: Worktree, files: list[str]) -> str:
    """What to ask the model once a merge has stopped on conflicts."""
    mainline = mainline_branch(worktree.main)
    listed = "\n".join(f"- {name}" for name in files)
    return (
        f"Merging `{mainline}` into `{worktree.branch}` in {worktree.path} stopped on "
        f"conflicts in:\n{listed}\n\n"
        "Resolve each file so the intent of both sides survives: read the conflict "
        "markers, check `git log -p` on both branches for a hunk when its purpose is "
        "unclear, and remove every marker. Run the relevant tests. Then `git add` the "
        "resolved files and `git commit` (no message needed; the merge message is "
        "prepared) to complete the merge. Do not run `git merge --abort`, and do not "
        "discard either side's change without saying so."
    )


def merge(worktree: Worktree) -> str:
    """Merge the mainline branch into the worktree, then fast-forward the mainline.

    Conflicts are resolved inside the worktree so the mainline checkout is
    never left mid-merge; it only ever moves by fast-forward.
    """
    from pcode.task_worktrees import parent_operation

    with parent_operation(worktree.path, worktree.main):
        return _merge(worktree)


def _merge(worktree: Worktree) -> str:
    """Merge with the source parent operation lock already held."""
    from pcode.task_worktrees import children_reason, moved_reason, task_for

    if reason := children_reason(worktree.path):
        raise WorktreeError(f"not merging {worktree.path}: {reason}")
    if record := task_for(worktree):
        if reason := moved_reason(worktree, record):
            raise WorktreeError(reason)
        raise WorktreeError(
            f"task {record.task_id} belongs to parent {record.parent}; "
            "use task integration, not a mainline merge"
        )
    if conflicted := conflicted_files(worktree.path):
        raise WorktreeError(
            f"a merge is already in progress with conflicts in {', '.join(conflicted)}; "
            "/worktree resolve has the model finish it"
        )
    if is_dirty(worktree.path):
        raise WorktreeError(f"{worktree.path} has uncommitted changes; commit them first")
    mainline = mainline_branch(worktree.main)
    result = _git(worktree.path, "merge", "--no-edit", mainline, check=False)
    if result.returncode:
        conflicted = conflicted_files(worktree.path)
        raise WorktreeError(
            f"conflicts merging {mainline} into {worktree.branch} in "
            f"{', '.join(conflicted) or worktree.path}; /worktree resolve has the model fix "
            "them, then run this again"
        )
    result = _git(worktree.main, "merge", "--ff-only", worktree.branch, check=False)
    if result.returncode:
        detail = (result.stderr or result.stdout).strip()
        raise WorktreeError(
            f"could not fast-forward {mainline} in {worktree.main}: {detail}\n"
            "Commit or stash those mainline changes, or re-run if the branch moved."
        )
    return f"merged {worktree.branch} into {mainline}"


def remove(worktree: Worktree, force: bool = False) -> str:
    """Delete the worktree directory, keeping its branch.

    `force` is for a worktree pcode itself just created and abandoned; user
    work is never forced because another session may still be in there.
    """
    from pcode.task_worktrees import discard_integrated, moved_reason, parent_operation, task_for

    if record := task_for(worktree):
        if reason := moved_reason(worktree, record):
            raise WorktreeError(reason)
        # Task discard locks its owner and then its own potential children.
        # Do not hold the child lock here: flock is not reentrant.
        if record.status != "integrated":
            raise WorktreeError(
                f"not removing {worktree.path}: task {record.task_id} ({record.status}), "
                "awaiting integration or explicit discard"
            )
        discard_integrated(worktree, record)
        return f"removed {worktree.path}"
    with parent_operation(worktree.path, worktree.main):
        return _remove(worktree, force)


def _remove(worktree: Worktree, force: bool = False) -> str:
    """Remove an ordinary checkout with its parent operation lock held."""
    from pcode.task_worktrees import keep_reason as task_keep_reason

    if reason := task_keep_reason(worktree):
        raise WorktreeError(f"not removing {worktree.path}: {reason}")
    flags = ["--force"] if force else []
    result = _git(worktree.main, "worktree", "remove", *flags, str(worktree.path), check=False)
    if result.returncode:
        detail = (result.stderr or result.stdout).strip()
        raise WorktreeError(
            f"not removing {worktree.path}: {detail}\n"
            f"If that work is disposable: git worktree remove --force {worktree.path}"
        )
    return f"removed {worktree.path}"


def keep_reason(worktree: Worktree) -> str:
    """Why this worktree must not be deleted, or "" when there is nothing to lose.

    Ignored files (a `.venv`, the shared `tmp` symlink) do not count, matching
    what a non-forced `git worktree remove` tolerates.
    """
    from pcode.task_worktrees import keep_reason as task_keep_reason

    task_reason = task_keep_reason(worktree)
    if task_reason is not None:
        return task_reason
    status = _git(worktree.path, "status", "--porcelain", check=False)
    if status.returncode:
        return "git status failed"
    if status.stdout.strip():
        return "uncommitted or untracked files"
    count = unmerged_commits(worktree)
    return f"{count} unmerged commit{'s' if count != 1 else ''}" if count else ""


def is_untouched(worktree: Worktree) -> bool:
    """Nothing to lose: no tracked changes, no untracked files, nothing unmerged."""
    return not keep_reason(worktree)


def fork_untouched(worktree: Worktree, base: str) -> bool:
    """A forked session's worktree with nothing of its own to lose.

    It starts at its original's commit (`base`), so `unmerged_commits` counts
    the original's work too, and a fork that did nothing would never look
    untouched. Here it is when the checkout is clean, has no commit past
    `base`, and another branch still holds `base`, so deleting it loses nothing.
    """
    status = _git(worktree.path, "status", "--porcelain", check=False)
    if status.returncode or status.stdout.strip():
        return False
    ahead = _git(worktree.path, "rev-list", "--count", f"{base}..HEAD", check=False)
    if ahead.returncode or ahead.stdout.strip() != "0":
        return False
    holders = _git(
        worktree.main,
        "for-each-ref",
        "--contains",
        base,
        "--format=%(refname:short)",
        "refs/heads",
        check=False,
    )
    return any(name != worktree.branch for name in holders.stdout.split())


def delete_branch(worktree: Worktree, *, force: bool = False) -> None:
    """Drop a fully merged branch; `-d` refuses anything unmerged, which is the point.

    `force` is for a branch whose commits another branch is known to hold.
    """
    from pcode.task_worktrees import records

    # Integrated children still need their parent's ref as the cleanup safety anchor,
    # even when the parent checkout has already gone away.
    if any(
        record.parent == str(worktree.path.resolve()) and record.status != "discarded"
        for record in records(worktree.main)
    ):
        return
    if worktree.branch != DETACHED:
        _git(worktree.main, "branch", "-D" if force else "-d", worktree.branch, check=False)


def finish(worktree: Worktree) -> str:
    """Merge into the mainline, then remove the worktree and its branch.

    Any refusal (dirty tree, conflicts, blocked fast-forward, untracked files)
    raises before anything is deleted, leaving the worktree resumable.
    """
    from pcode.task_worktrees import children_reason, parent_operation, task_for

    if task_for(worktree):
        return remove(worktree)  # refuses pending tasks; never merges one into mainline
    with parent_operation(worktree.path, worktree.main):
        if reason := children_reason(worktree.path):
            raise WorktreeError(f"not finishing {worktree.path}: {reason}")
        if conflicted_files(worktree.path) or is_dirty(worktree.path):
            _merge(worktree)  # raises with the precise reason
        merged = _merge(worktree) if unmerged_commits(worktree) else None
        _remove(worktree)
        delete_branch(worktree)
        if merged:
            return f"{merged} and removed its worktree"
        return f"removed worktree {worktree.branch} (nothing to merge)"


def listing(repo: Path) -> str:
    from pcode.task_worktrees import records

    output = _git(repo, "worktree", "list").stdout.rstrip()
    tasks = {record.worktree: record for record in records(repo) if record.status != "discarded"}
    lines = []
    for line in output.splitlines():
        record = next(
            (record for path, record in tasks.items() if line.startswith(path + " ")), None
        )
        if record:
            pending = "" if record.status == "integrated" else ", awaiting integration"
            line += f"  (task, parent={record.parent}, {record.status}{pending})"
        lines.append(line)
    return "\n".join(lines)


def linked_worktrees(repo: Path) -> list[Worktree]:
    """Every secondary worktree of this repository, mainline first entry excluded.

    Locked worktrees are left out: a lock is someone saying "this one is in
    use", and `git worktree remove` refuses them anyway.
    """
    result = _git(repo, "worktree", "list", "--porcelain", check=False)
    if result.returncode:
        return []
    blocks = [
        dict(line.partition(" ")[::2] for line in block.splitlines())
        for block in result.stdout.strip().split("\n\n")
    ]
    if not blocks:
        return []
    main = Path(blocks[0]["worktree"]).resolve()
    return [
        Worktree(
            path=Path(block["worktree"]).resolve(),
            branch=block.get("branch", "").removeprefix("refs/heads/") or DETACHED,
            main=main,
        )
        for block in blocks[1:]
        if "worktree" in block and "locked" not in block
    ]


def clean(repo: Path, keep: Path | None = None) -> list[str]:
    """Remove every worktree of this repository that has nothing to lose.

    "Nothing to lose" is `keep_reason`: no uncommitted or untracked files and
    nothing the mainline branch does not already have. Everything else is left
    alone and reported with the reason, so this is never a destructive command.
    `keep` defaults to the worktree `repo` itself sits in, so cleaning from
    inside one never deletes the caller's own directory out from under it.

    An idle session sitting in one of the others loses its working directory,
    not its work; stale registrations whose directory is already gone are
    pruned first.
    """
    main = main_checkout(repo)
    if main is None:
        raise WorktreeError(f"{repo} is not inside a git repository")
    _git(main, "worktree", "prune", check=False)
    own = describe(repo)
    keep = keep or (own.path if own else None)
    keep = keep.resolve() if keep else None
    report = []
    for tree in linked_worktrees(main):
        if tree.path == keep:
            continue
        reason = keep_reason(tree) or _removal_failure(tree)
        report.append(f"kept {tree.path} ({reason})" if reason else f"removed {tree.path}")
    return report or ["nothing to clean"]


def _removal_failure(tree: Worktree) -> str:
    """Delete the worktree and its branch, returning "" or why git refused."""
    try:
        remove(tree)
    except WorktreeError as error:
        return str(error).splitlines()[0].removeprefix(f"not removing {tree.path}: ")
    delete_branch(tree)
    return ""


def main(argv: list[str] | None = None) -> int:
    """`python -m pcode.worktree new|merge|remove|list|clean [NAME]`, for Makefiles and humans.

    Same operations as `pcode --worktree` and `/worktree`, without a session.
    The project's setup script runs unconditionally here: invoking this inside
    the repo is the consent that `project_extensions` stands in for otherwise.
    """
    import argparse
    import sys

    parser = argparse.ArgumentParser(prog="python -m pcode.worktree")
    parser.add_argument("command", choices=("new", "merge", "remove", "list", "clean"))
    parser.add_argument("name", nargs="?")
    parser.add_argument("--base", help="branch or commit to start from (new only)")
    args = parser.parse_args(argv)
    cwd = Path.cwd()
    try:
        if args.command == "list":
            print(listing(cwd))
            return 0
        if args.command == "clean":
            print("\n".join(clean(cwd)))
            return 0
        if not args.name:
            parser.error(f"{args.command} needs a NAME")
        if args.command == "new":
            created = create(cwd, args.name, args.base)
            try:
                run_setup(created, stream=sys.stderr, project=True)
            except WorktreeError:
                remove(created, force=True)
                raise
            print(f"worktree ready: {created.path} (branch {created.branch})")
            return 0
        main_path = main_checkout(cwd)
        if main_path is None:
            raise WorktreeError(f"{cwd} is not inside a git repository")
        target = describe(main_path / WORKTREES_DIR / args.name)
        if target is None:
            raise WorktreeError(f"no worktree named {args.name} under {main_path / WORKTREES_DIR}")
        print(merge(target) if args.command == "merge" else remove(target))
        return 0
    except WorktreeError as error:
        print(f"worktree: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())


# Session worktrees pcode created itself; hand-made ones are never removed.
SESSION_WORKTREE_PREFIX = "pcode-"


def session_scope(info) -> Path:
    """The repository a saved session belongs to, even once its worktree is gone.

    `repo_scope` asks git inside the directory, which can answer nothing at all
    after the directory is deleted; the recorded project checkout outlives it.
    """
    workspace = Path(info.workspace)
    if workspace.is_dir():
        return repo_scope(workspace)
    return Path(info.project) if info.project else workspace


def leave_worktree(workspace: Path, session, *, ask, notify, merge: bool = True) -> bool:
    """Tidy a session worktree on the way out of it, never losing work.

    Untouched (clean, nothing unmerged): removed with its branch, no question;
    a session that never had a turn is deleted too, and True is returned.
    Unmerged commits: per `worktree_exit`, ask (default yes), merge silently,
    or keep. Uncommitted changes, refusals, and hand-made worktrees (no
    `pcode-` prefix): kept, with a note on how to resume. `ask=None` means
    nobody is there to answer, and `merge=False` keeps unmerged commits even
    under `worktree_exit=merge`. Used at process exit and when a live session
    switches to another worktree of the same repository.
    """
    import shutil

    try:
        linked = describe(workspace)
        if linked is None:
            return False
        ours = linked.branch.startswith(SESSION_WORKTREE_PREFIX)
        dirty = is_dirty(linked.path)
        unmerged = unmerged_commits(linked)
        untouched = ours and is_untouched(linked)
        # A fork (`/fork`) starts at its original's commit, recorded as
        # its start: what lies before that is the original's to merge.
        info = getattr(session, "info", None)
        fork_base = (
            getattr(info, "start_commit", None) if getattr(info, "forked_from", None) else None
        )
        abandoned_fork = (
            not untouched and ours and fork_base is not None and fork_untouched(linked, fork_base)
        )
        untouched = untouched or abandoned_fork
    except WorktreeError:
        return False
    if session is not None:
        # A copied session shares its original's worktree; never pull it out
        # from under the other one, open or waiting to be resumed. Only asked
        # with a session, whose module is then already loaded: a bare exit
        # must not import the agent stack.
        from pcode.sessions import sharing

        others = sharing(linked.path, session.directory.parent, exclude=session.info.id)
        if others:
            other, running = others[0]
            state = "is still open in" if running else "still works in"
            notify(f"worktree: kept; session {other} {state} {linked.path}")
            return False
    resume = f"`pcode -C {linked.path} -c` resumes there"

    def repoint():
        if session is not None:
            session.info.workspace = str(linked.main)
            session.save_info()

    def forget():
        # Its review checkpoint was of the checkout just removed.
        if session is not None:
            from pcode.sessions import forget_refs

            forget_refs(session.info)

    try:
        if untouched:
            remove(linked)
            delete_branch(linked, force=abandoned_fork)
            deleted = session is not None and session.info.turns == 0
            if deleted:
                session.close()
                shutil.rmtree(session.directory, ignore_errors=True)
            else:
                repoint()
            forget()
            notify(f"worktree: removed untouched {linked.path}")
            return deleted
        if dirty or not ours or not unmerged:
            state = "uncommitted changes" if dirty else f"{unmerged} unmerged commit(s)"
            notify(f"worktree: {linked.path} ({linked.branch}) has {state}; {resume}")
            return False
        from pcode.preferences import load_preferences

        mode = load_preferences().get("worktree_exit", "ask")
        mainline = mainline_branch(linked.main)
        if mode == "ask" and ask is not None:
            notify(f"worktree: {linked.branch} has {unmerged} commit(s) not in {mainline}.")
            try:
                answer = ask("Merge and remove the worktree? [Y/n] ").strip().lower()
            except EOFError, OSError, KeyboardInterrupt:
                answer = "n"
            if answer not in ("", "y", "yes"):
                notify(f"worktree: kept; {resume}")
                return False
        elif mode != "merge" or not merge:
            notify(
                f"worktree: {linked.path} ({linked.branch}) has {unmerged} unmerged commit(s); "
                f"{resume}"
            )
            return False
        notify("worktree: " + finish(linked))
        repoint()
        forget()
    except (WorktreeError, OSError) as error:
        notify(f"worktree: {error}\nworktree: kept; {resume}")
    return False
