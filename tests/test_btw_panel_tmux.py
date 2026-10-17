"""The /btw panel over a real terminal: it opens above the editor, which replies to it."""

import shutil

import pytest
from test_tmux import capture
from test_tmux import pane as pane
from test_tmux import release as release

pytestmark = pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux is not installed")

SCRIPT = r"""
from pcode.app import PreviewApp
from pcode.aside import Aside, SideReply

app = PreviewApp()
asides = app.controller.asides
for question, answer in (
    ("why is the parser LL(1)?", "Because **one token** of lookahead decides every rule."),
    ("what does the cache key on?", "The request prefix:\n\n```sh\nmake cache-report\n```"),
):
    aside = Aside(question=question, answer=answer, activity="")
    aside.settle("answered")
    aside.reply = SideReply(answer)
    asides.items.append(aside)
asides.items[0].read = asides.items[1].read = True


def follow_up(thread, question):
    asides.follows(thread)
    aside = Aside(question=question, answer=f"Echo: {question}", activity="", thread=thread)
    aside.settle("answered")
    aside.reply = SideReply(aside.answer)
    asides.items.append(aside)


app.controller.follow_up_aside = follow_up
app.run()
"""


def row(screen: str, text: str) -> int:
    return next(i for i, line in enumerate(screen.splitlines()) if text in line)


@pytest.mark.parametrize("pane", [SCRIPT], indirect=True)
def test_btw_panel_replies_from_the_main_editor(pane):
    capture(pane, "❯")
    pane("send-keys", "-t", "preview:0.0", "/btw", "Enter")
    screen = capture(pane, "Enter replies here")
    # The newest thread, framed above the editor, with its answer.
    assert " btw  what does the cache key on?" in screen
    assert "make cache-report" in screen
    assert "thread 2/2" in screen
    assert row(screen, "what does the cache key on?") < row(screen, "Enter replies here")

    # Enter sends to the thread, not the conversation.
    pane("send-keys", "-t", "preview:0.0", "and the TTL?", "Enter")
    screen = capture(pane, "Echo: and the TTL?")
    assert "▌ and the TTL?" not in screen.split(" btw ")[0]

    # Ctrl+Q shows the other thread; Escape closes the panel and Enter goes
    # back to the conversation.
    pane("send-keys", "-t", "preview:0.0", "C-q")
    screen = capture(pane, "thread 1/2")
    assert "why is the parser LL(1)?" in screen
    pane("send-keys", "-t", "preview:0.0", "Escape")
    pane("send-keys", "-t", "preview:0.0", "main message", "Enter")
    # The preview's own reply: the conversation got it.
    screen = capture(pane, "message arrived intact")
    assert "▌ main message" in screen
    assert "Enter replies here" not in screen


RUNNING = r"""
import os, tempfile
os.environ["XDG_CONFIG_HOME"] = tempfile.mkdtemp()
import pcode.delta
pcode.delta.find_delta = lambda: None
from pcode.app import PreviewApp
from pcode.aside import Aside, SideReply
from pcode.runtime import EditPreview, Message

class Runtime:
    session = None
    tree = None

    async def stream(self, prompt):
        yield EditPreview("one", "sample.py", "-OLD_EDIT_LINE\n+LIVE_EDIT_LINE")
        await gate()
        yield EditPreview("one")
        yield Message("MAIN_TURN_DONE")

    def reset(self):
        pass

app = PreviewApp(model="test:local", runtime=Runtime())
app.transcript.live_edits = True
asides = app.controller.asides
aside = Aside(question="SIDE_QUESTION?", answer="SIDE_ANSWER", activity="")
aside.settle("answered")
aside.reply = SideReply("SIDE_ANSWER")
aside.read = True
asides.items.append(aside)


def follow_up(thread, question):
    asides.follows(thread)
    reply = Aside(question=question, answer=f"Echo: {question}", activity="", thread=thread)
    reply.settle("answered")
    reply.reply = SideReply(reply.answer)
    asides.items.append(reply)


app.controller.follow_up_aside = follow_up
app.run()
"""


@pytest.mark.parametrize("pane", [RUNNING], indirect=True)
def test_btw_panel_shares_the_screen_with_a_running_turn(pane, release):
    capture(pane, "❯")
    pane("send-keys", "-t", "preview:0.0", "do the work", "Enter")
    capture(pane, "LIVE_EDIT_LINE", running=True)
    pane("send-keys", "-t", "preview:0.0", "C-q")
    screen = capture(pane, "SIDE_ANSWER", running=True)
    # The turn's live edit and the thread are both on screen, the thread
    # directly above the editor.
    assert "LIVE_EDIT_LINE" in screen
    assert row(screen, "LIVE_EDIT_LINE") < row(screen, "SIDE_QUESTION?")
    # Enter replies to the thread rather than steering the running turn.
    pane("send-keys", "-t", "preview:0.0", "while it runs?", "Enter")
    screen = capture(pane, "Echo: while it runs?", running=True)
    assert "steer" not in screen.lower().split(" btw ")[0]
    release()
    screen = capture(pane, "MAIN_TURN_DONE")
    # The turn finished into scrollback above the panel, which stayed open.
    assert row(screen, "MAIN_TURN_DONE") < row(screen, "SIDE_QUESTION?")
    assert "Enter replies here" in screen
