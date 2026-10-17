import io
import sys
from unittest.mock import patch

import pytest
from pydantic_ai.exceptions import UserError
from pydantic_ai.providers.openai_codex import CredentialsRefreshError

from pcode.agent import create_agent
from pcode.app import forked_note, main
from pcode.live import error_message
from pcode.sessions import SavedSession, list_sessions


def test_cli_version(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["pcode", "--version"])
    with pytest.raises(SystemExit) as exit_info:
        main()
    assert exit_info.value.code == 0
    assert capsys.readouterr().out.startswith("pcode 0.")


def test_cli_passes_model_and_workspace(monkeypatch, tmp_path):
    monkeypatch.setenv("PCODE_SESSION_DIR", str(tmp_path / "sessions"))
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "pcode",
            "-m",
            "openai-codex:gpt-5.6-luna",
            "-C",
            str(tmp_path),
        ],
    )
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    with patch("pcode.app.PreviewApp") as app:
        main()
    assert app.call_args.kwargs["theme"] == "auto"
    assert "color_style" not in app.call_args.kwargs
    assert app.call_args.kwargs["model"] == "openai-codex:gpt-5.6-luna"
    assert app.call_args.kwargs["workspace"] == tmp_path
    assert app.call_args.kwargs["saved_session"] is None
    assert app.call_args.kwargs["save"] is True
    assert not (tmp_path / "sessions").exists()
    app.return_value.run.assert_called_once()


def test_continue_restores_model_workspace_and_releases_lock(monkeypatch, tmp_path):
    root = tmp_path / "sessions"
    saved = SavedSession.create("test:local", tmp_path, root)
    identity = saved.info.id
    saved.close()
    monkeypatch.setattr(sys, "argv", ["pcode", "--continue", identity, "--session-dir", str(root)])
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    with patch("pcode.app.PreviewApp") as app:
        main()
    assert app.call_args.kwargs["resume"] is True
    assert app.call_args.kwargs["model"] == "test:local"
    assert app.call_args.kwargs["workspace"] == tmp_path
    reopened = SavedSession.open(identity, root)
    reopened.close()


def test_continue_copies_a_session_open_elsewhere(monkeypatch, tmp_path):
    root = tmp_path / "sessions"
    saved = SavedSession.create("test:local", tmp_path, root)
    saved.append("turn_started", prompt="First question", run_id="r1", parent_id=None)
    saved.append("turn_completed", run_id="r1")
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    argv = ["pcode", "--continue", saved.info.id, "--session-dir", str(root)]
    try:
        monkeypatch.setattr(sys, "argv", argv)
        with patch("pcode.app.PreviewApp") as app:
            main()
        copy = app.call_args.kwargs["saved_session"]
        assert copy.forked_from == saved.info.id
        assert app.call_args.kwargs["resume"] is True
        assert app.call_args.kwargs["workspace"] == tmp_path
        SavedSession.open(copy.info.id, root).close()  # Released on exit.

        # A refused resume removes the copy it made rather than leaving it listed.
        monkeypatch.setattr(sys, "argv", [*argv, "-m", "other:model"])
        with pytest.raises(SystemExit) as raised:
            main()
        assert raised.value.code == 2
        assert len(list(root.iterdir())) == 2
    finally:
        saved.close()


def test_fork_continues_a_copy_of_a_session_nobody_has_open(monkeypatch, tmp_path):
    root = tmp_path / "sessions"
    saved = SavedSession.create("test:local", tmp_path, root)
    identity = saved.info.id
    saved.close()
    monkeypatch.setattr(
        sys, "argv", ["pcode", "--continue", identity, "--fork", "--session-dir", str(root)]
    )
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    with patch("pcode.app.PreviewApp") as app:
        main()
    copy = app.call_args.kwargs["saved_session"]
    assert copy.info.id != identity
    assert copy.forked_from == identity
    assert copy.fork_requested
    assert forked_note(copy).startswith(f"Continuing a copy of session {identity}. ")
    assert {path.name for path in root.iterdir()} == {identity, copy.info.id}
    SavedSession.open(identity, root).close()  # The original stays free.


def test_fork_is_removed_when_startup_fails_after_copying(monkeypatch, tmp_path):
    root = tmp_path / "sessions"
    saved = SavedSession.create("test:local", tmp_path, root)
    saved.close()
    argv = ["pcode", "-c", saved.info.id, "--fork", "--session-dir", str(root)]
    monkeypatch.setattr(sys, "argv", argv)
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    with (
        patch("pcode.project_trust.prompt_trust", side_effect=OSError("no")),
        patch("pcode.app.PreviewApp"),
        pytest.raises(SystemExit),
    ):
        main()
    assert [path.name for path in root.iterdir()] == [saved.info.id]


def test_fork_skips_the_running_host_and_forks_in_a_new_one(monkeypatch, tmp_path):
    root = tmp_path / "sessions"
    saved = SavedSession.create("test:local", tmp_path, root)
    saved.close()
    argv = ["pcode", "-c", saved.info.id, "--fork", "--host", "--session-dir", str(root)]
    monkeypatch.setattr(sys, "argv", [*argv, "-C", str(tmp_path)])
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    with (
        patch("pcode.app._running_host", side_effect=AssertionError("attached")),
        patch("pcode.project_trust.prompt_trust"),
        patch("pcode.remote.spawn_host", return_value=("h1", None, tmp_path / "log")) as spawn,
        patch("pcode.app._tidy_stopped_host"),
        patch("pcode.app.PreviewApp"),
    ):
        main()
    assert spawn.call_args.kwargs["resume"] == saved.info.id
    assert spawn.call_args.kwargs["fork"] is True


@pytest.mark.parametrize("extra", [[], ["--attach"]])
def test_fork_needs_continue_without_attach(monkeypatch, extra, capsys):
    argv = ["pcode", "--fork", *extra] if not extra else ["pcode", "-c", "--fork", *extra]
    monkeypatch.setattr(sys, "argv", argv)
    with pytest.raises(SystemExit) as raised:
        main()
    assert raised.value.code == 2
    assert "--fork" in capsys.readouterr().err


def test_no_save_never_creates_session(monkeypatch, tmp_path):
    root = tmp_path / "sessions"
    monkeypatch.setenv("PCODE_SESSION_DIR", str(root))
    monkeypatch.setattr(sys, "argv", ["pcode", "-m", "test:local", "--no-save"])
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    with patch("pcode.app.PreviewApp") as app:
        main()
    assert app.call_args.kwargs["saved_session"] is None
    assert list_sessions(root) == []


def test_continue_rejects_different_workspace(monkeypatch, tmp_path, capsys):
    root = tmp_path / "sessions"
    saved = SavedSession.create("test:local", tmp_path, root)
    identity = saved.info.id
    saved.close()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.setattr(
        sys,
        "argv",
        ["pcode", "--continue", identity, "--session-dir", str(root), "-C", str(elsewhere)],
    )
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    with pytest.raises(SystemExit) as raised:
        main()
    assert raised.value.code == 2
    assert "cross-repo resume" in capsys.readouterr().err


def test_continue_from_a_sibling_worktree_returns_to_the_sessions_own(monkeypatch, tmp_path):
    import subprocess

    from pcode import worktree

    repo = tmp_path / "repo"
    repo.mkdir()
    for args in (("init", "-q", "-b", "main"), ("commit", "-q", "--allow-empty", "-m", "init")):
        subprocess.run(
            ["git", "-C", str(repo), "-c", "user.email=t@x", "-c", "user.name=t", *args],
            check=True,
        )
    own = worktree.create(repo, "pcode-own")
    sibling = worktree.create(repo, "pcode-sibling")
    root = tmp_path / "sessions"
    saved = SavedSession.create("test:local", own.path, root)
    identity = saved.info.id
    saved.close()
    monkeypatch.setattr(
        sys,
        "argv",
        ["pcode", "--continue", identity, "--session-dir", str(root), "-C", str(sibling.path)],
    )
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    with patch("pcode.app.PreviewApp") as app:
        main()
    assert app.call_args.kwargs["workspace"] == own.path


def test_continue_without_session_picks_this_workspaces_latest(monkeypatch, tmp_path, capsys):
    root = tmp_path / "sessions"
    here = tmp_path / "here"
    elsewhere = tmp_path / "elsewhere"
    here.mkdir()
    elsewhere.mkdir()
    mine = SavedSession.create("test:local", here, root)
    mine.close()
    # Newer, but another checkout's: it must not win "latest".
    other = SavedSession.create("test:other", elsewhere, root)
    other.close()
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr(
        sys, "argv", ["pcode", "--continue", "--session-dir", str(root), "-C", str(here)]
    )
    with patch("pcode.app.PreviewApp") as app:
        main()
    assert app.call_args.kwargs["saved_session"].info.id == mine.info.id
    assert app.call_args.kwargs["model"] == "test:local"

    empty = tmp_path / "empty"
    empty.mkdir()
    monkeypatch.setattr(
        sys, "argv", ["pcode", "--continue", "--session-dir", str(root), "-C", str(empty)]
    )
    with pytest.raises(SystemExit) as raised:
        main()
    assert raised.value.code == 2
    assert f"No saved session for {empty}" in capsys.readouterr().err


def test_continue_keeps_an_unquoted_prompt_out_of_the_session_selector(monkeypatch, tmp_path):
    root = tmp_path / "sessions"
    saved = SavedSession.create("test:local", tmp_path, root)
    identity = saved.info.id
    saved.close()
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr(
        sys,
        "argv",
        ["pcode", "-c", "fix", "the", "bug", "--session-dir", str(root), "-C", str(tmp_path)],
    )
    with patch("pcode.app.PreviewApp") as app:
        main()
    assert app.call_args.kwargs["initial_prompt"] == "fix the bug"
    assert app.call_args.kwargs["saved_session"].info.id == identity


def test_theme_preview_never_initializes_a_provider(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["pcode", "--theme-preview", "-m", "invalid:provider"])
    with patch("pcode.agent.create_agent") as create:
        main()
    create.assert_not_called()
    output = capsys.readouterr().out
    assert "no model connected" in output
    # The gallery is the reason the sample exists; every style has to appear.
    for style in ("monokai", "gruvbox-dark", "solarized-light"):
        assert style in output
    assert "/config set syntax_light NAME" in output


def test_cli_passes_initial_prompt_to_interactive_app(monkeypatch, tmp_path):
    monkeypatch.setenv("PCODE_SESSION_DIR", str(tmp_path / "sessions"))
    monkeypatch.setattr(sys, "argv", ["pcode", "-m", "test:local", "fix the failing test"])
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    with patch("pcode.app.PreviewApp") as app:
        main()
    assert app.call_args.kwargs["initial_prompt"] == "fix the failing test"
    assert app.call_args.kwargs["console"] is None
    app.return_value.run.assert_called_once()
    app.return_value.run_print.assert_not_called()


def test_print_runs_without_a_terminal_and_exits_with_turn_status(monkeypatch, tmp_path):
    monkeypatch.setenv("PCODE_SESSION_DIR", str(tmp_path / "sessions"))
    monkeypatch.setattr(sys, "argv", ["pcode", "-m", "test:local", "--print", "summarize"])
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: False)
    with patch("pcode.app.PreviewApp") as app:
        app.return_value.run_print.return_value = True
        main()
    assert app.call_args.kwargs["initial_prompt"] == "summarize"
    assert app.call_args.kwargs["console"].stderr is True
    app.return_value.run_print.assert_called_once_with("summarize")
    app.return_value.run.assert_not_called()
    with patch("pcode.app.PreviewApp") as app:
        app.return_value.run_print.return_value = False
        with pytest.raises(SystemExit) as raised:
            main()
    assert raised.value.code == 1


def test_print_reads_prompt_from_stdin_when_omitted(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("PCODE_SESSION_DIR", str(tmp_path / "sessions"))
    monkeypatch.setattr(sys, "argv", ["pcode", "-m", "test:local", "-p"])
    monkeypatch.setattr(sys.stdout, "isatty", lambda: False)
    monkeypatch.setattr(sys, "stdin", io.StringIO("piped question\n"))
    with patch("pcode.app.PreviewApp") as app:
        app.return_value.run_print.return_value = True
        main()
    app.return_value.run_print.assert_called_once_with("piped question\n")
    monkeypatch.setattr(sys, "stdin", io.StringIO("   \n"))
    with pytest.raises(SystemExit) as raised:
        main()
    assert raised.value.code == 2
    assert "non-empty prompt" in capsys.readouterr().err


def test_print_without_prompt_on_a_terminal_is_an_error(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["pcode", "-m", "test:local", "-p"])
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    with pytest.raises(SystemExit) as raised:
        main()
    assert raised.value.code == 2
    assert "PROMPT argument or text on stdin" in capsys.readouterr().err


def test_invalidated_login_shows_safe_error_code_only():
    error = CredentialsRefreshError("HTTP 401: refresh_token_invalidated; sensitive-body-sentinel")
    message = error_message(error)
    assert "refresh_token_invalidated" in message
    assert "codex login" in message
    assert "sensitive-body-sentinel" not in message


def test_missing_login_has_actionable_message_without_dumping_auth(monkeypatch, tmp_path):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    with pytest.raises(UserError) as raised:
        create_agent("openai-codex:gpt-5.6-luna", tmp_path)
    assert error_message(raised.value) == (
        "Provider login missing or invalid. "
        "Run `/login openai-codex` (or `codex login`, then restart pcode)."
    )


def test_help_hides_old_aliases_and_legacy_flags(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["pcode", "--help"])
    with pytest.raises(SystemExit):
        main()
    shown = capsys.readouterr().out
    assert "--theme-preview" in shown
    assert "--demo" not in shown


def test_theme_preview_cli_passes_the_theme(monkeypatch):
    # `--demo` is the old name for this flag, kept so scripts keep working.
    monkeypatch.setattr(sys, "argv", ["pcode", "--demo", "--theme", "light"])
    with patch("pcode.app.PreviewApp") as app:
        main()
    app.assert_called_once_with(theme="light")
    app.return_value.transcript.events.assert_called_once()
    app.return_value.transcript.syntax_gallery.assert_called_once_with()
