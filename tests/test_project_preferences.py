import json
import subprocess
import sys
from unittest.mock import patch

import pytest

from pcode.app import main
from pcode.config import config_argument_descriptions, config_arguments, configure
from pcode.preferences import (
    SETTINGS,
    USER_ONLY,
    load_preferences,
    project_preferences_path,
    read_preferences,
    rejected_project_keys,
    save_preferences,
    set_project_root,
)


def write_project(root, **values):
    path = root / ".pcode" / "preferences.json"
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(values))
    return path


def test_no_root_means_user_file_only(tmp_path):
    save_preferences(theme="light")
    assert project_preferences_path() is None
    assert load_preferences()["theme"] == "light"
    assert rejected_project_keys() == []


def test_project_file_overlays_user_except_user_only_keys(tmp_path):
    save_preferences(theme="light", effort="high", project_extensions="off")
    write_project(
        tmp_path,
        theme="dark",
        worktree="on",
        project_extensions="on",
        extension_dirs="/evil",
        extensions_off="web_research",
        extensions_on="evil",
        trusted_projects="/",
        # A repository must not choose who controls it by email, or loosen its limits.
        **{key: "1" for key in USER_ONLY if key.startswith("email_")},
        bogus="x",
        autocompact="maybe",  # invalid value: ignored, user default stands
    )
    set_project_root(tmp_path)
    assert project_preferences_path() == tmp_path / ".pcode" / "preferences.json"
    prefs = load_preferences()
    assert prefs["theme"] == "dark"
    assert prefs["worktree"] == "on"
    assert prefs["effort"] == "high"
    assert prefs["project_extensions"] == "off"
    assert "extension_dirs" not in prefs
    assert "extensions_off" not in prefs and "extensions_on" not in prefs
    assert "trusted_projects" not in prefs
    assert "email_owner" not in prefs and "email_turn_requests" not in prefs
    assert "autocompact" not in prefs
    assert rejected_project_keys() == sorted(USER_ONLY)


def test_broken_project_file_is_ignored(tmp_path):
    save_preferences(theme="light")
    path = write_project(tmp_path)
    path.write_text("{not json")
    set_project_root(tmp_path)
    assert load_preferences()["theme"] == "light"
    assert rejected_project_keys() == []


def test_configure_project_subcommands(tmp_path):
    with pytest.raises(ValueError, match="No workspace"):
        configure(["project", "set", "theme", "dark"])
    set_project_root(tmp_path)
    assert configure(["project", "path"]) == str(tmp_path / ".pcode" / "preferences.json")
    assert json.loads(configure(["project", "list"])) == {}
    assert "project default" in configure(["project", "set", "worktree", "on"])
    assert json.loads(configure(["project", "list"])) == {"worktree": "on"}
    # Effective views merge the overlay; the user file is untouched.
    assert configure(["get", "worktree"]) == "on"
    assert json.loads(configure(["list"]))["worktree"] == "on"
    assert "worktree" not in read_preferences()
    with pytest.raises(ValueError, match="user-only"):
        configure(["project", "set", "project_extensions", "on"])
    with pytest.raises(ValueError, match="must be one of"):
        configure(["project", "set", "worktree", "sometimes"])
    with pytest.raises(ValueError, match="Unknown setting"):
        configure(["project", "set", "nope", "1"])
    assert "Removed" in configure(["project", "unset", "worktree"])
    assert json.loads(configure(["project", "list"])) == {}
    configure(["project", "set", "worktree", "on"])
    assert "Reset project defaults: worktree" in configure(["project", "reset"])
    assert json.loads(configure(["project", "list"])) == {}
    assert configure(["project", "reset"]) == "No project defaults to reset."
    with pytest.raises(ValueError, match="Usage: config project"):
        configure(["project", "frobnicate"])
    completions = config_arguments()
    assert "reset" in completions
    assert "project reset" in completions
    assert "project set worktree on" in completions
    assert "project set project_extensions on" not in completions


def test_every_setting_describes_itself_in_completions():
    described = config_argument_descriptions()
    for key, setting in SETTINGS.items():
        assert setting.description, key
        assert described[f"set {key}"].startswith(setting.description)
        assert "(default " in described[f"get {key}"]
    assert described["set worktree off"].endswith("(default)")
    assert not described["set worktree on"].endswith("(default)")
    assert described["set model"].endswith("(default unset)")
    for argument in config_arguments():
        assert argument in described, argument


def git(cwd, *args):
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True)


def cli(monkeypatch, *argv):
    monkeypatch.setattr(sys, "argv", ["pcode", *argv])
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)


def test_cli_project_file_turns_worktree_on_for_one_repo(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("PCODE_SESSION_DIR", str(tmp_path / "sessions"))
    repo = (tmp_path / "repo").resolve()
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    git(repo, "config", "user.email", "t@example.com")
    git(repo, "config", "user.name", "t")
    write_project(repo, worktree="on", theme="light", project_extensions="on")
    git(repo, "add", ".pcode")
    git(repo, "commit", "-q", "-m", "prefs")
    other = (tmp_path / "other").resolve()
    other.mkdir()
    git(other, "init", "-q")

    cli(monkeypatch, "-m", "test:local", "-C", str(repo))
    with patch("pcode.app.PreviewApp") as app:
        main()
    assert app.call_args.kwargs["workspace"].parent == repo / ".worktrees"
    assert app.call_args.kwargs["theme"] == "light"
    assert "ignoring user-only settings" in capsys.readouterr().err

    cli(monkeypatch, "-m", "test:local", "-C", str(other))
    with patch("pcode.app.PreviewApp") as app:
        main()
    assert app.call_args.kwargs["workspace"] == other
    assert app.call_args.kwargs["theme"] == "auto"


def test_cli_config_project_uses_workspace_flag(tmp_path, monkeypatch, capsys):
    cli(monkeypatch, "-C", str(tmp_path), "config", "project", "set", "effort", "low")
    main()
    assert "project default" in capsys.readouterr().out
    assert json.loads((tmp_path / ".pcode" / "preferences.json").read_text()) == {"effort": "low"}
    cli(monkeypatch, "config", "project", "list")
    monkeypatch.chdir(tmp_path)
    main()
    assert json.loads(capsys.readouterr().out) == {"effort": "low"}
