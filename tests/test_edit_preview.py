import asyncio
import json
from unittest.mock import Mock

import pytest
from pydantic_ai.messages import (
    FunctionToolCallEvent,
    PartDeltaEvent,
    PartEndEvent,
    PartStartEvent,
    ToolCallPart,
    ToolCallPartDelta,
)

from pcode.edit_preview import StreamingEditPreview
from pcode.edits import MAX_SOURCE
from pcode.runtime import EditPreview


def start(args, *, index=0, call_id="a", name="edit_file"):
    return PartStartEvent(index=index, part=ToolCallPart(name, args, tool_call_id=call_id))


def test_partial_arguments_only_show_complete_lines_and_complete_paths(monkeypatch):
    monkeypatch.setattr("pcode.edit_preview.monotonic", lambda: 100)
    preview = StreamingEditPreview()
    assert not preview.update(start('{"path":"example'))
    preview.updated.clear()
    result = preview.update(
        PartDeltaEvent(
            index=0,
            delta=ToolCallPartDelta(
                args_delta='.py","old_text":"old\\n","new_text":"new\\npartial'
            ),
        )
    )
    assert result[-1].path == "example.py"
    assert result[-1].text == "-old\n+new"
    assert "partial" not in result[-1].text
    end = ToolCallPart(
        "edit_file",
        {"path": "example.py", "old_text": "old\n", "new_text": "new\npartial"},
        tool_call_id="a",
    )
    assert "+partial" in preview.update(PartEndEvent(index=0, part=end))[-1].text
    assert preview.update(FunctionToolCallEvent(end)) == [EditPreview("edit-preview:0")]
    assert not preview.parts


def test_parallel_previews_clear_only_the_settled_call():
    preview = StreamingEditPreview()
    args = {"path": "example.py", "content": "new\n"}
    first = start(args, name="write_file")
    second = start(args, index=1, call_id="b", name="write_file")
    assert preview.update(first)
    assert preview.update(second)
    assert preview.update(FunctionToolCallEvent(first.part)) == [EditPreview("edit-preview:0")]
    assert 1 in preview.shown
    assert preview.update(FunctionToolCallEvent(second.part)) == [EditPreview("edit-preview:1")]


def test_preview_bounds_and_sanitizes():
    preview = StreamingEditPreview()
    args = json.dumps({"path": "example.py", "content": 'token = "first\nsecond'})[:-2]
    events = preview.update(start(args, name="write_file"))
    assert "first" not in repr(events) and "second" not in repr(events)
    assert preview.update(start("x" * (MAX_SOURCE * 2 + 1), name="write_file")) == [
        EditPreview("edit-preview:0")
    ]
    assert not preview.parts


def test_toggle_persists_and_requests_redraw(tmp_path):
    from io import StringIO

    from rich.console import Console

    from pcode.app import PreviewApp
    from pcode.preferences import load_preferences

    app = PreviewApp(console=Console(file=StringIO()))
    app.transcript.regenerate = Mock()
    app.show_edits("off")
    assert not app.transcript.show_edits
    assert load_preferences()["show_edits"] == "off"
    app.show_edits("")
    assert app.transcript.show_edits
    assert load_preferences()["show_edits"] == "on"
    assert app.transcript.regenerate.call_count == 2
    with pytest.raises(ValueError, match="Usage"):
        app.show_edits("bogus")
    app.present_events((EditPreview("one", "x.py", "+proposed"),))
    assert app.activity.edit_previews
    app.present_events((EditPreview("one"),))
    assert not app.activity.edit_previews


@pytest.mark.parametrize("outcome", ["done", "cancel", "error"])
def test_runtime_streams_previews_but_never_saves_them(tmp_path, outcome):
    from pydantic_ai import Agent
    from pydantic_ai.capabilities import LocalWorkspace
    from pydantic_ai.messages import ToolReturnPart
    from pydantic_ai.models.function import DeltaToolCall, FunctionModel

    from pcode.filesystem import DisplayFileSystem
    from pcode.live import AgentRuntime
    from pcode.runtime import EditCompleted
    from pcode.sessions import SavedSession

    async def model(messages, info):
        if any(isinstance(p, ToolReturnPart) for m in messages for p in m.parts):
            yield "Done"
            return
        yield {
            0: DeltaToolCall(
                name="write_file",
                tool_call_id="one",
                json_args='{"path":"example.py","content":"PREVIEW_ONLY\\n',
            )
        }
        await asyncio.sleep(0.06)
        if outcome == "cancel":
            raise asyncio.CancelledError
        if outcome == "error":
            raise RuntimeError("test error")
        yield {0: DeltaToolCall(json_args='FINAL_LINE\\n"}')}

    async def exercise():
        saved = SavedSession.create("test:local", tmp_path, tmp_path / "sessions")
        agent = Agent(
            FunctionModel(stream_function=model),
            capabilities=[LocalWorkspace(tmp_path), DisplayFileSystem()],
        )
        runtime = AgentRuntime(agent, saved)
        observed = []
        try:
            async for event in runtime.stream("write"):
                observed.append(event)
                if isinstance(event, EditPreview) and event.path:
                    assert not (tmp_path / "example.py").exists()
        except asyncio.CancelledError, RuntimeError:
            assert outcome != "done"
        try:
            assert any(isinstance(e, EditPreview) and "+PREVIEW_ONLY" in e.text for e in observed)
            records = list(saved.records())
            assert not any(r["kind"] == "EditPreview" for r in records)
            assert any(isinstance(e, EditCompleted) for e in observed) == (outcome == "done")
        finally:
            runtime.close()

    asyncio.run(exercise())


def test_streamed_outside_paths_are_hidden(tmp_path):
    (tmp_path / "alias.py").symlink_to(tmp_path.parent / "outside.py")
    preview = StreamingEditPreview(tmp_path)
    for path in ("alias.py", "../outside.py", str(tmp_path / "absolute.py")):
        assert not preview.update(
            start(
                {"path": path, "old_text": "synthetic confidential text", "new_text": "replacement"}
            )
        )


def test_unquoted_credentials_are_redacted_in_partial_arguments(tmp_path):
    preview = StreamingEditPreview(tmp_path)
    events = preview.update(
        start(
            json.dumps(
                {
                    "path": "example.ini",
                    "content": "token = synthetic-value\npasswd = other-value\nunfinished",
                }
            )[:-2],
            name="write_file",
        )
    )
    assert events and "[redacted]" in events[-1].text
    assert "synthetic-value" not in repr(events)
    assert "other-value" not in repr(events)


def test_batched_replacement_preview_waits_for_complete_strings(tmp_path):
    preview = StreamingEditPreview(tmp_path)
    result = preview.update(
        start(
            '{"path":"example.py","replacements":['
            '{"old_text":"one\\n","new_text":"first\\n"},'
            '{"old_text":"two\\n","new_text":"second\\nunfinished'
        )
    )
    assert result[-1].text == "-one\n+first\n-two\n+second"
    assert "unfinished" not in result[-1].text
    event = PartEndEvent(
        index=0,
        part=ToolCallPart(
            "edit_file",
            {
                "path": "example.py",
                "replacements": [
                    {"old_text": "one\n", "new_text": "first\n"},
                    {"old_text": "two\n", "new_text": "second\nunfinished"},
                ],
            },
            tool_call_id="a",
        ),
    )
    assert "+unfinished" in preview.update(event)[-1].text
