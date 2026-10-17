import asyncio
from io import StringIO

import pytest
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput
from rich.console import Console

from pcode.app import PreviewApp
from pcode.command_transcript import SEPARATOR
from pcode.commands import CommandRegistry
from pcode.config import configure
from pcode.preferences import load_preferences, save_preferences
from pcode.runtime import ToolSummary
from pcode.tool_display import label
from pcode.ui import Transcript, create_prompt


def transcript(width=80):
    stream = StringIO()
    return Transcript(Console(file=stream, width=width, color_system=None)), stream


def test_command_mirroring_is_off_by_default():
    view, stream = transcript()
    event = ToolSummary("run_command", "pytest -q → exit 0", command="pytest -q", result="2 passed")
    assert view.command_output(event) is False
    assert view.streams_command(event) is False
    assert stream.getvalue() == ""


def test_disabled_mirroring_still_leaves_a_summary_line():
    view, stream = transcript()
    event = ToolSummary(
        "run_command",
        "pytest -q → exit 0",
        elapsed_seconds=0.25,
        command="pytest -q",
        result="2 passed",
    )
    assert view.writes_tool_result(event)
    view.tool_result(event)
    output = stream.getvalue()
    assert "✓ Run shell" in output
    assert "2 passed" not in output


@pytest.mark.parametrize("name", ["wait_for_job", "job_output"])
@pytest.mark.parametrize("status", ["running", "exit 0", "exit 2", "stopped"])
@pytest.mark.parametrize("show_commands", ["off", "on"])
def test_job_inspection_results_stay_out_of_scrollback(name, status, show_commands):
    save_preferences(show_commands=show_commands, tool_error_scrollback="on")
    view, stream = transcript()
    event = ToolSummary(
        name,
        f"j14 · {status}",
        failed=status == "exit 2",
        outcome="success",
        result=f"test output\n[j14 · {status} · 26.4s]",
    )
    assert not view.writes_tool_result(event)
    view.tool_result(event)
    assert stream.getvalue() == ""
    assert view.replay() == []
    # Presentation suppression does not discard the retained tool result.
    assert view.log.entries[-1].args == (event,)


@pytest.mark.parametrize("name", ["wait_for_job", "job_output"])
@pytest.mark.parametrize("outcome", ["error", "retry"])
@pytest.mark.parametrize("tool_error_scrollback", ["off", "on"])
def test_job_inspection_errors_still_reach_scrollback(name, outcome, tool_error_scrollback):
    save_preferences(tool_error_scrollback=tool_error_scrollback)
    view, stream = transcript()
    event = ToolSummary(
        name,
        "j99 → Failed · No job 'j99'",
        failed=True,
        outcome=outcome,
        result="No job 'j99'",
    )
    assert view.writes_tool_result(event)
    view.tool_result(event)
    assert label(name) in stream.getvalue()
    assert "No job 'j99'" in stream.getvalue()


def test_enabled_option_mirrors_command_and_output():
    save_preferences(show_commands="on")
    view, stream = transcript()
    event = ToolSummary(
        "run_command",
        "pytest -q → exit 0",
        call_id="one",
        elapsed_seconds=0.25,
        command="pytest -q",
        result="[stdout]\n2 passed\n[exit code: 0]",
    )
    assert view.command_output(event) is True
    lines = [line.rstrip() for line in stream.getvalue().splitlines()]
    # The heading rides the opening rule; a plain rule closes the block.
    assert lines[0] == "✓ Run shell · 0.2s " + "─" * 61
    assert lines[-1] == "─" * 80
    assert "  $ pytest -q" in lines
    assert "  2 passed" in lines
    assert "  [exit code: 0]" in lines


def test_job_marker_moves_from_the_footer_into_the_heading():
    save_preferences(show_commands="on")
    view, stream = transcript()
    event = ToolSummary(
        "shell",
        "j27 · exit 0",
        elapsed_seconds=0.25,
        command="make test",
        result="2 passed\n[j27 · exit 0 · 159ms]",
        purpose="running the suite",
    )
    assert view.command_output(event) is True
    lines = [line.rstrip() for line in stream.getvalue().splitlines()]
    assert lines[0].startswith("✓ Run shell · j27 · running the suite · 0.2s ")
    assert "  2 passed" in lines
    # The marker, the exit status and the elapsed time are all in the heading.
    assert "[j27" not in stream.getvalue()


def test_unfinished_job_marker_stays_in_the_mirrored_output():
    save_preferences(show_commands="on")
    view, stream = transcript()
    event = ToolSummary(
        "shell",
        "j3 · still running",
        command="make serve",
        result="booting\n[j3 · running · pid 12 · 2.0s] The wait ended.\nCommand: make serve",
    )
    assert view.command_output(event) is True
    printed = stream.getvalue()
    assert "j3" not in printed.splitlines()[0]
    assert "[j3 · running · pid 12 · 2.0s]" in printed


def test_mirroring_covers_process_tools_and_empty_output():
    save_preferences(show_commands="on")
    view, stream = transcript()
    assert view.command_output(ToolSummary("check_command", "process → running", result="")) is True
    assert "(no output)" in stream.getvalue()
    assert "✓ Check" in stream.getvalue()


def test_mirroring_ignores_tools_without_commands():
    save_preferences(show_commands="on")
    view, stream = transcript()
    event = ToolSummary("read_file", "a.py · 2 lines", result="file body")
    assert view.command_output(event) is False
    assert stream.getvalue() == ""


def test_failed_commands_report_failure_in_the_mirrored_block():
    save_preferences(show_commands="on", tool_error_scrollback="on")
    view, stream = transcript()
    event = ToolSummary(
        "run_command",
        "pytest -q → exit 1",
        failed=True,
        command="pytest -q",
        result="[stderr]\nModuleNotFoundError: example\n[exit code: 1]",
        error="ModuleNotFoundError: example",
    )
    assert view.command_output(event) is True
    assert "✗ Run shell" in stream.getvalue()
    assert "ModuleNotFoundError: example" in stream.getvalue()


def test_mirrored_failure_replaces_the_error_excerpt_block():
    save_preferences(show_commands="on", tool_error_scrollback="on")
    stream = StringIO()
    app = PreviewApp(console=Console(file=stream, width=80, color_system=None))
    event = ToolSummary(
        "run_command",
        "pytest -q → exit 1",
        failed=True,
        call_id="one",
        command="pytest -q",
        result="[stdout]\nkept context\n[exit code: 1]",
        error="FAILED test_example",
    )
    app.present_events((event,))
    output = stream.getvalue()
    assert "✗ Run shell" in output
    assert output.count("✗ Run shell") == 1
    assert "kept context" in output
    # Scrollback is the only record: a settled call leaves the live panel.
    assert app.activity.tools.calls == []


def test_disabled_option_keeps_command_output_out_of_scrollback():
    stream = StringIO()
    app = PreviewApp(console=Console(file=stream, width=80, color_system=None))
    app.present_events(
        (ToolSummary("run_command", "pytest -q → exit 0", command="pytest -q", result="2 passed"),)
    )
    output = stream.getvalue()
    assert "✓ Run shell" in output
    assert "2 passed" not in output


@pytest.mark.parametrize("limit", [1, 3, 20])
def test_command_scrollback_lines_bounds_rows_and_keeps_tail(limit):
    save_preferences(show_commands="on", command_scrollback_lines=str(limit))
    view, stream = transcript(width=45)
    view.command_output(
        ToolSummary(
            "run_command",
            "noisy → exit 0",
            command="noisy",
            result="\n".join(f"line {i}" for i in range(60)) + "\nfinal line",
        )
    )
    lines = stream.getvalue().splitlines()
    assert lines[0] == "✓ Run shell " + "─" * 33
    assert lines[-1] == "─" * 45
    assert len(lines) == limit + 5  # Rules, command, separator, and omission marker.
    assert lines[1].strip() == "$ noisy"
    assert lines[2].strip() == SEPARATOR
    assert f"{61 - limit} earlier output rows omitted" in lines[3]
    assert "earlier error output" not in lines[3]
    assert lines[-2].strip() == "final line"


def test_settings_round_trip_through_config():
    # `on` is the old name for `scrollback`, saved under the new one.
    configure(["set", "show_commands", "on"])
    configure(["set", "command_scrollback_lines", "120"])
    assert load_preferences()["show_commands"] == "scrollback"
    assert load_preferences()["command_scrollback_lines"] == "120"
    assert configure(["get", "show_commands"]) == "scrollback"
    configure(["set", "show_commands", "activity-panel"])
    assert configure(["get", "show_commands"]) == "activity-panel"
    configure(["unset", "show_commands"])
    configure(["unset", "command_scrollback_lines"])
    assert configure(["get", "show_commands"]) == "off"
    assert configure(["get", "command_scrollback_lines"]) == "20"
    with pytest.raises(ValueError):
        configure(["set", "show_commands", "yes"])
    with pytest.raises(ValueError):
        configure(["set", "command_scrollback_lines", "0"])


def test_slash_command_cycles_modes_and_saves_the_default():
    stream = StringIO()
    app = PreviewApp(console=Console(file=stream, width=80, color_system=None))
    assert app.registry.dispatch("/show-commands")
    assert "Commands: activity-panel" in stream.getvalue()
    # Live while it runs, nothing kept afterwards.
    assert app.transcript.command_preview and not app.transcript.command_scrollback
    assert app.registry.dispatch("/show-commands scrollback")
    assert app.transcript.command_scrollback is True
    assert load_preferences()["show_commands"] == "scrollback"
    assert (
        "Commands: scrollback. Usage: /show-commands [off|activity-panel|scrollback]; "
        "bare cycles (Ctrl+G)"
    ) in " ".join(stream.getvalue().split())
    assert app.registry.dispatch("/show-commands off")
    assert not app.transcript.command_preview
    assert load_preferences()["show_commands"] == "off"
    # The old values still work.
    assert app.registry.dispatch("/show-commands on")
    assert app.transcript.command_mode == "scrollback"
    with pytest.raises(ValueError, match=r"Usage: /show-commands"):
        app.registry.dispatch("/show-commands yes")


@pytest.mark.parametrize("vi_mode", [False, True])
def test_ctrl_g_toggles_mirroring_without_starting_a_search_or_inserting_text(vi_mode):
    stream = StringIO()
    app = PreviewApp(console=Console(file=stream, width=80, color_system=None))

    async def run():
        with create_pipe_input() as pipe:
            prompt = create_prompt(
                CommandRegistry(),
                on_commands=lambda: app.show_commands(""),
                vi_mode=vi_mode,
                input=pipe,
                output=DummyOutput(),
            )
            pipe.send_text("\x07\x07\x07\x07draft\r")
            return await asyncio.wait_for(prompt.prompt_async(), timeout=3)

    assert asyncio.run(run()) == "draft"
    assert app.transcript.command_mode == "activity-panel"
    assert load_preferences()["show_commands"] == "activity-panel"
    notes = [line for line in stream.getvalue().splitlines() if "Commands:" in line]
    assert [note.split("Commands: ")[1].split(".")[0] for note in notes] == [
        "activity-panel",
        "scrollback",
        "off",
        "activity-panel",
    ]


def test_toggle_survives_an_unwritable_preferences_file(monkeypatch):
    stream = StringIO()
    app = PreviewApp(console=Console(file=stream, width=80, color_system=None))

    def fail(**updates):
        raise OSError("read-only configuration directory")

    monkeypatch.setattr("pcode.app.save_preferences", fail)
    app.show_commands("")
    assert app.transcript.command_mode == "activity-panel"
    assert "Could not save defaults" in stream.getvalue()
    assert "Commands: activity-panel" in stream.getvalue()


def test_toggled_mirroring_takes_effect_on_the_next_settled_command():
    stream = StringIO()
    app = PreviewApp(console=Console(file=stream, width=80, color_system=None))
    event = ToolSummary(
        "run_command", "echo → exit 0", call_id="one", command="echo hi", result="hi"
    )
    app.present_events((event,))
    assert "$ echo hi" not in stream.getvalue()
    app.show_commands("scrollback")
    app.present_events((event,))
    assert "✓ Run shell" in stream.getvalue()
    assert "$ echo hi" in stream.getvalue()


def test_mirrored_output_is_sanitized_and_literal():
    save_preferences(show_commands="on")
    view, stream = transcript()
    view.command_output(
        ToolSummary(
            "run_command",
            "client → exit 0",
            command="client --token 'dummy credential'",
            result="[bold]literal[/bold]\n\x1b[31mred\x1b[0m\u202e",
        )
    )
    output = stream.getvalue()
    assert "client --token 'dummy credential'" in output
    assert "[bold]literal[/bold]" in output
    assert "\x1b" not in output
    assert "\u202e" not in output


@pytest.mark.parametrize("width", [1, 2, 3, 12, 45, 80])
def test_command_block_wraps_without_losing_command_or_literal_indentation(width):
    from rich.cells import cell_len

    save_preferences(show_commands="on", command_scrollback_lines="3")
    view, stream = transcript(width=width)
    view.command_output(
        ToolSummary(
            "run_command",
            "run",
            command="printf '界 hello'\necho done",
            result="    [bold]literal[/bold]\n```\n# heading\n" + "tail " * 40,
        )
    )
    lines = stream.getvalue().splitlines()
    assert all(cell_len(line) <= width for line in lines)
    joined = "".join(line.strip() for line in lines)
    assert "printf" in joined and "echodone" in joined.replace(" ", "")
    if width == 80:
        assert "earlier output rows omitted" in stream.getvalue()


def test_output_keeps_leading_indentation_and_markdown_literal_without_padding():
    save_preferences(show_commands="on")
    view, stream = transcript()
    view.command_output(
        ToolSummary(
            "run_command", "run", command="echo hi", result="    # heading\n```\n[bold]x[/bold]"
        )
    )
    assert stream.getvalue().splitlines() == [
        "✓ Run shell " + "─" * 68,
        "  $ echo hi",
        "  " + SEPARATOR,
        "      # heading",
        "  ```",
        "  [bold]x[/bold]",
        "─" * 80,
    ]


def test_process_details_are_not_presented_as_shell_source():
    save_preferences(show_commands="on")
    view, stream = transcript()
    view.command_output(ToolSummary("check_command", "process → running", result="still running"))
    assert "$" not in stream.getvalue()
    assert "process → running" in stream.getvalue()


def test_command_replay_uses_current_theme_and_output_budget():
    from pcode.command_transcript import CommandTranscript

    save_preferences(show_commands="on")
    view, _ = transcript()
    view.tool_result(ToolSummary("run_command", "run", command="echo hi", result="one\ntwo\nthree"))
    view.theme = "light"
    view.command_scrollback_lines = 1
    blocks = [obj for objects, _, _ in view.replay() for obj in objects]
    block = next(obj for obj in blocks if isinstance(obj, CommandTranscript))
    assert block.code_theme == view.code_theme
    assert block.max_lines == 1
    assert block.command == "echo hi"


def test_command_highlighting_and_failure_title_do_not_style_output_as_code():
    from rich.text import Text

    from pcode.command_transcript import CommandTranscript

    view, _ = transcript()
    block = CommandTranscript("echo '$HOME'", "[bold]literal[/bold]", "Run", failed=True)
    succeeded = CommandTranscript("echo '$HOME'", "out", "Run")
    with view.console.use_theme(view.rich_theme):
        parts = list(block.__rich_console__(view.console, view.console.options))
        heading = parts[0].title
        assert isinstance(heading, Text)
        # A failed command reads the same as a successful one; only ✗ differs.
        assert heading.style == "pcode.accent"
        assert heading.plain.startswith("✗ Run") and "failed" not in heading.plain
        headings = list(succeeded.__rich_console__(view.console, view.console.options))
        assert headings[0].title.style == heading.style
        segments = list(view.console.render(block))
    command_segments = [segment for segment in segments if "$HOME" in segment.text]
    assert command_segments and command_segments[0].style.color is not None
    output_segments = [segment for segment in segments if "[bold]literal[/bold]" in segment.text]
    assert output_segments and not output_segments[0].style


def test_default_budget_retains_last_twenty_output_rows():
    save_preferences(show_commands="on")
    view, stream = transcript()
    view.command_output(
        ToolSummary(
            "run_command",
            "run",
            command="noisy",
            result="\n".join(f"row {i}" for i in range(50)),
        )
    )
    lines = stream.getvalue().splitlines()
    assert view.command_scrollback_lines == 20
    assert "30 earlier output rows omitted" in lines[3]
    assert [line.strip() for line in lines[4:-1]] == [f"row {i}" for i in range(30, 50)]


@pytest.mark.parametrize(
    "name", ["shell", "run_command", "start_command", "check_command", "stop_command"]
)
@pytest.mark.parametrize("failed", [False, True])
def test_command_visibility_controls_all_completions(name, failed):
    view, stream = transcript()
    event = ToolSummary(name, "command detail", failed=failed, result="OUTPUT", error="DIAGNOSTIC")
    view.tool_result(event)
    view.events((event,))
    # Only the captured output is withheld; the summary line always shows.
    assert "OUTPUT" not in stream.getvalue()
    assert "DIAGNOSTIC" not in stream.getvalue()
    assert stream.getvalue().count("✗" if failed else "✓") == 2
    stream.truncate(0)
    stream.seek(0)
    view.command_scrollback = True
    # Mirrored output of a failure is the one part that waits for its own option.
    view.tool_error_scrollback = failed
    view.tool_result(event)
    assert stream.getvalue().count("OUTPUT") == 1
    assert "DIAGNOSTIC" not in stream.getvalue()


@pytest.mark.parametrize("limit", ["1", "10", "80"])
def test_preview_height_setting_is_independent_of_scrollback_limit(limit):
    configure(["set", "command_preview_lines", limit])
    view, _ = transcript()
    assert view.command_preview_lines == int(limit)
    assert view.command_scrollback_lines == 20
    assert not view.command_scrollback
    configure(["unset", "command_preview_lines"])
    assert configure(["get", "command_preview_lines"]) == "10"


@pytest.mark.parametrize("value", ["0", "-1", "1.5", "abc"])
def test_preview_height_rejects_invalid_values(value):
    with pytest.raises(ValueError, match="whole number of rows or a fraction"):
        configure(["set", "command_preview_lines", value])


def test_preview_height_accepts_a_share_of_the_screen():
    configure(["set", "command_preview_lines", "0.25"])
    view, _ = transcript()
    assert view.command_preview_lines == 0.25
