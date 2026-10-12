import asyncio
from io import StringIO

import pytest
from pydantic_ai import ModelRetry
from pydantic_ai.workspaces import LocalWorkspaceBackend, Workspace
from rich.console import Console

from pcode.edits import MAX_SOURCE, completed_change, dedent_patch
from pcode.filesystem import DisplayFileSystem, FileChangeEvent
from pcode.runtime import EditCompleted
from pcode.ui import Transcript


class Context:
    """The parts of a run context the file tools use, on a local workspace."""

    tool_call_id = "edit-1"
    tool_name = None
    tool_manager = None

    def __init__(self, root):
        self.events = []
        self.workspace = Workspace(LocalWorkspaceBackend(root))

    async def emit(self, event):
        self.events.append(event)

    @property
    def changes(self):
        return [event.change for event in self.events if isinstance(event, FileChangeEvent)]


def test_mutations_capture_actual_contents_and_failed_edits_emit_nothing(tmp_path):
    async def exercise():
        toolset = DisplayFileSystem().get_toolset()
        ctx = Context(tmp_path)
        await toolset._write_file_tool(ctx, "example.py", "context\r\nold\r\n")
        created = ctx.changes[-1]
        assert created.operation == "created" and created.added == 2
        assert "--- /dev/null" in created.patch
        await toolset._edit_file_tool(ctx, "example.py", "old", "new")
        changed = ctx.changes[-1]
        assert " context" in changed.patch
        assert "-old" in changed.patch and "+new" in changed.patch
        assert (changed.added, changed.removed) == (1, 1)
        assert (tmp_path / "example.py").read_bytes() == b"context\r\nnew\r\n"
        await toolset._write_file_tool(ctx, "example.py", "overwrite\n")
        assert "-new" in ctx.changes[-1].patch
        await toolset._edit_file_tool(ctx, "example.py", "overwrite", "overwrite")
        assert ctx.changes[-1].operation == "unchanged"
        count = len(ctx.changes)
        for args in [("missing", "new", None), ("overwrite", "new", "stale")]:
            with pytest.raises(ModelRetry):
                await toolset._edit_file_tool(ctx, "example.py", *args[:2], expected_hash=args[2])
        with pytest.raises(ModelRetry):
            await toolset._write_file_tool(ctx, "example.py", "bad", expected_hash="stale")
        assert len(ctx.changes) == count
        assert (tmp_path / "example.py").read_text() == "overwrite\n"

    asyncio.run(exercise())


def test_binary_large_named_path_and_missing_newline():
    assert completed_change("image", "\0", "text").omitted == "Binary content"
    assert completed_change("large", "x" * (MAX_SOURCE + 1), "small").omitted
    # A secret-looking name is shown and diffed like any other file.
    change = completed_change("token_accounting.py", "old\n", "new\n")
    assert change.path == "token_accounting.py" and (change.added, change.removed) == (1, 1)
    assert r"\ No newline at end of file" in completed_change("x", "old", "new\n").patch


def test_completed_diffs_retained_while_hidden_and_reprojected():
    stream = StringIO()
    console = Console(file=stream, width=50)
    transcript = Transcript(console)
    change = completed_change("example.py", "old\n", "new\n")
    transcript.show_edits = False
    transcript.edit(change)
    assert not stream.getvalue()
    assert len(transcript.log.entries) == 1
    transcript.show_edits = True
    for objects, end, _ in transcript.replay():
        console.print(*objects, end=end)
    assert stream.getvalue().count("Edited example.py") == 1
    assert "+new" in stream.getvalue()
    assert len(transcript.log.entries) == 1
    transcript.show_edits = False
    assert transcript.replay() == []


def test_live_edit_previews_are_opt_in_and_follow_show_edits():
    console = Console(file=StringIO())
    assert not Transcript(console, preferences={}).live_edit_previews
    transcript = Transcript(console, preferences={"live_edits": "on"})
    assert transcript.live_edit_previews
    transcript.show_edits = False
    assert not transcript.live_edit_previews


def test_read_only_adapter_never_exposes_writes(tmp_path):
    original = DisplayFileSystem(read_only=True)
    assert original.get_toolset().__class__.__name__ == "FilteredToolset"


def test_new_empty_file_is_not_an_unchanged_existing_file():
    assert completed_change("x", "", "", existed=False).operation == "created"
    assert completed_change("x", "", "").operation == "unchanged"


def test_completed_changes_survive_resume_without_rereading_files(tmp_path):
    import json

    from pydantic_ai import Agent
    from pydantic_ai.capabilities import LocalWorkspace
    from pydantic_ai.messages import ToolReturnPart
    from pydantic_ai.models.function import DeltaToolCall, FunctionModel

    from pcode.app import PreviewApp
    from pcode.live import AgentRuntime
    from pcode.sessions import SavedSession

    async def model(messages, info):
        if any(isinstance(p, ToolReturnPart) for m in messages for p in m.parts):
            yield "Done"
        else:
            yield {
                0: DeltaToolCall(
                    name="write_file",
                    json_args=json.dumps(
                        {
                            "path": "sample.py",
                            "content": "saved_change\n",
                        }
                    ),
                    tool_call_id="write-1",
                )
            }

    async def exercise():
        saved = SavedSession.create("test:local", tmp_path, tmp_path / "sessions")
        identity = saved.info.id
        agent = Agent(
            FunctionModel(stream_function=model),
            capabilities=[LocalWorkspace(tmp_path), DisplayFileSystem()],
        )
        runtime = AgentRuntime(agent, saved)
        events = [event async for event in runtime.stream("write a file")]
        assert len([e for e in events if isinstance(e, EditCompleted)]) == 1
        runtime.close()
        (tmp_path / "sample.py").write_text("unrelated later contents\n")
        reopened = SavedSession.open(identity, tmp_path / "sessions")
        try:
            restored = AgentRuntime(agent, reopened)
            app = PreviewApp(model="test:local", runtime=restored, console=Console(file=StringIO()))
            app.transcript.show_edits = False
            app.replay()
            app.transcript.show_edits = True
            output = StringIO()
            console = Console(file=output, theme=app.transcript.rich_theme)
            for objects, end, _ in app.transcript.replay():
                console.print(*objects, end=end)
            assert "+saved_change" in output.getvalue()
            assert "unrelated later contents" not in output.getvalue()
            assert output.getvalue().count("Created sample.py") == 1
        finally:
            reopened.close()

    asyncio.run(exercise())


def test_write_only_destination_still_writes_without_a_diff(tmp_path):
    path = tmp_path / "write_only.py"
    path.write_text("old\n")
    path.chmod(0o200)

    async def exercise():
        ctx = Context(tmp_path)
        toolset = DisplayFileSystem().get_toolset()
        await toolset._write_file_tool(ctx, path.name, "new\n")
        assert ctx.changes[-1].omitted == "Before snapshot unavailable"
        assert not ctx.changes[-1].patch

    try:
        asyncio.run(exercise())
    finally:
        path.chmod(0o600)
    assert path.read_text() == "new\n"


def test_overwrite_non_utf8_content_and_missing_parent(tmp_path):
    path = tmp_path / "binary"
    path.write_bytes(b"\xff\xfe")

    async def exercise():
        ctx = Context(tmp_path)
        toolset = DisplayFileSystem().get_toolset()
        await toolset._write_file_tool(ctx, "binary", "text\n")
        assert ctx.changes[-1].omitted == "Binary or non-UTF-8 content"
        count = len(ctx.changes)
        with pytest.raises(ModelRetry):
            await toolset._write_file_tool(ctx, "missing/child", "no")
        with pytest.raises(ModelRetry):
            await toolset._write_file_tool(ctx, ".env", "no")
        assert len(ctx.changes) == count

    asyncio.run(exercise())
    assert path.read_text() == "text\n"


def test_parallel_writes_capture_their_own_operation(tmp_path):
    async def exercise():
        toolset = DisplayFileSystem().get_toolset()
        contexts = [Context(tmp_path), Context(tmp_path)]
        # Two tool calls in one run share its workspace, which Harness locks per file.
        contexts[1].workspace = contexts[0].workspace
        await asyncio.gather(
            toolset._write_file_tool(contexts[0], "x.py", "first\n"),
            toolset._write_file_tool(contexts[1], "x.py", "second\n"),
        )
        # Either may take the file's lock first; each reports its own write,
        # and the later one diffs from the earlier one's content.
        first, second = contexts[0].changes[-1], contexts[1].changes[-1]
        assert "+first" in first.patch and "+second" in second.patch
        created, replaced = sorted([first, second], key=lambda c: c.operation != "created")
        assert created.operation == "created" and replaced.operation == "edited"
        earlier = "first" if created is first else "second"
        assert f"-{earlier}" in replaced.patch

    asyncio.run(exercise())


def test_edits_are_shown_as_written_even_where_they_look_like_credentials():
    code = 'input_token: int = 0\npassword = form.data["pw"]\n'
    change = completed_change("example.py", "", code + "api_key = sk-0123456789ab\n")
    assert "+api_key = sk-0123456789ab" in change.patch
    assert "+input_token: int = 0" in change.patch
    assert '+password = form.data["pw"]' in change.patch


def test_external_mutations_keep_absolute_evidence_paths(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    external = tmp_path / "external"
    external.mkdir()
    target = external / "sample.py"
    (workspace / "alias.py").symlink_to(target)

    async def exercise():
        ctx = Context(workspace)
        toolset = DisplayFileSystem().get_toolset()
        with pytest.raises(ModelRetry, match="does not exist"):
            await toolset._write_file_tool(ctx, str(external / "missing" / "sample.py"), "no")
        assert not ctx.changes
        await toolset._write_file_tool(ctx, "../external/sample.py", "old\n")
        assert ctx.changes[-1].path == str(target)
        assert ctx.changes[-1].operation == "created"
        assert "+old" in ctx.changes[-1].patch
        await toolset._edit_file_tool(ctx, "alias.py", "old", "new")
        assert ctx.changes[-1].path == str(target)
        assert "-old" in ctx.changes[-1].patch and "+new" in ctx.changes[-1].patch
        await toolset._write_file_tool(ctx, str(target), "last\n")
        assert ctx.changes[-1].path == str(target)
        assert "-new" in ctx.changes[-1].patch and "+last" in ctx.changes[-1].patch
        assert target.read_text() == "last\n"

    asyncio.run(exercise())


def test_dedent_patch_strips_each_hunks_shared_indent():
    patch = "\n".join(
        [
            "--- a/x.py",
            "+++ b/x.py",
            "@@ -1,3 +1,3 @@",
            "         if ready:",
            "-            go()",
            "+            run()",
            "",
            "@@ -20,2 +20,3 @@",
            "     def f():",
            "-        return 1",
            "+        return 2",
            "+",
            r"\ No newline at end of file",
        ]
    )
    assert dedent_patch(patch).split("\n") == [
        "--- a/x.py",
        "+++ b/x.py",
        "@@ -1,3 +1,3 @@",
        " if ready:",
        "-    go()",
        "+    run()",
        "",
        "@@ -20,2 +20,3 @@",
        " def f():",
        "-    return 1",
        "+    return 2",
        "+",
        r"\ No newline at end of file",
    ]


def test_dedent_patch_reads_hunks_by_their_counts():
    # A removed "-- x" line looks like a file header, and an unindented line
    # anywhere in the hunk means there is nothing to strip.
    patch = "@@ -1,2 +1 @@\n-    -- x\n-    y\n+    z"
    assert dedent_patch(patch) == "@@ -1,2 +1 @@\n--- x\n-y\n+z"
    flush = "@@ -1 +1 @@\n-top\n+    nested"
    assert dedent_patch(flush) == flush


def test_diff_dedent_setting_controls_rendered_indent():
    before = "class A:\n" + "".join(f"    def f{n}(self):\n        return {n}\n" for n in range(9))
    after = before.replace("return 4", "return 40")
    change = completed_change("a.py", before, after)

    def rendered(dedent: str) -> str:
        stream = StringIO()
        console = Console(file=stream, width=60)
        preferences = {"diff_renderer": "rich", "diff_dedent": dedent}
        Transcript(console, preferences=preferences).edit(change)
        return stream.getvalue()

    assert "\n-    return 4\n" in rendered("on")
    assert "\n-        return 4\n" in rendered("off")


def test_dedent_patch_keeps_whitespace_changes_and_skips_malformed_hunks():
    # Trailing spaces removed from a blank line stay visible past the shared indent.
    whitespace = "@@ -1,2 +1,2 @@\n         x\n-          \n+"
    assert dedent_patch(whitespace) == "@@ -1,2 +1,2 @@\n x\n-  \n+"
    # A line without a marker breaks the counts: dedenting half the hunk would
    # misalign it, so none of it is.
    broken = "@@ -1,3 +1,3 @@\n     a\nc\n+    d"
    assert dedent_patch(broken) == broken


def test_change_counts_lines_as_git_does():
    # U+2028 inside a line is not a line break: replacing that line is -1 +1.
    change = completed_change("a.py", "s = 'a\u2028b'\n", "s = 'c'\n")
    assert (change.added, change.removed) == (1, 1)
    assert change.patch.split("\n")[3:] == ["-s = 'a b'", "+s = 'c'"]
