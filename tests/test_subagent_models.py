"""`subagent_models`: which models `delegate_task` may run a sub-agent on."""

import asyncio
import json
from io import StringIO

from prompt_toolkit.completion import CompleteEvent
from prompt_toolkit.document import Document
from pydantic_ai import Agent
from pydantic_ai.messages import ToolReturnPart
from pydantic_ai.models.function import DeltaToolCall, FunctionModel
from rich.console import Console

from pcode import agent as agent_module
from pcode.agent import SideModel, create_coder, subagent_menu
from pcode.app import PreviewApp
from pcode.commands import SlashCompleter
from pcode.live import AgentRuntime
from pcode.preferences import save_preferences, subagent_models


def fake_side_model(models):
    """A `side_model` stand-in resolving only the names in `models`."""

    def resolve(name, effort=""):
        if name not in models:
            raise ValueError(f"Cannot use {name}: no credentials")
        return SideModel(name, models[name], {"temperature": 0.5})

    return resolve


def test_the_setting_reads_as_ordered_unique_names():
    assert subagent_models() == []
    save_preferences(subagent_models="b:y,a:x,b:y")
    assert subagent_models() == ["b:y", "a:x"]


def test_the_menu_keeps_resolvable_models_and_reports_the_rest(monkeypatch):
    model = FunctionModel(lambda messages, info: None)
    monkeypatch.setattr(agent_module, "side_model", fake_side_model({"a:x": model}))
    menu, problems = subagent_menu(["a:x", "b:y"])
    assert list(menu) == ["a:x"]
    assert menu["a:x"].model is model and menu["a:x"].settings == {"temperature": 0.5}
    assert problems == ["Cannot use b:y: no credentials"]


def test_a_delegation_runs_on_the_model_it_picks(tmp_path, monkeypatch):
    """The worker keeps its tools but answers from the chosen menu model."""
    seen = {}

    async def child(messages, info):
        seen["tools"] = {tool.name for tool in info.function_tools}
        seen["settings"] = info.model_settings
        yield "from the other provider"

    monkeypatch.setattr(
        agent_module,
        "side_model",
        fake_side_model({"other:big": FunctionModel(stream_function=child)}),
    )
    save_preferences(subagent_models="other:big")

    async def parent(messages, info):
        delegate = next(tool for tool in info.function_tools if tool.name == "delegate_task")
        seen["model"] = delegate.parameters_json_schema["properties"]["model"]
        if any(isinstance(p, ToolReturnPart) for m in messages for p in m.parts):
            yield "done"
            return
        yield {
            0: DeltaToolCall(
                name="delegate_task",
                json_args=json.dumps(
                    {"agent_name": "worker", "task": "Look around", "model": "other:big"}
                ),
                tool_call_id="call",
            )
        }

    runtime = AgentRuntime(
        Agent(FunctionModel(stream_function=parent), capabilities=[create_coder(tmp_path)])
    )

    async def run():
        async for _ in runtime.stream("Delegate it"):
            pass

    asyncio.run(run())
    # Any provider:model name is accepted; the menu only resolves some up front.
    assert seen["model"]["type"] == "string" and "enum" not in seen["model"]
    assert "read_file" in seen["tools"] and "delegate_task" not in seen["tools"]
    # The option's own settings (its defaults and saved /effort) reach the child.
    assert seen["settings"]["temperature"] == 0.5
    returned = [
        part.content
        for message in runtime.history
        for part in message.parts
        if isinstance(part, ToolReturnPart)
    ]
    assert returned == ["[worker w1]\nfrom the other provider"]


def test_a_worker_without_delegation_resolves_no_models(tmp_path, monkeypatch):
    def refuse(name, effort=""):
        raise AssertionError("an isolated child resolved the menu")

    monkeypatch.setattr(agent_module, "side_model", refuse)
    save_preferences(subagent_models="a:x")
    create_coder(tmp_path, delegation=False)


def test_a_terminal_completes_btw_models_from_a_host_that_predates_completer_names():
    from pcode.remote import RemoteController, _proxy
    from pcode.ui import Activity

    controller = RemoteController(None, Activity())
    old = _proxy({"name": "/btw", "description": "", "models": True}, controller)
    new = _proxy(
        {"name": "/subagents", "description": "", "completer": "model_list_completions"}, controller
    )
    unknown = _proxy({"name": "/x", "description": "", "completer": "subagents"}, controller)
    assert old.argument_completer == controller.aside_completions
    assert new.argument_completer == controller.model_list_completions
    assert unknown.argument_completer is None


def test_a_delegation_runs_on_a_model_off_the_menu(tmp_path, monkeypatch):
    """Without /subagents, `model` still takes any name, resolved when used."""
    seen = {}

    async def child(messages, info):
        yield "second opinion"

    resolve = fake_side_model({"other:big": FunctionModel(stream_function=child)})
    resolved = []

    def counting(name, effort=""):
        resolved.append(name)
        return resolve(name, effort)

    monkeypatch.setattr(agent_module, "side_model", counting)

    async def parent(messages, info):
        returns = [p for m in messages for p in m.parts if isinstance(p, ToolReturnPart)]
        retries = [p for m in messages for p in m.parts if type(p).__name__ == "RetryPromptPart"]
        if len(returns) == 2:
            seen["returned"] = [r.content for r in returns]
            yield "done"
            return
        name = "other:big" if retries else "nope:z"
        if retries:
            seen["retry"] = retries[0].content
        yield {
            0: DeltaToolCall(
                name="delegate_task",
                json_args=json.dumps({"agent_name": "worker", "task": "Review", "model": name}),
                tool_call_id=f"call-{name}-{len(returns)}",
            )
        }

    runtime = AgentRuntime(
        Agent(FunctionModel(stream_function=parent), capabilities=[create_coder(tmp_path)])
    )

    async def run():
        async for _ in runtime.stream("Get a second opinion"):
            pass

    asyncio.run(run())
    assert "Cannot use nope:z: no credentials" in seen["retry"]
    assert [r.split("\n", 1)[1] for r in seen["returned"]] == ["second opinion"] * 2
    # A name is resolved once, then reused; a failed one is not remembered.
    assert resolved == ["nope:z", "other:big"]


def test_the_command_sets_lists_and_clears_the_models(tmp_path, monkeypatch):
    monkeypatch.delenv("EXA_API_KEY", raising=False)
    model = FunctionModel(lambda messages, info: None)
    monkeypatch.setattr(agent_module, "side_model", fake_side_model({"a:x": model, "b:y": model}))
    output = StringIO()
    app = PreviewApp(console=Console(file=output, width=200), workspace=tmp_path, model="test")
    controller = app.controller

    async def scenario():
        await app._initialize_runtime()
        await controller.run_command("/subagents")
        assert "No sub-agent models" in output.getvalue()

        await controller.run_command("/subagents a:x b:y a:x")
        assert subagent_models() == ["a:x", "b:y"]
        assert controller.reload_requested
        await controller.reload_extensions()
        assert "Sub-agent models: a:x, b:y. Reloading." in output.getvalue()

        # A name that does not resolve is refused, and nothing is saved.
        await controller.run_command("/subagents a:x nope:z")
        assert "Cannot use nope:z: no credentials" in output.getvalue()
        assert subagent_models() == ["a:x", "b:y"]
        assert not controller.reload_requested

        # One saved earlier that no longer resolves is listed as left out.
        save_preferences(subagent_models="a:x,gone:q")
        await controller.run_command("/subagents")
        assert "    a:x\n  Unavailable, left out: Cannot use gone:q" in output.getvalue()

        await controller.run_command("/subagents off")
        assert subagent_models() == []
        await controller.reload_extensions()
        app.runtime.close()

    asyncio.run(scenario())


def test_setting_models_waits_for_the_running_turn(tmp_path, monkeypatch):
    model = FunctionModel(lambda messages, info: None)
    monkeypatch.setattr(agent_module, "side_model", fake_side_model({"a:x": model}))
    output = StringIO()
    app = PreviewApp(console=Console(file=output, width=200), workspace=tmp_path, model="test")

    async def scenario():
        await app._initialize_runtime()
        app.activity.busy = True
        await app.controller.run_command("/subagents a:x")
        app.activity.busy = False
        app.runtime.close()

    asyncio.run(scenario())
    assert "/reload is unavailable while working" in output.getvalue()
    assert subagent_models() == []


def test_an_unsaved_choice_is_refused_without_reloading(tmp_path, monkeypatch):
    from pcode import controller as controller_module

    model = FunctionModel(lambda messages, info: None)
    monkeypatch.setattr(agent_module, "side_model", fake_side_model({"a:x": model}))

    def fail(**updates):
        raise OSError("read-only")

    monkeypatch.setattr(controller_module, "save_preferences", fail)
    output = StringIO()
    app = PreviewApp(console=Console(file=output, width=200), workspace=tmp_path, model="test")

    async def scenario():
        await app._initialize_runtime()
        await app.controller.run_command("/subagents a:x")
        assert not app.controller.reload_requested
        app.runtime.close()

    asyncio.run(scenario())
    text = output.getvalue()
    assert "Could not save subagent_models: read-only" in text
    assert "overrides" not in text


def test_a_workspace_setting_is_named_and_not_shadowed_by_a_user_choice(tmp_path, monkeypatch):
    from pcode import preferences

    model = FunctionModel(lambda messages, info: None)
    monkeypatch.setattr(agent_module, "side_model", fake_side_model({"a:x": model, "b:y": model}))
    (tmp_path / ".pcode").mkdir()
    (tmp_path / ".pcode" / "preferences.json").write_text('{"subagent_models": "a:x"}')
    monkeypatch.setattr(preferences, "_project_root", tmp_path)
    output = StringIO()
    app = PreviewApp(console=Console(file=output, width=200), workspace=tmp_path, model="test")

    async def scenario():
        await app._initialize_runtime()
        await app.controller.run_command("/subagents")
        await app.controller.run_command("/subagents b:y")
        assert not app.controller.reload_requested
        app.runtime.close()

    asyncio.run(scenario())
    text = output.getvalue()
    assert "set by this workspace's .pcode/preferences.json:\n    a:x" in text
    assert "change it with pcode config project set|unset subagent_models" in text
    assert "subagent_models" not in preferences.read_preferences()


def test_a_name_outside_the_catalog_is_saved_with_a_warning(tmp_path, monkeypatch):
    model = FunctionModel(lambda messages, info: None)
    monkeypatch.setattr(
        agent_module, "side_model", fake_side_model({"a:x": model, "a:typo": model})
    )
    output = StringIO()
    app = PreviewApp(console=Console(file=output, width=200), workspace=tmp_path, model="test")
    monkeypatch.setattr(app.controller, "model_suggestions", lambda: ["a:x"])

    async def scenario():
        await app._initialize_runtime()
        await app.controller.run_command("/subagents a:x a:typo")
        await app.controller.reload_extensions()
        app.runtime.close()

    asyncio.run(scenario())
    assert "Not in the /model catalog, so check the spelling: a:typo" in output.getvalue()
    assert subagent_models() == ["a:x", "a:typo"]


def test_model_names_complete_for_every_word(monkeypatch):
    app = PreviewApp(console=Console(file=StringIO()))
    monkeypatch.setattr(
        app.controller,
        "model_suggestions",
        lambda: ["anthropic:claude-opus", "openai-codex:gpt-6-astra", "openai:gpt-6"],
    )
    completer = SlashCompleter(app.registry)

    def complete(text):
        return [
            (item.text, item.start_position)
            for item in completer.get_completions(Document(text), CompleteEvent())
        ]

    assert complete("/subagents ") == [
        ("off", 0),
        ("anthropic:claude-opus", 0),
        ("openai-codex:gpt-6-astra", 0),
        ("openai:gpt-6", 0),
    ]
    assert complete("/subagents o") == [
        ("off", -1),
        ("anthropic:claude-opus", -1),
        ("openai-codex:gpt-6-astra", -1),
        ("openai:gpt-6", -1),
    ]
    assert complete("/subagents astra") == [("openai-codex:gpt-6-astra", -5)]
    # Later words complete too, without repeating a name already typed.
    assert complete("/subagents openai-codex:gpt-6-astra gpt") == [("openai:gpt-6", -3)]
    assert complete("/subagents anthropic:claude-opus ") == [
        ("openai-codex:gpt-6-astra", 0),
        ("openai:gpt-6", 0),
    ]
    # `off` stands alone.
    assert complete("/subagents off ") == []
