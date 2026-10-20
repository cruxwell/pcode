"""`pcode --attach HOST --print PROMPT`: one message or command for a running host, no editor.

It attaches like a terminal, sends the message (queued behind whatever the host
is already doing) or the slash command, writes what that produces the way a
local `--print` writes its turn (the reply on stdout, the rest on stderr), and
detaches. The host keeps running, and every other terminal sees the turn too.

The host tells this caller alone when its message's turn starts, or that it
was dropped, in order with everything else it shows (`pcode.host._Owner`), so
a turn another terminal started, or one already running, is never mistaken
for it. Notes other terminals cause while it runs are written all the same.
"""

import asyncio
import sys

from pcode.controller import runs_in_terminal
from pcode.gateway import HeadlessView, attached, host_call, submit
from pcode.host_protocol import HostEntry
from pcode.remote import stop_entry
from pcode.runtime import PlanUpdated
from pcode.stream_display import PrintedReply


class PrintView(HeadlessView):
    """The view a `--print` attach gives the host: its own turn or command, written out."""

    def __init__(self, transcript, reply: PrintedReply) -> None:
        super().__init__()
        self.transcript = transcript
        self.reply = reply
        # Busy while the host works, on this message or the turns ahead of it.
        self.tab = reply.tab_progress(self.activity)

    def gone(self, *, stopped: bool) -> None:
        if self.ended:
            self.transcript.note("The session ended, and its host stopped.")
        else:
            self.transcript.error(
                "The session host stopped before this finished."
                if stopped
                else "The session host went away."
            )

    def event(self, event) -> None:
        self.tab.turn_event()
        if isinstance(event, PlanUpdated):
            self.activity.plan = event.items
        self.reply.event(event)

    def retry(self, text: str) -> None:
        self.tab.turn_retry()
        self.reply.settle()
        self.transcript.note(text)

    def settle(self) -> None:
        self.reply.settle()

    def scrollback(self, name: str, args: tuple, kwargs: dict) -> None:
        getattr(self.transcript, name)(*args, **kwargs)


async def print_to_host(entry: HostEntry, prompt: str, *, transcript, present, stdout=None) -> bool:
    """Send `prompt` to the host at `entry`, write what it produces; returns whether it worked."""
    prompt = prompt.strip()
    name = prompt.split(maxsplit=1)[0] if prompt.startswith("/") else ""
    if name == "/stop":
        return await _stop(entry, prompt, transcript)
    if name and runs_in_terminal(prompt):
        transcript.error(f"{name} needs the interactive terminal: pcode --attach {entry.id}")
        return False
    reply = PrintedReply(
        sys.stdout if stdout is None else stdout, transcript=transcript, present=present
    )
    view = PrintView(transcript, reply)
    async with view.tab.shown():
        return await _send(entry, prompt, name, view)


async def _send(entry: HostEntry, prompt: str, name: str, view: PrintView) -> bool:
    """Attach, send the message or command, and write what it produces."""
    transcript, reply = view.transcript, view.reply
    async with attached(entry, view) as controller:
        if controller.startup_error is not None:
            transcript.error(str(controller.startup_error), title="Agent startup failed")
            return False
        if name:
            view.command = True
            ran = await host_call(controller, "run", prompt, view)
            view.command = False
            if ran is False and not view.failed:
                transcript.error(f"{name} did not run: the host's queue was cleared first.")
                return False
            # A host that went away has said whether that was a failure.
            return not view.failed
        try:
            await submit(controller, prompt, view)
        except asyncio.CancelledError:
            # Ctrl+C only detaches: the host's own cancel would also clear
            # every other terminal's queue. Attach to stop the turn there.
            reply.settle()
            where = "keeps running" if view.turn else "is still queued"
            transcript.note(f"Detached; the message {where} in host {entry.id}.")
            raise
        if view.dropped:
            # Startup can fail after this attached, and then drops the queue.
            error = controller.startup_error
            transcript.error(
                str(error) if error else "The host dropped this message before it ran.",
                title="Agent startup failed" if error else "Error",
            )
        return not view.failed


async def _stop(entry: HostEntry, prompt: str, transcript) -> bool:
    """`/stop`: end the host; it tidies its own worktree, as nobody is here to ask."""
    if prompt != "/stop":
        transcript.error("Usage: /stop")
        return False
    await stop_entry(entry)
    line = f"Stopped session host {entry.id}."
    if entry.session_id:
        line += f" Continue with: pcode --continue {entry.session_id}"
    transcript.note(line)
    return True
