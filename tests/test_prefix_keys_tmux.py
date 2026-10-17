"""A waiting leader lists the prompt's shortcuts above the editor, in real-CPR layout."""

import shutil

import pytest
from test_tmux import capture, input_rows, resize, settle
from test_tmux import pane as pane

pytestmark = pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux is not installed")

SCRIPT = """
import json, os, pathlib, tempfile
os.environ["XDG_CONFIG_HOME"] = config = tempfile.mkdtemp()
pathlib.Path(config, "pcode").mkdir()
pathlib.Path(config, "pcode", "preferences.json").write_text(json.dumps(
    {"key_prefix": "ctrl+p", "tasks_min_rows": "0", "tasks_min_columns": "0"}
))
from pcode.app import PreviewApp
PreviewApp().run()
"""
VI_SCRIPT = """
from pcode.preferences import save_preferences
from pcode.app import PreviewApp
save_preferences(
    editing_mode="vi", vi_key_prefix="<space>", vi_escape_sequence="jj",
    key_prefix="ctrl+b", show_hints="on",
)
PreviewApp().run()
"""
HINT = "Cycle send mode"
# The overlay's last row: a short terminal shows it only after scrolling.
LAST = "Group / ungroup tool calls"


def draft_line(screen: str) -> str:
    return next(line for line in screen.splitlines() if line.startswith("│❯"))


def test_custom_ctrl_binding_runs_command_without_sending_or_moving_draft(pane):
    capture(pane, "steering")
    pane("send-keys", "-t", "preview:0.0", "-l", "/bind e /show-thinking off")
    pane("send-keys", "-t", "preview:0.0", "Enter")
    capture(pane, "Saved Ctrl+E")
    pane("send-keys", "-t", "preview:0.0", "-l", "keep this draft")
    capture(pane, "keep this draft")
    pane("send-keys", "-t", "preview:0.0", "C-a", "C-e")
    screen = capture(pane, "Thinking: off")
    assert "keep this draft" in draft_line(screen)
    pane("send-keys", "-t", "preview:0.0", "-l", "X")
    screen = capture(pane, "Xkeep this draft")
    assert "Xkeep this draft" in draft_line(screen)
    pane("send-keys", "-t", "preview:0.0", "C-_")
    capture(pane, "/show-thinking off")
    pane("send-keys", "-t", "preview:0.0", "Escape")
    screen = settle(pane, lambda screen: "Keybindings · Prompt" not in screen)
    assert "Xkeep this draft" in draft_line(screen)


@pytest.mark.parametrize("pane", [VI_SCRIPT], indirect=True)
def test_vi_space_leader_and_jj_share_actions_with_global_prefix(pane):
    capture(pane, " INSERT ")
    pane("send-keys", "-t", "preview:0.0", "-l", "keep this draft")
    capture(pane, "keep this draft")
    pane("send-keys", "-t", "preview:0.0", "-l", "jj")
    capture(pane, " NORMAL ")
    pane("send-keys", "-t", "preview:0.0", "Space")
    screen = capture(pane, HINT)
    assert "Select model" in screen
    assert "keep this draft" in draft_line(screen)
    pane("send-keys", "-t", "preview:0.0", "s")
    screen = capture(pane, "queue")
    assert HINT not in screen and " NORMAL " in screen
    assert "keep this draft" in draft_line(screen)
    # The ordinary prefix still opens the very same menu.
    pane("send-keys", "-t", "preview:0.0", "C-b")
    capture(pane, HINT)
    pane("send-keys", "-t", "preview:0.0", "s")
    capture(pane, "interrupt")
    # Repeated Space dismisses the menu rather than typing or moving the draft.
    pane("send-keys", "-t", "preview:0.0", "Space")
    capture(pane, HINT)
    pane("send-keys", "-t", "preview:0.0", "Space")
    screen = settle(pane, lambda screen: HINT not in screen)
    assert HINT not in screen and "keep this draft" in draft_line(screen)
    pane("send-keys", "-t", "preview:0.0", "-l", "a more")
    screen = capture(pane, "keep this draft more")
    assert " INSERT " in screen


@pytest.mark.parametrize("pane", [SCRIPT], indirect=True)
def test_leader_hint_shows_above_the_editor_and_runs_the_shortcut(pane):
    assert input_rows(capture(pane, "steering")) == 1
    pane("send-keys", "-t", "preview:0.0", "draft")
    capture(pane, "draft")
    # Ctrl+S alone no longer cycles the send mode; the leader and `s` do.
    pane("send-keys", "-t", "preview:0.0", "C-s")
    pane("send-keys", "-t", "preview:0.0", "C-p")
    screen = capture(pane, HINT)
    lines = screen.splitlines()
    hint = next(i for i, line in enumerate(lines) if HINT in line)
    editor = next(i for i, line in enumerate(lines) if line.startswith("│❯"))
    assert hint < editor, screen
    assert "Thinking effort up / down" in screen
    assert "Select thinking visibility" in screen
    assert "steering" in lines[-1] and input_rows(screen) == 1
    pane("send-keys", "-t", "preview:0.0", "s")
    screen = capture(pane, "queue")
    assert HINT not in screen
    # The letter ran the shortcut instead of being typed.
    assert "draft" in draft_line(screen) and "drafts" not in draft_line(screen)
    assert input_rows(screen) == 1
    # Esc backs out, leaving the draft and the mode alone.
    pane("send-keys", "-t", "preview:0.0", "C-p")
    capture(pane, HINT)
    pane("send-keys", "-t", "preview:0.0", "Escape")
    screen = settle(pane, lambda screen: HINT not in screen)
    assert HINT not in screen and "queue" in screen.splitlines()[-1], screen
    assert "draft" in draft_line(screen) and input_rows(screen) == 1
    # The same panel offers explicit thinking choices without changing the draft.
    pane("send-keys", "-t", "preview:0.0", "C-p", "t")
    screen = capture(pane, "Thinking visibility")
    assert "Off" in screen and "Activity panel" in screen and "Scrollback" in screen
    pane("send-keys", "-t", "preview:0.0", "b")
    screen = settle(pane, lambda screen: "Thinking visibility" not in screen)
    assert "draft" in draft_line(screen) and input_rows(screen) == 1


@pytest.mark.parametrize("pane", [SCRIPT], indirect=True)
@pytest.mark.parametrize(("height", "scroll_key"), [(11, "Down"), (10, "NPage")])
def test_short_terminal_help_scrolls_without_changing_draft(pane, height, scroll_key):
    capture(pane, "steering")
    pane("send-keys", "-t", "preview:0.0", "keep this draft")
    capture(pane, "keep this draft")
    resize(pane, "resize-window", "-t", "preview:0", "-y", str(height))
    capture(pane, "keep this draft")

    pane("send-keys", "-t", "preview:0.0", "C-p")
    screen = capture(pane, HINT)
    assert LAST not in screen, screen
    assert "keep this draft" in draft_line(screen), screen
    # More keys than rows reaches the bottom and exercises the scroll bound.
    pane("send-keys", "-t", "preview:0.0", *([scroll_key] * 50))
    screen = capture(pane, LAST)
    assert "keep this draft" in draft_line(screen) and input_rows(screen) == 1, screen
    pane("send-keys", "-t", "preview:0.0", "Escape")
    screen = settle(pane, lambda screen: LAST not in screen)
    assert LAST not in screen, screen
    assert "keep this draft" in draft_line(screen) and input_rows(screen) == 1, screen

    # Ctrl+/ is read-only, including letters that otherwise run shortcuts.
    pane("send-keys", "-t", "preview:0.0", "C-_")
    screen = capture(pane, "Keybindings")
    assert LAST not in screen, screen
    pane("send-keys", "-t", "preview:0.0", "s", *([scroll_key] * 50))
    screen = capture(pane, LAST)
    assert "steering" in screen.splitlines()[-1], screen
    pane("send-keys", "-t", "preview:0.0", "Escape")
    screen = settle(pane, lambda screen: LAST not in screen)
    assert LAST not in screen, screen
    assert "keep this draft" in draft_line(screen) and "drafts" not in draft_line(screen), screen
    assert input_rows(screen) == 1, screen

    # A chooser still fits above the editor after scrolling either menu.
    pane("send-keys", "-t", "preview:0.0", "C-p", "t")
    screen = capture(pane, "Thinking visibility")
    assert "Off" in screen and "Activity panel" in screen and "Scrollback" in screen, screen
    assert "keep this draft" in draft_line(screen), screen
    pane("send-keys", "-t", "preview:0.0", "Escape")
    screen = settle(pane, lambda screen: "Thinking visibility" not in screen)
    assert "Thinking visibility" not in screen, screen
    assert "keep this draft" in draft_line(screen) and input_rows(screen) == 1, screen
