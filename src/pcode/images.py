"""Telling an image from other files, and sending a request only the images it should carry.

The bytes decide what a file is, never its name, so a misnamed file is not sent
under the wrong media type.

What a request carries is judged per request, by `ImageGate`, not by the tool or
prompt that brought an image in. History keeps every picture: a `/model` switch
to a text-only model must not make the next request fail, and the switch back
must find the image still there. A request gets only the images its model can
see, and only the newest few, since each one is sent again with every request
after it. The catalog (`model_metadata`) says what a model takes;
`PCODE_IMAGE_INPUT=on|off` settles one it does not know.
"""

import os
from dataclasses import replace

from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.messages import (
    BinaryContent,
    ImageUrl,
    ModelRequest,
    ToolReturn,
    ToolReturnPart,
    UploadedFile,
    UserPromptPart,
    is_multi_modal_content,
)
from pydantic_ai.models import Model

from pcode import model_metadata
from pcode.model_metadata import refresh_context

# Anthropic refuses an image whose base64 is over 5 MiB, the strictest limit of
# the mainstream providers; base64 is 4/3 the size. A refused image fails every
# request after it, since history keeps it, so a larger one is described
# instead. (Harness's own cap, 5,000,000 raw bytes, lets a 4 MB PNG through.)
MAX_IMAGE_BYTES = 5 * 1024 * 1024 * 3 // 4
# Anthropic also refuses an image over 8000 pixels on a side.
MAX_IMAGE_SIDE = 8000

# What one request may carry, newest first. Gemini takes 20 MB of inline data
# per request, base64 included; Anthropic 32 MB and 100 images. Past this the
# provider refuses the request, and so would the summary request that `/compact`
# sends to shrink it.
MAX_REQUEST_IMAGES = 20
MAX_REQUEST_IMAGE_BYTES = 12_000_000
# Older images are left out this many at a time, so which ones a request carries
# changes once every so many new images rather than with each: every change ends
# the provider's prompt cache at the first image it touches.
IMAGE_STEP = 10

# Names worth a second look when Harness took the bytes for text: a GIF can have
# no null byte in its first 8 KB, which is all that check reads.
IMAGE_SUFFIXES = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp"})

# A data URL can be the whole image; a note only needs to say which one it was.
NOTE_URL_CHARS = 80

# Set on a message `degrade_images` copied, naming the one it was copied from.
# Not a field, so it is never serialized or sent.
ORIGINAL = "_pcode_before_image_gate"

NOT_ACCEPTED = "This model does not accept images."
UNKNOWN = (
    "This model's image support is unknown, so it was not sent; "
    "the user can restart pcode with PCODE_IMAGE_INPUT=on to send images."
)
TOO_MANY = "Older images are left out to keep the request small; read the file again to see it."


def image_type(data: bytes) -> str | None:
    """The media type of a PNG, JPEG, GIF or WebP, read from its leading bytes."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def image_pixels(data: bytes) -> tuple[int, int] | None:
    """Width and height of a PNG or GIF from its header; None for anything else."""
    if data.startswith(b"\x89PNG\r\n\x1a\n") and data[12:16] == b"IHDR" and len(data) >= 24:
        return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")
    if data.startswith((b"GIF87a", b"GIF89a")) and len(data) >= 10:
        return int.from_bytes(data[6:8], "little"), int.from_bytes(data[8:10], "little")
    return None


def unsendable(raw: bytes) -> str | None:
    """Why no provider would take the image in `raw`, or None when one would."""
    if len(raw) > MAX_IMAGE_BYTES:
        return f"{len(raw)} bytes, and the limit is {MAX_IMAGE_BYTES}"
    pixels = image_pixels(raw)
    if pixels and max(pixels) > MAX_IMAGE_SIDE:
        return f"{pixels[0]}x{pixels[1]} pixels, and the limit is {MAX_IMAGE_SIDE} on a side"
    return None


def image_support(model: str | Model) -> bool | None:
    """Whether the model takes images, or None when nothing says."""
    setting = os.environ.get("PCODE_IMAGE_INPUT", "").strip().lower()
    if setting in ("on", "off"):
        return setting == "on"
    kinds = model_metadata.model_inputs(model)
    if kinds:
        return "image" in kinds
    identity = model if isinstance(model, str) else model.model_id
    # Claude Code serves only Claude models, which all take images, so an alias
    # the catalog has no row for (`claude:opus`) still counts.
    return True if identity.partition(":")[0] == "claude" else None


def accepts_images(model: str | Model) -> bool:
    return image_support(model) is True


def is_image(item: object) -> bool:
    if isinstance(item, BinaryContent):
        return item.is_image
    if isinstance(item, UploadedFile):
        return item.media_type.startswith("image/")
    return isinstance(item, ImageUrl)


def has_files(value: object) -> bool:
    """Whether a tool's result holds a file (an image, a PDF) rather than only text.

    A file cannot be truncated or spilled the way text can: serialized, it is a
    wall of base64 that no model reads as the file.
    """
    if isinstance(value, ToolReturn):
        value = value.return_value
    if is_multi_modal_content(value):
        return True
    return isinstance(value, (list, tuple)) and any(is_multi_modal_content(i) for i in value)


def _images_in(part) -> list:
    """The images a request part carries at its top level, where providers look for them."""
    if isinstance(part, UserPromptPart):
        content = [] if isinstance(part.content, str) else part.content
    elif isinstance(part, ToolReturnPart):
        content = part.content if isinstance(part.content, (list, tuple)) else [part.content]
    else:
        return []
    return [item for item in content if is_image(item)]


def _note(item, why: str) -> str:
    match item:
        case BinaryContent():
            what = f"{item.media_type}, {len(item.data)} bytes"
        case UploadedFile():
            what = f"{item.media_type}, uploaded file"
        case _:
            what = item.url[:NOTE_URL_CHARS] + ("…" if len(item.url) > NOTE_URL_CHARS else "")
    return f"[Image omitted ({what}). {why}]"


def _without(part, notes: dict[int, str]):
    """`part` with each image named in `notes` (by `id`) swapped for its note."""
    if not any(id(item) in notes for item in _images_in(part)):
        return part
    content = part.content
    if not isinstance(content, (list, tuple)):
        # A tool that returned the picture and nothing else.
        return replace(part, content=notes[id(content)])
    items = [notes.get(id(item), item) for item in content]
    if isinstance(part, ToolReturnPart) and all(isinstance(item, str) for item in items):
        # Nothing but text left: one string reads better than a JSON list of lines.
        items = "\n".join(items)
    return replace(part, content=items)


def _requests(messages):
    return [m for m in messages if isinstance(m, ModelRequest)]


def _first_kept(sizes: list[int]) -> int:
    """Index of the oldest image a request keeps; the ones before it are left out.

    The line moves `IMAGE_STEP` images at a time. Only when the newest step
    alone is over the byte budget does it fall back to the newest that fit.
    """
    count = len(sizes)
    for first in range(0, count, IMAGE_STEP):
        if count - first <= MAX_REQUEST_IMAGES and sum(sizes[first:]) <= MAX_REQUEST_IMAGE_BYTES:
            return first
    first, spent = count, 0
    while (
        first
        and count - first < MAX_REQUEST_IMAGES
        and spent + sizes[first - 1] <= MAX_REQUEST_IMAGE_BYTES
    ):
        first -= 1
        spent += sizes[first]
    return first


def degrade_images(messages: list, model: str | Model) -> list:
    """`messages` as a request to `model` should carry them.

    Images the model cannot see become notes, and so do the oldest when the rest
    would be over `MAX_REQUEST_IMAGES` or `MAX_REQUEST_IMAGE_BYTES`. The same
    list comes back when nothing changes. Otherwise the changed messages are
    copies, each holding the message it was made from (see `as_in_history`).
    """
    support = image_support(model)
    images = [i for m in _requests(messages) for part in m.parts for i in _images_in(part)]
    if support is True:
        first = _first_kept([len(i.data) if isinstance(i, BinaryContent) else 0 for i in images])
        notes = {id(item): _note(item, TOO_MANY) for item in images[:first]}
    else:
        why = NOT_ACCEPTED if support is False else UNKNOWN
        notes = {id(item): _note(item, why) for item in images}
    if not notes:
        return messages
    result = []
    for message in messages:
        if isinstance(message, ModelRequest):
            parts = [_without(part, notes) for part in message.parts]
            if any(new is not old for new, old in zip(parts, message.parts, strict=True)):
                original, message = message, replace(message, parts=parts)
                object.__setattr__(message, ORIGINAL, original)
        result.append(message)
    return result


def as_in_history(messages: list) -> list:
    """`messages` with every copy `degrade_images` made swapped back for its original.

    `RequestCheckpoint` sees a request after this gate; what it saves for a
    failed turn to recover must still hold the pictures the gate left out.
    """
    return [getattr(message, ORIGINAL, message) for message in messages]


class ImageGate(AbstractCapability):
    """Send each request only the images it should carry; history keeps them all."""

    async def before_model_request(self, ctx, request_context):
        if not any(
            _images_in(part) for m in _requests(request_context.messages) for part in m.parts
        ):
            return request_context
        # The account's own answer may not be in memory yet for a model other
        # than the conversation's (a sub-agent's).
        await refresh_context(request_context.model)
        messages = degrade_images(request_context.messages, request_context.model)
        if messages is request_context.messages:
            return request_context
        return replace(request_context, messages=messages)
