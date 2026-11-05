"""Resends use the real-CPR prompt row, not a system-work label."""

import shutil

import pytest
from test_tmux import capture, input_rows
from test_tmux import pane as pane

pytestmark = pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux is not installed")

SCRIPT = """
import asyncio
import httpx2
from pydantic_ai import Agent
from pydantic_ai.models.function import FunctionModel
from pcode.app import PreviewApp
from pcode.live import AgentRuntime

calls = 0
async def model(messages, info):
    global calls
    calls += 1
    if calls == 1:
        raise httpx2.RemoteProtocolError("incomplete stream")
    await asyncio.Event().wait()
    yield "unreachable"

runtime = AgentRuntime(Agent(FunctionModel(stream_function=model)))
runtime.retry_attempts = 0
app = PreviewApp(model="test:local", runtime=runtime)
app.activity.plan = [{"id": "1", "content": "Check checkpoint", "status": "in_progress"}]
asyncio.run(app.run_async())
"""


@pytest.mark.parametrize("pane", [SCRIPT], indirect=True)
def test_continue_shows_previous_prompt_above_tasks_with_spinner(pane):
    capture(pane, "❯")
    pane("send-keys", "-t", "preview:0.0", "original requested work", "Enter")
    capture(pane, "Agent failed")
    pane("send-keys", "-t", "preview:0.0", "/continue", "Enter")
    screen = capture(pane, "Check checkpoint", running=True)
    lines = screen.splitlines()
    task = next(i for i, line in enumerate(lines) if "Check checkpoint" in line)
    prompts = [line for line in lines[:task] if "original requested work" in line]
    assert prompts, screen
    prompt = prompts[-1].strip()
    assert prompt.endswith("original requested work"), screen
    assert prompt[0] not in {"!", "✓", "■", "◈", "/"}, screen
    assert "Resuming the last turn" not in screen
    assert input_rows(screen) == 1
    pane("send-keys", "-t", "preview:0.0", "C-c")
    capture(pane, "Run cancelled")
