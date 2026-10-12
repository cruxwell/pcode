import os
import subprocess
from io import StringIO
from pathlib import Path

import pytest
from rich.console import Console

from pcode import git_diff, worktree
from pcode.app import PreviewApp
from pcode.edits import patch_text
from pcode.git_diff import GitDiffError, load_review
from pcode.runtime import EditCompleted

pytestmark = pytest.mark.skipif(
    subprocess.run(["git", "--version"], capture_output=True).returncode != 0,
    reason="git is required",
)


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(cwd), *args], capture_output=True, text=True, check=True
    ).stdout.strip()


def commit(path: Path, name: str, text: str, message: str = "change") -> None:
    (path / name).write_text(text)
    git(path, "add", name)
    git(path, "commit", "-q", "-m", message)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    # Git resolves symlinks (tmp on macOS lives under /private).
    path = (tmp_path / "repo").resolve()
    path.mkdir()
    git(path, "init", "-q", "-b", "main")
    git(path, "config", "user.email", "t@example.com")
    git(path, "config", "user.name", "t")
    commit(path, "README", "hello\n", "init")
    monkeypatch.delenv("GIT_DIR", raising=False)
    monkeypatch.delenv("GIT_INDEX_FILE", raising=False)
    return path


@pytest.fixture
def linked(repo):
    return worktree.create(repo, "feature")


def by_path(view):
    return {change.path: change for change in view.changes}


def untouched(path: Path):
    """Porcelain status and the git dir listing, to prove the view wrote neither."""
    git_dir = Path(git(path, "rev-parse", "--path-format=absolute", "--git-dir"))
    return git(path, "status", "--porcelain=v1", "-uall"), sorted(os.listdir(git_dir))


def test_branch_diff_is_net_per_file_and_ignores_merged_mainline(repo, linked):
    commit(linked.path, "README", "hello\nfirst\n")
    (linked.path / "README").write_text("hello\nfirst\nsecond\n")  # uncommitted
    (linked.path / "new.py").write_text("print('new')\n")  # untracked
    commit(repo, "main_only.py", "from main\n")
    git(linked.path, "merge", "-q", "--no-edit", "main")
    before = untouched(linked.path)

    view = load_review(linked.path, [])

    assert untouched(linked.path) == before
    changes = by_path(view)
    assert list(changes) == ["README", "new.py"]  # mainline's file is not the branch's work
    readme = changes["README"]
    assert (readme.operation, readme.added, readme.removed) == ("edited", 2, 0)
    assert "+first" in readme.patch and "+second" in readme.patch
    assert "diff --git" not in readme.patch and "\nindex " not in readme.patch
    assert changes["new.py"].operation == "created"
    # Merging mainline in moved the merge-base up to mainline's tip.
    base = git(linked.path, "rev-parse", "--short", "main")
    assert view.title == (
        f"feature vs main (merge-base {base}) · includes uncommitted and new files"
    )


def test_merged_branch_has_nothing_to_show(repo, linked):
    commit(linked.path, "done.py", "x\n")
    worktree.merge(linked)
    view = load_review(linked.path, [])
    assert view.changes == []
    assert view.empty == "feature has nothing that main does not already have."


def test_renames_deletions_type_and_mode_changes_line_up_with_their_patches(repo):
    path = repo  # committed on mainline, so the merge-base has them
    for name, text in [
        ("old.py", "one\ntwo\nthree\nfour\n"),
        ("gone.py", "bye\n"),
        ("kind.txt", "file\n"),
        ("mode.sh", "echo\n"),
        ("sp ace.py", "a\n"),
    ]:
        (path / name).write_text(text)
    (path / "bin.dat").write_bytes(b"\x00\x01")
    git(path, "add", ".")
    git(path, "commit", "-q", "-m", "files")
    path = worktree.create(repo, "feature").path
    git(path, "mv", "old.py", "new.py")
    (path / "new.py").write_text("one\ntwo\nthree\nfour\nfive\n")
    (path / "gone.py").unlink()
    (path / "kind.txt").unlink()
    (path / "kind.txt").symlink_to("README")  # a type change prints two patches
    (path / "mode.sh").chmod(0o755)
    (path / "bin.dat").write_bytes(b"\x00\x02")
    (path / "sp ace.py").write_text("a\nb\n")
    (path / "ünï.py").write_text("u\n")

    changes = by_path(load_review(path, []))

    assert changes["old.py → new.py"].operation == "renamed"
    assert "+five" in changes["old.py → new.py"].patch
    assert changes["gone.py"].operation == "deleted" and "-bye" in changes["gone.py"].patch
    assert "+README" in changes["kind.txt"].patch and "-file" in changes["kind.txt"].patch
    assert (changes["kind.txt"].added, changes["kind.txt"].removed) == (1, 1)
    assert "new mode 100755" in changes["mode.sh"].patch
    assert "Binary files" in changes["bin.dat"].patch
    assert "+b" in changes["sp ace.py"].patch
    assert changes["ünï.py"].operation == "created" and "+u" in changes["ünï.py"].patch


def test_secret_looking_files_are_diffed_with_values_redacted(repo, linked):
    path = linked.path
    commit(path, "config.env", "A=1\n")
    (path / "config.env").write_text("A=2\n")
    (path / ".env.local").write_text("B=3\n")
    (path / "app.py").write_text('token = "sk-0123456789ab"\n')

    changes = by_path(load_review(path, []))

    assert changes[".env.local"].operation == "created" and "+B=3" in changes[".env.local"].patch
    assert "+A=2" in changes["config.env"].patch
    assert "sk-0123456789ab" not in changes["app.py"].patch
    assert "[redacted]" in changes["app.py"].patch


def test_large_diffs_are_clipped_or_only_counted(repo, linked, monkeypatch):
    (linked.path / "long.py").write_text("".join(f"line {i}\n" for i in range(2500)))
    change = by_path(load_review(linked.path, []))["long.py"]
    assert change.truncated and change.added == 2500
    assert len(change.patch.splitlines()) == git_diff.MAX_DIFF_LINES

    monkeypatch.setattr(git_diff, "MAX_FILE_DIFF", 100)
    change = by_path(load_review(linked.path, []))["long.py"]
    assert change.omitted == "Diff exceeds preview size limit" and change.added == 2500


def test_shared_checkout_shows_only_edited_files_against_head(repo):
    (repo / "sub").mkdir()
    commit(repo, "sub/edited.py", "old\n")
    commit(repo, "users.py", "theirs\n")
    git(repo, "commit", "-q", "--allow-empty", "-m", "head")
    (repo / "users.py").write_text("theirs, still dirty\n")  # not this session's
    (repo / "sub" / "edited.py").write_text("new\n")
    (repo / "sub" / "created.py").write_text("made\n")
    (repo / "sub" / "untouched.py").write_text("stray\n")
    workspace = repo / "sub"
    before = untouched(repo)

    view = load_review(workspace, ["edited.py", "created.py", str(repo / "sub" / "edited.py")])

    assert untouched(repo) == before
    assert list(by_path(view)) == ["sub/created.py", "sub/edited.py"]
    short = git(repo, "rev-parse", "--short", "HEAD")
    assert view.title == (
        f"uncommitted changes vs HEAD ({short}) · only files edited this session"
        " · no starting commit recorded"
    )
    assert load_review(workspace, []).changes == []
    assert load_review(workspace, ["../../elsewhere.py"]).changes == []


def test_outside_git_there_is_no_view_and_a_repository_without_commits_errors(tmp_path):
    assert load_review(tmp_path, ["a.py"]) is None
    git(tmp_path, "init", "-q")
    with pytest.raises(GitDiffError, match="no commits"):
        load_review(tmp_path, ["a.py"])


def edited(path: str, call_id: str) -> EditCompleted:
    return EditCompleted(call_id, path, "edited", "@@ -1 +1 @@\n-a\n+b", 1, 1)


def test_app_loads_the_review_and_keeps_an_unsaved_checkpoint(repo, linked, tmp_path):
    (linked.path / "new.py").write_text("x\n")
    app = PreviewApp(console=Console(file=StringIO()), workspace=linked.path)
    review = app.load_review()
    assert [c.path for c in review.changes] == [c.path for c in review.uncommitted] == ["new.py"]
    assert review.since_review is None and review.root == linked.path
    # An unsaved session keeps its checkpoint in memory, never in a ref.
    app.mark_reviewed(review.checkpoint)
    assert git(repo, "for-each-ref", "refs/pcode") == ""
    (linked.path / "new.py").write_text("y\n")
    (since,) = app.load_review().since_review
    assert "-x" in since.patch and "+y" in since.patch
    app.controller.new("")
    assert app.load_review().since_review is None

    outside = tmp_path / "plain"
    outside.mkdir()
    assert PreviewApp(console=Console(file=StringIO()), workspace=outside).load_review() is None


def test_review_checkpoints_live_in_a_ref_and_diff_tree_to_tree(repo, linked):
    path = linked.path
    commit(path, "a.py", "one\n")
    (path / "a.py").write_text("two\n")
    (path / "b.py").write_text("new\n")
    before = untouched(path)
    first = load_review(path, [])
    assert untouched(path) == before  # recording the tree writes no index or ref
    assert first.since_review is None
    assert git_diff.reviewed(path, "session-1") is None
    git_diff.mark_reviewed(path, "session-1", first.checkpoint)
    assert git_diff.reviewed(path, "session-1") == first.checkpoint
    assert git_diff.reviewed(repo, "session-1") == first.checkpoint  # shared across worktrees
    assert first.checkpoint.base == git(path, "rev-parse", "main^{tree}")
    # Trees only: nothing new reaches `git log --all`.
    assert git(path, "log", "--all", "--format=%H").count("\n") == 1

    unchanged = load_review(path, [], reviewed=first.checkpoint)
    assert unchanged.since_review == [] and unchanged.tree == first.tree
    commit(path, "a.py", "two\n")  # committing what was reviewed is not new
    (path / "b.py").write_text("newer\n")
    later = load_review(path, [], reviewed=first.checkpoint)
    assert [c.path for c in later.since_review] == ["b.py"]
    assert "-new" in later.since_review[0].patch and "+newer" in later.since_review[0].patch
    assert [c.path for c in later.uncommitted] == ["b.py"]
    # A tree git does not have shows everything, rather than failing the load.
    assert load_review(path, [], reviewed=git_diff.Checkpoint("f" * 40)).since_review is None
    # A checkpoint recorded without its base still loads, against today's.
    git_diff.mark_reviewed(path, "session-1", git_diff.Checkpoint(first.tree))
    assert git_diff.reviewed(path, "session-1") == git_diff.Checkpoint(first.tree)

    for key in ("../escape", "", "-x"):
        assert git_diff.reviewed(path, key) is None
        with pytest.raises(GitDiffError):
            git_diff.mark_reviewed(path, key, first.checkpoint)
    with pytest.raises(GitDiffError):
        git_diff.mark_reviewed(path, "ok", git_diff.Checkpoint("HEAD"))
    with pytest.raises(GitDiffError):
        git_diff.mark_reviewed(path, "ok", git_diff.Checkpoint(first.tree, "HEAD\nupdate x"))


def test_a_merge_in_progress_outside_the_session_still_loads(repo):
    commit(repo, "c.txt", "base\n")
    git(repo, "checkout", "-q", "-b", "other")
    commit(repo, "c.txt", "other\n")
    git(repo, "checkout", "-q", "main")
    commit(repo, "c.txt", "main\n")
    subprocess.run(["git", "-C", str(repo), "merge", "-q", "other"], capture_output=True)
    (repo / "mine.py").write_text("x\n")
    review = load_review(repo, ["mine.py"])
    assert [c.path for c in review.changes] == ["mine.py"] and review.tree


def test_shared_checkout_keeps_committed_session_work(repo):
    commit(repo, "a.py", "one\n")
    start = git(repo, "rev-parse", "HEAD")
    since = git(repo, "log", "-1", "--format=%cI")
    # A pulled commit: someone else's, touching a file the session never did.
    (repo / "theirs.py").write_text("x\n")
    git(repo, "add", "theirs.py")
    git(repo, "-c", "user.email=other@example.com", "commit", "-q", "-m", "theirs")
    (repo / "a.py").write_text("two\n")  # a tool edit, then committed
    commit(repo, "shell.py", "made by a shell command\n")  # never a tool edit
    git(repo, "commit", "-q", "-am", "agent")
    (repo / "a.py").write_text("three\n")  # and edited again, uncommitted

    net = load_review(repo, ["a.py"], start, since)
    assert list(by_path(net)) == ["a.py", "shell.py"]
    assert "-one" in by_path(net)["a.py"].patch and "+three" in by_path(net)["a.py"].patch
    assert net.title.startswith(
        f"since the session started ({git(repo, 'rev-parse', '--short', start)})"
    )
    uncommitted = {change.path: change for change in net.uncommitted}
    assert list(uncommitted) == ["a.py"]
    assert "-two" in uncommitted["a.py"].patch

    # HEAD no longer descends from the start: back to uncommitted, edited files only.
    git(repo, "reset", "-q", "--hard", f"{start}~1")
    commit(repo, "a.py", "rewritten\n")
    (repo / "a.py").write_text("dirty\n")
    view = load_review(repo, ["a.py"], start, since)
    assert list(by_path(view)) == ["a.py"] and view.title.endswith("HEAD moved off the start")
    assert load_review(repo, ["a.py"], "--output=/tmp/x", since).title.endswith(
        "HEAD moved off the start"
    )


def test_new_sessions_record_their_starting_commit(repo, tmp_path):
    from pcode.sessions import SavedSession

    session = SavedSession.create("test", repo, root=tmp_path / "sessions")
    try:
        assert session.info.start_commit == git(repo, "rev-parse", "HEAD")
    finally:
        session.close()
    outside = tmp_path / "plain"
    outside.mkdir()
    session = SavedSession.create("test", outside, root=tmp_path / "sessions")
    try:
        assert session.info.start_commit is None
    finally:
        session.close()


def test_since_review_ignores_what_merging_mainline_brings_in(repo, linked):
    path = linked.path
    commit(path, "mine.py", "one\n")
    reviewed = load_review(path, []).checkpoint
    commit(repo, "main_only.py", "from main\n")
    git(path, "merge", "-q", "--no-edit", "main")
    assert load_review(path, [], reviewed=reviewed).since_review == []
    (path / "mine.py").write_text("two\n")
    (path / "mine.py").unlink()  # work reverted since the review is news too
    (later,) = load_review(path, [], reviewed=reviewed).since_review
    assert later.path == "mine.py" and later.operation == "deleted"


def test_a_sessions_refs_go_with_the_session_or_its_worktree(repo, tmp_path):
    from pcode.sessions import SavedSession, delete_session

    linked = worktree.create(repo, "pcode-refs")
    root = tmp_path / "sessions"
    tree = load_review(linked.path, []).checkpoint
    gone = SavedSession.create("test:local", linked.path, root)
    git_diff.mark_reviewed(linked.path, gone.info.id, tree)
    gone.close()
    delete_session(gone.info.id, root)
    assert git(repo, "for-each-ref", "refs/pcode") == ""

    kept = SavedSession.create("test:local", linked.path, root)
    git_diff.mark_reviewed(linked.path, kept.info.id, tree)
    git_diff.mark_reviewed(linked.path, "other-session", tree)
    # Leaving an untouched worktree removes it, and the review of it.
    worktree.leave_worktree(linked.path, kept, ask=None, notify=lambda _: None)
    remaining = git(repo, "for-each-ref", "--format=%(refname)", "refs/pcode").split()
    assert remaining == [
        f"refs/pcode/sessions/other-session/{n}" for n in ("review-base", "reviewed")
    ]
    git_diff.forget_session(repo, "other-session")
    assert git(repo, "for-each-ref", "refs/pcode") == ""


def test_only_newlines_split_patch_lines(repo):
    # A form feed or U+2028 inside a line is not a line break to git: splitting
    # there adds lines its hunk counts do not include. A CRLF file's "\r" goes.
    source = "class A:\n    def f(self):\n        x = 1\n        \f\n        s = '\u2028'\n"
    commit(repo, "a.py", source + "        return x\n        y = '\u2028- 2'\n")
    commit(repo, "crlf.txt", "one\r\ntwo\r\n")
    (repo / "a.py").write_text(source + "        return x + 1\n        y = '\u2028+ 2'\n")
    (repo / "crlf.txt").write_bytes(b"one\r\nthree\r\n")

    tree = git_diff._snapshot(repo)
    changes = {change.path: change for change in git_diff._tree_diff(repo, "HEAD", tree, None)}

    shown = patch_text(changes["a.py"].patch, dedent=True).split("\n")
    assert shown[3:] == [
        " x = 1",
        " ",
        " s = ' '",
        "-return x",
        "-y = ' - 2'",
        "+return x + 1",
        "+y = ' + 2'",
    ]
    assert (changes["a.py"].added, changes["a.py"].removed) == (2, 2)
    assert changes["crlf.txt"].patch.split("\n")[3:] == [" one", "-two", "+three"]
