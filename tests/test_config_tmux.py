"""Configuration edits and prompt restoration in a real terminal."""

import shutil

import pytest
from prompt_toolkit.utils import get_cwidth
from test_inspector_tmux import modal
from test_tmux import capture, input_rows, resize, until
from test_tmux import pane as pane

from pcode.config import listed_settings
from pcode.config_ui import WIDE_COLUMNS, name_width

pytestmark = pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux is not installed")
# The list row after saving 7, built from the column widths it is drawn with.
SAVED_ROW = f'{"tool_max_lines":<{name_width()}}  {"user":<7}  "7"'

SCRIPT = r"""
from pathlib import Path
from tempfile import TemporaryDirectory

from pcode.app import PreviewApp
from pcode.preferences import project_preferences_path, set_project_root

class App(PreviewApp):
    async def browse_config(self, output, session):
        session.default_buffer.text = "draft must survive"
        previous = project_preferences_path()
        try:
            with TemporaryDirectory() as root:
                set_project_root(Path(root))
                await super().browse_config(output, session)
        finally:
            set_project_root(previous.parent.parent if previous is not None else None)

App().run()
"""


@pytest.mark.parametrize("pane", [SCRIPT], indirect=True)
@pytest.mark.parametrize("width,height", [(80, 16), (70, 24)])
def test_config_edit_resize_and_restore(pane, width, height):
    capture(pane, "❯")
    resize(pane, "resize-window", "-t", "preview:0", "-x", str(width), "-y", str(height))
    pane("send-keys", "-t", "preview:0.0", "/config", "Enter")
    modal(pane, "Saved configuration")
    if height >= 24:  # A smaller pane scrolls the details (Tab, then PgDn).
        modal(pane, "Saved effective:")
        modal(pane, "may require next launch")
    assert pane("display-message", "-p", "-t", "preview:0.0", "#{alternate_on}").strip() == "1"
    pane("send-keys", "-t", "preview:0.0", "tool_max_lines", "Enter", "C-a", "C-k", "7", "Enter")
    modal(pane, SAVED_ROW)
    # A compact terminal must retain the editor and its save/cancel controls.
    pane("resize-window", "-t", "preview:0", "-x", str(width), "-y", str(height))
    modal(pane, SAVED_ROW)
    pane("send-keys", "-t", "preview:0.0", "Enter", "C-a", "C-k", "bad", "Enter")
    modal(pane, "Value: bad")
    modal(pane, "must be")
    pane("send-keys", "-t", "preview:0.0", "Escape")
    modal(pane, "Edit cancelled")
    pane("send-keys", "-t", "preview:0.0", "Escape")
    screen = capture(pane, "draft must survive", columns=width)
    assert input_rows(screen) == 1
    assert pane("display-message", "-p", "-t", "preview:0.0", "#{alternate_on}").strip() == "0"
    history = pane("capture-pane", "-p", "-S", "-", "-t", "preview:0.0")
    assert "Saved effective:" not in history


def detail_region(lines, width, height):
    """The details pane's rows: right of the list when wide, below it when narrow."""
    if width >= WIDE_COLUMNS:
        split = lines[2].index("┌", 1)
        return [line[split:] for line in lines[2 : height - 3]]
    body = height - 5
    rows = max(3, min(16, body * 3 // 5, body - 3))
    top = height - 3 - rows
    assert lines[top - 1].startswith("└"), "\n".join(lines)
    assert lines[top].startswith("┌"), "\n".join(lines)
    return lines[top : height - 3]


def assert_config_geometry(screen, width, height, *, detail, content=None, notice=None):
    """Check physical terminal cells, not just whether popup text is visible."""
    lines = screen.splitlines()
    assert len(lines) == height, screen
    assert all(get_cwidth(line) <= width for line in lines), screen
    assert "Saved configuration" in lines[0] and "Scope:" in lines[0], screen
    assert "Filter:" in lines[1], screen
    # The list frame starts right under the filter and reaches the full width
    # or the details beside it; the frames end just above the two message rows.
    assert lines[2].startswith("┌─ Settings") and get_cwidth(lines[2]) == width, screen
    assert lines[height - 4].startswith("└") and lines[height - 4].endswith("┘"), screen
    if content is not None:
        assert content in lines[3], screen
    assert any(detail in line for line in detail_region(lines, width, height)), screen
    if notice is not None:
        assert notice in lines[height - 3] + lines[height - 2], screen
    assert "Keys" in lines[height - 1], screen


@pytest.mark.parametrize("pane", [SCRIPT], indirect=True)
def test_config_full_terminal_geometry_across_interactions(pane):
    capture(pane, "❯")
    pane("send-keys", "-t", "preview:0.0", "/config", "Enter")
    width, height = 100, 32

    def check(marker, *, detail, content=None, notice=None):
        screen = modal(pane, marker)
        assert_config_geometry(screen, width, height, detail=detail, content=content, notice=notice)
        return screen

    def send(*keys):
        pane("send-keys", "-t", "preview:0.0", *keys)

    first, second = listed_settings()[:2]
    check(f"› {first}", detail=first, content=f"› {first}")
    send("Down")
    check(f"› {second}", detail=second)

    send("tool_max_lines")
    check("Filter: tool_max_lines", detail="tool_max_lines", content="› tool_max_lines")
    send("C-a", "C-k", "not-a-real-setting-xyz")
    check(
        "No matching settings",
        detail="Saved effective values include",
        content="No matching settings",
    )
    send("C-a", "C-k", "tool_max_lines", "Enter")
    check(
        "Enter saves; Escape cancels.",
        detail="Value:",
        content="› tool_max_lines",
        notice="Enter saves; Escape cancels.",
    )
    send("C-a", "C-k", "bad", "Enter")
    check("must be", detail="Value: bad", content="› tool_max_lines", notice="must be")
    send("Escape")
    check(
        "Edit cancelled",
        detail="tool_max_lines",
        content="› tool_max_lines",
        notice="Edit cancelled",
    )
    send("Enter", "C-a", "C-k", "7", "Enter")
    check('"7" (from user)', detail='"7" (from user)', content="› tool_max_lines")

    send("C-a", "C-k", "show_thinking", "Enter")
    check("Enter saves; Escape cancels.", detail="› activity-panel")
    send("Down", "Escape")
    check(
        "Edit cancelled",
        detail="show_thinking",
        content="› show_thinking",
        notice="Edit cancelled",
    )

    send("C-a", "C-k", "tool_max_lines", "C-o")
    check("Show: Overrides only", detail="tool_max_lines", content="› tool_max_lines")
    send("C-t")
    check("Scope: project", detail="Saved effective values include", content="No matching settings")
    send("C-o")
    check("Show: All settings", detail="tool_max_lines", content="› tool_max_lines")
    send("C-t")
    check("Scope: user", detail="tool_max_lines", content="› tool_max_lines")

    # In the alternate screen a resize repaints the popup, not scrollback.
    # Wait for the new border coordinates, since the detail marker already exists.
    for width, height in [(80, 16), (70, 24), (110, 38)]:
        pane("resize-window", "-t", "preview:0", "-x", str(width), "-y", str(height))

        def resized():
            screen = pane("capture-pane", "-p", "-t", "preview:0.0")
            lines = screen.splitlines()
            return (
                len(lines) == height
                and lines[height - 4].startswith("└")
                and lines[height - 4].endswith("┘")
                and get_cwidth(lines[2]) == width
            )

        until(resized, lambda: pane("capture-pane", "-p", "-t", "preview:0.0"))
        check("› tool_max_lines", detail="tool_max_lines", content="› tool_max_lines")
    send("Escape")
    capture(pane, "draft must survive", columns=width)
