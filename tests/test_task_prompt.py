"""Submitted prompts are colored quotes, not interpreted Markdown."""

from io import StringIO

import pytest
from rich.console import Console

from pcode.task_prompt import TaskPrompt
from pcode.transcript_notice import Note
from pcode.ui import Transcript


def syntax_preferences(mode):
    """Preferences selecting pcode's derived palette or the terminal's colors."""
    if mode == "terminal":
        return {"syntax_dark": "terminal", "syntax_light": "terminal"}
    return {"syntax_dark": "gruvbox-dark", "syntax_light": "gruvbox-light"}


@pytest.mark.parametrize("theme", ["dark", "light"])
@pytest.mark.parametrize("mode", ["palette", "terminal"])
def test_prompt_uses_current_accent(theme, mode):
    console = Console(file=StringIO(), width=80)
    transcript = Transcript(console, theme=theme, preferences=syntax_preferences(mode))
    with console.use_theme(transcript.rich_theme):
        segments = list(console.render(TaskPrompt("literal **text**")))
        accent = console.get_style("pcode.accent")
    assert all(segment.style == accent for segment in segments if segment.text.strip())


def test_prompt_preserves_literal_text_and_blank_lines():
    stream = StringIO()
    transcript = Transcript(Console(file=stream, width=80, color_system=None))
    transcript.user("[red] **bold** `code`\n\n> quote")
    assert stream.getvalue() == "\n▌ [red] **bold** `code`\n▌ \n▌ > quote\n\n"


def test_prompt_rail_repeats_on_wrapped_lines():
    stream = StringIO()
    transcript = Transcript(Console(file=stream, width=8, color_system=None))
    transcript.user("abcdefghijkl")
    assert stream.getvalue() == "\n▌ abcdef\n▌ ghijkl\n\n"


@pytest.mark.parametrize("width", [1, 2])
def test_tiny_terminal_prioritizes_text(width):
    stream = StringIO()
    Transcript(Console(file=stream, width=width, color_system=None)).user("abc")
    assert stream.getvalue().replace("\n", "") == "abc"


def test_prompt_has_blank_line_after_repository_instructions():
    stream = StringIO()
    transcript = Transcript(Console(file=stream, width=80, color_system=None))
    transcript.note("Loaded repository instructions: AGENTS.md")
    transcript.user("submitted prompt")
    assert stream.getvalue() == (
        "· Loaded repository instructions: AGENTS.md\n\n▌ submitted prompt\n\n"
    )


@pytest.mark.parametrize("theme", ["dark", "light"])
@pytest.mark.parametrize("mode", ["palette", "terminal"])
def test_note_is_marked_in_accent_and_set_in_dim_italic_muted(theme, mode):
    """A note's mark shares the prompt rail's accent; its text is chrome, not prose."""
    console = Console(file=StringIO(), width=80)
    transcript = Transcript(console, theme=theme, preferences=syntax_preferences(mode))
    with console.use_theme(transcript.rich_theme):
        segments = [s for s in console.render(Note("MCP 'github' enabled.")) if s.text.strip()]
        accent = console.get_style("pcode.accent")
        note = console.get_style("pcode.note")
        muted = console.get_style("pcode.muted")
    mark, text = segments
    assert mark.text.strip() == "·" and mark.style == accent
    assert text.style == note and note.italic and note.dim
    assert note.color == muted.color


def test_note_hangs_wrapped_and_continuation_rows_under_the_text():
    """One mark per note: a listing keeps its indentation, a wrapped URL stays whole."""
    stream = StringIO()
    transcript = Transcript(Console(file=stream, width=8, color_system=None))
    transcript.note("abcdefghijkl")
    transcript.note("Rows:\n  a:x")
    assert stream.getvalue() == "· abcdef\n  ghijkl\n· Rows:\n    a:x\n"


def test_empty_note_is_a_blank_row_not_a_lone_mark():
    stream = StringIO()
    Transcript(Console(file=stream, width=80, color_system=None)).note("")
    assert stream.getvalue() == "\n"


@pytest.mark.parametrize("width", [1, 2])
def test_tiny_terminal_note_drops_its_mark(width):
    stream = StringIO()
    Transcript(Console(file=stream, width=width, color_system=None)).note("abc")
    assert stream.getvalue().replace("\n", "") == "abc"


def test_live_submission_does_not_echo_quote_before_response():
    from pcode.app import PreviewApp

    stream = StringIO()
    app = PreviewApp(model="test:local", runtime=object(), console=Console(file=stream))
    assert app.handle("waiting prompt")
    assert stream.getvalue() == ""


@pytest.mark.parametrize("theme", ["dark", "light"])
def test_status_row_colours_the_live_phase_and_mutes_its_chrome(theme):
    from pcode.ui import PALETTES, Activity

    palette = PALETTES[theme]
    console = Console(file=StringIO())
    with console.use_theme(palette.rich_theme()):
        muted = console.get_style("pcode.muted").color.name.lstrip("#")
    prompt_style = palette.prompt_style()
    activity = Activity(prompt="A quiet prompt", prompt_state="running", status="Retrying · soon…")
    fragments = activity.status_fragments("⠋", 80, "✓ 2 tools")
    attrs = {style: prompt_style.get_attrs_for_style_str(style) for style, _ in fragments if style}
    accent = palette.accent.lstrip("#")
    hue = palette.task_heading.lstrip("#")
    # Live: the spinner and phase take the task heading's hue, like the
    # footer's running jobs, not the accent of the task rows' in-progress
    # icon; only the phase is bold.
    assert attrs["class:activity.spinner"].color == hue != accent
    assert attrs["class:activity.phase"].color == hue
    assert attrs["class:activity.phase"].bold
    # Content takes the same hue, unbolded; chrome is muted and never bold.
    assert attrs["class:activity.detail"].color == hue
    assert not attrs["class:activity.detail"].bold
    assert attrs["class:activity.meta"].color == muted
    assert not attrs["class:activity.meta"].bold
    editor_attrs = prompt_style.get_attrs_for_style_str("class:prompt")
    assert editor_attrs.color == accent
    assert editor_attrs.bold
