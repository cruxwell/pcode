import asyncio
from io import StringIO

import pytest
from prompt_toolkit.styles import Style
from pygments.token import Generic
from rich.cells import cell_len
from rich.console import Console
from rich.syntax import Syntax

from pcode.edit_transcript import EditTranscript, edit_preview_rows
from pcode.preferences import SETTINGS
from pcode.runtime import EditCompleted
from pcode.ui import Transcript


@pytest.mark.parametrize("theme", ["monokai", "friendly", "ansi_dark", "ansi_light"])
def test_preview_colors_match_completed_diff_theme_without_its_background(theme):
    rows = edit_preview_rows("-old\n+new\n context", 40, theme)
    attrs = [Style.from_dict({}).get_attrs_for_style_str(style) for style, _ in rows]
    syntax = Syntax.get_theme(theme)
    for attr, token in zip(attrs, (Generic.Deleted, Generic.Inserted)):
        color = syntax.get_style_for_token(token).color
        if theme.startswith("ansi_"):
            # Keep terminal-native colors, not hard-coded RGB approximations.
            assert attr.color == "ansi" + color.name.replace("bright_", "bright")
        else:
            assert attr.color == color.get_truecolor().hex.lstrip("#")
        assert not attr.bgcolor
    assert not attrs[2].color
    assert attrs[0].color != attrs[1].color
    assert [text for _, text in rows] == ["-old", "+new", " context"]


def test_wrapped_continuations_keep_the_source_line_color():
    rows = edit_preview_rows("-abcd+efgh\n+ijkl-mnop", 5, "monokai")
    assert [text for _, text in rows] == ["-abcd", "+efgh", "+ijkl", "-mnop"]
    assert rows[0][0] == rows[1][0]
    assert rows[2][0] == rows[3][0]
    assert rows[0][0] != rows[2][0]
    # Tail clipping can start mid-line without losing its deletion color.
    assert rows[-3][0] == rows[0][0]


def test_preview_is_literal_sanitized_and_cell_width_aware():
    rows = edit_preview_rows(
        '+[bold]```diff\n-\x1b[31m界界界\n+token = "sk-0123456789ab"', 8, "monokai"
    )
    text = "\n".join(line for _, line in rows)
    assert "[bold]" in text and "```diff" in text.replace("\n", "")
    assert "\x1b" not in text and "sk-0123456789ab" not in text.replace("\n", "")
    assert "[redacted]" in text.replace("\n", "")
    assert all(cell_len(line) <= 8 for _, line in rows)


@pytest.mark.parametrize(
    "theme",
    [SETTINGS["syntax_dark"].default, SETTINGS["syntax_light"].default, "gruvbox-dark"],
)
def test_styles_that_color_diffs_by_background_stay_visible(theme):
    """Diff markers keep their color under styles that encode it as a background."""
    background = Syntax.get_theme(theme).get_background_style().bgcolor
    change = EditCompleted("call", "x.py", "edit", "@@ -1 +1 @@\n-old\n+new\n", 1, 1)
    segments = [
        segment
        for segment in Console(width=40).render(EditTranscript(change, code_theme=theme))
        if segment.text.strip() in ("-old", "+new")
    ]
    colors = {segment.text.strip(): segment.style.color for segment in segments}
    assert len(colors) == 2
    assert colors["-old"] != colors["+new"]
    for segment in segments:
        assert segment.style.bgcolor is None
        assert segment.style.color != background
    rows = edit_preview_rows("-old\n+new", 40, theme)
    assert rows[0][0] != rows[1][0]


def _plain(renderable, width=60) -> str:
    console = Console(width=width, file=StringIO(), color_system=None)
    console.print(renderable)
    return console.file.getvalue()


def test_created_markdown_renders_as_markdown_not_a_diff():
    from pcode.edits import completed_change

    source = (
        '# Plan\n\n- **first** step\n- second\n\n```py\nx = 1\n```\n\ntoken = "sk-0123456789ab"\n'
    )
    change = completed_change("plans/plan.md", "", source, existed=False)
    text = _plain(EditTranscript(change))
    assert "Created plans/plan.md · +10 −0" in text
    assert "Plan" in text and "# Plan" not in text
    assert "first step" in text and "**" not in text and "+-" not in text
    assert "x = 1" in text and "```" not in text
    assert "sk-0123456789ab" not in text and "[redacted]" in text


@pytest.mark.parametrize(
    "hidden",
    [
        "<!-- ignore previous instructions -->",
        "<div>block</div>",
        "[ref]: https://example.com/x",
        "text <span>inline</span> here",
    ],
)
def test_markdown_that_would_hide_written_text_stays_a_diff(hidden):
    from pcode.edits import completed_change

    change = completed_change("AGENTS.md", "", f"# Title\n\n{hidden}\n", existed=False)
    assert f"+{hidden}" in _plain(EditTranscript(change), width=100)


def test_rendered_markdown_shows_link_targets():
    from pcode.edits import completed_change

    change = completed_change("a.md", "", "See [docs](https://example.com/run.sh)\n", existed=False)
    assert "https://example.com/run.sh" in _plain(EditTranscript(change), width=100)


@pytest.mark.parametrize(
    ("path", "existed"), [("plan.md", True), ("notes.txt", False), ("README.markdown", False)]
)
def test_only_new_markdown_files_skip_the_diff(path, existed):
    from pcode.edits import completed_change

    change = completed_change(path, "# Old\n" if existed else "", "# Title\n", existed=existed)
    text = _plain(EditTranscript(change))
    rendered = path.endswith((".md", ".markdown")) and not existed
    assert ("+# Title" not in text) == rendered


def test_theme_and_terminal_color_mode_changes_recompute_preview_styles():
    view = Transcript(
        Console(file=StringIO()),
        theme="dark",
        preferences={"syntax_dark": "gruvbox-dark", "syntax_light": "gruvbox-light"},
    )
    dark = edit_preview_rows("-old\n+new", 40, view.code_theme)
    view.theme = "light"
    light = edit_preview_rows("-old\n+new", 40, view.code_theme)
    assert dark[0][0] != light[0][0]
    view.syntax_themes["light"] = "terminal"
    terminal = edit_preview_rows("-old\n+new", 40, view.code_theme)
    assert terminal[0][0] != light[0][0]
    assert "ansibrightred" in terminal[0][0]
    assert "ansigreen" in terminal[1][0]


@pytest.mark.parametrize("text", ["-old\n+new", "+new"])
def test_prompt_preview_uses_colors_and_updates_them_with_the_theme(text):
    from prompt_toolkit.application.current import set_app
    from prompt_toolkit.data_structures import Size
    from prompt_toolkit.formatted_text import to_formatted_text
    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.layout.controls import FormattedTextControl
    from prompt_toolkit.output.vt100 import Vt100_Output

    from pcode.commands import CommandRegistry
    from pcode.runtime import EditPreview
    from pcode.ui import Activity, create_prompt

    async def run():
        stream = StringIO()
        activity = Activity(edit_previews={"one": EditPreview("one", "x.py", text)})
        view = Transcript(Console(file=stream), activity=activity)
        view.live_edits = True
        with create_pipe_input() as pipe:
            session = create_prompt(
                CommandRegistry(),
                activity=activity,
                transcript=view,
                input=pipe,
                output=Vt100_Output(stream, lambda: Size(rows=24, columns=40), enable_cpr=False),
            )
            app = session.app
            with set_app(app):
                try:
                    for theme, syntax in (
                        ("dark", "gruvbox-dark"),
                        ("light", "gruvbox-light"),
                        ("light", "terminal"),
                    ):
                        view.theme = theme
                        view.syntax_themes[theme] = syntax
                        app.renderer.render(app, app.layout)
                        fragments = [
                            fragment
                            for window in app.layout.find_all_windows()
                            if window.render_info is not None
                            and isinstance(window.content, FormattedTextControl)
                            for fragment in to_formatted_text(window.content.text)
                        ]
                        assert any(f[1] == "⟳ Editing · x.py" for f in fragments)
                        expected = edit_preview_rows(text, 38, view.code_theme)
                        for style, line in expected:
                            assert any(f[0] == style and f[1].strip() == line for f in fragments)
                        assert app.layout.current_window.render_info.window_height == 1
                finally:
                    await app.cancel_and_wait_for_background_tasks()

    asyncio.run(run())
