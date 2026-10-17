"""Best-effort system clipboard: copying text, and reading an image for
`/paste-image`. Never raises at the call site.

A local helper (`pbcopy`, `wl-copy`, `xclip`) is tried first because it needs no
cooperation from the terminal emulator. OSC 52 is the fallback, and goes first
over ssh, where the helper would target the clipboard of the wrong machine.
tmux only forwards OSC 52 with `set -g set-clipboard on`, so neither path alone
is enough.
"""

import base64
import os
import re
import shutil
import subprocess
import sys

_HELPERS = (
    ("pbcopy",),
    ("wl-copy",),
    ("xclip", "-selection", "clipboard"),
    ("xsel", "--clipboard", "--input"),
    ("clip.exe",),
)

# Terminals drop oversized OSC 52 payloads, and a clipboard is not a transfer
# channel: truncate loudly rather than copy something silently incomplete.
LIMIT = 64 * 1024


def osc52(text: str) -> str:
    payload = base64.b64encode(text.encode("utf-8", "replace")).decode("ascii")
    return f"\x1b]52;c;{payload}\x07"


def _helper(text: str, output=None) -> bool:
    for helper in _HELPERS:
        if shutil.which(helper[0]) is None:
            continue
        try:
            # The caller owns the alternate screen: a chatty helper must not
            # write over it, so its streams are discarded rather than inherited.
            subprocess.run(
                helper,
                input=text.encode("utf-8", "replace"),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=True,
                timeout=5,
            )
            return True
        except OSError, subprocess.SubprocessError:
            continue
    return False


def _terminal(text: str, output) -> bool:
    if output is None:
        return False
    try:
        output.write_raw(osc52(text))
        output.flush()
        return True
    except Exception:
        return False


def _run(command: tuple[str, ...]) -> bytes | None:
    try:
        done = subprocess.run(command, capture_output=True, timeout=5, check=False)
    except OSError, subprocess.SubprocessError:
        return None
    return done.stdout if done.returncode == 0 and done.stdout else None


def _mac_image() -> bytes | None:
    if shutil.which("pngpaste") and (data := _run(("pngpaste", "-"))):
        return data
    # AppleScript hands the clipboard's PNG flavor back as «data PNGf89504E47…»
    # (the guillemets are UTF-8). A screenshot copied with Cmd+Ctrl+Shift+4 has
    # one; so do most apps' images.
    out = _run(("osascript", "-e", "the clipboard as «class PNGf»"))
    found = re.search(rb"PNGf([0-9A-Fa-f]+)", out or b"")
    return bytes.fromhex(found[1].decode()) if found and len(found[1]) % 2 == 0 else None


def image() -> tuple[bytes | None, str]:
    """The clipboard's image as PNG bytes, or None and why there is none.

    Terminals paste text, so an image never arrives as keystrokes: it is read
    from the system clipboard here. Over ssh that clipboard belongs to the wrong
    machine, so it is not read at all.
    """
    if os.environ.get("SSH_CONNECTION") or os.environ.get("SSH_TTY"):
        return None, "Over ssh, the clipboard pcode can read is the remote machine's"
    if sys.platform == "darwin":
        data = _mac_image()
    else:
        commands = (
            ("wl-paste", "--no-newline", "--type", "image/png"),
            ("xclip", "-selection", "clipboard", "-target", "image/png", "-out"),
        )
        found = [c for c in commands if shutil.which(c[0])]
        if not found:
            return None, "Reading an image from the clipboard needs wl-paste or xclip"
        data = next((d for c in found if (d := _run(c))), None)
    if not data or not data.startswith(b"\x89PNG\r\n\x1a\n"):
        return None, "There is no image on the clipboard"
    return data, ""


def copy(text: str, output=None) -> tuple[bool, bool]:
    """Copy `text`, returning whether it worked and whether it was truncated.

    `output` is a prompt_toolkit output to write OSC 52 to; without one, only a
    local helper can copy.
    """
    truncated = len(text) > LIMIT
    if truncated:
        text = text[:LIMIT] + "\n[truncated by pcode at 64 KiB]"
    remote = bool(os.environ.get("SSH_CONNECTION") or os.environ.get("SSH_TTY"))
    order = (_terminal, _helper) if remote else (_helper, _terminal)
    return any(attempt(text, output) for attempt in order), truncated
