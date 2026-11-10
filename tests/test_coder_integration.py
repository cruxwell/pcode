import asyncio
import json

import pytest
from pydantic_ai import Agent
from pydantic_ai.messages import ToolReturnPart
from pydantic_ai.models.function import DeltaToolCall, FunctionModel
from pydantic_ai.workspaces import LocalWorkspaceBackend
from pydantic_ai_harness.coder._capability import MAX_READ_CHARS
from pydantic_ai_harness.filesystem import FileSystem
from retry_parts import is_retry

from pcode.agent import create_coder
from pcode.live import AgentRuntime
from pcode.runtime import EditCompleted, ToolSummary


def test_coder_preserves_upstream_tools_schemas_and_read_budget(tmp_path, monkeypatch):
    monkeypatch.delenv("EXA_API_KEY", raising=False)
    coder = create_coder(tmp_path)
    files = next(c for c in coder.capabilities if isinstance(c, FileSystem))
    assert files.content_hashes is False
    assert files.max_read_chars == MAX_READ_CHARS
    assert set(files.tools) == {"read_file", "write_file", "edit_file", "list_files", "grep"}

    async def model(messages, info):
        tools = {tool.name: tool for tool in info.function_tools}
        assert {*files.tools, "shell", "write_plan", "delegate_task"} <= tools.keys()
        assert (
            not {
                "run_command",
                "start_command",
                "stop_command",
                "check_command",
                "list_directory",
                "search_files",
                "find_files",
                "create_directory",
            }
            & tools.keys()
        )
        assert "expected_hash" not in tools["write_file"].parameters_json_schema["properties"]
        assert "expected_hash" not in tools["edit_file"].parameters_json_schema["properties"]
        assert "replacements" in tools["edit_file"].parameters_json_schema["properties"]
        yield "Done"

    Agent(FunctionModel(stream_function=model), capabilities=[coder]).run_sync("inspect")
    (tmp_path / "large.txt").write_text("abcdefghij\n" * 10000)

    async def read():
        result = await files.get_toolset().read_file(
            "large.txt", limit=10000, workspace=LocalWorkspaceBackend(tmp_path)
        )
        assert len(result) <= MAX_READ_CHARS
        assert "Use offset=" in result
        assert "hash:" not in result

    asyncio.run(read())


def test_parent_and_worker_replace_finish_before_responding_guidance(tmp_path):
    seen = []

    async def model(messages, info):
        parent = any(tool.name == "delegate_task" for tool in info.function_tools)
        seen.append("parent" if parent else "worker")
        instructions = info.instructions
        assert instructions.count("You are a software engineering agent.") == 1
        assert "Finish required long-running work before responding" not in instructions
        assert "poll status and output" not in instructions
        assert "Work autonomously until complete." in instructions
        assert "Servers may remain running once readiness is verified" in instructions
        if parent and not any(
            isinstance(part, ToolReturnPart) for message in messages for part in message.parts
        ):
            yield {
                0: DeltaToolCall(
                    name="delegate_task",
                    json_args=json.dumps({"agent_name": "worker", "task": "Inspect the guidance"}),
                )
            }
        else:
            yield "Done"

    agent = Agent(FunctionModel(stream_function=model), capabilities=[create_coder(tmp_path)])
    assert agent.run_sync("Inspect the guidance").output == "Done"
    assert seen == ["parent", "worker", "parent"]


def test_bundled_ripgrep_works_without_activating_the_tool_environment(tmp_path, monkeypatch):
    import os
    import shutil
    import sys
    from pathlib import Path

    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    (tmp_path / "sample.txt").write_text("BUNDLED_RG_MARKER")
    files = next(c for c in create_coder(tmp_path).capabilities if isinstance(c, FileSystem))
    assert os.environ["PATH"].startswith("/usr/bin:/bin:")
    assert Path(shutil.which("rg")) == Path(sys.executable).parent / "rg"

    async def run():
        tools = files.get_toolset()
        workspace = LocalWorkspaceBackend(tmp_path)
        assert "sample.txt" in await tools.list_files(workspace=workspace)
        assert "BUNDLED_RG_MARKER" in await tools.grep("BUNDLED_RG_MARKER", workspace=workspace)

    asyncio.run(run())


def test_native_ripgrep_tools_keep_external_paths_and_hidden_ancestor_access(tmp_path):
    workspace = tmp_path / ".hidden" / "workspace"
    external = workspace.parent / "external"
    workspace.mkdir(parents=True)
    external.mkdir()
    (external / "sample.py").write_text("SEARCH_MARKER\n")
    files = next(c for c in create_coder(workspace).capabilities if isinstance(c, FileSystem))

    async def run():
        toolset = files.get_toolset()
        local = LocalWorkspaceBackend(workspace)
        paths = await toolset.list_files(path="../external", workspace=local)
        assert "../external/sample.py" in paths
        result = await toolset.grep(
            "SEARCH_MARKER", path="../external", glob="*.py", workspace=local
        )
        assert "../external/sample.py:1:SEARCH_MARKER" in result

    asyncio.run(run())


@pytest.mark.parametrize("valid", [True, False])
def test_real_coder_batch_edits_are_atomic_and_emit_one_completed_diff(tmp_path, valid):
    path = tmp_path / "sample.txt"
    path.write_text("one\ntwo\n")
    calls = 0

    async def model(messages, info):
        nonlocal calls
        calls += 1
        if calls == 1:
            yield {
                0: DeltaToolCall(
                    name="edit_file",
                    json_args=json.dumps(
                        {
                            "path": "sample.txt",
                            "replacements": [
                                {"old_text": "one", "new_text": "first"},
                                {"old_text": "two" if valid else "missing", "new_text": "second"},
                            ],
                        }
                    ),
                )
            }
        else:
            parts = [part for msg in messages for part in msg.parts]
            assert any(is_retry(p) for p in parts) is not valid
            if valid:
                result = next(p for p in parts if isinstance(p, ToolReturnPart))
                assert result.content == "Edited sample.txt."
            yield "Done"

    runtime = AgentRuntime(
        Agent(FunctionModel(stream_function=model), capabilities=[create_coder(tmp_path)])
    )

    async def run():
        events = [event async for event in runtime.stream("edit both")]
        changes = [event for event in events if isinstance(event, EditCompleted)]
        assert len(changes) == int(valid)
        result = next(event for event in events if isinstance(event, ToolSummary))
        assert result.failed is not valid
        if valid:
            assert (changes[0].added, changes[0].removed) == (2, 2)
            assert "+first" in changes[0].patch and "+second" in changes[0].patch

    asyncio.run(run())
    assert calls == 2
    assert path.read_text() == ("first\nsecond\n" if valid else "one\ntwo\n")
