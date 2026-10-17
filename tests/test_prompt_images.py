"""A prompt that names an image file by path sends the picture with it."""

import pytest
from pydantic_ai.messages import BinaryContent

from pcode import images
from pcode.prompt_images import MAX_PROMPT_IMAGES, attach

PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
    + (64).to_bytes(4, "big")
    + (48).to_bytes(4, "big")
    + b"\x00" * 100
)


@pytest.fixture
def work(tmp_path):
    (tmp_path / "shot.png").write_bytes(PNG)
    (tmp_path / "My Shot.png").write_bytes(PNG + b"\x01")
    (tmp_path / "notes.txt").write_text("not a picture")
    (tmp_path / "fake.png").write_text("not a picture either")
    return tmp_path


def pictures(attached):
    """The attached files, in order, each with the label sent before it."""
    content = attached.content
    assert isinstance(content, list)
    found = []
    for label, item in zip(content[1:], content[2:], strict=False):
        if isinstance(item, BinaryContent):
            found.append((label, item))
    return found


@pytest.mark.parametrize(
    "text",
    [
        pytest.param("what is wrong in ./shot.png", id="relative"),
        pytest.param("what is wrong in shot.png", id="bare-name"),
        pytest.param("what is wrong in ./shot.png?", id="trailing-punctuation"),
        pytest.param("see (`./shot.png`).", id="backticks-and-brackets"),
        pytest.param('"./shot.png" looks off', id="quoted"),
    ],
)
def test_a_named_image_rides_with_the_prompt(work, text):
    attached = attach(text, work)
    # The text is sent as typed: the path is how the model knows which picture.
    assert attached.content[0] == text
    [(label, picture)] = pictures(attached)
    assert picture.data == PNG and picture.media_type == "image/png"
    # Named as typed, the way the prompt names it.
    [name] = attached.names
    assert name in ("./shot.png", "shot.png") and name in text and name in label


@pytest.mark.parametrize(
    "text",
    [
        pytest.param("don't send ./shot.png, it's fine", id="apostrophes-around"),
        pytest.param("it's in ./shot.png and the user's", id="apostrophes-after"),
        pytest.param("**./shot.png** is wrong", id="bold"),
        pytest.param("_shot.png_ is wrong", id="emphasis"),
        pytest.param("see [the shot](./shot.png).", id="markdown-link"),
        pytest.param("see ![shot](./shot.png)", id="markdown-image"),
    ],
)
def test_prose_and_markdown_around_a_path(work, text):
    [(_, picture)] = pictures(attach(text, work))
    assert picture.data == PNG


def test_a_fenced_block_is_output_not_a_request(work):
    text = "why does ls show this?\n```\nshot.png\nnotes.txt\n```\n"
    assert attach(text, work).content == text
    # Outside the fence it still counts.
    assert len(pictures(attach(text + "compare ./shot.png", work))) == 1


def test_a_file_too_large_to_send_is_not_even_read(work, monkeypatch):
    from pathlib import Path

    (work / "huge.png").write_bytes(b"\x00" * (images.MAX_IMAGE_BYTES + 1))
    read = []
    real = Path.read_bytes
    monkeypatch.setattr(Path, "read_bytes", lambda self: read.append(self) or real(self))
    attached = attach("./huge.png", work)
    assert read == [] and "too large to send" in attached.problems[0]


def test_paths_a_terminal_inserts_for_a_dropped_file(work):
    escaped = str(work / "My Shot.png").replace(" ", "\\ ")
    for text in (escaped, f"'{work / 'My Shot.png'}'", (work / "My Shot.png").as_uri()):
        attached = attach(f"{text} ", work)
        [(_, picture)] = pictures(attached)
        assert picture.data == PNG + b"\x01", text


def test_home_relative_paths(work, monkeypatch):
    monkeypatch.setenv("HOME", str(work))
    [(_, picture)] = pictures(attach("look at ~/shot.png", work.parent))
    assert picture.data == PNG


def test_text_without_an_image_is_left_as_text(work):
    for text in ("hello", "./notes.txt", "./missing.png", "/", "~", "a.png.b"):
        attached = attach(text, work)
        assert attached.content == text and not attached.names and not attached.problems


def test_one_image_named_twice_is_sent_once(work):
    attached = attach("./shot.png and shot.png and ./shot.png", work)
    assert len(pictures(attached)) == 1


def test_a_file_named_like_an_image_that_is_not_one_is_reported(work):
    attached = attach("./fake.png", work)
    assert attached.content == "./fake.png"
    assert attached.problems == ["./fake.png is not a PNG, JPEG, GIF or WebP image"]


def test_an_image_no_provider_would_take_is_reported_not_sent(work):
    (work / "huge.png").write_bytes(PNG + b"\x00" * images.MAX_IMAGE_BYTES)
    attached = attach("./huge.png", work)
    assert attached.content == "./huge.png"
    [problem] = attached.problems
    assert problem.startswith("./huge.png is too large to send")


def test_only_so_many_images_ride_with_one_prompt(work):
    names = []
    for n in range(MAX_PROMPT_IMAGES + 2):
        (work / f"{n}.png").write_bytes(PNG + bytes([n]))
        names.append(f"./{n}.png")
    attached = attach(" ".join(names), work)
    assert len(pictures(attached)) == MAX_PROMPT_IMAGES
    assert attached.problems == [f"Only the first {MAX_PROMPT_IMAGES} images were attached"]


def run_turn(work, prompt, *, steering=()):
    """Run one turn of a real runtime; return the requests the model got and the notices."""
    import asyncio

    from pydantic_ai import Agent
    from pydantic_ai.models.function import FunctionModel

    from pcode.agent import create_coder
    from pcode.live import AgentRuntime

    requests = []

    async def script(messages, info):
        requests.append(messages)
        yield "ok"

    runtime = AgentRuntime(
        Agent(FunctionModel(stream_function=script), capabilities=[create_coder(work)])
    )
    notes, warnings = [], []
    runtime.notice, runtime.warning_notice = notes.append, warnings.append
    pending = list(steering)
    runtime.take_steering = lambda: [pending.pop()] if pending else []
    runtime.has_steering = lambda: bool(pending)

    async def run():
        return [event async for event in runtime.stream(prompt)]

    asyncio.run(run())
    return requests, notes, warnings, runtime


def prompts(request):
    from pydantic_ai.messages import UserPromptPart

    return [part.content for part in request[-1].parts if isinstance(part, UserPromptPart)]


def test_a_turn_sends_the_images_its_prompt_names(work, monkeypatch):
    monkeypatch.setenv("PCODE_IMAGE_INPUT", "on")
    requests, notes, warnings, runtime = run_turn(work, "what is off in ./shot.png and ./fake.png?")
    [content] = prompts(requests[0])
    assert content[0] == "what is off in ./shot.png and ./fake.png?"
    assert [item.data for item in content if isinstance(item, BinaryContent)] == [PNG]
    assert notes == ["Attached 1 image: ./shot.png"]
    assert warnings == ["Image not attached: ./fake.png is not a PNG, JPEG, GIF or WebP image."]
    # The conversation's record of the prompt is the text: resend and recall attach again.
    assert runtime.resend_prompt() == "what is off in ./shot.png and ./fake.png?"


def test_a_model_of_unknown_sight_is_warned_about_and_sent_a_note(work):
    requests, notes, warnings, _ = run_turn(work, "look at ./shot.png")
    [content] = prompts(requests[0])
    assert not any(isinstance(item, BinaryContent) for item in content)
    assert any("Image omitted" in item for item in content if isinstance(item, str))
    assert notes == ["Attached 1 image: ./shot.png"]
    assert len(warnings) == 1 and "may not see images" in warnings[0]


def test_a_steering_message_brings_the_images_it_names(work, monkeypatch):
    monkeypatch.setenv("PCODE_IMAGE_INPUT", "on")
    requests, notes, _, _ = run_turn(work, "start", steering=["and ./shot.png too"])
    contents = [c for request in requests for c in prompts(request) if not isinstance(c, str)]
    assert [item.data for item in contents[0] if isinstance(item, BinaryContent)] == [PNG]
    assert notes == ["Attached 1 image: ./shot.png"]


class FakeHelpers:
    """Stand-ins for the clipboard commands, so no test reads the real clipboard."""

    def __init__(self, monkeypatch, installed, outputs):
        import pcode.clipboard as clipboard

        self.calls = []
        monkeypatch.delenv("SSH_CONNECTION", raising=False)
        monkeypatch.delenv("SSH_TTY", raising=False)
        monkeypatch.setattr(
            clipboard.shutil, "which", lambda name: f"/bin/{name}" if name in installed else None
        )

        def run(command, **kwargs):
            import subprocess

            self.calls.append(command[0])
            out = outputs.get(command[0])
            return subprocess.CompletedProcess(command, 0 if out else 1, out or b"", b"")

        monkeypatch.setattr(clipboard.subprocess, "run", run)


def test_a_mac_clipboard_image_comes_back_from_applescript(monkeypatch):
    from pcode import clipboard

    monkeypatch.setattr(clipboard.sys, "platform", "darwin")
    hex_png = "«data PNGf" + PNG.hex().upper() + "»\n"
    FakeHelpers(monkeypatch, {"osascript"}, {"osascript": hex_png.encode()})
    assert clipboard.image() == (PNG, "")


def test_pngpaste_is_preferred_when_installed(monkeypatch):
    from pcode import clipboard

    monkeypatch.setattr(clipboard.sys, "platform", "darwin")
    fake = FakeHelpers(monkeypatch, {"pngpaste", "osascript"}, {"pngpaste": PNG})
    assert clipboard.image() == (PNG, "")
    assert fake.calls == ["pngpaste"]


def test_a_failing_pngpaste_falls_back_to_applescript(monkeypatch):
    from pcode import clipboard

    monkeypatch.setattr(clipboard.sys, "platform", "darwin")
    hex_png = ("«data PNGf" + PNG.hex() + "»").encode()
    fake = FakeHelpers(monkeypatch, {"pngpaste", "osascript"}, {"osascript": hex_png})
    assert clipboard.image() == (PNG, "")
    assert fake.calls == ["pngpaste", "osascript"]


def run_paste(monkeypatch, clipboard_result, draft="look at"):
    """Run /paste-image's work in an app whose draft is `draft`; return draft, notes, warnings."""
    import asyncio
    from io import StringIO
    from types import SimpleNamespace

    from prompt_toolkit.buffer import Buffer
    from rich.console import Console

    from pcode import clipboard
    from pcode.app import PreviewApp

    monkeypatch.setattr(clipboard, "image", lambda: clipboard_result)
    app = PreviewApp(model="test:local", console=Console(file=StringIO()))
    buffer = Buffer()
    buffer.insert_text(draft)
    app.prompt_session = SimpleNamespace(
        default_buffer=buffer, app=SimpleNamespace(invalidate=lambda: None)
    )
    notes, warnings = [], []
    monkeypatch.setattr(app.transcript, "note", notes.append)
    monkeypatch.setattr(app.transcript, "warning", warnings.append)
    asyncio.run(app._paste_image())
    return buffer.text, notes, warnings


def test_paste_image_names_the_saved_picture_in_the_draft(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    text, notes, warnings = run_paste(monkeypatch, (PNG, ""))
    assert warnings == [] and notes[0].startswith("Pasted an image: ")
    named = text.removeprefix("look at ").strip()
    assert text.startswith("look at ") and text.endswith(" ")
    # What it inserted is a path the turn will attach.
    [(_, picture)] = pictures(attach(text, tmp_path))
    assert picture.data == PNG and named in notes[0]


def test_paste_image_says_why_when_there_is_nothing_to_paste(monkeypatch):
    text, notes, warnings = run_paste(monkeypatch, (None, "There is no image on the clipboard"))
    assert text == "look at" and notes == []
    assert warnings[0].startswith("There is no image on the clipboard.")


def test_a_paste_too_large_to_send_is_refused_at_once(tmp_path, monkeypatch):
    from pcode import prompt_images

    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setattr(prompt_images.sys, "platform", "linux")  # no sips to shrink it
    wide = PNG[:16] + (9000).to_bytes(4, "big") + PNG[20:]
    text, notes, warnings = run_paste(monkeypatch, (wide, ""))
    assert text == "look at" and notes == []
    assert "too large to send" in warnings[0] and "9000x48 pixels" in warnings[0]


@pytest.mark.parametrize(
    ("platform", "installed", "outputs", "why"),
    [
        ("darwin", {"osascript"}, {}, "no image on the clipboard"),
        ("darwin", {"osascript"}, {"osascript": b"text, not data"}, "no image on the clipboard"),
        ("linux", set(), {}, "needs wl-paste or xclip"),
        ("linux", {"xclip"}, {"xclip": b"not a png"}, "no image on the clipboard"),
    ],
)
def test_no_image_says_why(monkeypatch, platform, installed, outputs, why):
    from pcode import clipboard

    monkeypatch.setattr(clipboard.sys, "platform", platform)
    FakeHelpers(monkeypatch, installed, outputs)
    data, reason = clipboard.image()
    assert data is None and why in reason


def test_linux_tries_wayland_then_x11(monkeypatch):
    from pcode import clipboard

    monkeypatch.setattr(clipboard.sys, "platform", "linux")
    fake = FakeHelpers(monkeypatch, {"wl-paste", "xclip"}, {"xclip": PNG})
    assert clipboard.image() == (PNG, "")
    assert fake.calls == ["wl-paste", "xclip"]


def test_over_ssh_the_local_clipboard_is_not_read(monkeypatch):
    from pcode import clipboard

    fake = FakeHelpers(monkeypatch, {"pngpaste", "wl-paste"}, {"pngpaste": PNG, "wl-paste": PNG})
    monkeypatch.setenv("SSH_CONNECTION", "1.2.3.4 1 5.6.7.8 22")
    data, reason = clipboard.image()
    assert data is None and "ssh" in reason and fake.calls == []


def test_a_paste_is_saved_once_and_old_ones_are_cleared(tmp_path, monkeypatch):
    import os
    import time

    from pcode.prompt_images import PASTE_DAYS, pastes_path, reference, save_paste

    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    first = save_paste(PNG)
    assert save_paste(PNG) == first and first.parent == pastes_path()
    assert first.read_bytes() == PNG and first.name.startswith("paste-")
    stale = pastes_path() / "paste-000000000000.png"
    stale.write_bytes(PNG)
    old = time.time() - (PASTE_DAYS + 1) * 24 * 60 * 60
    os.utime(stale, (old, old))
    save_paste(PNG + b"\x01")
    assert not stale.exists() and first.exists()
    # The path a prompt gets names the file the turn will attach.
    [(_, picture)] = pictures(attach(f"see {reference(first)}", tmp_path))
    assert picture.data == PNG


def test_a_path_with_a_space_is_quoted_and_still_found(tmp_path, monkeypatch):
    from pcode.prompt_images import reference

    folder = tmp_path / "with space"
    folder.mkdir()
    (folder / "a.png").write_bytes(PNG)
    named = reference(folder / "a.png")
    assert named.startswith('"') and named.endswith('"')
    assert len(pictures(attach(f"look {named} please", tmp_path))) == 1


def test_a_directory_named_like_an_image_is_not_read(work):
    (work / "odd.png").mkdir()
    assert attach("./odd.png", work).content == "./odd.png"
