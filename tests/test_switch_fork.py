"""`/fork`: a copy of the conversation in a host and worktree of its own."""

import asyncio
import subprocess
from io import StringIO
from pathlib import Path

import pytest
from prompt_toolkit.output import DummyOutput
from rich.console import Console

from pcode.app import PreviewApp
from pcode.host import fork_worktree
from pcode.session_ui import SessionBrowser, fork_forest
from pcode.sessions import SavedSession, SessionInfo, list_sessions, read_info


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(cwd), *args], capture_output=True, text=True, check=True
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path, monkeypatch):
    path = (tmp_path / "repo").resolve()
    path.mkdir()
    git(path, "init", "-q", "-b", "main")
    git(path, "config", "user.email", "t@example.com")
    git(path, "config", "user.name", "t")
    (path / "README").write_text("hello\n")
    git(path, "add", "README")
    git(path, "commit", "-q", "-m", "init")
    monkeypatch.delenv("GIT_DIR", raising=False)
    return path


def info(identity: str, updated: str, forked_from: str | None = None) -> SessionInfo:
    return SessionInfo(
        id=identity,
        model="m",
        workspace="/w",
        created=updated,
        updated=updated,
        forked_from=forked_from,
        packages={},
    )


def test_a_copy_records_the_session_it_came_from(tmp_path):
    root = tmp_path / "sessions"
    saved = SavedSession.create("test:local", tmp_path, root)
    saved.close()
    copy = SavedSession.open(saved.info.id, root, fork=True)
    copy.close()
    assert read_info(copy.directory).forked_from == saved.info.id
    # The original is no fork, and a copy of a copy names its own original.
    assert read_info(saved.directory).forked_from is None
    again = SavedSession.open(copy.info.id, root, fork=True)
    again.close()
    assert read_info(again.directory).forked_from == copy.info.id


def test_fork_forest_nests_forks_under_their_original():
    records = [
        info("c", "2026-01-05", forked_from="a"),
        info("b", "2026-01-04"),
        info("d", "2026-01-03", forked_from="c"),
        info("e", "2026-01-02", forked_from="a"),
        info("a", "2026-01-01"),
        info("orphan", "2025-12-31", forked_from="deleted"),
    ]
    rows = [(row.id, guide) for row, guide in fork_forest(records)]
    # `a`'s family is placed by `c`, its newest member, so it leads.
    assert rows == [
        ("a", ""),
        ("c", "├─ "),
        ("d", "│  └─ "),
        ("e", "└─ "),
        ("b", ""),
        ("orphan", ""),
    ]


def test_fork_forest_lists_a_cycle_rather_than_losing_it():
    records = [info("a", "2", forked_from="b"), info("b", "1", forked_from="a")]
    assert [row.id for row, _ in fork_forest(records)] == ["a", "b"]


def test_resume_shows_forks_as_a_tree_and_flattens_to_search(tmp_path):
    root = tmp_path / "sessions"
    original = SavedSession.create("test:local", tmp_path, root)
    original.append("turn_started", prompt="Plan the parser")
    original.append("turn_completed")
    original.close()
    other = SavedSession.create("test:local", tmp_path, root)
    other.append("turn_started", prompt="Unrelated")
    other.append("turn_completed")
    other.close()
    copy = SavedSession.open(original.info.id, root, fork=True)
    copy.close()
    browser = SessionBrowser(
        list_sessions(root), root=root, workspace=tmp_path, output=DummyOutput()
    )
    assert [row.id for row in browser.visible] == [
        original.info.id,
        copy.info.id,
        other.info.id,
    ]
    rows = browser.list.text.splitlines()
    assert f"└─ {copy.info.id[:8]}  Plan the parser" in rows[1]
    assert rows[0].index(original.info.id[:8]) == rows[2].index(other.info.id[:8])

    browser.query.text = "parser"
    assert "└─" not in browser.list.text
    browser.query.text = ""
    assert "└─" in browser.list.text
    heading = browser.heading(read_info(copy.directory), 1, 1)
    assert f"forked from {original.info.id[:8]}" in heading


def test_fork_worktree_starts_from_the_originals_commit(repo, tmp_path):
    from pcode.app import _enter_worktree

    parent, _ = _enter_worktree(repo, "parent", always=True)
    (parent / "work.txt").write_text("done\n")
    git(parent, "add", "work.txt")
    git(parent, "commit", "-q", "-m", "parent work")

    root = tmp_path / "sessions"
    saved = SavedSession.create("test:local", parent, root)
    saved.close()
    copy = SavedSession.open(saved.info.id, root, fork=True)
    try:
        created = fork_worktree(copy, parent)
    finally:
        copy.close()
    assert created.name == f"pcode-{copy.info.id[:8]}"
    head = git(parent, "rev-parse", "HEAD")
    assert git(created, "rev-parse", "HEAD") == head
    assert (created / "work.txt").exists()
    recorded = read_info(copy.directory)
    assert recorded.workspace == str(created)
    # Its own work, for /diffs and tidying, starts at the fork point.
    assert recorded.start_commit == head
    assert read_info(saved.directory).workspace == str(parent.resolve())


def forked_worktree(repo, tmp_path):
    """A fork of a session whose worktree holds a commit the mainline lacks."""
    from pcode.app import _enter_worktree

    parent, _ = _enter_worktree(repo, "parent", always=True)
    (parent / "work.txt").write_text("done\n")
    git(parent, "add", "work.txt")
    git(parent, "commit", "-q", "-m", "parent work")
    root = tmp_path / "sessions"
    saved = SavedSession.create("test:local", parent, root)
    saved.info.turns = 1
    saved.save_info()
    saved.close()
    copy = SavedSession.open(saved.info.id, root, fork=True)
    return fork_worktree(copy, parent), copy


def test_a_fork_that_did_nothing_is_tidied_away_on_exit(repo, tmp_path):
    from pcode.worktree import leave_worktree

    created, copy = forked_worktree(repo, tmp_path)
    notes = []
    try:
        leave_worktree(created, copy, ask=None, notify=notes.append)
    finally:
        copy.close()
    assert not created.exists()
    assert f"pcode-{copy.info.id[:8]}" not in git(repo, "branch")
    # The original's commit is still on its own branch, and never reached main.
    assert "parent work" in git(repo, "log", "--oneline", "pcode-parent")
    assert "parent work" not in git(repo, "log", "--oneline", "main")


def test_a_fork_with_work_of_its_own_is_kept(repo, tmp_path):
    from pcode.worktree import leave_worktree

    created, copy = forked_worktree(repo, tmp_path)
    (created / "fork.txt").write_text("mine\n")
    git(created, "add", "fork.txt")
    git(created, "commit", "-q", "-m", "fork work")
    notes = []
    try:
        leave_worktree(created, copy, ask=None, notify=notes.append)
    finally:
        copy.close()
    assert created.exists()
    assert "unmerged commit" in notes[-1]


def test_fork_worktree_outside_git_shares_the_directory(tmp_path):
    root = tmp_path / "sessions"
    saved = SavedSession.create("test:local", tmp_path, root)
    saved.close()
    copy = SavedSession.open(saved.info.id, root, fork=True)
    try:
        assert fork_worktree(copy, tmp_path) == tmp_path
    finally:
        copy.close()


class Busy:
    """A session running in this terminal, mid-turn."""

    remote = False

    def __init__(self, session=None):
        self.session = session


def app_with(runtime) -> PreviewApp:
    app = PreviewApp(model="m", runtime=runtime, console=Console(file=StringIO()))
    app.activity.busy = True
    return app


def test_fork_with_a_prompt_leaves_a_busy_session_running(tmp_path):
    saved = SavedSession.create("test:local", tmp_path, tmp_path / "sessions")
    app = app_with(Busy(saved))
    try:
        app.fork("try the other approach")
        assert app.fork_requested == "try the other approach"
        with pytest.raises(ValueError, match="leaving it stops its turn"):
            app.fork("")
        # Once it is due to move into a host, it says the wait is short.
        app.host_when_idle = True
        with pytest.raises(ValueError, match="then moves into a background host"):
            app.fork("")
    finally:
        saved.close()


def test_switch_no_longer_forks(tmp_path):
    saved = SavedSession.create("test:local", tmp_path, tmp_path / "sessions")
    app = app_with(Busy(saved))
    app.activity.busy = False
    try:
        app.switch("fork")
        # Read as a host ID prefix, like any other word.
        assert app.switch_requested == "fork"
        assert app.fork_requested is None
    finally:
        saved.close()


def test_forking_starts_a_host_on_a_copy_in_its_own_worktree(tmp_path):
    from unittest.mock import patch

    root = tmp_path / "sessions"
    saved = SavedSession.create("test:local", tmp_path, root)
    app = app_with(Busy(saved))
    with (
        patch("pcode.remote.spawn_host", side_effect=RuntimeError("stop here")) as spawn,
        pytest.raises(RuntimeError, match="stop here"),
    ):
        asyncio.run(app.start_host_session("go", resume=saved.info.id, fork=True))
    saved.close()
    options = spawn.call_args.kwargs
    assert (options["resume"], options["fork"], options["worktree"]) == (
        saved.info.id,
        True,
        True,
    )


def test_fork_session_forks_the_active_session(tmp_path):
    root = tmp_path / "sessions"
    saved = SavedSession.create("test:local", tmp_path, root)
    app = app_with(Busy(saved))
    app.controller.workspace = tmp_path
    started = []

    async def start_host_session(prompt="", *, resume=None, note=None, fork=False):
        started.append((prompt, resume, fork))

    app.start_host_session = start_host_session
    try:
        asyncio.run(app.fork_session("go"))
    finally:
        saved.close()
    assert started == [("go", saved.info.id, True)]


def test_fork_needs_a_saved_conversation():
    app = app_with(Busy())
    app.activity.busy = False
    with pytest.raises(ValueError, match="Nothing to fork yet"):
        app.fork("")
    with pytest.raises(ValueError, match="Nothing to fork yet"):
        app.fork("go")


@pytest.mark.parametrize(
    "argument,expected",
    [
        ("new", ("new", "")),
        ("new\ttry  this", ("new", "try  this")),
        ("new go", ("new", "go")),
        ("fork", None),
        ("abcd1234", None),
        ("forked", None),
        ("", None),
    ],
)
def test_session_start_reads_new(argument, expected):
    from pcode.app import _session_start

    assert _session_start(argument) == expected


def test_the_move_into_a_host_waits_for_an_idle_session(tmp_path):
    saved = SavedSession.create("test:local", tmp_path, tmp_path / "sessions")
    app = app_with(Busy(saved))
    queued = []
    app.controller.command = lambda text, tag=None: queued.append(text)
    try:
        app.hand_off_when_idle()
        assert queued == [], "not asked for"
        app.host_when_idle = True
        app.hand_off_when_idle()
        assert queued == [], "mid-turn: a turn cannot move"
        app.activity.busy = False
        app.hand_off_when_idle()
        app.hand_off_when_idle()
        assert queued == ["/detach"], "queued once"
        # Busy again by the time it runs: it waits for the next turn's end, quietly.
        app.activity.busy = True
        app.detach("")
        assert not app.handoff_requested and not app.handoff_queued
        app.activity.busy = False
        app.hand_off_when_idle()
        app.detach("")
        assert app.handoff_requested
    finally:
        saved.close()
