"""`/advisor` and `advisor_model`: a stronger model the session's model may consult."""

import asyncio
from io import StringIO

from anthropic import AsyncAnthropic
from prompt_toolkit.completion import CompleteEvent
from prompt_toolkit.document import Document
from pydantic_ai import Agent
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.anthropic import AnthropicModel
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.providers.anthropic import AnthropicProvider
from rich.console import Console

from pcode import agent as agent_module
from pcode.advisor import AdvisorChoice, _build, advisor_capability, native_pairing
from pcode.agent import SideModel
from pcode.app import PreviewApp
from pcode.auth import anthropic_model
from pcode.commands import SlashCompleter
from pcode.preferences import load_preferences, save_preferences


def fake_side_model(models):
    def resolve(name, effort=""):
        if name not in models:
            raise ValueError(f"Cannot use {name}: no credentials")
        return SideModel(name, models[name], None)

    return resolve


def claude_api(name):
    return anthropic_model(f"anthropic:{name}", "test-key")


def test_the_saved_default_is_the_session_starting_choice():
    assert AdvisorChoice().name is None
    save_preferences(advisor_model="anthropic:claude-fable-5-2")
    assert AdvisorChoice().name == "anthropic:claude-fable-5-2"


def test_a_claude_api_executor_runs_a_claude_advisor_natively():
    opus = claude_api("claude-opus-5-5")
    assert native_pairing("anthropic:claude-fable-5-2", opus) == "claude-fable-5-2"
    # A Claude Code name for the same model still runs in the executor's request.
    assert native_pairing("claude:claude-fable-5-2", opus) == "claude-fable-5-2"
    # Another provider's advisor, or an executor without the tool, goes local.
    assert native_pairing("openai:gpt-5.4", opus) is None
    assert native_pairing("anthropic:claude-fable-5-2", FunctionModel(lambda m, i: None)) is None
    old = AnthropicModel(
        "claude-3-5-haiku-latest",
        provider=AnthropicProvider(anthropic_client=AsyncAnthropic(api_key="test-key")),
    )
    assert native_pairing("anthropic:claude-fable-5-2", old) is None


def test_a_fable_5_2_executor_only_pairs_natively_with_a_5_2_advisor():
    fable = claude_api("claude-fable-5-2")
    assert native_pairing("anthropic:claude-opus-5-5", fable) is None
    assert native_pairing("anthropic:claude-mythos-5-2", fable) == "claude-mythos-5-2"


def test_the_native_advisor_names_the_anthropic_model(monkeypatch):
    def refuse(name, effort=""):
        raise AssertionError("a native advisor resolved a local model")

    monkeypatch.setattr(agent_module, "side_model", refuse)
    advisor = _build("claude:claude-fable-5-2", "claude-fable-5-2").capabilities[0]
    assert advisor.mode == "native" and advisor.model == "anthropic:claude-fable-5-2"


def run_turn(agent, prompt="Plan it"):
    return asyncio.run(agent.run(prompt))


def test_a_local_advisor_answers_and_turning_it_off_removes_it(monkeypatch):
    seen = []

    def advise(messages, info):
        return ModelResponse(parts=[TextPart("use a queue")])

    monkeypatch.setattr(
        agent_module, "side_model", fake_side_model({"other:big": FunctionModel(advise)})
    )

    def executor(messages, info):
        tools = {tool.name for tool in info.function_tools}
        instructions = info.instructions or ""
        seen.append((tools, "consults a stronger model (other:big)" in instructions))
        returns = [p for m in messages for p in m.parts if isinstance(p, ToolReturnPart)]
        if "advisor" in tools and not returns:
            return ModelResponse(parts=[ToolCallPart("advisor", {"prompt": "How?"}, "c")])
        return ModelResponse(parts=[TextPart(f"done: {returns[0].content}" if returns else "done")])

    choice = AdvisorChoice("other:big")
    agent = Agent(FunctionModel(executor), capabilities=[advisor_capability(choice)])
    assert run_turn(agent).output == "done: use a queue"
    assert seen[0] == ({"advisor"}, True)

    choice.name = None
    seen.clear()
    assert run_turn(agent).output == "done"
    assert seen == [(set(), False)]


def test_a_local_advisor_is_resolved_each_turn_with_its_own_settings(monkeypatch):
    resolved = []
    seen = {}

    def advise(messages, info):
        seen["settings"] = info.model_settings
        return ModelResponse(parts=[TextPart("ok")])

    def resolve(name, effort=""):
        resolved.append(name)
        return SideModel(name, FunctionModel(advise), {"temperature": 0.25})

    monkeypatch.setattr(agent_module, "side_model", resolve)

    def executor(messages, info):
        returns = [p for m in messages for p in m.parts if isinstance(p, ToolReturnPart)]
        if not returns:
            return ModelResponse(parts=[ToolCallPart("advisor", {"prompt": "How?"}, "c")])
        return ModelResponse(parts=[TextPart("done")])

    agent = Agent(
        FunctionModel(executor), capabilities=[advisor_capability(AdvisorChoice("other:big"))]
    )
    run_turn(agent)
    run_turn(agent)
    # Not cached: a /login or /logout since the last turn reaches the next one.
    assert resolved == ["other:big", "other:big"]
    assert seen["settings"]["temperature"] == 0.25


def test_a_side_question_cannot_consult_the_local_advisor():
    from pcode.aside_guard import REFUSED_TOOLS

    assert "advisor" in REFUSED_TOOLS


def test_the_executor_is_not_its_own_advisor():
    from pcode.advisor import same_model

    opus = claude_api("claude-opus-5-5")
    assert same_model("anthropic:claude-opus-5-5", opus)
    assert same_model("claude:claude-opus-5-5", opus)
    assert not same_model("anthropic:claude-fable-5-2", opus)


def test_an_unavailable_advisor_leaves_the_turn_without_one(monkeypatch):
    monkeypatch.setattr(agent_module, "side_model", fake_side_model({}))
    tools = []

    def executor(messages, info):
        tools.append({tool.name for tool in info.function_tools})
        return ModelResponse(parts=[TextPart("done")])

    agent = Agent(
        FunctionModel(executor),
        capabilities=[advisor_capability(AdvisorChoice("gone:q"))],
    )
    assert run_turn(agent).output == "done"
    assert tools == [set()]


def test_the_command_sets_saves_and_clears_the_advisor(tmp_path, monkeypatch):
    model = FunctionModel(lambda messages, info: None)
    monkeypatch.setattr(
        agent_module,
        "side_model",
        fake_side_model({"a:x": model, "anthropic:claude-fabel-5-2": model}),
    )
    output = StringIO()
    app = PreviewApp(console=Console(file=output, width=200), workspace=tmp_path, model="test")
    controller = app.controller

    async def scenario():
        await app._initialize_runtime()
        # For this session only.
        await controller.run_command("/advisor a:x")
        assert controller.advisor.name == "a:x"
        assert "advisor_model" not in load_preferences()
        assert "Advisor: a:x (next turn)." in output.getvalue()
        assert ("Advisor", "a:x") in controller.session_overview()

        # Refused before anything changes.
        await controller.run_command("/advisor nope:z")
        assert "Cannot use nope:z: no credentials" in output.getvalue()
        assert controller.advisor.name == "a:x"

        await controller.run_command("/advisor a:x --save")
        assert load_preferences()["advisor_model"] == "a:x"
        assert "Saved as the default." in output.getvalue()

        await controller.run_command("/advisor off --save")
        assert controller.advisor.name is None
        assert "advisor_model" not in load_preferences()

        await controller.run_command("/advisor a:x b:y")
        assert "Usage: /advisor [MODEL | off] [--save]" in output.getvalue()

        # A Claude name Anthropic may be asked for natively must be a known one.
        await controller.run_command("/advisor anthropic:claude-fabel-5-2")
        assert "Not in the /model catalog: anthropic:claude-fabel-5-2" in output.getvalue()
        assert controller.advisor.name is None
        app.runtime.close()

    asyncio.run(scenario())


def test_the_session_agent_reads_the_choice_and_keeps_it_across_a_reload(tmp_path):
    from pydantic_ai.capabilities import DynamicCapability

    output = StringIO()
    app = PreviewApp(console=Console(file=output, width=200), workspace=tmp_path, model="test")
    controller = app.controller

    def advisors():
        capabilities = app.runtime.agent.root_capability.capabilities
        return [c for c in capabilities if isinstance(c, DynamicCapability)]

    async def scenario():
        await app._initialize_runtime()
        assert len(advisors()) == 1
        controller.advisor.name = "a:x"
        await controller.reload_extensions()
        assert len(advisors()) == 1
        assert controller.advisor.name == "a:x"
        app.runtime.close()

    asyncio.run(scenario())


def test_the_command_completes_models_off_and_save(tmp_path, monkeypatch):
    app = PreviewApp(console=Console(file=StringIO()), workspace=tmp_path, model="test")
    controller = app.controller
    monkeypatch.setattr(controller, "model_suggestions", lambda: ["a:x", "b:y"])
    completer = SlashCompleter(controller.registry)

    def complete(text):
        return [c.text for c in completer.get_completions(Document(text), CompleteEvent())]

    assert complete("/advisor ") == ["off", "a:x", "b:y"]
    assert complete("/advisor b") == ["b:y"]
    assert complete("/advisor a:x ") == []
    assert complete("/advisor a:x --") == ["--save"]
    assert complete("/advisor --save ") == ["off", "a:x", "b:y"]
