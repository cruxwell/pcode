"""`$MODEL` and `+EFFORT` on a prompt, and on a side-question follow-up: that one ask only."""

import asyncio
from copy import deepcopy
from io import StringIO
from unittest.mock import MagicMock

import pytest
from pydantic_ai import Agent
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.models.function import FunctionModel
from rich.console import Console

from pcode.agent import SideModel, create_coder
from pcode.app import PreviewApp
from pcode.aside import SideReply, SideTarget, prompt_target
from pcode.live import AgentRuntime
from pcode.runtime import Message


class Recorder(AbstractCapability):
    """Records each request's model, conversation id, settings and messages."""

    def __init__(self, seen):
        self.seen = seen

    async def before_model_request(self, ctx, request_context):
        self.seen.append(
            {
                "model": request_context.model.model_name,
                "conversation": ctx.conversation_id,
                "settings": request_context.model_settings,
                "messages": deepcopy(list(request_context.messages)),
            }
        )
        return request_context


@pytest.mark.parametrize(
    ("text", "target", "rest"),
    [
        ("$openai:gpt-5 fix it", SideTarget("openai:gpt-5"), "fix it"),
        ("$openai:gpt-5+high fix it", SideTarget("openai:gpt-5", "high"), "fix it"),
        ("+low what changed?", SideTarget(effort="low"), "what changed?"),
        ("$openai:gpt-5", SideTarget("openai:gpt-5"), ""),
    ],
)
def test_a_leading_target_is_split_off_the_prompt(text, target, rest):
    assert prompt_target(text) == (target, rest)


@pytest.mark.parametrize(
    "text",
    ["$HOME is unset", "+1 to that", "$:x hi", "$openai: hi", "plain $openai:gpt-5 later", ""],
)
def test_anything_less_than_an_unmistakable_target_is_prompt_text(text):
    assert prompt_target(text) == (None, text)


def test_a_turn_on_another_model_leaves_the_conversation_on_its_own(tmp_path):
    seen = []

    async def main_model(messages, info):
        yield "main"

    async def other_model(messages, info):
        yield "other"

    runtime = AgentRuntime(
        Agent(
            FunctionModel(stream_function=main_model, model_name="main"),
            capabilities=[create_coder(tmp_path), Recorder(seen)],
            model_settings={"temperature": 0.5},
        )
    )
    other = SideModel(
        "fn:other", FunctionModel(stream_function=other_model, model_name="other"), {"top_p": 0.9}
    )

    async def run():
        [event async for event in runtime.stream("first", model=other)]
        [event async for event in runtime.stream("second")]
        [event async for event in runtime.stream("third", settings={"temperature": 0.1})]

    asyncio.run(run())
    first, second, third = seen
    assert (first["model"], second["model"], third["model"]) == ("other", "main", "main")
    assert first["settings"].get("top_p") == 0.9
    assert second["settings"] == {"temperature": 0.5}
    assert third["settings"] == {"temperature": 0.1}
    # Another model keeps its own provider session; the conversation's is untouched.
    assert first["conversation"].startswith(f"{runtime.conversation_id}.turn-")
    assert second["conversation"] == third["conversation"] == runtime.conversation_id
    # The turn joined the conversation like any other.
    assert [node.prompt for node in runtime.tree.nodes.values() if node.prompt] == [
        "first",
        "second",
        "third",
    ]


def test_a_follow_up_can_switch_the_threads_model(tmp_path):
    seen = []

    async def main_model(messages, info):
        yield "main"

    async def other_model(messages, info):
        yield "other"

    runtime = AgentRuntime(
        Agent(
            FunctionModel(stream_function=main_model, model_name="main"),
            capabilities=[create_coder(tmp_path), Recorder(seen)],
            model_settings={"temperature": 0.5},
        )
    )
    other = SideModel(
        "fn:other", FunctionModel(stream_function=other_model, model_name="other"), {"top_p": 0.9}
    )

    async def run():
        [event async for event in runtime.stream("Main task")]
        reply = await runtime.aside("Why?")
        switched = await runtime.aside("And on yours?", after=reply, model=other)
        stays = await runtime.aside("And then?", after=switched)
        back = await runtime.aside("Back home?", after=stays, settings={"temperature": 0.1})
        return reply, switched, stays, back

    reply, switched, stays, back = asyncio.run(run())
    _, side, on_other, still_other, home = seen
    assert [side["model"], on_other["model"], still_other["model"], home["model"]] == [
        "main",
        "other",
        "other",
        "main",
    ]
    # The switched follow-up still sees the whole thread.
    assert on_other["messages"][: len(reply.messages)] == reply.messages
    assert on_other["conversation"].startswith(f"{runtime.conversation_id}.btw-")
    assert still_other["conversation"] == on_other["conversation"]
    assert home["conversation"] == runtime.conversation_id
    assert home["settings"] == {"temperature": 0.1}


def test_a_prompts_leading_dollar_completes_like_btw():
    from prompt_toolkit.completion import CompleteEvent, Completion
    from prompt_toolkit.document import Document

    from pcode.commands import Command, CommandRegistry, SlashCompleter

    asked = []

    def complete(argument):
        asked.append(argument)
        yield Completion("$q:other", start_position=-len(argument))

    registry = CommandRegistry()
    registry.register(Command("/btw", "", lambda argument: None, argument_completer=complete))
    completer = SlashCompleter(registry)

    def texts(typed):
        return [c.text for c in completer.get_completions(Document(typed), CompleteEvent())]

    assert texts("$oth") == ["$q:other"]
    assert texts("+hi") == ["$q:other"]
    # Once the prompt itself starts, `$` is text.
    assert texts("$q:other fix") == []
    assert texts("fix $oth") == []
    assert asked == ["$oth", "+hi"]


def app_with(runtime) -> PreviewApp:
    return PreviewApp(model="test:local", runtime=runtime, console=Console(file=StringIO()))


def test_a_prompt_naming_a_model_runs_that_turn_on_it(monkeypatch):
    calls = []

    class Runtime:
        session = None
        recovery_blocked = None

        async def stream(self, prompt, **options):
            calls.append((prompt, options))
            yield Message("done")

    app = app_with(Runtime())
    chosen = object()

    async def options(target):
        return {"model": chosen} if target.model else {"settings": {"effort": target.effort}}

    monkeypatch.setattr(app.controller, "target_options", options)
    monkeypatch.setattr(app.controller, "check_effort", lambda target: None)
    output = MagicMock()

    async def run():
        assert await app.run_live(output, "$q:other+high fix it")
        assert await app.run_live(output, "+low again")
        assert await app.run_live(output, "$HOME is unset")
        assert not await app.run_live(output, "$q:other")

    asyncio.run(run())
    assert calls == [
        ("fix it", {"model": chosen}),
        ("again", {"settings": {"effort": "low"}}),
        ("$HOME is unset", {}),
    ]


def test_a_follow_up_naming_a_model_asks_it_and_labels_it(monkeypatch):
    calls = []

    class Runtime:
        session = None
        tree = None

        async def aside(self, question, report, **options):
            calls.append((question, options))
            return SideReply(answer=f"answer to {question}", messages=[question])

    app = app_with(Runtime())
    chosen = object()

    async def options(target):
        return {"model": chosen}

    monkeypatch.setattr(app.controller, "target_options", options)
    monkeypatch.setattr(app.controller, "check_effort", lambda target: None)

    async def settled():
        while app.asides.running:
            await asyncio.sleep(0.01)

    async def run():
        await app.controller.start_aside("why?")
        root = app.asides.items[0]
        await settled()
        app.controller.follow_up_aside(root.thread, "$q:other+low and you?")
        follow = app.asides.items[-1]
        assert (follow.question, follow.model, follow.label, follow.effort) == (
            "and you?",
            "q:other",
            "other · low",
            "low",
        )
        await settled()
        with pytest.raises(ValueError, match="one model"):
            app.controller.follow_up_aside(root.thread, "$q:a $q:b both?")

    asyncio.run(run())
    question, options = calls[1]
    assert question == "and you?"
    assert options["model"] is chosen
    assert options["after"].answer == "answer to why?"


def test_shell_style_dollar_words_stay_text():
    assert prompt_target("$PATH:/usr/bin is wrong") == (None, "$PATH:/usr/bin is wrong")
    assert prompt_target("$google-gla:gemini-3 hi") == (SideTarget("google-gla:gemini-3"), "hi")


def test_a_per_model_conversation_id_maps_back_to_its_session():
    from pcode.conversation_ids import base_conversation, model_conversation

    for kind in ("btw", "turn"):
        assert base_conversation(model_conversation("abc", kind)) == "abc"
    assert base_conversation("abc") == "abc"
    assert base_conversation("abc.def") == "abc.def"
    assert base_conversation(None) is None


def test_continue_after_a_turn_on_another_model_asks_the_conversations(tmp_path):
    seen = []

    async def main_model(messages, info):
        yield "main"

    async def other_model(messages, info):
        yield "other"

    runtime = AgentRuntime(
        Agent(
            FunctionModel(stream_function=main_model, model_name="main"),
            capabilities=[create_coder(tmp_path), Recorder(seen)],
        )
    )
    other = SideModel(
        "fn:other", FunctionModel(stream_function=other_model, model_name="other"), None
    )

    async def run():
        [event async for event in runtime.stream("first", model=other)]
        [event async for event in runtime.stream(None)]

    asyncio.run(run())
    assert [request["model"] for request in seen] == ["other", "main"]


def test_a_fresh_follow_up_returns_to_the_conversations_model(tmp_path):
    seen = []

    async def main_model(messages, info):
        yield "main"

    async def other_model(messages, info):
        yield "other"

    runtime = AgentRuntime(
        Agent(
            FunctionModel(stream_function=main_model, model_name="main"),
            capabilities=[create_coder(tmp_path), Recorder(seen)],
            model_settings={"temperature": 0.5},
        )
    )
    other = SideModel(
        "fn:other", FunctionModel(stream_function=other_model, model_name="other"), None
    )

    async def run():
        [event async for event in runtime.stream("Main task")]
        reply = await runtime.aside("Why?", model=other)
        await runtime.aside("Yours?", after=reply, fresh=True)

    asyncio.run(run())
    _, side, home = seen
    assert (side["model"], home["model"]) == ("other", "main")
    assert home["conversation"] == runtime.conversation_id
    assert home["settings"] == {"temperature": 0.5}


def test_a_follow_up_target_is_read_against_its_thread(monkeypatch):
    from pcode.aside import Aside

    app = app_with(MagicMock())
    monkeypatch.setattr(app.controller, "check_effort", lambda target: None)
    on_other = Aside("q", model="q:other", effort="high")
    at_home = Aside("q")
    follow = app.controller.follow_up_target
    # A bare effort keeps the thread's model.
    assert follow(on_other, SideTarget(effort="low")) == SideTarget("q:other", "low")
    # The conversation's model by name is the conversation's own path.
    assert follow(on_other, SideTarget("test:local")) == SideTarget()
    assert follow(at_home, SideTarget(effort="low")) == SideTarget(effort="low")
    # Where the thread already is: no switch, so its cache is kept.
    assert follow(on_other, SideTarget("q:other", "high")) is None
    assert follow(at_home, SideTarget("test:local")) is None


def test_a_prompt_naming_a_model_never_steers():
    app = app_with(MagicMock())

    async def run():
        app.controller.submit("$q:other look", "steering")
        app.controller.submit("+low look", "steering")
        app.controller.submit("$HOME look", "steering")
        return app.controller.prompts.take_steering()

    assert asyncio.run(run()) == ["$HOME look"]
