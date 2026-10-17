"""A prompt that names an image file sends the picture with it.

The prompt stays text. A path in it is whatever put it there: `@` completion,
a file dropped on the terminal (which types its path, shell-escaped or quoted),
`/paste-image` (which saves the clipboard's image to a file first), or the
user. When the turn starts, each path that names a PNG, JPEG, GIF or WebP file
is read and sent after the text, labelled with the path as typed so the model
can tell several apart. Queues, `/resend`, history recall
and the session host all carry the text alone, and attach again from it.

Whether the model can see the picture is `ImageGate`'s call, per request, as
for any other image; the conversation keeps it either way.
"""

import hashlib
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import unquote, urlsplit

from pydantic_ai.messages import BinaryImage

from pcode.images import IMAGE_SUFFIXES, MAX_IMAGE_BYTES, image_type, unsendable

# A prompt naming more than this is more likely a list of files to work on
# than pictures to look at; the request budget would drop the rest anyway.
MAX_PROMPT_IMAGES = 10
# How long a pasted image is kept for a prompt (or a resend) to name it.
PASTE_DAYS = 30
# The longest side a pasted image too large to send is shrunk to. Anthropic
# scales anything over 1568 down itself, so more would only cost bytes.
PASTE_SIDE = 2000

# A quoted path, quotes at its edges only (so the apostrophe in "don't" opens
# nothing), or a run of non-space characters in which a backslash escapes the
# next one: the two ways terminals type a dropped file's path.
_TOKEN = re.compile(
    r"""(?<!\S)"([^"\n]+)"(?=[\s.,;:!?)\]]|$)"""
    r"""|(?<!\S)'([^'\n]+)'(?=[\s.,;:!?)\]]|$)"""
    r"""|((?:\\.|\S)+)"""
)
# Prose and Markdown around a path: "(see ./a.png)." or "`./a.png`" or "**a.png**".
_WRAPPING = "`<([{*_\"'"
_TRAILING = "`>)]}.,;:!?*_\"'"
# A fenced block is pasted output or code, whose file names are not requests.
_FENCE = re.compile(r"^(```|~~~).*?^\1", re.MULTILINE | re.DOTALL)


@dataclass
class Attached:
    """What the model is sent for a prompt, and what to tell the user about it."""

    content: str | list
    names: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)


def _candidates(text: str):
    """Each path-like token in `text`, unescaped and unwrapped, that ends like an image."""
    for match in _TOKEN.finditer(_FENCE.sub("", text)):
        quoted = match.group(1) or match.group(2)
        token = quoted if quoted is not None else re.sub(r"\\(.)", r"\1", match.group(3))
        token = token.lstrip(_WRAPPING).rstrip(_TRAILING)
        # A Markdown link or image, `[shot](./shot.png)`: the target.
        token = token.rpartition("](")[2]
        if token.startswith("file://"):
            token = unquote(urlsplit(token).path)
        token = token.removeprefix("@")
        if Path(token).suffix.lower() in IMAGE_SUFFIXES:
            yield token


def pastes_path() -> Path:
    """Where pasted images are kept: under pcode's state, never the workspace."""
    state = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state")
    return state / "pcode" / "pastes"


def save_paste(data: bytes) -> Path:
    """Write a pasted PNG where a prompt can name it; the same image keeps one file.

    Pastes older than `PASTE_DAYS` are removed on the way: a session that still
    names one gets a note that the file is gone, which is all a prompt holds.
    """
    folder = pastes_path()
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    cutoff = time.time() - PASTE_DAYS * 24 * 60 * 60
    for old in folder.glob("paste-*.png"):
        try:
            if old.stat().st_mtime < cutoff:
                old.unlink()
        except OSError:
            pass
    path = folder / f"paste-{hashlib.sha256(data).hexdigest()[:12]}.png"
    # A screenshot can hold anything that was on screen: readable by you only.
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(descriptor, "wb") as file:
        file.write(data)
    return path


def paste(data: bytes) -> tuple[Path, str]:
    """Save a pasted image for a prompt to name, and say why it can't be sent, if so.

    A full-screen Retina screenshot is often over the size providers take; on
    macOS it is shrunk with `sips` (built in) so the paste still works.
    """
    path = save_paste(data)
    why = unsendable(data)
    if why and sys.platform == "darwin" and shutil.which("sips"):
        try:
            subprocess.run(
                ("sips", "--resampleHeightWidthMax", str(PASTE_SIDE), str(path)),
                capture_output=True,
                timeout=30,
                check=True,
            )
            why = unsendable(path.read_bytes())
        except OSError, subprocess.SubprocessError:
            pass
    return path, why or ""


def reference(path: Path) -> str:
    """`path` as a prompt names it, quoted when it holds a space."""
    text = str(path)
    home = str(Path.home())
    if text.startswith(home + os.sep):
        text = "~" + text[len(home) :]
    return f'"{text}"' if any(char.isspace() for char in text) else text


def attach(text: str, base: Path) -> Attached:
    """`text` with the images it names, read from disk; relative paths start at `base`."""
    attached = Attached(content=text)
    parts: list = [text]
    seen: set[Path] = set()
    for token in _candidates(text):
        path = Path(token).expanduser()
        path = path if path.is_absolute() else base / path
        try:
            if not path.is_file():
                continue
            resolved = path.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            if len(attached.names) == MAX_PROMPT_IMAGES:
                attached.problems.append(f"Only the first {MAX_PROMPT_IMAGES} images were attached")
                break
            size = path.stat().st_size
            if size > MAX_IMAGE_BYTES:
                # Not read: it could be anything, on any disk, and it can't be sent.
                limit = f"{size} bytes, and the limit is {MAX_IMAGE_BYTES}"
                attached.problems.append(f"{token} is too large to send: {limit}")
                continue
            raw = path.read_bytes()
        except OSError as error:
            attached.problems.append(f"{token} could not be read: {error.strerror or error}")
            continue
        media_type = image_type(raw)
        if media_type is None:
            attached.problems.append(f"{token} is not a PNG, JPEG, GIF or WebP image")
        elif why := unsendable(raw):
            attached.problems.append(f"{token} is too large to send: {why}")
        else:
            parts += [
                f"[{token} | {media_type} | {len(raw)} bytes]",
                BinaryImage(raw, media_type=media_type),
            ]
            attached.names.append(token)
    if attached.names:
        attached.content = parts
    return attached
