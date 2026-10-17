"""The editor footer is context, not a second keyboard-help menu."""

import subprocess
from io import StringIO
from pathlib import Path
from types import SimpleNamespace

import pytest
from prompt_toolkit.formatted_text import fragment_list_to_text
from prompt_toolkit.styles import default_ui_style, merge_styles
from rich.cells import cell_len
from rich.console import Console

from pcode.app import PreviewApp
from pcode.runtime import PreviewRuntime
from pcode.ui import PALETTES, TERMINAL_PALETTE


def make_app(workspace, monkeypatch, *, model=None, width=100):
    monkeypatch.setattr(
        "pcode.app.get_app",
        lambda: SimpleNamespace(
            output=SimpleNamespace(get_size=lambda: SimpleNamespace(columns=width))
        ),
    )
    stream = StringIO()
    app = PreviewApp(
        workspace=workspace,
        model=model,
        runtime=PreviewRuntime(),
        console=Console(file=stream, width=120, color_system=None),
    )
    return app, stream


@pytest.mark.parametrize("busy", [False, True])
@pytest.mark.parametrize("count,label", [(0, ""), (1, "1 job"), (12, "12 jobs")])
@pytest.mark.parametrize("width", [32, 100])
def test_footer_counts_running_jobs_before_model_metadata(
    tmp_path, monkeypatch, busy, count, label, width
):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    app, _ = make_app(tmp_path, monkeypatch, model="test:local", width=width)
    app.activity.busy = busy
    app.activity.job_count = count
    text = fragment_list_to_text(app.toolbar())
    assert cell_len(text) <= width
    if label:
        assert ("class:bottom-toolbar.activity", label) in app.toolbar()
        assert f"steering · {label}" in text
    else:
        assert "job" not in text
    app.activity.job_count = 0
    assert "job" not in fragment_list_to_text(app.toolbar())


def test_footer_home_branch_model_and_effort(tmp_path, monkeypatch):
    monkeypatch.setattr("pcode.context_usage.context_window", lambda model: 400_000)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    app, _ = make_app(tmp_path / "p/pcode", monkeypatch, model="openai:gpt-5")
    app.branch = "master"
    assert (
        fragment_list_to_text(app.toolbar())
        == " ~/p/pcode@master · steering · openai:gpt-5 (default) · 0/400k"
    )
    app.runtime.agent = SimpleNamespace(
        model=SimpleNamespace(settings={"openai_reasoning_effort": "low"}),
        model_settings={"openai_reasoning_effort": "high"},
    )
    assert fragment_list_to_text(app.toolbar()).endswith("openai:gpt-5 (high) · 0/400k")
    app.runtime.agent.model_settings = None
    assert fragment_list_to_text(app.toolbar()).endswith("openai:gpt-5 (low) · 0/400k")


@pytest.mark.parametrize(
    ("relative", "branch", "expected"),
    [
        # A session worktree names itself three times; keep one copy.
        ("p/pcode/.worktrees/pcode-f2135546", "pcode-f2135546", "~/p/pcode@f2135546"),
        # Hand-made worktrees collapse the same way, keeping their full branch.
        ("p/pcode/.worktrees/fix-thing", "fix-thing", "~/p/pcode@fix-thing"),
        # A worktree checked out elsewhere keeps its own path.
        ("p/pcode/.worktrees/fix-thing", "other", "~/p/pcode/.worktrees/fix-thing@other"),
        # Detached HEAD in a worktree: no branch to collapse onto.
        ("p/pcode/.worktrees/pcode-f2135546", "", "~/p/pcode/.worktrees/pcode-f2135546"),
        # A checkout whose directory already is the branch name.
        ("p/main", "main", "~/p/main"),
    ],
)
def test_footer_location_is_compact(tmp_path, monkeypatch, relative, branch, expected):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    app, _ = make_app(tmp_path / relative, monkeypatch, width=200)
    app.branch = branch
    assert fragment_list_to_text(app.toolbar()).startswith(f" {expected} · steering")


def test_preview_home_and_help(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    app, stream = make_app(tmp_path, monkeypatch)
    assert fragment_list_to_text(app.toolbar()) == " ~ · steering · preview"
    app.handle("/help")
    help_text = stream.getvalue()
    for hint in (
        "/help /commands",
        "Display",
        "Enter send",
        "Ctrl+J newline",
        "Ctrl+D exit",
        "Ctrl+S picks",
        "cancel",
    ):
        assert hint in help_text
        assert hint not in fragment_list_to_text(app.toolbar())


def test_footer_outside_home_and_busy(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    app, _ = make_app(tmp_path, monkeypatch, width=200)
    app.activity.busy = True
    app.activity.queued = 2
    text = fragment_list_to_text(app.toolbar())
    assert str(tmp_path) in text
    assert text.endswith("steering · 2 queued · preview")
    assert "Ctrl" not in text


@pytest.mark.parametrize("width", [20, 40, 60, 100])
def test_long_unicode_path_stays_one_row(tmp_path, monkeypatch, width):
    app, _ = make_app(tmp_path / ("界" * 80 + "\npath"), monkeypatch, width=width)
    text = fragment_list_to_text(app.toolbar())
    assert cell_len(text) <= width
    assert "\n" not in text
    if width >= 40:
        assert "· preview" in text


def test_narrow_footer_keeps_the_path_and_cuts_from_the_end(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    app, _ = make_app(tmp_path / "p/pcode", monkeypatch, model="test:local", width=30)
    app.activity.busy = True
    text = fragment_list_to_text(app.toolbar())
    assert text.startswith(" ~/p/pcode · steering · test:")
    assert text.endswith("…")
    assert cell_len(text) <= 30


def test_branch_refresh_handles_switches_detached_and_non_repo(tmp_path, monkeypatch):
    def git(*args):
        return subprocess.check_output(["git", "-C", str(tmp_path), *args], text=True).strip()

    git("init", "-b", "main")
    app, _ = make_app(tmp_path, monkeypatch)
    # The footer only repaints when the answer moved, so the caller is told.
    assert app.refresh_branch() is True
    assert app.branch == "main"  # Even an unborn branch has a useful name.
    assert app.refresh_branch() is False
    git(
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.invalid",
        "commit",
        "--allow-empty",
        "-m",
        "Test",
    )
    git("checkout", "-b", "feature")
    assert app.refresh_branch() is True
    assert app.branch == "feature"
    git("checkout", "--detach")
    app.refresh_branch()
    assert app.branch == git("rev-parse", "--short", "HEAD")
    elsewhere = tmp_path / "outside"
    elsewhere.mkdir()
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp_path))
    app.workspace = elsewhere
    app.refresh_branch()
    assert app.branch == ""


@pytest.mark.parametrize("error", [FileNotFoundError(), subprocess.TimeoutExpired("git", 1)])
def test_git_unavailable_does_not_break_footer(tmp_path, monkeypatch, error):
    app, _ = make_app(tmp_path, monkeypatch)
    app.branch = "old"

    def fail(*args, **kwargs):
        raise error

    monkeypatch.setattr("pcode.app.subprocess.run", fail)
    app.refresh_branch()
    assert app.branch == ""
    assert "preview" in fragment_list_to_text(app.toolbar())


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_footer_keeps_the_terminal_background(theme):
    # A light terminal can use the default dark app palette (and vice versa),
    # so the footer never paints a background of its own.
    palette = PALETTES[theme]
    style = merge_styles([default_ui_style(), palette.prompt_style()])
    for role in ("text", "sep", "location", "mode", "model", "context", "activity"):
        attrs = style.get_attrs_for_style_str(
            f"class:bottom-toolbar class:bottom-toolbar.text class:bottom-toolbar.{role}"
        )
        assert attrs.bgcolor == "default"
        assert not attrs.reverse
        assert attrs.bold == (role in ("location", "activity"))


def test_footer_uses_ansi_colors_for_terminal_syntax():
    style = merge_styles([default_ui_style(), TERMINAL_PALETTE.prompt_style()])

    def attrs(role):
        return style.get_attrs_for_style_str(
            f"class:bottom-toolbar class:bottom-toolbar.text class:bottom-toolbar.{role}"
        )

    assert attrs("location").color == "ansicyan"
    assert attrs("model").color == "ansicyan"
    assert attrs("mode").color == "ansimagenta"
    assert attrs("sep").dim and attrs("context").dim
    assert attrs("text").color == "default" and not attrs("text").dim


def test_footer_segments_highlight_context_and_activity(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    app, _ = make_app(tmp_path, monkeypatch, model="test:local", width=200)
    app.activity.busy = True
    app.activity.queued = 2
    fragments = app.toolbar()
    assert ("class:bottom-toolbar.location", str(tmp_path)) in fragments
    # A model with no effort control says so rather than showing a level.
    assert ("class:bottom-toolbar.model", "test:local (n/a)") in fragments
    assert "working" not in fragment_list_to_text(fragments)
    assert ("class:bottom-toolbar.activity", "2 queued") in fragments


@pytest.mark.parametrize("model", ["anthropic:claude-sonnet-4-6", "openai-codex:gpt-5"])
def test_footer_provider_and_context(tmp_path, monkeypatch, model):
    from pydantic_ai.messages import ModelResponse
    from pydantic_ai.usage import RequestUsage

    app, _ = make_app(tmp_path, monkeypatch, model=model, width=200)
    app.runtime.history = [ModelResponse(parts=[], usage=RequestUsage(input_tokens=12_500))]
    text = fragment_list_to_text(app.toolbar())
    assert model in text
    assert "12.5k/" in text
    app.runtime.history = []
    assert "0/" in fragment_list_to_text(app.toolbar())


@pytest.mark.parametrize("width", [1, 20, 40, 60, 100])
def test_provider_and_context_stay_one_row(tmp_path, monkeypatch, width):
    monkeypatch.setattr("pcode.context_usage.context_window", lambda model: 1_000_000)
    app, _ = make_app(
        tmp_path / ("界" * 80), monkeypatch, model="anthropic:claude-sonnet-4-6", width=width
    )
    text = fragment_list_to_text(app.toolbar())
    assert cell_len(text) <= width
    # The path leads, so at 60 columns it pushes the model name a few cells off.
    if width >= 100:
        assert "anthropic:claude-sonnet-4-6" in text
        assert "0/1m" in text


@pytest.mark.parametrize("mode", ["steering", "queue", "interrupt"])
@pytest.mark.parametrize("width", [20, 35, 40, 100])
def test_a_long_path_leaves_room_for_the_send_mode(tmp_path, monkeypatch, mode, width):
    app, _ = make_app(
        tmp_path / ("workspace" * 20),
        monkeypatch,
        model="provider:" + "long-model-name" * 20,
        width=width,
    )
    app.send_mode = mode
    text = fragment_list_to_text(app.toolbar())
    # Under pressure the path gives way just enough for the send mode.
    assert f"… · {mode}" in text
    assert cell_len(text) <= width


def test_a_cache_drop_is_a_footer_note_not_a_scrollback_line(tmp_path, monkeypatch):
    from pcode.runtime import CacheBust

    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    app, stream = make_app(tmp_path, monkeypatch, width=200)
    text = (
        "Prompt cache: request 25 reused 0 of ~165,832 previously cached tokens "
        "(claude/claude-opus-5-5).\nRequest fingerprints unchanged; cause unknown."
    )
    app.output = SimpleNamespace(
        app=SimpleNamespace(invalidate=lambda: None), begin_turn=lambda text: None
    )
    app.turn_event(CacheBust(text))
    assert fragment_list_to_text(app.toolbar()).endswith("preview · cache miss 0/165.8k")
    assert "Prompt cache" not in stream.getvalue()

    # A new turn starts without the last one's note.
    app.turn_started("next", echo=False)
    assert "cache" not in fragment_list_to_text(app.toolbar())


def test_the_cache_note_is_cut_before_the_path(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    app, _ = make_app(tmp_path / "p/pcode", monkeypatch, width=32)
    app.activity.cache_note = "cache miss 0/165.8k"
    assert fragment_list_to_text(app.toolbar()) == " ~/p/pcode · steering · preview…"


@pytest.mark.parametrize(
    ("text", "label"),
    [
        (
            "Prompt cache: request 2 reused 0 of ~18,597 previously cached tokens.",
            "cache miss 0/18.6k",
        ),
        (
            "Prompt cache: request 9 reused 41,200 of ~1,250,000 tokens cached in an earlier turn.",
            "cache drop 41.2k/1.25m",
        ),
        (
            "Sub-agent: Prompt cache: request 3 reused 0 of ~900 previously cached tokens.",
            "sub-agent cache miss 0/900",
        ),
        ("something else entirely", "cache drop"),
    ],
)
def test_footer_label_keeps_only_the_size_of_the_drop(text, label):
    from pcode.cache_warnings import footer_label

    assert footer_label(text) == label
