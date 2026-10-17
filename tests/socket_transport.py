"""Run the app-level tests with the session in a host, over a real socket.

`pytest --transport socket` (or `PCODE_TEST_TRANSPORT=socket`) makes every
`PreviewApp` that would run its session in-process run it in a `SessionHost`
in the same event loop instead, and attach to that host the way a terminal
attaches to a spawned one: `HostLaunch`, the welcome, then intents and view
calls over the socket. The test drives the terminal exactly as before, so the
same assertions check the hosted path.

Tests that look inside the in-process controller, or at what only a local
session prints, are marked `in_process` and skipped under this transport.
"""

import asyncio
import itertools
import os

from pcode.host import SessionHost
from pcode.host_protocol import HostEntry
from pcode.remote import HostLaunch

TRANSPORT_ENV = "PCODE_TEST_TRANSPORT"

# What a terminal settles about its session before the session starts; the
# host's controller takes it over, as `pcode.host` takes it from its arguments.
SETTINGS = (
    "model",
    "workspace",
    "session_dir",
    "save_sessions",
    "_saved_session",
    "_session_id",
    "resuming",
    "_needs_runtime",
    "startup_pending",
    "extensions",
)

_numbers = itertools.count(1)


def host_for(app) -> SessionHost | None:
    """Move `app`'s in-process session into a host; None when there is nothing to move."""
    source = app.controller
    if app._host_launch is not None or not source.model:
        return None
    entry = HostEntry(
        id=f"t{os.getpid() % 10000:04d}{next(_numbers):03d}",
        pid=os.getpid(),
        model=source.model,
        workspace=str(source.workspace),
    )
    host = SessionHost(entry)
    target = host.controller
    for name in SETTINGS:
        setattr(target, name, getattr(source, name))
    target.runtime = source.runtime
    target.activity.thinking_mode = app.activity.thinking_mode
    # A test's stand-in for a controller method (`app.controller.x = fake`) goes too.
    for name, value in vars(source).items():
        if callable(getattr(type(source), name, None)) and not name.startswith("__"):
            setattr(target, name, value)
    # So do side questions a test asked before starting the terminal.
    target.asides.items.extend(source.asides.items)
    source.asides.items.clear()
    target.register_skills()
    if target.runtime is not None and not target._needs_runtime:
        target.register_extension_commands()
    # The terminal keeps a placeholder until the host's welcome replaces it.
    source.runtime = app.preview
    source.extensions = None
    source._needs_runtime = False
    app._host_launch = HostLaunch(entry.id)
    return host


def install(monkeypatch) -> None:
    """Patch `PreviewApp.run_async` to serve the session from a host for the run."""
    from pcode.app import PreviewApp

    run_async = PreviewApp.run_async

    async def run_in_host(self):
        host = host_for(self)
        if host is None:
            return await run_async(self)
        await host.serve()
        booting = asyncio.create_task(host.boot())
        try:
            return await run_async(self)
        finally:
            # The terminal has gone; a real host would carry on. This one stops
            # with the test, the way an in-process session stops with its terminal.
            booting.cancel()
            await asyncio.gather(booting, return_exceptions=True)
            host.stop()
            await host.close()

    monkeypatch.setattr(PreviewApp, "run_async", run_in_host)
