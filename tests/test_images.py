"""Which files are images, and what a model that cannot see them is sent instead."""

import asyncio

import pytest
from pydantic_ai.messages import (
    BinaryContent,
    ImageUrl,
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models.test import TestModel

from pcode import images, model_metadata
from pcode.model_metadata import ContextCatalog, ModelLimits

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + b"\x00" * 16
JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00" + b"\x01" * 8
GIF = b"GIF89a" + b"\x01\x02\x03"
WEBP = b"RIFF\x24\x00\x00\x00WEBPVP8 "


@pytest.mark.parametrize(
    ("data", "media_type"),
    [
        (PNG, "image/png"),
        (JPEG, "image/jpeg"),
        (GIF, "image/gif"),
        (b"GIF87a" + b"\x00", "image/gif"),
        (WEBP, "image/webp"),
        # The bytes decide: an HTML page called cat.png is not an image, and a RIFF
        # file that is not WebP (a WAV) is not one either.
        (b"<html>not an image</html>", None),
        (b"RIFF\x24\x00\x00\x00WAVEfmt ", None),
        (b"%PDF-1.7", None),
        (b"", None),
    ],
)
def test_the_bytes_decide_what_is_an_image(data, media_type):
    assert images.image_type(data) == media_type


def known(monkeypatch, **inputs):
    """A catalog that lists the given `provider:model` names with their input kinds."""
    catalog = ContextCatalog()
    catalog.public = {
        name: ModelLimits(context=1000, inputs=kinds, source="models.dev")
        for name, kinds in inputs.items()
    }
    monkeypatch.setattr(model_metadata, "catalog", catalog)


def test_support_comes_from_the_catalog_and_unknown_is_not_no(monkeypatch):
    monkeypatch.delenv("PCODE_IMAGE_INPUT", raising=False)
    catalog = ContextCatalog()
    catalog.public = {
        "anthropic:sees": ModelLimits(context=1, inputs=("text", "image"), source="models.dev"),
        "anthropic:blind": ModelLimits(context=1, inputs=("text",), source="models.dev"),
        "anthropic:silent": ModelLimits(context=1, source="models.dev"),
    }
    monkeypatch.setattr(model_metadata, "catalog", catalog)
    assert images.image_support("anthropic:sees") is True
    assert images.image_support("anthropic:blind") is False
    assert images.image_support("anthropic:silent") is None
    assert images.image_support("anthropic:unlisted") is None
    assert images.accepts_images("anthropic:sees")
    assert not images.accepts_images("anthropic:blind")
    assert not images.accepts_images("anthropic:silent")


def test_a_claude_code_alias_is_still_claude(monkeypatch):
    monkeypatch.delenv("PCODE_IMAGE_INPUT", raising=False)
    known(monkeypatch)
    # Claude Code serves only Claude models, which all take images, so an alias the
    # catalog has no row for still counts. Another provider's unlisted model does not.
    assert images.image_support("claude:opus") is True
    assert images.image_support("openai:mystery") is None


@pytest.mark.parametrize(
    ("setting", "expected"), [("on", True), ("ON", True), ("off", False), (" Off ", False)]
)
def test_the_environment_settles_a_model_the_catalog_cannot(monkeypatch, setting, expected):
    known(monkeypatch, **{"anthropic:blind": ("text",)})
    monkeypatch.setenv("PCODE_IMAGE_INPUT", setting)
    assert images.image_support("anthropic:blind") is expected
    assert images.image_support("openai:mystery") is expected


def test_an_unrecognized_setting_changes_nothing(monkeypatch):
    known(monkeypatch, **{"anthropic:sees": ("text", "image")})
    monkeypatch.setenv("PCODE_IMAGE_INPUT", "maybe")
    assert images.image_support("anthropic:sees") is True
    assert images.image_support("openai:mystery") is None


PICTURE = BinaryContent(data=PNG, media_type="image/png")


def tool_request(content, name="read_file"):
    return ModelRequest(parts=[ToolReturnPart(tool_name=name, content=content, tool_call_id="1")])


def test_a_model_that_sees_is_sent_the_messages_untouched(monkeypatch):
    monkeypatch.delenv("PCODE_IMAGE_INPUT", raising=False)
    known(monkeypatch, **{"anthropic:sees": ("text", "image")})
    messages = [tool_request(["[a.png | image/png]", PICTURE])]
    assert images.degrade_images(messages, "anthropic:sees") is messages


def test_a_tool_returned_image_becomes_a_note_the_model_can_act_on(monkeypatch):
    monkeypatch.delenv("PCODE_IMAGE_INPUT", raising=False)
    known(monkeypatch, **{"anthropic:blind": ("text",)})
    original = tool_request(["[a.png | image/png | 33 bytes]\n", PICTURE])
    [degraded] = images.degrade_images([original], "anthropic:blind")
    [part] = degraded.parts
    # Text from the tool stays; what replaces the image says what it was and why it is gone.
    assert isinstance(part.content, str)
    assert part.content.startswith("[a.png | image/png | 33 bytes]")
    assert "Image omitted" in part.content
    assert "image/png" in part.content and f"{len(PNG)} bytes" in part.content
    assert "does not accept images" in part.content
    assert part.tool_call_id == "1" and part.tool_name == "read_file"
    # The history it was made from still holds the picture: a later turn may see it.
    assert original.parts[0].content[1] is PICTURE


def test_a_model_of_unknown_sight_is_told_how_to_change_that(monkeypatch):
    monkeypatch.delenv("PCODE_IMAGE_INPUT", raising=False)
    known(monkeypatch)
    [degraded] = images.degrade_images([tool_request(PICTURE)], "openai:mystery")
    assert "PCODE_IMAGE_INPUT=on" in degraded.parts[0].content
    assert "unknown" in degraded.parts[0].content


def test_prompt_images_are_replaced_in_place_and_other_parts_are_kept(monkeypatch):
    monkeypatch.delenv("PCODE_IMAGE_INPUT", raising=False)
    known(monkeypatch, **{"anthropic:blind": ("text",)})
    url = ImageUrl(url="https://example.com/cat.png")
    prompt = ModelRequest(parts=[UserPromptPart(content=["look at ./a.png", PICTURE, url])])
    answer = ModelResponse(parts=[TextPart("ok")])
    messages = [prompt, answer, tool_request("plain text")]
    degraded = images.degrade_images(messages, "anthropic:blind")
    assert degraded[1] is answer and degraded[2] is messages[2]
    content = degraded[0].parts[0].content
    assert content[0] == "look at ./a.png"
    assert "Image omitted" in content[1] and "Image omitted" in content[2]
    assert "https://example.com/cat.png" in content[2]
    assert prompt.parts[0].content[1] is PICTURE


def test_a_data_url_is_not_copied_into_the_note(monkeypatch):
    monkeypatch.delenv("PCODE_IMAGE_INPUT", raising=False)
    known(monkeypatch, **{"anthropic:blind": ("text",)})
    huge = ImageUrl(url="data:image/png;base64," + "A" * 100_000, media_type="image/png")
    [degraded] = images.degrade_images([tool_request(huge)], "anthropic:blind")
    assert len(degraded.parts[0].content) < 500


def numbered(count, size=10):
    """`count` tool results, oldest first, each holding its own picture of `size` bytes."""
    return [
        tool_request(
            [
                f"[{n}.png]",
                BinaryContent(data=PNG[:8] + bytes([n]) * (size - 8), media_type="image/png"),
            ]
        )
        for n in range(count)
    ]


def kept(messages):
    """The numbers of the results that still hold their picture."""
    return [n for n, m in enumerate(messages) if not isinstance(m.parts[0].content, str)]


def test_only_the_newest_images_ride_along(monkeypatch):
    known(monkeypatch, **{"anthropic:sees": ("text", "image")})
    messages = numbered(images.MAX_REQUEST_IMAGES + 5)
    degraded = images.degrade_images(messages, "anthropic:sees")
    # A whole step of the oldest goes, not just the five over the count.
    assert kept(degraded) == list(range(images.IMAGE_STEP, images.MAX_REQUEST_IMAGES + 5))
    # An older one says why it is gone and how to get it back.
    assert "Older images are left out" in degraded[0].parts[0].content
    assert "read the file again" in degraded[0].parts[0].content
    assert kept(messages) == list(range(len(messages)))


def test_the_line_moves_a_step_at_a_time_so_the_cached_prefix_holds(monkeypatch):
    known(monkeypatch, **{"anthropic:sees": ("text", "image")})
    limit, step = images.MAX_REQUEST_IMAGES, images.IMAGE_STEP
    firsts = [
        kept(images.degrade_images(numbered(count), "anthropic:sees"))[0]
        for count in range(limit + 1, limit + 2 * step + 1)
    ]
    # Ten new images in a row leave out the same old ones; the eleventh moves the line.
    assert firsts == [step] * step + [2 * step] * step


def test_the_bytes_ride_along_within_a_budget_too(monkeypatch):
    known(monkeypatch, **{"anthropic:sees": ("text", "image")})
    each = images.MAX_REQUEST_IMAGE_BYTES // 2 - 10
    messages = numbered(4, size=each)
    assert kept(images.degrade_images(messages, "anthropic:sees")) == [2, 3]


def test_once_over_the_line_everything_older_is_left_out(monkeypatch):
    known(monkeypatch, **{"anthropic:sees": ("text", "image")})
    big = images.MAX_REQUEST_IMAGE_BYTES // 2
    # The oldest would fit on its own, but keeping it would change which images
    # a request carries every time a newer one arrives.
    messages = [numbered(1)[0], *numbered(3, size=big)]
    assert kept(images.degrade_images(messages, "anthropic:sees")) == [2, 3]


def test_a_model_that_cannot_see_is_not_told_the_images_were_too_many(monkeypatch):
    known(monkeypatch, **{"anthropic:blind": ("text",)})
    degraded = images.degrade_images(numbered(2), "anthropic:blind")
    assert kept(degraded) == []
    assert all("does not accept images" in m.parts[0].content for m in degraded)


def test_a_request_within_the_budget_is_returned_as_it_came(monkeypatch):
    known(monkeypatch, **{"anthropic:sees": ("text", "image")})
    messages = numbered(images.MAX_REQUEST_IMAGES)
    assert images.degrade_images(messages, "anthropic:sees") is messages


def test_a_recovered_history_gets_back_what_a_request_left_out(monkeypatch):
    known(monkeypatch, **{"anthropic:blind": ("text",)})
    messages = [ModelRequest(parts=[UserPromptPart("hi")]), *numbered(2)]
    degraded = images.degrade_images(messages, "anthropic:blind")
    assert kept(degraded[1:]) == []
    restored = images.as_in_history(degraded)
    assert all(new is old for new, old in zip(restored, messages, strict=True))


def test_the_checkpoint_saves_the_history_not_the_request_it_was_sent(monkeypatch):
    from dataclasses import dataclass

    from pydantic_ai.messages import ToolCallPart

    from pcode.retries import RequestCheckpoint

    known(monkeypatch, **{"anthropic:blind": ("text",)})
    # A history a provider would take, which is all the checkpoint saves.
    messages = [
        ModelRequest(parts=[UserPromptPart("look")]),
        ModelResponse(parts=[ToolCallPart("read_file", {"path": "0.png"}, tool_call_id="1")]),
        numbered(1)[0],
    ]

    @dataclass
    class Context:
        model: object
        messages: list

    class Run:
        run_step = 1

    async def send():
        request = await images.ImageGate().before_model_request(
            None, Context("anthropic:blind", messages)
        )
        checkpoint = RequestCheckpoint()
        await checkpoint.before_model_request(Run(), request)
        return request, checkpoint

    request, checkpoint = asyncio.run(send())
    assert isinstance(request.messages[-1].parts[0].content, str)
    assert checkpoint.messages[-1].parts[0].content[1].data == messages[-1].parts[0].content[1].data


@pytest.mark.parametrize(
    ("data", "pixels"),
    [
        (PNG[:16] + (9000).to_bytes(4, "big") + (20).to_bytes(4, "big"), (9000, 20)),
        (b"GIF89a" + (640).to_bytes(2, "little") + (480).to_bytes(2, "little"), (640, 480)),
        (JPEG, None),
        (b"\x89PNG\r\n\x1a\n", None),
    ],
)
def test_the_header_says_how_big_a_picture_is(data, pixels):
    assert images.image_pixels(data) == pixels


def test_a_file_in_a_tool_result_is_one_however_it_is_wrapped():
    from pydantic_ai.messages import ToolReturn

    assert images.has_files(PICTURE)
    assert images.has_files(["text", PICTURE])
    assert images.has_files(("text", PICTURE))
    assert images.has_files(ToolReturn(return_value=["text", PICTURE]))
    assert not images.has_files("text")
    assert not images.has_files(["a", "b"])
    assert not images.has_files({"data": "x"})
    # Text with a picture beside it: the text can still be reduced, the picture is left alone.
    assert not images.has_files(ToolReturn(return_value="text", content=[PICTURE]))


def test_documents_and_audio_are_not_this_gates_business(monkeypatch):
    monkeypatch.delenv("PCODE_IMAGE_INPUT", raising=False)
    known(monkeypatch, **{"anthropic:blind": ("text",)})
    pdf = BinaryContent(data=b"%PDF-1.7", media_type="application/pdf")
    messages = [tool_request(["[a.pdf]", pdf])]
    assert images.degrade_images(messages, "anthropic:blind") is messages


def test_the_gate_leaves_the_request_alone_without_images():
    class Context:
        model = TestModel()
        messages = [ModelRequest(parts=[UserPromptPart("hello")])]

    request = Context()
    assert asyncio.run(images.ImageGate().before_model_request(None, request)) is request


def test_the_gate_swaps_images_for_a_model_that_cannot_take_them(monkeypatch):
    from dataclasses import dataclass

    monkeypatch.delenv("PCODE_IMAGE_INPUT", raising=False)
    known(monkeypatch)
    refreshed = []

    async def refresh(model):
        refreshed.append(model)

    monkeypatch.setattr(images, "refresh_context", refresh)

    @dataclass
    class Context:
        model: object
        messages: list

    model = TestModel()
    request = Context(model, [tool_request(["x", PICTURE])])
    result = asyncio.run(images.ImageGate().before_model_request(None, request))
    assert refreshed == [model]
    assert "Image omitted" in result.messages[0].parts[0].content
    assert isinstance(request.messages[0].parts[0].content, list)
