import asyncio
import json
import shlex
from dataclasses import fields
from unittest.mock import patch

from pydantic_ai import Agent
from pydantic_ai.messages import RetryPromptPart, ToolReturnPart
from pydantic_ai.models.function import DeltaToolCall, FunctionModel
from pydantic_ai_harness.shell import Shell

from pcode.agent import create_coder
from pcode.shell_tools import JobShell


def test_worker_copies_parent_shell_policy(tmp_path):
    with patch("pcode.agent.Agent", wraps=Agent) as constructor:
        coder = create_coder(tmp_path)
    options = constructor.call_args.kwargs
    parent = next(c for c in coder.capabilities if isinstance(c, Shell))
    child = next(c for c in options["capabilities"] if isinstance(c, Shell))
    assert type(child) is JobShell
    assert child is not parent
    for field in fields(Shell):
        if field.init:
            assert getattr(child, field.name) == getattr(parent, field.name)
    assert "Do not edit" not in options["instructions"]
    assert "permission checks" in options["instructions"]
    assert "shell" in options["description"]


def test_delegated_worker_reads_external_worktree_and_runs_shell(tmp_path):
    workspace = tmp_path / "workspace"
    external = tmp_path / "review"
    workspace.mkdir()
    external.mkdir()
    (workspace / "inside.txt").write_text("WORKSPACE_MARKER")
    (external / "sample.txt").write_text("EXTERNAL_MARKER")
    parent_calls = child_calls = 0

    async def model(messages, info):
        nonlocal parent_calls, child_calls
        names = {tool.name for tool in info.function_tools}
        parts = [part for msg in messages for part in msg.parts]
        assert not any(isinstance(p, RetryPromptPart) for p in parts)
        results = [str(p.content) for p in parts if isinstance(p, ToolReturnPart)]
        if "delegate_task" in names:
            parent_calls += 1
            if parent_calls == 1:
                yield {
                    0: DeltaToolCall(
                        name="delegate_task",
                        json_args=json.dumps(
                            {"agent_name": "worker", "task": f"Inspect {external} without edits."}
                        ),
                    )
                }
            elif parent_calls == 2:
                assert "[worker w1]\nExternal review complete" in results
                yield {0: DeltaToolCall(name="shell", json_args='{"command":"pwd"}')}
            else:
                # The child's shell cd did not change the parent's cwd.
                assert str(workspace) in results[-1]
                yield "Done"
            return

        assert "shell" in names
        assert not {"run_command", "start_command", "check_command", "stop_command"} & names
        assert {"write_file", "edit_file"} <= names
        assert "You are a general-purpose worker" in info.instructions
        assert str(workspace) in info.instructions
        child_calls += 1
        calls = [
            ("read_file", {"path": str(external / "sample.txt")}),
            ("shell", {"command": f"cd {shlex.quote(str(external))}; pwd"}),
            ("shell", {"command": "pwd"}),
            ("read_file", {"path": "inside.txt"}),
        ]
        if child_calls > 1:
            expected = ["EXTERNAL_MARKER", str(external), str(workspace), "WORKSPACE_MARKER"]
            assert expected[child_calls - 2] in results[-1]
        if child_calls <= len(calls):
            name, arguments = calls[child_calls - 1]
            yield {0: DeltaToolCall(name=name, json_args=json.dumps(arguments))}
        else:
            yield "External review complete"

    agent = Agent(FunctionModel(stream_function=model), capabilities=[create_coder(workspace)])
    assert asyncio.run(agent.run("Review")).output == "Done"
    assert child_calls == 5
    assert parent_calls == 3
