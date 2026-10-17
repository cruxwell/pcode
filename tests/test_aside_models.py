"""`/btw $MODEL ... QUESTION` asks side questions on chosen models."""

import asyncio
from io import StringIO
from unittest.mock import AsyncMock

import pytest
from prompt_toolkit.completion import CompleteEvent
from prompt_toolkit.document import Document
from pydantic_ai import Agent
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.messages import UserPromptPart
from pydantic_ai.models.function import FunctionModel
from pydantic_ai.models.test import TestModel
from pydantic_ai.profiles.anthropic import AnthropicModelProfile
from rich.console import Console

from pcode.agent import SideModel, create_coder, side_model
from pcode.app import PreviewApp
from pcode.aside import (
    ASIDE_FRAMING,
    ASIDE_MODEL_LIMIT,
    Aside,
    Asides,
    SideTarget,
    effort_fragment,
    model_fragment,
    model_labels,
    parse_models,
)
from pcode.commands import SlashCompleter
from pcode.live import AgentRuntime
from pcode.preferences import save_model_effort


def targets(*models):
    return [SideTarget(model) for model in models]


def test_leading_dollar_words_select_models_and_the_rest_is_the_question():
    assert parse_models("why?") == ([], "why?")
    assert parse_models("  $a:x   is  this right? ") == (targets("a:x"), "is  this right?")
    assert parse_models("$a:x $b:y $a:x second opinions?") == (
        targets("a:x", "b:y"),
        "second opinions?",
    )
    # Only leading words name models; a `$` later on is part of the question.
    assert parse_models("$a:x costs $5?") == (targets("a:x"), "costs $5?")
    assert parse_models("what does $HOME hold?") == ([], "what does $HOME hold?")
    with pytest.raises(ValueError, match="No question after the model"):
        parse_models("$a:x")
    with pytest.raises(ValueError, match="No question after the model"):
        parse_models("$a:x $b:y  ")
    with pytest.raises(ValueError, match="model name must follow"):
        parse_models("$ why?")
    models = " ".join(f"$p:m{index}" for index in range(ASIDE_MODEL_LIMIT))
    assert len(parse_models(f"{models} q")[0]) == ASIDE_MODEL_LIMIT
    # Duplicates collapse before the cap is counted.
    assert len(parse_models(f"{models} $p:m0 q")[0]) == ASIDE_MODEL_LIMIT
    with pytest.raises(ValueError, match=f"At most {ASIDE_MODEL_LIMIT} models"):
        parse_models(f"{models} $p:extra q")


def test_plus_words_pick_an_effort_for_the_question():
    # Bare: the conversation's own model at that effort.
    assert parse_models("+low why?") == ([SideTarget(effort="low")], "why?")
    assert parse_models("$openai:gpt-5+high $anthropic:claude-opus-4-5+low q") == (
        [SideTarget("openai:gpt-5", "high"), SideTarget("anthropic:claude-opus-4-5", "low")],
        "q",
    )
    assert parse_models("+xhigh $a:x+default q")[0] == [
        SideTarget(effort="xhigh"),
        SideTarget("a:x", "default"),
    ]
    # Model ids hold `:` and `/`; only a level after the last `+` is an effort.
    assert parse_models("$openrouter:meta/llama+3:8b+medium q")[0] == [
        SideTarget("openrouter:meta/llama+3:8b", "medium")
    ]
    assert parse_models("$openrouter:meta/llama+3:8b q")[0] == targets("openrouter:meta/llama+3:8b")
    # Once the question starts, `+` is text.
    assert parse_models("is c+high or c+low better? +low") == (
        [],
        "is c+high or c+low better? +low",
    )
    assert parse_models("+high does 1 +low == 2?") == (
        [SideTarget(effort="high")],
        "does 1 +low == 2?",
    )
    # A repeated target collapses; one model at two efforts is two questions.
    assert parse_models("+low +low $a:x+high $a:x+high $a:x $a:x+low q")[0] == [
        SideTarget(effort="low"),
        SideTarget("a:x", "high"),
        SideTarget("a:x"),
        SideTarget("a:x", "low"),
    ]
    with pytest.raises(ValueError, match="Unknown effort `\\+max`"):
        parse_models("+max q")
    with pytest.raises(ValueError, match="Unknown effort `\\+`"):
        parse_models("+ q")
    with pytest.raises(ValueError, match="No question after the model"):
        parse_models("+low")
    with pytest.raises(ValueError, match="No question after the model"):
        parse_models("$a:x+high   ")


def test_labels_drop_the_provider_unless_two_models_would_look_the_same():
    assert model_labels(targets("anthropic:claude-x", "openai-codex:gpt-y")) == {
        SideTarget("anthropic:claude-x"): "claude-x",
        SideTarget("openai-codex:gpt-y"): "gpt-y",
    }
    assert model_labels(targets("anthropic:claude-x", "claude:claude-x", "test")) == {
        SideTarget("anthropic:claude-x"): "anthropic:claude-x",
        SideTarget("claude:claude-x"): "claude:claude-x",
        SideTarget("test"): "test",
    }


def test_labels_carry_the_effort():
    chosen = [
        SideTarget(),
        SideTarget(effort="high"),
        SideTarget("openai:gpt-5", "high"),
        SideTarget("openai:gpt-5", "low"),
        SideTarget("openai:gpt-5"),
    ]
    assert model_labels(chosen) == {
        SideTarget(): "",
        SideTarget(effort="high"): "high",
        SideTarget("openai:gpt-5", "high"): "gpt-5 \u00b7 high",
        SideTarget("openai:gpt-5", "low"): "gpt-5 \u00b7 low",
        SideTarget("openai:gpt-5"): "gpt-5",
    }


def test_dollar_completes_models_only_among_leading_btw_words(monkeypatch):
    app = PreviewApp(console=Console(file=StringIO()))
    monkeypatch.setattr(
        app.controller, "model_suggestions", lambda: ["anthropic:claude-opus", "openai-codex:gpt-6"]
    )
    completer = SlashCompleter(app.registry)

    def complete(text):
        return [
            (item.text, item.start_position)
            for item in completer.get_completions(Document(text), CompleteEvent())
        ]

    assert complete("/btw $") == [("$anthropic:claude-opus", -1), ("$openai-codex:gpt-6", -1)]
    assert complete("/btw $opus") == [("$anthropic:claude-opus", -5)]
    # Later model words complete too.
    assert complete("/btw $anthropic:claude-opus $gpt") == [("$openai-codex:gpt-6", -4)]
    # Once the question starts, `$` is text.
    assert complete("/btw why $") == []
    assert complete("/btw $opus ") == []
    assert complete("/btw why") == []
    # A prompt's own leading word picks its model, so it completes too.
    assert complete("$opus") == [("$anthropic:claude-opus", -5)]
    assert complete("$opus fix") == []
    # Nowhere else: not later in a prompt, shell mode, or another command.
    assert complete("fix $opus") == []
    assert complete("!echo $opus") == []
    assert complete("/compact $opus") == []
    assert model_fragment("$a $b") == "b"
    assert model_fragment("") is None


def test_plus_completes_effort_levels_among_leading_btw_words(monkeypatch):
    app = PreviewApp(console=Console(file=StringIO()))
    monkeypatch.setattr(app.controller, "model_suggestions", lambda: ["anthropic:claude-opus"])
    completer = SlashCompleter(app.registry)

    def complete(text):
        return [
            (item.text, item.start_position)
            for item in completer.get_completions(Document(text), CompleteEvent())
        ]

    levels = [("low", 0), ("medium", 0), ("high", 0), ("xhigh", 0), ("default", 0)]
    assert complete("/btw +") == levels
    assert complete("/btw +h") == [("high", -1)]
    assert complete("/btw $anthropic:claude-opus+") == levels
    assert complete("/btw $anthropic:claude-opus+X") == [("xhigh", -1)]
    assert complete("/btw +low $anthropic:claude-opus +me") == [("medium", -2)]
    # A `$` word after an effort still completes as a model.
    assert complete("/btw +low $opus") == [("$anthropic:claude-opus", -5)]
    # Once the question starts, `+` is text.
    assert complete("/btw why +") == []
    assert complete("/btw +low ") == []
    assert model_fragment("$a+hi") is None
    assert effort_fragment("$a:x/y+hi") == "hi"
    assert effort_fragment("why +") is None


def test_model_suggestions_come_from_the_model_picker_catalog(monkeypatch):
    from pcode import models

    app = PreviewApp(model="test:local", console=Console(file=StringIO()))
    calls = []

    def providers(current):
        calls.append(current)
        return {"claude"}

    monkeypatch.setattr(models, "active_providers", providers)
    first = app.controller.model_suggestions()
    assert first == models.model_catalog({"claude"}, "test:local")
    assert first and all(name.startswith("claude:") for name in first)
    # Typing does not rescan providers on every keystroke.
    assert app.controller.model_suggestions() is first
    assert calls == ["test:local"]


def test_side_model_uses_the_chosen_models_own_settings(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    save_model_effort("openai:gpt-5", "high")
    chosen = side_model("openai:gpt-5")
    assert chosen.name == "openai:gpt-5"
    assert chosen.model.model_name == "gpt-5"
    assert chosen.settings == {"openai_reasoning_effort": "high"}
    # Without a saved effort, only the model's defaults.
    assert side_model("openai:gpt-5.1").settings is None
    with pytest.raises(ValueError, match="Cannot use nowhere:model"):
        side_model("nowhere:model")
    monkeypatch.delenv("OPENAI_API_KEY")
    with pytest.raises(ValueError, match="Cannot use openai:gpt-5"):
        side_model("openai:gpt-5")


def test_side_model_takes_the_asked_effort_over_the_saved_one(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    save_model_effort("openai:gpt-5", "high")
    assert side_model("openai:gpt-5", "low").settings == {"openai_reasoning_effort": "low"}
    # `default` is the provider's default, as with /effort default.
    assert side_model("openai:gpt-5", "default").settings == {}
    assert side_model("openai:gpt-5.1", "xhigh").settings == {"openai_reasoning_effort": "xhigh"}


class Recorder(AbstractCapability):
    """Records each request's conversation id and settings."""

    def __init__(self, seen):
        self.seen = seen

    async def before_model_request(self, ctx, request_context):
        aside = any(
            isinstance(part, UserPromptPart) and str(part.content).startswith(ASIDE_FRAMING)
            for message in request_context.messages
            for part in message.parts
        )
        self.seen.append(
            (
                aside,
                request_context.model.model_name,
                ctx.conversation_id,
                request_context.model_settings,
            )
        )
        return request_context


def test_another_model_runs_with_its_settings_and_its_own_conversation_id(tmp_path):
    seen = []

    async def main_model(messages, info):
        yield "main"

    async def other_model(messages, info):
        yield "other"

    runtime = AgentRuntime(
        Agent(
            FunctionModel(stream_function=main_model, model_name="main"),
            capabilities=[create_coder(tmp_path), Recorder(seen)],
            model_settings={"temperature": 0.5, "anthropic_effort": "high"},
        )
    )
    other = SideModel(
        "fn:other", FunctionModel(stream_function=other_model, model_name="other"), {"top_p": 0.9}
    )

    async def run():
        [event async for event in runtime.stream("Main task")]
        assert (await runtime.aside("same?")).answer == "main"
        assert (await runtime.aside("other?", model=other)).answer == "other"
        assert (await runtime.aside("again?", model=other)).answer == "other"
        # The override ended with the run: the conversation keeps its settings.
        [event async for event in runtime.stream("Next")]

    asyncio.run(run())
    main, same, first, second, following = seen
    conversation = runtime.conversation_id
    assert main[:3] == (False, "main", conversation)
    # The default path is the conversation's own request, cache identity included.
    assert same[:3] == (True, "main", conversation)
    assert same[3] == main[3]
    for request in (first, second):
        aside, name, identity, settings = request
        assert (aside, name) == (True, "other")
        assert identity.startswith(f"{conversation}.btw-")
        # Nothing of the conversation model's settings leaks into another model.
        assert settings.get("top_p") == 0.9
        assert "temperature" not in settings
        assert "anthropic_effort" not in settings
    assert first[2] != second[2]
    assert following[2] == conversation
    assert following[3] == main[3]


def test_an_effort_reaches_the_request_on_the_conversations_model_and_another(
    tmp_path, monkeypatch
):
    from pcode import agent as agent_module

    seen = []

    async def main_model(messages, info):
        yield "main"

    async def other_model(messages, info):
        yield "other"

    conversation = {"temperature": 0.5, "anthropic_effort": "high"}
    runtime = AgentRuntime(
        Agent(
            FunctionModel(
                stream_function=main_model,
                model_name="main",
                profile=AnthropicModelProfile(anthropic_supports_effort=True),
            ),
            capabilities=[create_coder(tmp_path), Recorder(seen)],
            model_settings=conversation,
        )
    )
    other = FunctionModel(stream_function=other_model, model_name="other")
    monkeypatch.setattr(
        agent_module,
        "side_model",
        lambda name, effort="": SideModel(name, other, {"openai_reasoning_effort": effort}),
    )
    app = PreviewApp(
        model="anthropic:claude-opus-4-5", runtime=runtime, console=Console(file=StringIO())
    )

    async def settled():
        while app.asides.running:
            await asyncio.sleep(0.01)

    async def run():
        [event async for event in runtime.stream("Main task")]
        await app.controller.start_aside(
            "low?", [SideTarget(effort="low"), SideTarget("openai:x", "xhigh")]
        )
        await settled()
        await app.controller.start_aside("default?", [SideTarget(effort="default")])
        await settled()
        [event async for event in runtime.stream("Next")]

    asyncio.run(run())
    assert [aside.status for aside in app.asides.items] == ["answered"] * 3
    main, *asides, following = seen
    own_low, own_default = [request for request in asides if request[1] == "main"]
    # The conversation's own model keeps its settings and cache identity; only
    # the effort changes.
    assert own_low[2] == own_default[2] == runtime.conversation_id
    assert own_low[3] == {**main[3], "anthropic_effort": "low"}
    # `default` drops the effort, leaving the provider's own.
    assert own_default[3] == {"temperature": 0.5}
    another = next(request for request in asides if request[1] == "other")
    assert another[3]["openai_reasoning_effort"] == "xhigh"
    assert "temperature" not in another[3]
    # The effort was for those questions alone.
    assert following[3] == main[3]
    assert runtime.agent.model_settings == conversation
    labels = [(aside.model, aside.label, aside.effort) for aside in app.asides.items]
    assert labels == [
        ("", "low", "low"),
        ("openai:x", "x \u00b7 xhigh", "xhigh"),
        ("", "default", "default"),
    ]


def test_btw_refuses_an_effort_on_a_model_without_effort_control():
    app = PreviewApp(
        model="test:local",
        runtime=AgentRuntime(Agent(TestModel())),
        console=Console(file=StringIO()),
    )
    start = app.controller.start_aside = AsyncMock()

    def btw(argument):
        asyncio.run(app.controller.aside(argument))

    with pytest.raises(ValueError, match="Effort control requires.*test:local is not one"):
        btw("+low why?")
    with pytest.raises(ValueError, match="nowhere:model is not one"):
        btw("$nowhere:model+high why?")
    start.assert_not_awaited()
    # Without an effort, any model goes, as before.
    btw("$nowhere:model why?")
    btw("$openai:gpt-5+high why?")
    start.assert_awaited_with("why?", [SideTarget("openai:gpt-5", "high")])


def test_btw_judges_anthropic_effort_the_way_effort_does():
    """The conversation's own model is judged on the object /effort judges."""
    model = FunctionModel(
        lambda *args: None,
        model_name="claude-haiku-4-5",
        profile=AnthropicModelProfile(anthropic_supports_effort=False),
    )
    app = PreviewApp(
        model="anthropic:claude-haiku-4-5",
        runtime=AgentRuntime(Agent(model)),
        console=Console(file=StringIO()),
    )
    start = app.controller.start_aside = AsyncMock()

    def btw(argument):
        asyncio.run(app.controller.aside(argument))

    with pytest.raises(ValueError, match="claude-haiku-4-5 has no effort control"):
        btw("+low why?")
    with pytest.raises(ValueError, match="claude-haiku-4-5 has no effort control"):
        btw("$anthropic:claude-haiku-4-5+high why?")
    start.assert_not_awaited()
    # An aside without an effort still runs on the gated model.
    btw("why?")
    # A model that does support effort is not caught by the gate.
    btw("$anthropic:claude-opus-4-5+high why?")
    start.assert_awaited_with("why?", [SideTarget("anthropic:claude-opus-4-5", "high")])


class FanOutRuntime:
    session = None
    tree = None

    def __init__(self):
        self.calls = []
        self.release = asyncio.Event()

    async def aside(self, question, report, model=None, settings=None):
        self.calls.append(model.name if model else None)
        await self.release.wait()
        if model is not None and model.name == "p:broken":
            raise ValueError("provider refused")
        report(f"{model.name if model else 'own'} says hi", "")
        return "done"


def fan_out_app(monkeypatch, runtime):
    from pcode import agent

    monkeypatch.setattr(
        agent,
        "side_model",
        lambda name, effort="": SideModel(name, object(), {"effort": effort} if effort else None),
    )
    output = StringIO()
    app = PreviewApp(
        model="p:own", runtime=runtime, console=Console(file=output, color_system=None, width=200)
    )
    return app, output


def test_several_models_run_in_parallel_and_fail_independently(monkeypatch):
    runtime = FanOutRuntime()
    app, output = fan_out_app(monkeypatch, runtime)
    failures = []
    app.asides.on_failure = lambda aside, error: failures.append(aside.model)

    async def run():
        await app.controller.start_aside(
            "second opinions?", targets("p:own", "p:broken", "q:other")
        )
        await asyncio.sleep(0)
        # One side question per model, all running at once.
        assert app.asides.running == 3
        # The conversation's own model takes the default, cache-sharing path.
        assert runtime.calls == [None, "p:broken", "q:other"]
        assert [(a.model, a.label) for a in app.asides.items] == [
            ("p:own", "own"),
            ("p:broken", "broken"),
            ("q:other", "other"),
        ]
        runtime.release.set()
        await asyncio.sleep(0.05)
        await app.asides.close()

    asyncio.run(run())
    statuses = {aside.model: aside.status for aside in app.asides.items}
    assert statuses == {"p:own": "answered", "p:broken": "failed", "q:other": "answered"}
    assert app.asides.items[2].answer == "q:other says hi"
    assert failures == ["p:broken"]
    text = " ".join(output.getvalue().split())
    assert "Asking beside the conversation on p:broken, q:other" in text
    assert "without the conversation's prompt cache" in text


def test_the_conversations_model_named_explicitly_is_the_default_path(monkeypatch):
    runtime = FanOutRuntime()
    app, output = fan_out_app(monkeypatch, runtime)

    async def run():
        await app.controller.start_aside("why?", targets("p:own"))
        await asyncio.sleep(0)
        runtime.release.set()
        await app.asides.close()

    asyncio.run(run())
    assert runtime.calls == [None]
    assert "prompt cache" not in output.getvalue()


def test_an_unusable_model_fails_the_command_before_anything_starts(monkeypatch):
    from pcode import agent

    def refuse(name, effort=""):
        if name == "bad:model":
            raise ValueError(f"Cannot use {name}: Unknown model")
        return SideModel(name, object(), None)

    monkeypatch.setattr(agent, "side_model", refuse)
    app = PreviewApp(model="p:own", runtime=FanOutRuntime(), console=Console(file=StringIO()))

    async def run():
        with pytest.raises(ValueError, match="Cannot use bad:model"):
            await app.controller.start_aside("why?", targets("q:fine", "bad:model"))

    asyncio.run(run())
    assert app.asides.items == []


def test_btw_command_parses_models_and_explains_its_syntax():
    app = PreviewApp(
        model="test:local",
        runtime=AgentRuntime(Agent(TestModel())),
        console=Console(file=StringIO()),
    )
    start = app.controller.start_aside = AsyncMock()
    app.read_asides = AsyncMock(return_value=None)
    asyncio.run(app.controller.aside("$a:x $b:y $a:x is it right?"))
    start.assert_awaited_once_with("is it right?", [SideTarget("a:x"), SideTarget("b:y")])
    with pytest.raises(ValueError, match=r"Usage: /btw \[\$PROVIDER:MODEL\[\+EFFORT\]"):
        asyncio.run(app.controller.aside("$a:x"))
    # Bare /btw still opens the viewer.
    app.asides.items.append(Aside(question="earlier"))
    asyncio.run(app.controller.aside(""))
    app.read_asides.assert_awaited_once()
    assert start.await_count == 1


def test_the_model_labels_running_rows_and_the_viewer():
    from pcode.aside_ui import AsideBrowser, row
    from pcode.ui import Activity

    asides = Asides()
    labeled = Aside(question="why?", model="q:other", label="other", activity="Reading x")
    asides.items.extend([labeled, Aside(question="own?")])
    activity = Activity()
    activity.asides = asides.items
    rows = [text for _, text in activity.aside_rows("⠋", 80)]
    assert rows[0].startswith("⠋ btw · other · why? · Reading x · ")
    assert rows[1].startswith("⠋ btw · own? · ")
    assert row([labeled]).startswith("[other] why?")
    assert row([asides.items[1]]).startswith("own?")
    browser = AsideBrowser(asides, selected=labeled.id, output=None, input=None)
    assert "on q:other" in browser.detail.text(80)
    quick = Aside(question="fast?", model="q:other", label="other \u00b7 low", effort="low")
    asides.items.append(quick)
    assert row([quick]).startswith("[other \u00b7 low] fast?")
    browser = AsideBrowser(asides, selected=quick.id, output=None, input=None)
    assert "on q:other at low effort" in browser.detail.text(80)
