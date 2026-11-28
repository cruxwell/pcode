"""`pcode --host --spawn-only`, which Pcode.app's native composer starts a pane's host with."""

import json
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from pcode import project_trust
from pcode.app import main
from pcode.host_protocol import HostEntry
from pcode.sessions import SavedSession


def run(monkeypatch, capsys, *argv) -> tuple[int, dict]:
    monkeypatch.setattr(sys, "argv", ["pcode", "--host", "--spawn-only", *argv])
    with pytest.raises(SystemExit) as exit_info:
        main()
    out = capsys.readouterr().out.strip().splitlines()
    assert len(out) == 1, out  # Exactly one JSON line: the app parses stdout whole.
    return exit_info.value.code, json.loads(out[0])


def fake_spawn(identity="ab12cd34"):
    process = MagicMock(pid=4321)
    return patch(
        "pcode.remote.spawn_host",
        return_value=(identity, process, Path("/tmp/hosts") / f"{identity}.log"),
    )


@pytest.fixture
def host_dir(monkeypatch):
    # pytest's tmp_path is too long for a Unix socket path on macOS.
    with tempfile.TemporaryDirectory(dir="/tmp") as directory:
        monkeypatch.setenv("PCODE_HOST_DIR", directory)
        yield Path(directory)


def test_spawns_a_host_and_prints_where_it_is(monkeypatch, capsys, tmp_path, host_dir):
    monkeypatch.setenv("PCODE_HOST_ID", "0badf00d")
    with fake_spawn("0badf00d") as spawn:
        code, reply = run(monkeypatch, capsys, "-C", str(tmp_path), "--no-worktree")
    assert code == 0
    assert reply == {
        "id": "0badf00d",
        "socket": str(host_dir / "0badf00d.sock"),
        "log": "/tmp/hosts/0badf00d.log",
        "pid": 4321,
        "existing": False,
    }
    kwargs = spawn.call_args.kwargs
    # The id the app picked, so closing the pane stops this host before it reports itself.
    assert kwargs["identity"] == "0badf00d"
    assert kwargs["workspace"] == tmp_path.resolve()
    assert kwargs["no_worktree"] is True
    assert kwargs["resume"] is None
    # No model configured: the host runs the canned preview until /model.
    assert kwargs["model"] is None


def test_continue_resumes_in_the_sessions_workspace(monkeypatch, capsys, tmp_path):
    root = tmp_path / "sessions"
    saved = SavedSession.create("test:local", tmp_path, root)
    identity = saved.info.id
    saved.close()
    with fake_spawn() as spawn:
        code, reply = run(monkeypatch, capsys, "-c", identity, "--session-dir", str(root))
    assert code == 0 and reply["existing"] is False
    kwargs = spawn.call_args.kwargs
    assert kwargs["resume"] == identity
    assert kwargs["model"] == "test:local"
    assert kwargs["workspace"] == tmp_path.resolve()


def test_continue_attaches_to_the_host_already_holding_it(monkeypatch, capsys, host_dir):
    entry = HostEntry(
        id="feedbeef",
        pid=99,
        model="",
        workspace="/tmp",
        session_id="s1",
        log="/tmp/feedbeef.log",
    )
    with (
        patch("pcode.app._running_host", return_value=entry),
        fake_spawn() as spawn,
    ):
        code, reply = run(monkeypatch, capsys, "-c", "s1")
    assert code == 0
    spawn.assert_not_called()
    assert reply == {
        "id": "feedbeef",
        "socket": str(host_dir / "feedbeef.sock"),
        "log": "/tmp/feedbeef.log",
        "pid": 99,
        "existing": True,
    }


def make_untrusted_repo(path: Path) -> Path:
    path.mkdir()
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    (path / ".pcode").mkdir()
    (path / ".pcode" / "worktree-setup").write_text("true\n")
    return path.resolve()


def test_untrusted_project_code_is_asked_about_before_spawning(monkeypatch, capsys, tmp_path):
    repo = make_untrusted_repo(tmp_path / "repo")
    with fake_spawn() as spawn:
        code, reply = run(monkeypatch, capsys, "-C", str(repo))
    assert code == 0
    spawn.assert_not_called()
    assert reply == {"trust": {"key": str(repo), "code": [".pcode/worktree-setup"]}}
    assert not project_trust.is_trusted(repo)


def test_trust_answer_records_trust_and_spawns(monkeypatch, capsys, tmp_path):
    repo = make_untrusted_repo(tmp_path / "repo")
    monkeypatch.setattr(sys, "argv", ["pcode", "--host", "--spawn-only=trust", "-C", str(repo)])
    with fake_spawn() as spawn, pytest.raises(SystemExit) as exit_info:
        main()
    assert exit_info.value.code == 0
    assert json.loads(capsys.readouterr().out)["existing"] is False
    spawn.assert_called_once()
    assert project_trust.is_trusted(repo)


def test_skip_answer_spawns_without_trusting(monkeypatch, capsys, tmp_path):
    repo = make_untrusted_repo(tmp_path / "repo")
    monkeypatch.setattr(sys, "argv", ["pcode", "--host", "--spawn-only=skip", "-C", str(repo)])
    with fake_spawn() as spawn, pytest.raises(SystemExit):
        main()
    captured = capsys.readouterr()
    assert json.loads(captured.out)["id"] == "ab12cd34"
    assert "skipping untrusted project code" in captured.err
    spawn.assert_called_once()
    assert not project_trust.is_trusted(repo)


def test_failure_is_one_json_error_line(monkeypatch, capsys, tmp_path):
    code, reply = run(monkeypatch, capsys, "-C", str(tmp_path / "missing"))
    assert code == 1
    assert "not an existing directory" in reply["error"]
