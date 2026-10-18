import json
import sys
from concurrent.futures import ThreadPoolExecutor
from io import StringIO
from unittest.mock import patch

import pytest
from prompt_toolkit.completion import CompleteEvent
from prompt_toolkit.document import Document
from rich.console import Console

from pcode.app import PreviewApp, main
from pcode.commands import SlashCompleter
from pcode.config import SETTINGS, config_arguments, configure
from pcode.preferences import (
    load_preferences,
    preferences_path,
    read_preferences,
    save_model_effort,
    save_preferences,
    set_project_root,
    update_preferences,
)
from pcode.ui import Activity


def test_defaults_and_path_do_not_create_files():
    assert configure(["path"]) == str(preferences_path())
    assert json.loads(configure([])) == {
        "model_providers": "",
        "send_mode": "steering",
        "repo_context_walk_up": "on",
        "repo_context_nested": "off",
        "skill_commands": "prefix",
        "skill_dirs": "~/.agents/skills:.agents/skills",
        "project_extensions": "off",
        "trusted_projects": "",
        "extension_dirs": "",
        "extensions_off": "",
        "extensions_on": "",
        "worktree": "off",
        "worker_isolation": "off",
        "worker_concurrency": "0",
        "subagent_models": "",
        "session_host": "off",
        "session_host_idle_minutes": "0",
        "email_owner": None,
        "email_mcp": "off",
        "email_turn_minutes": "30",
        "email_turn_requests": "100",
        "email_turn_tool_calls": "100",
        "email_concurrent_sessions": "2",
        "email_max_sessions": "20",
        "email_session_inputs": "20",
        "email_max_inputs": "100",
        "claude_idle_processes": "1",
        "claude_idle_minutes": "10",
        "desktop_notifications": "on",
        # conftest's test defaults; both ship "on".
        "session_naming": "off",
        "terminal_title": "off",
        "terminal_progress": "auto",
        "worktree_exit": "ask",
        "retry_attempts": "3",
        "tool_retries": "3",
        "strict_tools": "on",
        "cache_notices": "on",
        "debug": "off",
        "profile": "off",
        "stall_log": "on",
        "spinner": "dots",
        "tool_spinner": "arc",
        "show_thinking": "activity-panel",
        "thinking_max_lines": "10",
        "show_activity": "on",
        "show_tasks": "on",
        "autohide_tasks": "on",
        # conftest's override; the shipped thresholds are 30 and 100.
        "tasks_min_rows": "0",
        "tasks_min_columns": "0",
        "show_hints": "off",  # conftest's test default; the shipped one is "on".
        "attach_tasks": "on",
        "attach_activity": "on",
        "task_style": "status",
        "frame_style": "borders",  # conftest's test default; the shipped one is "shaded".
        "tool_glyphs": "auto",
        "glyphs": "auto",
        "color_depth": "auto",
        "tool_max_lines": "3",
        "tool_linger_seconds": "10",
        "tasks_max_height": None,
        "transcript_max_chars": "2000000",
        "error_scrollback_lines": "20",
        "tool_error_scrollback": "off",
        "regenerate_on_resize": "on",
        "paced_scrollback": "typed",
        "show_commands": "off",
        "show_edits": "on",
        "live_edits": "off",
        "diff_renderer": "delta",
        "delta_args": "",
        "diff_layout": "unified",
        "diff_dedent": "on",
        "group_tools": "off",
        "command_scrollback_lines": "20",
        "command_preview_lines": "10",
        "theme": "auto",
        "syntax_dark": "terminal",
        "syntax_light": "terminal",
        "editing_mode": "emacs",
        "vi_escape_sequence": "escape",
        "vi_wrapped_rows": "on",
        "vi_key_prefix": "off",
        "autocompact": "on",
        "autocompact_tokens": None,
        "key_prefix": "ctrl",
        "popup_mouse": "on",
        "btw_auto_open": "on",
        "btw_view": "swap",
        "job_wake": "on",
        "code_mode": "off",
        "web_search": "auto",
        "web_fetch": "auto",
        "tool_output_mode": "spill",
        "tool_output_threshold": "10000",
        "tool_output_preview_chars": "1000",
        "tool_output_max_chars": "4000",
        "tool_output_strategy": "head_tail",
        "tool_output_retention_hours": "0",
        "effort": "default",
        "model": None,
    }
    assert configure(["get", "model"]) == "null"
    assert not preferences_path().parent.exists()


@pytest.mark.parametrize(
    "key,value",
    [
        ("repo_context_walk_up", "on"),
        ("repo_context_walk_up", "off"),
        ("worker_isolation", "on"),
        ("worker_isolation", "off"),
        ("repo_context_nested", "off"),
        ("repo_context_nested", "pointer"),
        ("repo_context_nested", "contents"),
        ("transcript_max_chars", "4000000"),
        ("theme", "light"),
        ("editing_mode", "vi"),
        ("editing_mode", "emacs"),
        ("vi_escape_sequence", "escape"),
        ("vi_escape_sequence", "jj"),
        ("vi_escape_sequence", "jk"),
        ("vi_key_prefix", "off"),
        ("vi_key_prefix", "<space>"),
        ("vi_key_prefix", ","),
        ("autocompact", "on"),
        ("autocompact_tokens", "200000"),
        ("autocompact_tokens", "200k"),
        ("attach_tasks", "on"),
        ("attach_tasks", "off"),
        ("attach_activity", "off"),
        ("paced_scrollback", "off"),
        ("paced_scrollback", "rows"),
        ("effort", "high"),
        ("skill_dirs", "~/.agents/skills:/opt/skills"),
        ("skill_dirs", ""),
        ("extensions_off", "browser,web_research"),
        ("extensions_off", ""),
        ("model", "test:local"),
    ],
)
def test_set_get_unset(key, value):
    save_preferences(theme="dark")
    assert "next launch" in configure(["set", key, value])
    assert configure(["get", key]) == value
    assert load_preferences()[key] == value
    assert "Reset global default" in configure(["unset", key])
    assert key not in read_preferences()
    default = SETTINGS[key].default
    assert configure(["get", key]) == ("null" if default is None else default)
    if key != "theme":
        assert load_preferences()["theme"] == "dark"


def test_diff_reports_only_changed_settings(tmp_path):
    assert configure(["diff"]) == "Every setting is at its default."
    save_preferences(theme="auto", effort="high")
    save_model_effort("test:local", "low")
    set_project_root(tmp_path)
    configure(["project", "set", "worktree", "on"])
    lines = configure(["diff"]).splitlines()
    assert "effort = high (default default, from user)" in lines
    assert "worktree = on (default off, from project)" in lines
    assert "model_efforts = test:local=low (default none, from user)" in lines
    # A stored value equal to the default is not a difference.
    assert not [line for line in lines if line.startswith("theme ")]
    with pytest.raises(ValueError):
        configure(["diff", "extra"])
    assert "diff" in config_arguments()


def test_reset_clears_known_settings_and_keeps_unknown_keys():
    path = preferences_path()
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"future": {"nested": True}, "theme": "light", "effort": "high"}))
    message = configure(["reset"])
    assert "effort, theme" in message
    assert read_preferences() == {"future": {"nested": True}}
    assert json.loads(configure(["list"]))["theme"] == "auto"
    assert configure(["reset"]) == "No global defaults to reset."


def test_reset_names_the_settings_whose_loss_needs_action():
    save_preferences(trusted_projects="/repo")
    message = configure(["reset"])
    assert "trusted again" in message
    assert read_preferences() == {}


def test_reset_without_a_file_writes_nothing():
    assert configure(["reset"]) == "No global defaults to reset."
    assert not preferences_path().exists()


@pytest.mark.parametrize(
    "args",
    [
        ["set", "repo_context_walk_up", "true"],
        ["set", "repo_context_nested", "on"],
        ["set", "unknown", "value"],
        ["get", "unknown"],
        ["unset", "unknown"],
        ["set", "theme", "blue"],
        ["set", "editing_mode", "vim"],
        ["set", "vi_key_prefix", ""],
        ["set", "vi_key_prefix", "jj"],
        ["set", "vi_key_prefix", " "],
        ["set", "vi_key_prefix", "ctrl+b"],
        ["set", "vi_escape_sequence", ""],
        ["set", "vi_escape_sequence", "j j"],
        ["set", "vi_escape_sequence", "\u001b"],
        ["set", "autocompact", "true"],
        ["set", "attach_tasks", "true"],
        ["set", "effort", "max"],
        ["set", "model", ""],
        ["set", "model", "a b"],
        ["set", "skill_dirs", "a::b"],
        ["set", "extensions_off", "a,,b"],
        ["set", "extensions_on", "two words"],
        ["set", "theme"],
        ["list", "extra"],
        ["path", "extra"],
        ["reset", "extra"],
        ["unset"],
        ["bad"],
    ],
)
def test_invalid_commands_do_not_write(args):
    with pytest.raises(ValueError):
        configure(args)
    assert not preferences_path().exists()


@pytest.mark.parametrize("content", ["{", "[]", "null"])
def test_broken_file_is_reported_and_never_overwritten(content):
    path = preferences_path()
    path.parent.mkdir(parents=True)
    path.write_text(content)
    for args in (
        ["list"],
        ["get", "theme"],
        ["set", "theme", "light"],
        ["unset", "theme"],
        ["reset"],
    ):
        with pytest.raises(ValueError):
            configure(args)
        assert path.read_text() == content
    with pytest.raises(ValueError):
        save_preferences(theme="light")
    assert path.read_text() == content
    # Discovery and normal startup still work with malformed JSON.
    assert configure(["path"]) == str(path)
    assert load_preferences() == {}


def test_unknown_keys_survive_config_and_shortcut_writes():
    path = preferences_path()
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"future": {"nested": True}, "theme": "light"}))
    configure(["set", "effort", "high"])
    configure(["unset", "theme"])
    save_preferences(model="test:local")
    assert read_preferences() == {
        "future": {"nested": True},
        "effort": "high",
        "model": "test:local",
    }
    assert "future" not in json.loads(configure([]))


def test_invalid_saved_values_report_builtin_defaults():
    path = preferences_path()
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"theme": [], "effort": 42, "autocompact": True, "model": ""}))
    listed = json.loads(configure([]))
    assert listed == {key: SETTINGS[key].default for key in listed}
    assert set(listed) == set(SETTINGS)


def test_failed_atomic_write_keeps_old_file_and_cleans_temporary_file():
    save_preferences(theme="dark")
    with patch("pcode.preferences.os.replace", side_effect=PermissionError("denied")):
        with pytest.raises(PermissionError):
            configure(["set", "theme", "light"])
    assert load_preferences()["theme"] == "dark"
    assert {path.name for path in preferences_path().parent.iterdir()} == {
        "preferences.json",
        "preferences.json.lock",
    }


def test_concurrent_updates_do_not_lose_keys():
    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(lambda i: update_preferences({f"key{i}": str(i)}), range(20)))
    assert read_preferences() == {f"key{i}": str(i) for i in range(20)}


def test_config_cli_works_without_terminal_model_or_session(monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: False)
    monkeypatch.setenv("PCODE_SESSION_DIR", str(tmp_path / "sessions"))
    with patch("pcode.app.PreviewApp") as app, patch("pcode.agent.create_agent") as agent:
        for args in (["set", "theme", "light"], ["get", "theme"], []):
            monkeypatch.setattr(sys, "argv", ["pcode", "config", *args])
            main()
        app.assert_not_called()
        agent.assert_not_called()
    output = capsys.readouterr().out
    assert "Saved global default" in output
    assert "\nlight\n" in output
    assert not (tmp_path / "sessions").exists()


@pytest.mark.parametrize("failure", ["invalid", "io"])
def test_config_cli_errors_exit_nonzero(monkeypatch, capsys, failure):
    monkeypatch.setattr(sys, "argv", ["pcode", "config", "set", "theme", "invalid"])
    if failure == "io":
        monkeypatch.setattr(
            "pcode.app.configure", lambda _: (_ for _ in ()).throw(PermissionError("denied"))
        )
    with pytest.raises(SystemExit) as raised:
        main()
    assert raised.value.code == 2
    assert "Traceback" not in capsys.readouterr().err


def test_slash_config_edits_defaults_not_active_session():
    output = StringIO()
    app = PreviewApp(theme="dark", console=Console(file=output, width=160))
    app.handle("/config set theme light")
    assert app.transcript.theme == "dark"
    assert PreviewApp(console=Console(file=StringIO())).transcript.theme == "light"
    app.handle("/config get theme")
    app.handle("/config path")
    app.handle("/config set autocompact on")  # Works even in offline preview.
    assert load_preferences()["autocompact"] == "on"
    app.handle("/config set theme invalid")
    app.handle('/config set model "unterminated')
    assert "next launch" in output.getvalue()
    assert "theme must be one of" in output.getvalue()
    assert "No closing quotation" in output.getvalue()
    assert load_preferences()["theme"] == "light"


@pytest.mark.parametrize(
    "saved,attached", [(None, True), ("on", True), ("off", False), ("invalid", True)]
)
def test_attach_tasks_startup_default_and_saved_preference(saved, attached):
    assert Activity().attach_tasks is True
    if saved is not None:
        save_preferences(attach_tasks=saved)
    app = PreviewApp(console=Console(file=StringIO()))
    assert app.activity.attach_tasks is attached
    assert configure(["get", "attach_tasks"]) == ("on" if attached else "off")
    assert "/attach-tasks" not in [command.name for command in app.registry.commands]


@pytest.mark.parametrize("setting", ["attach_tasks", "attach_activity"])
@pytest.mark.parametrize("scope", ["", "project "])
def test_slash_config_updates_task_attachment_immediately(tmp_path, scope, setting):
    set_project_root(tmp_path)
    output = StringIO()
    app = PreviewApp(console=Console(file=output, width=160))
    assert getattr(app.activity, setting) is True
    for edit, attached in (
        (f"set {setting} off", False),
        (f"set {setting} on", True),
        (f"set {setting} off", False),
        (f"unset {setting}", True),
        (f"set {setting} off", False),
        ("reset", True),
    ):
        app.handle(f"/config {scope}{edit}")
        assert getattr(app.activity, setting) is attached
        assert getattr(PreviewApp(console=Console(file=StringIO())).activity, setting) is attached
    assert "Layout settings apply immediately." in output.getvalue()
    assert setting not in load_preferences()


def test_slash_config_switches_task_style_immediately(tmp_path):
    set_project_root(tmp_path)
    output = StringIO()
    app = PreviewApp(console=Console(file=output, width=160))
    assert app.activity.task_style == "status"

    def text_style():
        style = app.transcript.prompt_style(app.activity.task_style)
        return style.get_attrs_for_style_str("class:plan.completed")

    assert text_style().dim
    app.handle("/config set tool_glyphs off")
    assert app.activity.tool_glyphs == "off"
    assert app.activity.tool_max_rows == 3
    app.handle("/config set tool_max_lines 5")
    assert app.activity.tool_max_rows == 5
    app.handle("/config set task_style icons")
    assert app.activity.task_style == "icons"
    assert not text_style().dim
    assert PreviewApp(console=Console(file=StringIO())).activity.task_style == "icons"
    app.handle("/config unset task_style")
    assert app.activity.task_style == "status"
    assert text_style().dim
    assert "Layout settings apply immediately." in output.getvalue()


def test_slash_config_toggles_live_edits_immediately(tmp_path):
    set_project_root(tmp_path)
    output = StringIO()
    app = PreviewApp(console=Console(file=output, width=160))
    assert not app.transcript.live_edit_previews
    app.handle("/config set live_edits on")
    assert app.transcript.live_edit_previews
    assert "Layout settings apply immediately." in output.getvalue()
    app.handle("/config unset live_edits")
    assert not app.transcript.live_edit_previews


def test_an_unknown_saved_task_style_falls_back_to_status(tmp_path):
    set_project_root(tmp_path)
    path = preferences_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"task_style": "bogus"}))
    assert PreviewApp(console=Console(file=StringIO())).activity.task_style == "status"


def test_slash_config_task_attachment_respects_project_override(tmp_path):
    set_project_root(tmp_path)
    configure(["project", "set", "attach_tasks", "off"])
    app = PreviewApp(console=Console(file=StringIO()))
    assert app.activity.attach_tasks is False
    app.handle("/config set attach_tasks on")
    assert app.activity.attach_tasks is False
    app.handle("/config project unset attach_tasks")
    assert app.activity.attach_tasks is True


def test_slash_config_io_errors_do_not_crash():
    output = StringIO()
    app = PreviewApp(console=Console(file=output))
    with patch("pcode.app.configure", side_effect=PermissionError("denied")):
        app.handle("/config list")
    assert "Could not access global defaults" in output.getvalue()


def test_shortcut_corrupt_config_keeps_active_selection():
    path = preferences_path()
    path.parent.mkdir(parents=True)
    path.write_text("{")
    output = StringIO()
    app = PreviewApp(console=Console(file=output))
    app.theme("light")
    assert app.transcript.theme == "light"
    assert "Could not save defaults" in output.getvalue()
    assert path.read_text() == "{"


@pytest.mark.parametrize(
    "prefix,expected",
    [
        ("/conf", "/config"),
        ("/config get th", "get theme"),
        ("/config set theme l", "set theme light"),
        ("/config set autocompact o", "set autocompact on"),
        ("/config set attach_tasks o", "set attach_tasks on"),
        ("/config set attach_tasks o", "set attach_tasks off"),
        ("/config project set attach_tasks o", "project set attach_tasks off"),
        ("/config unset ef", "unset effort"),
        ("/config set tool_output_mode s", "set tool_output_mode spill"),
        ("/config set tool_output_strategy t", "set tool_output_strategy tail"),
        ("/config get tool_output_th", "get tool_output_threshold"),
        ("/config set repo_context_walk_up o", "set repo_context_walk_up off"),
        ("/config set repo_context_nested p", "set repo_context_nested pointer"),
        ("/config set repo_context_nested c", "set repo_context_nested contents"),
        ("/config set worker_isolation o", "set worker_isolation on"),
    ],
)
def test_config_completion(prefix, expected):
    app = PreviewApp(console=Console(file=StringIO()))
    completions = SlashCompleter(app.registry).get_completions(Document(prefix), CompleteEvent())
    assert expected in [completion.text for completion in completions]


@pytest.mark.parametrize("value", ["0", "-1", "1.5", "many", "", " 20", "２０"])
def test_error_scrollback_lines_reject_invalid_values(value):
    with pytest.raises(ValueError, match="positive integer"):
        configure(["set", "error_scrollback_lines", value])
    assert configure(["get", "error_scrollback_lines"]) == "20"


def test_error_scrollback_settings_round_trip():
    configure(["set", "error_scrollback_lines", "35"])
    with pytest.raises(ValueError, match="Unknown"):
        configure(["set", "error_scrollback", "off"])
    assert load_preferences()["error_scrollback_lines"] == "35"
    assert "error_scrollback" not in load_preferences()
    configure(["unset", "error_scrollback_lines"])
    assert configure(["get", "error_scrollback_lines"]) == "20"


def test_spinner_setting_picks_the_status_row_animation():
    from pcode.preferences import SPINNERS
    from pcode.ui import PromptLayout

    assert configure(["get", "spinner"]) == "dots"
    assert configure(["get", "tool_spinner"]) == "arc"
    # Every offered spinner keeps the row steady: no emoji, one width per frame.
    assert {"dots", "line", "point"} <= set(SPINNERS)
    assert not {"moon", "clock", "arrow2", "shark"} & set(SPINNERS)
    with pytest.raises(ValueError, match="spinner must be one of"):
        configure(["set", "spinner", "moon"])

    def frame():
        from prompt_toolkit import PromptSession
        from prompt_toolkit.input import create_pipe_input
        from prompt_toolkit.output import DummyOutput

        with create_pipe_input() as pipe:
            session = PromptSession(input=pipe, output=DummyOutput())
        layout = PromptLayout(session, None, None, None)
        return layout.spinner_frame(), layout.tool_spinner_frame()

    main, tool = frame()
    assert main in tuple("⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏")
    assert tool in tuple("◜◠◝◞◡◟")
    configure(["set", "spinner", "line"])
    assert frame()[0] in tuple("-\\|/")
    configure(["set", "tool_spinner", "point"])
    assert frame()[1] in {"∙∙∙", "●∙∙", "∙●∙", "∙∙●"}


@pytest.mark.parametrize("value", ["0", "1", "3"])
@pytest.mark.parametrize("key,default", [("retry_attempts", "3"), ("worker_concurrency", "0")])
def test_whole_number_settings_round_trip(value, key, default):
    assert configure(["get", key]) == default
    configure(["set", key, value])
    assert load_preferences()[key] == value
    configure(["unset", key])
    assert configure(["get", key]) == default


@pytest.mark.parametrize("value", ["0", "5", "off"])
def test_session_host_idle_minutes_takes_minutes_or_off(value):
    configure(["set", "session_host_idle_minutes", value])
    assert load_preferences()["session_host_idle_minutes"] == value


@pytest.mark.parametrize("value", ["-1", "1.5", "many", "", " 1", "１"])
@pytest.mark.parametrize(
    "key,default",
    [("retry_attempts", "3"), ("worker_concurrency", "0"), ("session_host_idle_minutes", "0")],
)
def test_whole_number_settings_reject_invalid_values(value, key, default):
    with pytest.raises(ValueError, match="whole number"):
        configure(["set", key, value])
    assert configure(["get", key]) == default
