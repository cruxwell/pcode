"""Delegation panel height under real cursor-position reports, not a no-CPR PTY."""

import shutil

import pytest
from test_tmux import SPINNER_ROW, active_task, capture, input_rows, resize
from test_tmux import pane as pane

pytestmark = pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux is not installed")

SCRIPT = """
import asyncio
from pcode.app import PreviewApp
from pcode.runtime import ChildPlan, PlanUpdated, ToolStarted, ToolSummary

class Runtime:
    session = None
    turns = 0

    async def stream(self, prompt):
        if prompt == "tree":
            yield PlanUpdated([{"content": "Main task", "status": "in_progress"}])
        yield ToolStarted("delegate_task", "explorer · investigate authentication", "parent",
                          activity="Working", agent="explorer",
                          task="investigate authentication")
        if prompt == "tree":
            yield ChildPlan("parent", [
                {"content": "Inspect", "status": "completed"},
                {"content": "Work", "status": "in_progress"},
                {"content": "Validate", "status": "pending"},
            ])
        for i in range(20):
            yield ToolStarted("read_file", f"chatter-{i}", str(i))
            yield ToolSummary("read_file", f"chatter-{i}", call_id=str(i))
        yield ToolStarted("search_files", "auth in src", "parent:search",
                          parent_call_id="parent")
        yield ToolStarted("read_file", "src/auth.py", "parent:read", parent_call_id="parent")
        await asyncio.sleep(60)

PreviewApp(model="test:local", runtime=Runtime()).run()
"""


@pytest.mark.parametrize("pane", [SCRIPT], indirect=True)
def test_delegate_stays_visible_without_its_calls_through_resize_and_cancel(pane):
    capture(pane, "❯")
    pane("send-keys", "-t", "preview:0.0", "h", "Enter")
    screen = capture(pane, "src/auth.py", running=True)
    assert "Explorer" in screen
    # The newest child call owns the status row; its other call gets no row.
    assert "⟳ Search" not in screen
    pane("send-keys", "-t", "preview:0.0", "-l", "keep draft")
    for width, height in ((40, 14), (100, 32), (60, 20)):
        resize(pane, "resize-window", "-t", "preview:0", "-x", str(width), "-y", str(height))
        screen = capture(pane, "src/auth.py", running=True, columns=width)
        lines = screen.splitlines()
        top = max(i for i, line in enumerate(lines) if line.startswith("┌"))
        assert lines[top].startswith(SPINNER_ROW)
        assert "» Explorer" in lines[top + 1]
        assert lines[top + 2].startswith("├")
        assert "Search" not in screen
        assert input_rows(screen) == 1
        assert "keep draft" in screen
    # The draft absorbs the first Ctrl+C; the second one reaches the run.
    pane("send-keys", "-t", "preview:0.0", "C-c")
    capture(pane, "Input discarded", running=True)
    pane("send-keys", "-t", "preview:0.0", "C-c")
    screen = capture(pane, "! Run cancelled")
    # Only the editor is left: the widget above it is gone, not merely emptied.
    lines = screen.splitlines()
    editor_top = max(i for i, line in enumerate(lines) if line.startswith("┌"))
    assert "Tools" not in lines[editor_top]
    assert not lines[editor_top - 1].startswith("└")
    assert input_rows(screen) == 1


@pytest.mark.parametrize("pane", [SCRIPT], indirect=True)
def test_task_tree_guides_survive_resize_at_each_depth(pane):
    capture(pane, "❯")
    pane("send-keys", "-t", "preview:0.0", "tree", "Enter")
    capture(pane, "src/auth.py", running=True)
    pane("send-keys", "-t", "preview:0.0", "-l", "keep draft")
    for width, height in ((40, 18), (100, 32), (60, 20)):
        pane("resize-window", "-t", "preview:0", "-x", str(width), "-y", str(height))
        screen = capture(pane, "src/auth.py", running=True, columns=width)
        assert "Main task" in screen
        assert "│└── » Explorer" in screen
        assert "│    ├── ✔ Inspect" in screen
        assert any("│    ├── " + row in screen for row in active_task(" Work"))
        assert "│    └── ○ Validate" in screen
        assert "Search" not in screen
        assert "keep draft" in screen
        assert input_rows(screen) == 1
