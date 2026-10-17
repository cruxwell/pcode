"""`read_file` shows the model an image instead of a note that it is binary.

These run the real coder under a scripted model, so what is asserted is what a
provider adapter would be handed.
"""

import asyncio
import json

import pytest
from pydantic_ai import Agent
from pydantic_ai.messages import BinaryContent, ModelRequest, ToolReturnPart
from pydantic_ai.models.function import DeltaToolCall, FunctionModel
from pydantic_ai.workspaces import LocalWorkspaceBackend
from pydantic_ai_harness.filesystem import FileReadEvent

from pcode import images, model_metadata
from pcode.agent import create_coder
from pcode.filesystem import DisplayFileSystem
from pcode.model_metadata import ContextCatalog

PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
    + (640).to_bytes(4, "big")
    + (480).to_bytes(4, "big")
    + bytes(range(256)) * 4
)
JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00" + bytes(range(256)) * 4
# No null byte anywhere: Harness's own check would call this text and decode it.
GIF = b"GIF89a\x01\x02" + bytes(range(1, 256)) * 4
WEBP = b"RIFF\x24\x00\x00\x00WEBPVP8 " + bytes(range(256)) * 4


@pytest.fixture(autouse=True)
def show_images(monkeypatch):
    """The scripted model is not in any catalog: say it sees, unless a test says otherwise."""
    monkeypatch.setenv("PCODE_IMAGE_INPUT", "on")


def shown(workspace, path, *, tool="read_file", toolsets=()):
    """What the model is handed after it reads `path` (or calls another `tool` with it)."""
    seen = []

    async def script(messages, info):
        returned = [
            part
            for message in messages
            if isinstance(message, ModelRequest)
            for part in message.parts
            if isinstance(part, ToolReturnPart)
        ]
        if returned:
            seen.append(returned[-1].content)
            yield "done"
        else:
            yield {0: DeltaToolCall(name=tool, json_args=json.dumps({"path": path}))}

    agent = Agent(
        FunctionModel(stream_function=script),
        capabilities=[create_coder(workspace)],
        toolsets=list(toolsets),
    )
    asyncio.run(agent.run("look at it"))
    return seen[0]


def picture(content):
    """The one image in a result, with the text that came with it."""
    assert isinstance(content, list), content
    text = [item for item in content if isinstance(item, str)]
    files = [item for item in content if isinstance(item, BinaryContent)]
    assert len(files) == 1
    return "".join(text), files[0]


@pytest.mark.parametrize(
    ("name", "data", "media_type"),
    [
        pytest.param("shot.png", PNG, "image/png", id="png"),
        pytest.param("photo.jpg", JPEG, "image/jpeg", id="jpeg"),
        pytest.param("anim.gif", GIF, "image/gif", id="gif-without-a-null-byte"),
        pytest.param("pic.webp", WEBP, "image/webp", id="webp"),
        # The bytes decide, not the name.
        pytest.param("no-extension", PNG, "image/png", id="no-extension"),
        pytest.param("really-a-png.txt", PNG, "image/png", id="misnamed"),
    ],
)
def test_an_image_is_shown_as_the_file_itself(tmp_path, name, data, media_type):
    (tmp_path / name).write_bytes(data)
    text, file = picture(shown(tmp_path, name))
    assert file.data == data and file.media_type == media_type
    assert file.is_image
    # The path and size ride beside it, for a model that has to say which picture it means.
    assert name in text and str(len(data)) in text


def test_text_named_like_an_image_is_still_text(tmp_path):
    (tmp_path / "diagram.png").write_text("not really a picture\nsecond line\n")
    content = shown(tmp_path, "diagram.png")
    assert isinstance(content, str) and "not really a picture" in content


def test_other_binary_files_keep_their_note(tmp_path):
    (tmp_path / "data.bin").write_bytes(b"\x00\x01\x02" * 100)
    assert "Binary file" in shown(tmp_path, "data.bin")


def test_an_image_over_the_limit_is_described_not_sent(tmp_path):
    big = PNG + b"\x01" * images.MAX_IMAGE_BYTES
    (tmp_path / "huge.png").write_bytes(big)
    content = shown(tmp_path, "huge.png")
    assert isinstance(content, str)
    assert "Too large" in content and str(len(big)) in content


@pytest.mark.parametrize(
    ("size", "sent"),
    [
        pytest.param(images.MAX_IMAGE_BYTES, True, id="at-the-limit"),
        # Under 5,000,000 raw bytes, but over Anthropic's 5 MiB once base64 encoded.
        pytest.param(4_000_000, False, id="over-once-encoded"),
    ],
)
def test_the_size_limit_is_what_the_provider_measures(tmp_path, size, sent):
    (tmp_path / "shot.png").write_bytes(PNG + b"\x01" * (size - len(PNG)))
    content = shown(tmp_path, "shot.png")
    assert isinstance(content, list) is sent
    if sent:
        assert len(picture(content)[1].base64) <= 5 * 1024 * 1024


def test_a_picture_too_wide_for_the_provider_is_described(tmp_path):
    wide = PNG[:16] + (9000).to_bytes(4, "big") + (100).to_bytes(4, "big") + PNG[24:]
    (tmp_path / "wide.png").write_bytes(wide)
    content = shown(tmp_path, "wide.png")
    assert isinstance(content, str)
    assert "9000x100 pixels" in content and "smaller copy" in content


@pytest.mark.parametrize("wrapped", [False, True], ids=["list", "tool-return"])
def test_long_text_beside_a_picture_is_still_reduced(tmp_path, wrapped):
    from pydantic_ai.messages import ToolReturn
    from pydantic_ai.toolsets import FunctionToolset

    tools = FunctionToolset()

    # Standing in for an MCP tool: a page of text and a screenshot.
    @tools.tool_plain
    def snapshot(path: str):
        result = ["x" * 50_000, BinaryContent(data=PNG, media_type="image/png")]
        return ToolReturn(return_value=result) if wrapped else result

    text, file = picture(shown(tmp_path, "page", tool="snapshot", toolsets=[tools]))
    assert file.data == PNG
    assert len(text) < 20_000 and "x" * 100 in text


def test_short_text_beside_a_picture_is_left_alone(tmp_path):
    from pydantic_ai.toolsets import FunctionToolset

    tools = FunctionToolset()

    @tools.tool_plain
    def snapshot(path: str):
        return ["short", BinaryContent(data=PNG, media_type="image/png")]

    content = shown(tmp_path, "page", tool="snapshot", toolsets=[tools])
    assert content[0] == "short" and picture(content)[1].data == PNG


def test_a_missing_image_is_reported_as_missing(tmp_path):
    assert "Path not found" in shown(tmp_path, "gone.png")


def test_an_image_is_not_spilled_by_the_tool_output_limits(tmp_path):
    # Far over the threshold that sends a long text result to a file: a spilled
    # picture would reach the model as a preview of its own base64.
    data = PNG + b"\x07" * 200_000
    (tmp_path / "big.png").write_bytes(data)
    _, file = picture(shown(tmp_path, "big.png"))
    assert file.data == data


def test_a_model_that_cannot_see_is_told_so_and_history_keeps_the_picture(tmp_path, monkeypatch):
    monkeypatch.delenv("PCODE_IMAGE_INPUT")
    monkeypatch.setattr(model_metadata, "catalog", ContextCatalog())
    (tmp_path / "shot.png").write_bytes(PNG)
    content = shown(tmp_path, "shot.png")
    assert isinstance(content, str)
    assert "shot.png" in content and "Image omitted" in content
    assert "unknown" in content


def test_the_agents_own_history_still_holds_the_image_a_blind_model_was_denied(
    tmp_path, monkeypatch
):
    monkeypatch.delenv("PCODE_IMAGE_INPUT")
    monkeypatch.setattr(model_metadata, "catalog", ContextCatalog())
    (tmp_path / "shot.png").write_bytes(PNG)

    async def script(messages, info):
        if any(
            isinstance(m, ModelRequest) and m.parts[-1].part_kind == "tool-return" for m in messages
        ):
            yield "done"
        else:
            yield {0: DeltaToolCall(name="read_file", json_args='{"path": "shot.png"}')}

    agent = Agent(FunctionModel(stream_function=script), capabilities=[create_coder(tmp_path)])
    result = asyncio.run(agent.run("look"))
    [returned] = [
        part
        for message in result.all_messages()
        if isinstance(message, ModelRequest)
        for part in message.parts
        if isinstance(part, ToolReturnPart)
    ]
    # Switching to a model that sees must find the picture where it was left.
    assert picture(returned.content)[1].data == PNG


def test_a_saved_session_keeps_the_picture_and_logs_no_bytes(tmp_path):
    from pcode.live import AgentRuntime
    from pcode.runtime import ToolSummary
    from pcode.sessions import SavedSession

    root = tmp_path / "sessions"
    work = tmp_path / "work"
    work.mkdir()
    data = PNG + b"\x05" * 20_000
    (work / "shot.png").write_bytes(data)
    saved = SavedSession.create("test:local", work, root)

    async def script(messages, info):
        answered = any(
            isinstance(m, ModelRequest) and any(isinstance(p, ToolReturnPart) for p in m.parts)
            for m in messages
        )
        if answered:
            yield "It is a test card."
        else:
            yield {0: DeltaToolCall(name="read_file", json_args='{"path": "shot.png"}')}

    agent = Agent(FunctionModel(stream_function=script), capabilities=[create_coder(work)])

    async def run():
        runtime = AgentRuntime(agent, saved)
        events = [event async for event in runtime.stream("what is in shot.png?")]
        history = runtime.history
        runtime.close()
        reopened = SavedSession.open(saved.info.id[:8], root)
        try:
            restored = AgentRuntime(agent, reopened)
            await restored.restore()
            assert restored.history == history
            records = list(reopened.transcript_records())
        finally:
            reopened.close()
        return events, restored.history, records

    events, history, records = asyncio.run(run())
    returned = [
        part
        for message in history
        if isinstance(message, ModelRequest)
        for part in message.parts
        if isinstance(part, ToolReturnPart)
    ]
    assert picture(returned[0].content)[1].data == data
    # The terminal and the journal are told what it was, not shown its bytes.
    [summary] = [event for event in events if isinstance(event, ToolSummary)]
    assert "PNG image" in summary.detail and f"{len(data):,} bytes" in summary.detail
    assert len(summary.result) < 1000
    assert len(json.dumps(records)) < len(data) // 4


def test_the_model_is_told_it_can_look_at_images(tmp_path):
    seen = []

    async def script(messages, info):
        seen.append(info.instructions)
        yield "ok"

    agent = Agent(FunctionModel(stream_function=script), capabilities=[create_coder(tmp_path)])
    asyncio.run(agent.run("hi"))
    assert "read_file shows an image" in seen[0]


def test_a_direct_read_returns_the_image_too(tmp_path):
    (tmp_path / "shot.png").write_bytes(PNG)
    toolset = DisplayFileSystem().get_toolset()
    result = asyncio.run(toolset.read_file("shot.png", workspace=LocalWorkspaceBackend(tmp_path)))
    assert picture(result)[1].data == PNG


def test_reading_an_image_is_still_a_read(tmp_path):
    (tmp_path / "shot.png").write_bytes(PNG)
    events = []

    class Context:
        async def emit(self, event):
            events.append(event)

    toolset = DisplayFileSystem().get_toolset()

    async def run():
        scope = await toolset._scope(LocalWorkspaceBackend(tmp_path))
        return await toolset._read_file(scope, Context(), "shot.png")

    assert picture(asyncio.run(run()))[1].data == PNG
    assert [type(event) for event in events] == [FileReadEvent]
