"""Real CPR regression: hiding the widget reclaims its entire frame."""

import pytest
from test_tmux import PLAN_SCRIPT, capture, input_rows, pane, pytestmark, release  # noqa: F401


@pytest.mark.parametrize("pane", [PLAN_SCRIPT], indirect=True)
@pytest.mark.parametrize("split", ["-h", "-v"])
def test_toggle_removes_entire_widget_and_restores_the_latest_plan(pane, release, split):  # noqa: F811
    capture(pane, "❯")
    pane("send-keys", "-t", "preview:0.0", "h", "Enter")
    # Hide mid-turn: the widget must stay gone once the turn finishes too.
    capture(pane, "Task 8", running=True)
    pane("send-keys", "-t", "preview:0.0", "C-o")
    pane("send-keys", "-t", "preview:0.0", "-l", "kept draft")
    pane("split-window", split, "-t", "preview:0.0", "cat")
    assert "Task 0" not in capture(pane, "kept draft", running=True)
    release()
    # capture() waits for an idle toolbar, so this is the finished turn, still hidden.
    screen = capture(pane, "kept draft")
    assert "Task 0" not in screen
    assert "Tasks" not in screen and "Tools" not in screen
    assert screen.count("┌") == screen.count("└") == 1
    assert input_rows(screen) == 1
    pane("send-keys", "-t", "preview:0.0", "C-o")
    screen = capture(pane, "✔ Task 0")
    assert "Tasks 12/12" in screen  # Plan updates kept arriving while hidden.
    assert "kept draft" in screen
    assert screen.count("┌") == screen.count("└") == 1
    assert screen.count("├") == 1
    assert input_rows(screen) == 1


AUTOHIDE_SCRIPT = PLAN_SCRIPT.replace(
    "from pcode.app import PreviewApp",
    "from pcode.preferences import save_preferences\n"
    'save_preferences(autohide_tasks="on")\n'
    "from pcode.app import PreviewApp",
    1,
)


@pytest.mark.parametrize("pane", [AUTOHIDE_SCRIPT], indirect=True)
def test_autohide_reclaims_the_frame_when_the_turn_ends(pane, release):  # noqa: F811
    capture(pane, "❯")
    pane("send-keys", "-t", "preview:0.0", "h", "Enter")
    assert "Task 8" in capture(pane, "Task 8", running=True)
    pane("send-keys", "-t", "preview:0.0", "-l", "kept draft")
    release()
    screen = capture(pane, "kept draft")
    assert "Task 8" not in screen  # The finished turn folded the widget away.
    assert "Tasks" not in screen and "Tools" not in screen
    assert screen.count("┌") == screen.count("└") == 1
    assert input_rows(screen) == 1
    pane("send-keys", "-t", "preview:0.0", "C-o")  # Ctrl+O brings it straight back.
    screen = capture(pane, "✔ Task 0")
    assert "kept draft" in screen
    assert screen.count("┌") == screen.count("└") == 1
    assert screen.count("├") == 1
