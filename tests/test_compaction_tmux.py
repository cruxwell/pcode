"""Real cursor-position reports must not stretch the prompt during compaction."""

import shutil
import time

import pytest
from test_tmux import SPINNER_ROW, TIMEOUT, active_task, capture, input_rows, resize
from test_tmux import pane as pane

pytestmark = pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux is not installed")

SCRIPT = """
import asyncio
from pcode.app import PreviewApp

class Runtime:
    session = None
    history = []
    async def compact(self, focus):
        await asyncio.sleep(60)

app = PreviewApp(model="test:local", runtime=Runtime())
app.activity.plan = [{"id": "one", "content": "A task", "status": "in_progress"}]
app.run()
"""


@pytest.mark.parametrize("pane", [SCRIPT], indirect=True)
def test_compaction_keeps_editor_height_and_cancels_after_clearing_draft(pane):
    assert input_rows(capture(pane, "❯")) == 1
    pane("send-keys", "-t", "preview:0.0", "-l", "/compact keep test failures")
    pane("send-keys", "-t", "preview:0.0", "Enter")
    screen = capture(pane, "Compacting context", running=True)
    assert input_rows(screen) == 1

    def prompt_row(screen):
        # The badge row labels the work; it never echoes the typed command.
        return next(line for line in screen.splitlines() if "◈ Compacting context" in line)

    first = prompt_row(screen)
    assert "/compact" not in first
    assert "▸ keep test failures" in first
    assert first.startswith(SPINNER_ROW)
    lines = screen.splitlines()
    # The badge rides the editor box's top border, over the tasks.
    assert lines[lines.index(first) + 1].startswith(
        tuple("│" + row for row in active_task(" A task"))
    )
    deadline = time.monotonic() + TIMEOUT
    while prompt_row(screen) == first:
        assert time.monotonic() < deadline, "Compaction spinner did not animate"
        time.sleep(0.05)
        screen = capture(pane, "Compacting context", running=True)
    pane("send-keys", "-t", "preview:0.0", "-l", "keep this draft")
    for columns in (40, 100, 35):
        resize(pane, "resize-window", "-t", "preview:0", "-x", str(columns))
        screen = capture(pane, "keep this draft", running=True, columns=columns)
        assert input_rows(screen) == 1
        assert prompt_row(screen).startswith(SPINNER_ROW)
    # A draft absorbs the first Ctrl+C: only an empty prompt cancels the work.
    pane("send-keys", "-t", "preview:0.0", "C-c")
    screen = capture(pane, "Input discarded", running=True)
    assert "keep this draft" not in screen
    assert "Compaction cancelled" not in screen
    assert prompt_row(screen).startswith(SPINNER_ROW)
    assert input_rows(screen) == 1
    pane("send-keys", "-t", "preview:0.0", "C-c")
    screen = capture(pane, "Compaction cancelled")
    assert input_rows(screen) == 1
    # The row is live-only: once the work ends, the notice carries the outcome.
    assert "◈ Compacting context" not in screen
