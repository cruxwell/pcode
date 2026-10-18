"""The /btw thread view over a real terminal: the thread swaps in for the conversation."""

import shutil

import pytest
from test_tmux import capture, resize
from test_tmux import pane as pane
from test_tmux import release as release

pytestmark = pytest.mark.skipif(shutil.which("tmux") is None, reason="tmux is not installed")

SCRIPT = r"""
from pcode.app import PreviewApp
from pcode.aside import Aside, SideReply
from pcode.preferences import save_preferences

save_preferences(btw_view="swap")  # The default, pinned.
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


def history(pane) -> str:
    """The whole scrollback, not just the screen."""
    return pane("capture-pane", "-p", "-S", "-", "-t", "preview:0.0")


def row(screen: str, text: str) -> int:
    return next(i for i, line in enumerate(screen.splitlines()) if text in line)


@pytest.mark.parametrize("pane", [SCRIPT], indirect=True)
def test_btw_thread_swaps_in_for_the_conversation_and_back(pane):
    capture(pane, "❯")
    pane("send-keys", "-t", "preview:0.0", "first main message", "Enter")
    capture(pane, "message arrived intact")
    pane("send-keys", "-t", "preview:0.0", "/btw", "Enter")
    screen = capture(pane, "Esc back to the conversation")
    # The newest thread, in place of the conversation, scrollback and all.
    assert " btw  what does the cache key on?" in screen
    assert "make cache-report" in screen
    scrollback = history(pane)
    assert "first main message" not in scrollback and "message arrived intact" not in scrollback
    # No frame around it for a terminal selection to pick up.
    assert "│ The request prefix" not in scrollback

    # A resize rebuilds the thread, not the conversation it stands in for.
    resize(pane, "resize-window", "-t", "preview:0", "-x", "90")
    screen = capture(pane, "Esc back to the conversation")
    assert "make cache-report" in screen
    assert "message arrived intact" not in history(pane)

    # Enter sends to the thread, and the reply lands below it.
    pane("send-keys", "-t", "preview:0.0", "and the TTL?", "Enter")
    screen = capture(pane, "Echo: and the TTL?")
    assert row(screen, "make cache-report") < row(screen, "Echo: and the TTL?")

    # Ctrl+\ shows the older thread, and the divider says which of how many.
    pane("send-keys", "-t", "preview:0.0", "C-\\")
    screen = capture(pane, " btw  why is the parser LL(1)?")
    assert "make cache-report" not in screen
    divider = screen.splitlines()[row(screen, "↓ conversation")]
    assert "Ctrl+Q" in divider and "(2/2)" in divider
    # Ctrl+Q goes straight back to the conversation, the divider gone with the thread.
    pane("send-keys", "-t", "preview:0.0", "C-q")
    screen = capture(pane, "first main message")
    assert "↓ conversation" not in screen and "Esc back" not in history(pane)
    # Ctrl+Q again brings back the thread last shown; Escape comes back as
    # well, and Enter goes to the conversation again.
    pane("send-keys", "-t", "preview:0.0", "C-q")
    capture(pane, " btw  why is the parser LL(1)?")
    pane("send-keys", "-t", "preview:0.0", "Escape")
    screen = capture(pane, "first main message")
    assert "↓ conversation" not in screen and "Esc back" not in history(pane)
    pane("send-keys", "-t", "preview:0.0", "second main message", "Enter")
    screen = capture(pane, "▌ second main message")
    assert "lookahead" not in history(pane)


RUNNING = r"""
import os, tempfile
os.environ["XDG_CONFIG_HOME"] = tempfile.mkdtemp()
import pcode.delta
pcode.delta.find_delta = lambda: None
from pcode.app import PreviewApp
from pcode.aside import Aside, SideReply
from pcode.preferences import save_preferences
from pcode.runtime import EditPreview, Message

save_preferences(btw_view="swap")  # The default, pinned.

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
def test_a_running_turn_waits_behind_the_thread_and_lands_on_return(pane, release):
    capture(pane, "❯")
    pane("send-keys", "-t", "preview:0.0", "do the work", "Enter")
    capture(pane, "LIVE_EDIT_LINE", running=True)
    pane("send-keys", "-t", "preview:0.0", "C-q")
    screen = capture(pane, "SIDE_ANSWER", running=True)
    # The turn's live edit preview gives the screen to the thread.
    assert "LIVE_EDIT_LINE" not in screen
    # Enter replies to the thread rather than steering the running turn.
    pane("send-keys", "-t", "preview:0.0", "while it runs?", "Enter")
    capture(pane, "Echo: while it runs?", running=True)
    # A divider under the thread points up to it and down to the
    # conversation, with the key back.
    screen = capture(pane, "↓ conversation", running=True)
    divider = screen.splitlines()[row(screen, "↓ conversation")]
    assert "↑ btw: SIDE_QUESTION?" in divider and "Ctrl+Q or Esc back" in divider
    release()
    # The turn finishes behind the thread, which keeps the screen; its
    # newest output shows under the divider meanwhile.
    screen = capture(pane, "MAIN_TURN_DONE")
    assert row(screen, "Echo: while it runs?") < row(screen, "↓ conversation")
    assert row(screen, "↓ conversation") < row(screen, "MAIN_TURN_DONE")
    assert "do the work" not in history(pane)
    pane("send-keys", "-t", "preview:0.0", "Escape")
    screen = capture(pane, "do the work")
    assert "↓ conversation" not in screen
    assert row(screen, "do the work") < row(screen, "MAIN_TURN_DONE")
    # The reply went to the thread: neither steered nor queued for the turn.
    assert "SIDE_ANSWER" not in history(pane)
    assert "while it runs?" not in history(pane)
