from io import StringIO
from unittest.mock import patch

import pytest
from prompt_toolkit.completion import CompleteEvent
from prompt_toolkit.document import Document
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput
from rich.cells import cell_len
from rich.console import Console

from pcode.app import PreviewApp
from pcode.commands import Command, CommandRegistry, SlashCompleter
from pcode.ui import create_prompt


def make_app(width=80, workspace=None):
    stream = StringIO()
    return PreviewApp(
        console=Console(file=stream, width=width, color_system=None), workspace=workspace
    ), stream


@pytest.mark.parametrize(
    "text, expected",
    [
        (
            "/",
            [
                "/help",
                "/config",
                "/bind",
                "/unbind",
                "/quit",
                "/status",
                "/tools",
                "/diffs",
                "/links",
                "/copy",
                "/paste-image",
                "/tree",
                "/btw",
                "/agents",
                "/model",
                "/effort",
                "/mcp",
                "/login",
                "/logout",
                "/extensions",
                "/subagents",
                "/reload",
                "/new",
                "/resume",
                "/rename",
                "/switch",
                "/restart",
                "/stop",
                "/detach",
                "/compact",
                "/autocompact",
                "/jobs",
                "/resend",
                "/worktree",
                "/show-activity",
                "/show-tasks",
                "/autohide-tasks",
                "/show-thinking",
                "/show-edits",
                "/show-commands",
                "/group-tools",
                "/theme",
                "/syntax",
                "/theme-preview",
                "/redraw",
                "/usage",
            ],
        ),
        ("/sta", ["/status"]),
        ("/session", []),
        ("/res", ["/resume", "/restart", "/resend"]),
        ("/theme-p", ["/theme-preview"]),
        ("/theme ", ["dark", "light", "auto"]),
        ("/theme l", ["light"]),
        ("/syntax t", ["terminal", "tango", "trac"]),
        ("/syntax gruvbox", ["gruvbox-dark", "gruvbox-light"]),
        ("/ex", ["/quit", "/extensions"]),
        ("hello /", []),
        ("/theme-preview\n/", []),
        ("/missing", []),
        ("/attach-tasks", []),
        ("/tool", ["/tools"]),
    ],
)
def test_completion(text, expected, tmp_path):
    # An empty workspace keeps pcode's own .claude skills out of the expected list.
    app, _ = make_app(workspace=tmp_path)
    completions = list(
        SlashCompleter(app.registry).get_completions(Document(text), CompleteEvent())
    )
    assert [item.text for item in completions] == expected
    if text == "/theme-p":
        assert completions[0].start_position == -8
        assert completions[0].display_meta_text


def test_registry_guards_duplicate_names():
    registry = CommandRegistry()
    registry.register(Command("/one", "first", lambda _: None, aliases=("/alias",)))
    with pytest.raises(ValueError, match="Duplicate"):
        registry.register(Command("/alias", "collision", lambda _: None))
    assert len(registry.commands) == 1


def test_dispatch_theme_errors_reset_and_exit():
    app, stream = make_app()
    commands = ("hello", "/theme-preview", "/theme light", "/theme invalid", "/missing")
    for text in (*commands, "/context"):
        app.handle(text)
    assert app.runtime.turns == 2
    assert app.transcript.theme == "light"
    assert "Usage: /theme [dark|light|auto]" in stream.getvalue()
    assert "Unknown command" in stream.getvalue()
    with patch.object(app.transcript, "clear", wraps=app.transcript.clear) as clear:
        app.handle("/new")
        clear.assert_called_once_with()
    assert app.runtime.turns == 0
    # Only the "New conversation" rule survives; earlier scrollback is gone.
    assert len(app.transcript.log.entries) == 1
    app.handle("/exit")
    assert not app.running


@pytest.mark.parametrize(
    "command",
    [
        "/tree",
        "/new",
        "/help",
        "/theme-preview",
        "/theme light",
        "/theme invalid",
        "/missing",
        "/exit",
    ],
)
def test_commands_are_not_echoed_as_prompts(command):
    app, _ = make_app()
    with patch.object(app.transcript, "user", wraps=app.transcript.user) as user:
        assert not app.handle(f"  {command}  ")
        user.assert_not_called()


def test_preview_prompt_is_still_echoed():
    app, stream = make_app()
    with patch.object(app.transcript, "user", wraps=app.transcript.user) as user:
        assert not app.handle("  hello  ")
        user.assert_called_once_with("hello")
    assert "hello" in stream.getvalue()


@pytest.mark.parametrize("width", [24, 40, 80, 120])
@pytest.mark.parametrize("theme", ["dark", "light"])
@pytest.mark.parametrize("syntax", ["gruvbox-dark", "terminal"])
def test_rendering_fits_terminal(width, theme, syntax):
    app, stream = make_app(width)
    app.transcript.theme = theme
    app.transcript.syntax_themes[theme] = syntax
    app.transcript.welcome()
    app.help("")
    app.theme_preview("")
    app.handle("Hello 世界 👋 " + "unbroken" * 30)
    output = stream.getvalue()
    assert "世界" in output
    # Settled calls are summarized in scrollback, not retained by the live panel.
    assert output.count("preview only") == 2
    assert app.activity.tools.calls == []
    assert all(cell_len(line) <= width for line in output.splitlines())
    assert "\x1b" not in output


def test_theme_preview_gallery_marks_the_style_in_use_on_replay():
    app, stream = make_app(width=100)
    app.theme_preview("")
    assert "▸ terminal " in stream.getvalue()
    app.handle("/syntax monokai")
    console = Console(file=StringIO(), width=100, color_system=None)
    for objects, end, _ in app.transcript.replay():
        console.print(*objects, end=end)
    replayed = console.file.getvalue()
    assert "▸ monokai " in replayed
    assert "▸ terminal " not in replayed


@pytest.mark.parametrize(
    "keys, expected",
    [
        ("hello\r", "hello"),
        ("first\x1b\rsecond\r", "first\nsecond"),
        ("\x1b[200~one\ntwo\x1b[201~\r", "one\ntwo"),
    ],
)
def test_real_prompt_keybindings(keys, expected):
    app, _ = make_app()
    with create_pipe_input() as pipe:
        session = create_prompt(app.registry, input=pipe, output=DummyOutput())
        assert not session.app.full_screen
        assert not session.mouse_support
        pipe.send_text(keys)
        assert session.prompt() == expected


def test_prompt_accepts_completion_before_sending():
    app, _ = make_app()
    with create_pipe_input() as pipe:
        session = create_prompt(app.registry, input=pipe, output=DummyOutput())

        def complete(buffer):
            if buffer.complete_state:
                pipe.send_text("\t\r\r")

        session.default_buffer.on_completions_changed += complete
        pipe.send_text("/theme-p")
        assert session.prompt() == "/theme-preview"


@pytest.mark.parametrize("keys, exception", [("\x03", KeyboardInterrupt), ("\x04", EOFError)])
def test_prompt_interrupt_and_eof(keys, exception):
    app, _ = make_app()
    with create_pipe_input() as pipe:
        session = create_prompt(app.registry, input=pipe, output=DummyOutput())
        pipe.send_text(keys)
        with pytest.raises(exception):
            session.prompt()


def test_tool_command_is_not_available():
    app, stream = make_app()
    assert app.registry.find("/tool") is None
    for text in ("/tool", "/tool 1"):
        app.handle(text)
    assert stream.getvalue().count("Unknown command") == 2


def test_removed_command_names_are_unknown():
    app, stream = make_app()
    assert app.registry.find("/status") is not None
    for name in ("/session", "/context", "/errors", "/edits"):
        assert app.registry.find(name) is None
    app.handle("/help")
    assert "/sessions" not in stream.getvalue()
    assert not app.handle("/sessions")
    assert "Unknown command" in stream.getvalue()
