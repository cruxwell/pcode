"""The workflows extension ships off; on, one script fans out to sub-agents."""

import asyncio

from pydantic_ai import ModelRetry
from pydantic_ai.capabilities import AbstractCapability, Hooks
from pydantic_ai.messages import (
    ModelResponse,
    PartStartEvent,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
)
from pydantic_ai.models.function import DeltaToolCall, FunctionModel
from pydantic_ai_harness.subagents import SubAgents

from pcode.agent import create_agent
from pcode.edit_preview import StreamingEditPreview
from pcode.ext import BUNDLED_DIR, Extension, ExtensionUI, load_extension
from pcode.preferences import save_preferences
from pcode.tool_display import LABELS, target

SCRIPT = """\
import asyncio
reports = await asyncio.gather(worker(task="alpha"), worker(task="beta"))
sorted(reports)
"""


class Notes(ExtensionUI):
    def __init__(self):
        super().__init__()
        self.lines = []

    def notify(self, text, level="info"):
        self.lines.append(text)


def load(workspace, *, on=frozenset(), ui=None, is_worker=False):
    return load_extension(
        Extension("workflows", BUNDLED_DIR / "workflows.py", "bundled"),
        workspace,
        ui or ExtensionUI(),
        set(),
        set(on),
        is_worker=is_worker,
    )


def returned(messages, tool):
    return [
        part.content
        for part in messages[-1].parts
        if isinstance(part, ToolReturnPart) and part.tool_name == tool
    ]


def tool_names(info):
    return {tool.name for tool in info.function_tools}


def task(messages):
    return messages[0].parts[-1].content


def orchestrate(names, messages, script=SCRIPT):
    """The parent's side of a scripted run: call the workflow, then echo its result."""
    assert "run_workflow" in names
    if result := returned(messages, "run_workflow"):
        return ModelResponse([TextPart(str(result[0]))])
    return ModelResponse([ToolCallPart("run_workflow", {"code": script})])


def build(tmp_path, respond, ui=None, extra=()):
    extension = load(tmp_path, on={"workflows"}, ui=ui)
    assert extension.loaded, extension.error

    async def answer(messages, info):
        response = respond(messages, info)
        return await response if asyncio.iscoroutine(response) else response

    async def stream(messages, info):
        # A `delegate_task` worker streams; a workflow sub-agent does not.
        for index, part in enumerate((await answer(messages, info)).parts):
            if isinstance(part, TextPart):
                yield part.content
            else:
                yield {index: DeltaToolCall(name=part.tool_name, json_args=part.args_as_json_str())}

    model = FunctionModel(answer, stream_function=stream)
    agent = create_agent("test", tmp_path, extensions=[*extension.capabilities, *extra])
    return agent, lambda prompt="go": asyncio.run(agent.run(prompt, model=model))


def test_ships_off(tmp_path):
    extension = load(tmp_path)
    assert extension.state() == "off by default (/extensions on workflows)"
    assert extension.capabilities == []


def test_an_isolated_worker_gets_nothing(tmp_path):
    extension = load(tmp_path, on={"workflows"}, is_worker=True)
    assert extension.loaded and extension.capabilities == []


def test_a_script_fans_out_and_only_its_result_comes_back(tmp_path):
    seen = []
    ui = Notes()

    def respond(messages, info):
        names = tool_names(info)
        if "delegate_task" in names:
            return orchestrate(names, messages)
        seen.append(names)
        return ModelResponse([TextPart(f"report on {task(messages)}")])

    _, run = build(tmp_path, respond, ui)

    assert run().output == str(["report on alpha", "report on beta"])
    # Workers have the coder's tools, and neither kind of delegation.
    assert len(seen) == 2
    assert all({"read_file", "shell"} <= names for names in seen)
    assert not any({"run_workflow", "delegate_task", "ask_parent"} & names for names in seen)
    assert ui.lines[-1] == "Workflow: 2 done, 0 running"

    # The counts are one workflow's, not the session's.
    run()
    assert ui.lines[-1] == "Workflow: 2 done, 0 running"


def test_other_extensions_tools_and_guardrails_reach_a_workflow_worker(tmp_path):
    guard = Hooks(id="ext.guard.hooks")
    blocked = []

    @guard.on.before_tool_execute
    async def refuse_writes(ctx, *, call, tool_def, args):
        if tool_def.name == "write_file":
            blocked.append(args["path"])
            raise ModelRetry("writes are blocked")
        return args

    def respond(messages, info):
        names = tool_names(info)
        if "delegate_task" in names:
            return orchestrate(names, messages, 'await worker(task="write it")')
        if refusal := returned(messages, "write_file"):
            return ModelResponse([TextPart(str(refusal[0]))])
        return ModelResponse([ToolCallPart("write_file", {"path": "out.txt", "content": "x"})])

    _, run = build(tmp_path, respond, extra=[guard])

    assert run().output == "writes are blocked"
    assert blocked == ["out.txt"]
    assert not (tmp_path / "out.txt").exists()


def test_what_delegate_task_shares_with_delegates_reaches_a_workflow_worker(tmp_path):
    """Token accounting and step persistence are handed to delegates this way."""
    requests = []

    class Recorder(AbstractCapability):
        async def before_model_request(self, ctx, request_context):
            requests.append(ctx.agent.name)
            return request_context

    def respond(messages, info):
        names = tool_names(info)
        if "delegate_task" in names:
            return orchestrate(names, messages)
        return ModelResponse([TextPart("ok")])

    agent, run = build(tmp_path, respond)
    for capability in agent.root_capability.capabilities:
        if isinstance(capability, SubAgents):
            capability.shared_capabilities = [*capability.shared_capabilities, Recorder()]
    run()

    assert requests == ["worker", "worker"]


def test_the_worker_cap_bounds_a_gather(tmp_path):
    save_preferences(worker_concurrency="2")
    active = peak = 0

    async def respond(messages, info):
        nonlocal active, peak
        names = tool_names(info)
        if "delegate_task" in names:
            calls = ", ".join(f'worker(task="{n}")' for n in range(6))
            return orchestrate(names, messages, f"import asyncio\nawait asyncio.gather({calls})")
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0.01)
        active -= 1
        return ModelResponse([TextPart("ok")])

    _, run = build(tmp_path, respond)
    run()

    assert peak == 2


def test_a_failed_worker_is_counted_and_catchable(tmp_path):
    ui = Notes()
    script = """\
try:
    await worker(task="bad")
except RuntimeError:
    pass
await worker(task="good")
"""

    def respond(messages, info):
        names = tool_names(info)
        if "delegate_task" in names:
            return orchestrate(names, messages, script)
        if task(messages) == "bad":
            raise ValueError("boom")
        return ModelResponse([TextPart("fine")])

    _, run = build(tmp_path, respond, ui)

    assert run().output == "fine"
    assert ui.lines[-1] == "Workflow: 1 done, 1 failed, 0 running"


def test_a_delegate_task_worker_gets_neither_the_tool_nor_its_guidance(tmp_path):
    seen = []

    def respond(messages, info):
        names = tool_names(info)
        if "delegate_task" not in names:
            seen.append((names, info.instructions or ""))
            return ModelResponse([TextPart("done")])
        assert "run_workflow" in names and "`run_workflow` runs" in info.instructions
        if returned(messages, "delegate_task"):
            return ModelResponse([TextPart("finished")])
        return ModelResponse(
            [ToolCallPart("delegate_task", {"agent_name": "worker", "task": "anything"})]
        )

    _, run = build(tmp_path, respond)
    run()

    [(names, instructions)] = seen
    assert "run_workflow" not in names
    assert "run_workflow" not in instructions


def test_the_script_is_displayed_like_run_code(tmp_path):
    assert LABELS["run_workflow"] == "Run workflow"
    assert target("run_workflow", {"code": SCRIPT}) == target("run_code", {"code": SCRIPT})

    def preview(name):
        part = ToolCallPart(name, {"code": SCRIPT})
        return StreamingEditPreview(tmp_path).update(PartStartEvent(index=0, part=part))

    assert preview("run_workflow") and preview("run_workflow") == preview("run_code")
