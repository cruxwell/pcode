import asyncio
from io import StringIO
from unittest.mock import AsyncMock, Mock, patch

import pytest
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput
from pydantic_ai import Agent
from rich.console import Console

from pcode.app import PreviewApp
from pcode.aside import SideTarget
from pcode.live import AgentRuntime
from pcode.model_ui import ModelPicker
from pcode.sessions import SavedSession
from pcode.ui import create_prompt

MODELS = ["anthropic:claude-opus-5", "openai-codex:gpt-5.6-luna"]
PROVIDERS = {"anthropic", "openai-codex"}


async def wait_for(predicate):
    async with asyncio.timeout(5):
        while not predicate():
            await asyncio.sleep(0.01)


@pytest.mark.parametrize(
    "keys,expected",
    [
        ("\r", MODELS[0]),
        ("\x1b[B\r", MODELS[1]),
        ("luna\r", MODELS[1]),
        ("anthopus\r", MODELS[0]),
        ("codluna\r", MODELS[1]),
        ("anthropic:new-model\r", "anthropic:new-model"),
        ("\x1b", None),
        ("\x03", None),
        ("\x0c", None),
    ],
)
def test_picker_keyboard(keys, expected):
    async def run():
        with create_pipe_input() as pipe:
            picker = ModelPicker(
                MODELS, PROVIDERS, current=MODELS[0], input=pipe, output=DummyOutput()
            )
            task = asyncio.create_task(picker.run())
            try:
                await wait_for(lambda: picker.app.is_running)
                pipe.send_text(keys)
                assert await asyncio.wait_for(task, 3) == expected
            finally:
                if not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)

    asyncio.run(run())


@pytest.mark.parametrize("prefix", ["ctrl", "ctrl+x"])
def test_picker_contextual_help_preserves_selection_and_filter(monkeypatch, prefix):
    monkeypatch.setattr("pcode.prefix_keys.configured_prefix", lambda: prefix)

    async def run():
        with create_pipe_input() as pipe:
            picker = ModelPicker(MODELS, PROVIDERS, input=pipe, output=DummyOutput())
            task = asyncio.create_task(picker.run())
            try:
                await wait_for(lambda: picker.app.is_running)
                if prefix != "ctrl":
                    pipe.send_text("\x18")
                    await wait_for(lambda: picker.shortcuts.pending)
                    assert ("Ctrl+/", "all keys") in picker.shortcuts.hint_footer()
                pipe.send_text("\x1f")
                await wait_for(lambda: picker.shortcuts.browsing)
                assert ("Enter", "Apply selected model") in picker.shortcuts.hint_rows()
                assert any(
                    "provider:model-id" in label for _, label in picker.shortcuts.hint_rows()
                )
                pipe.send_text("ignored\r\x1b[B\x1f")
                await wait_for(lambda: not picker.shortcuts.visible)
                assert picker.search.text == ""
                assert picker.selected == 0
                assert not task.done()
                pipe.send_text("\x1b[B\r")
                assert await asyncio.wait_for(task, 3) == MODELS[1]
            finally:
                if not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)

    asyncio.run(run())


def test_filter_empty_matches_custom_validation_and_current_marker():
    with create_pipe_input() as pipe:
        picker = ModelPicker(MODELS, PROVIDERS, current=MODELS[0], input=pipe, output=DummyOutput())
        assert "current" in str(picker.fragments())
        picker.search.text = "no matches"
        assert picker.matches == []
        # Supported but unconfigured providers still take a typed ID.
        picker.search.text = "google:not-active"
        assert picker.matches == ["google:not-active"]
        picker.search.text = "unknown:provider"
        assert picker.matches == []
        picker.search.text = "anthropic:invalid model"
        assert picker.matches == []
        picker.search.text = "opus 5"
        assert picker.matches == [MODELS[0]]
        picker.search.text = "anthropic:custom-id"
        assert picker.matches == ["anthropic:custom-id"]
        assert "custom" in str(picker.fragments())


def test_ctrl_l_preserves_editor_draft_and_cursor():
    async def run():
        app = PreviewApp(console=Console(file=StringIO()))
        on_model = Mock()
        with create_pipe_input() as pipe:
            session = create_prompt(
                app.registry, on_model=on_model, input=pipe, output=DummyOutput()
            )
            task = asyncio.create_task(session.prompt_async())
            try:
                await wait_for(lambda: session.app.is_running)
                pipe.send_text("draft\x1b[D\x0c")
                await wait_for(lambda: on_model.called)
                assert session.default_buffer.text == "draft"
                assert session.default_buffer.cursor_position == 4
                assert not task.done()
                pipe.send_text("\r")
                assert await asyncio.wait_for(task, 3) == "draft"
            finally:
                if not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)

    asyncio.run(run())


@pytest.mark.parametrize("save", [False, True])
def test_switch_from_preview_is_lazy_and_honors_save(monkeypatch, tmp_path, save):
    monkeypatch.setattr("pcode.agent.create_agent", lambda *args: Agent("test"))
    root = tmp_path / "sessions"
    app = PreviewApp(
        workspace=tmp_path, session_dir=root, save=save, console=Console(file=StringIO())
    )

    async def run():
        await app.switch_model(MODELS[0])
        assert app.model == MODELS[0]
        assert app.runtime.session is None
        assert not root.exists()
        assert app.runtime.history == []
        assert (app.runtime.session_factory is not None) == save
        _ = [event async for event in app.runtime.stream("hello")]
        if save:
            assert app.runtime.session.info.model == MODELS[0]
            assert app.runtime.session.info.workspace == str(tmp_path)
        else:
            assert not root.exists()
        app.runtime.close()

    asyncio.run(run())


@pytest.mark.parametrize("save", [False, True])
def test_switch_continues_conversation(monkeypatch, tmp_path, save):
    from pydantic_ai.models.function import FunctionModel

    requests = []

    async def respond(messages, info):
        requests.append(list(messages))
        yield "remembered answer"

    monkeypatch.setattr(
        "pcode.agent.create_agent", lambda *args: Agent(FunctionModel(stream_function=respond))
    )
    root = tmp_path / "sessions"
    saved = SavedSession.create(MODELS[0], tmp_path, root) if save else None
    runtime = AgentRuntime(Agent("test"), saved)
    app = PreviewApp(
        model=MODELS[0],
        workspace=tmp_path,
        saved_session=saved,
        runtime=runtime,
        console=Console(file=StringIO()),
    )

    async def run():
        _ = [event async for event in runtime.stream("old question")]
        old_id = runtime.conversation_id
        history = list(runtime.history)
        plan_store = runtime.plan_store
        inspections = runtime.inspections
        usage = (runtime.input_tokens, runtime.output_tokens)
        app.activity.plan = [{"content": "old plan"}]
        runtime.agent.model_settings = {"temperature": 0.5}
        await app.switch_model(MODELS[1])
        assert app.runtime is runtime
        assert app.model == MODELS[1]
        assert runtime.history == history
        assert runtime.conversation_id == old_id
        assert runtime.turns == 1
        assert (runtime.input_tokens, runtime.output_tokens) == usage
        assert runtime.plan_store is plan_store
        assert runtime.inspections is inspections
        assert runtime.agent.model_settings is None
        assert app.activity.plan == [{"content": "old plan"}]
        assert runtime.session is saved
        if save:
            from pcode.sessions import read_info

            assert read_info(saved.directory).model == MODELS[1]
        _ = [event async for event in runtime.stream("follow-up question")]
        assert requests[0][: len(history)] == history
        assert runtime.turns == 2
        assert runtime.conversation_id == old_id
        if save:
            assert runtime.session.info.id == old_id
            assert len(list(root.iterdir())) == 1
            recovered = await runtime.session.recover()
            assert recovered == runtime.history
        else:
            assert runtime.session is None
            assert not root.exists()
        runtime.reset()
        _ = [event async for event in runtime.stream("new question")]
        assert runtime.conversation_id != old_id
        if save:
            assert runtime.session.info.model == MODELS[1]
        runtime.close()

    asyncio.run(run())


@pytest.mark.parametrize("resume", [False, True])
def test_model_recovers_failed_startup(monkeypatch, tmp_path, resume):
    """A model that cannot start must not strand the session: /model picks another."""
    root = tmp_path / "sessions"
    saved = SavedSession.create("bogus:model", tmp_path, root) if resume else None
    app = PreviewApp(
        model="bogus:model",
        workspace=tmp_path,
        session_dir=root,
        saved_session=saved,
        resume=resume,
        save=True,
        console=Console(file=StringIO()),
    )
    controller = app.controller
    monkeypatch.setattr(controller, "_load_extensions", lambda *args: None)
    monkeypatch.setattr(controller, "_create_runtime", Mock(side_effect=ValueError("bad model")))
    replays = Mock()
    monkeypatch.setattr(app, "replay_conversation", replays)

    async def run():
        with pytest.raises(ValueError, match="bad model"):
            await controller.initialize_runtime()
        controller.startup_error = ValueError("bad model")
        generation = controller.prompts.generation
        assert controller.dispatchable(generation, "/model")
        assert not controller.dispatchable(generation, "/new")

        monkeypatch.setattr("pcode.agent.create_agent", lambda *args: Agent("test"))
        await controller.switch_model(MODELS[0])
        assert controller.startup_error is None
        assert isinstance(controller.runtime, AgentRuntime)
        assert controller.model == MODELS[0]
        assert controller.mcp_defaults_requested
        assert controller.dispatchable(generation, "/new")
        if resume:
            assert controller.runtime.session is saved
            assert saved.info.model == MODELS[0]
            replays.assert_called_once()
        else:
            assert controller.runtime.session_factory is not None
            replays.assert_not_called()
        controller.runtime.close()

    asyncio.run(run())


def test_switch_manifest_failure_keeps_conversation(monkeypatch, tmp_path):
    saved = SavedSession.create(MODELS[0], tmp_path, tmp_path / "sessions")
    runtime = AgentRuntime(Agent("test"), saved)
    app = PreviewApp(
        model=MODELS[0],
        runtime=runtime,
        saved_session=saved,
        console=Console(file=StringIO()),
    )
    old_agent = runtime.agent
    monkeypatch.setattr("pcode.agent.create_agent", lambda *args: Agent("test"))
    monkeypatch.setattr(saved, "save_info", Mock(side_effect=OSError("disk full")))
    try:
        with pytest.raises(OSError, match="disk full"):
            asyncio.run(app.switch_model(MODELS[1]))
        assert app.runtime is runtime
        assert runtime.agent is old_agent
        assert app.model == saved.info.model == MODELS[0]
    finally:
        runtime.close()


def test_replacing_agent_rebinds_planning_store():
    from pydantic_ai_harness import Planning

    runtime = AgentRuntime(Agent("test"))
    planning = Planning()
    runtime.replace_agent(Agent("test", capabilities=[planning]))
    assert planning.store_resolver(None) is runtime.plan_store
    runtime.reset()
    assert planning.store_resolver(None) is runtime.plan_store


def test_failed_switch_and_same_model_keep_runtime(monkeypatch, tmp_path):
    runtime = AgentRuntime(Agent("test"))
    app = PreviewApp(model=MODELS[0], runtime=runtime, console=Console(file=StringIO()))
    create = Mock(side_effect=ValueError("bad provider"))
    monkeypatch.setattr("pcode.agent.create_agent", create)

    async def run():
        await app.switch_model(MODELS[0])
        create.assert_not_called()
        with pytest.raises(ValueError, match="bad provider"):
            await app.switch_model(MODELS[1])
        assert app.runtime is runtime
        assert app.model == MODELS[0]

    asyncio.run(run())


def test_selection_during_a_turn_is_adopted_at_once(monkeypatch, tmp_path):
    """The running turn's next request uses it; see `test_model_switch` for the run itself."""
    runtime = AgentRuntime(Agent("test"))
    buffer = StringIO()
    app = PreviewApp(model=MODELS[0], runtime=runtime, console=Console(file=buffer, width=200))
    created = Mock(return_value=Agent("test"))
    monkeypatch.setattr("pcode.agent.create_agent", created)
    monkeypatch.setattr(app.controller, "persist_defaults", Mock())

    async def run():
        app.activity.busy = True
        app.controller.live_task = asyncio.create_task(asyncio.Event().wait())
        try:
            await app.switch_model(MODELS[1])
        finally:
            app.controller.live_task.cancel()
        created.assert_called_once()
        assert app.model == MODELS[1]
        assert runtime.agent is created.return_value
        app.controller.persist_defaults.assert_called_once_with(model=MODELS[1])
        assert (
            f"The request in flight finishes on {MODELS[0]}; the next one uses {MODELS[1]}."
            in buffer.getvalue()
        )

    asyncio.run(run())


def test_selection_during_a_turn_on_another_model_says_that_turn_keeps_it(monkeypatch):
    runtime = AgentRuntime(Agent("test"))
    buffer = StringIO()
    app = PreviewApp(model=MODELS[0], runtime=runtime, console=Console(file=buffer, width=200))
    monkeypatch.setattr("pcode.agent.create_agent", Mock(return_value=Agent("test")))
    monkeypatch.setattr(app.controller, "persist_defaults", Mock())

    async def run():
        app.controller.borrowed_turn = SideTarget("openai:gpt-5")
        app.controller.live_task = asyncio.create_task(asyncio.Event().wait())
        try:
            await app.switch_model(MODELS[1])
        finally:
            app.controller.live_task.cancel()
        assert f"This turn stays on openai:gpt-5; the next one uses {MODELS[1]}." in (
            buffer.getvalue()
        )

    asyncio.run(run())


def test_selection_during_a_turn_that_fails_keeps_the_current_model(monkeypatch):
    runtime = AgentRuntime(Agent("test"))
    app = PreviewApp(model=MODELS[0], runtime=runtime, console=Console(file=StringIO()))
    agent = runtime.agent
    monkeypatch.setattr("pcode.agent.create_agent", Mock(side_effect=ValueError("bad provider")))

    async def run():
        app.activity.busy = True
        with pytest.raises(ValueError, match="bad provider"):
            await app.switch_model(MODELS[1])
        assert app.model == MODELS[0]
        assert runtime.agent is agent

    asyncio.run(run())


def test_selecting_the_current_model_while_working_changes_nothing(monkeypatch):
    buffer = StringIO()
    runtime = AgentRuntime(Agent("test"))
    app = PreviewApp(model=MODELS[0], runtime=runtime, console=Console(file=buffer))
    monkeypatch.setattr("pcode.agent.create_agent", Mock(side_effect=AssertionError("no switch")))

    async def run():
        app.activity.busy = True
        await app.switch_model(MODELS[0])
        assert "Already using" in buffer.getvalue()
        assert app.model == MODELS[0]

    asyncio.run(run())


@pytest.mark.parametrize("command", ["/model\r", "\x0c"])
@pytest.mark.parametrize("busy", [False, True])
def test_picker_shortcut_uses_serialized_command_flow(command, busy, monkeypatch):
    monkeypatch.setattr("pcode.models.active_providers", lambda _: PROVIDERS)

    async def run():
        output = StringIO()
        app = PreviewApp(console=Console(file=output))
        app.activity.busy = busy
        choose = AsyncMock(return_value=None)
        app.choose_model = choose
        session = None
        with create_pipe_input() as pipe:

            def prompt(*args, **kwargs):
                nonlocal session
                session = create_prompt(*args, input=pipe, output=DummyOutput(), **kwargs)
                return session

            with patch("pcode.app.create_prompt", prompt):
                task = asyncio.create_task(app.run_async())
                try:
                    await wait_for(lambda: session is not None and session.app.is_running)
                    pipe.send_text(command)
                    # The picker opens while working too; the switch itself is
                    # what waits for the request in flight.
                    await wait_for(lambda: choose.called)
                    choose.assert_awaited_once()
                    assert "unavailable while working" not in output.getvalue()
                    pipe.send_text("/quit\r")
                    await asyncio.wait_for(task, 3)
                finally:
                    if not task.done():
                        task.cancel()
                        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(run())


@pytest.mark.in_process("stubs the controller's activate_model with one setting the terminal's")
def test_model_chosen_mid_run_is_adopted_without_waiting_for_the_turn(monkeypatch):
    """The switch lands while the turn runs; a queued prompt starts on the new model."""
    monkeypatch.setattr("pcode.models.active_providers", lambda _: PROVIDERS)

    async def run():
        from pcode.runtime import Message

        started = asyncio.Event()
        finish = asyncio.Event()
        models = []

        class Runtime:
            session = None
            recovery_blocked = ""

            async def stream(self, text):
                models.append(app.model)
                started.set()
                await finish.wait()
                yield Message("done")

        output = StringIO()
        app = PreviewApp(model=MODELS[0], runtime=Runtime(), console=Console(file=output))

        async def activate(model):
            app.model = model

        async def choose(*_):
            return MODELS[1]

        app.controller.activate_model = activate
        app.choose_model = choose
        session = None
        with create_pipe_input() as pipe:

            def prompt(*args, **kwargs):
                nonlocal session
                session = create_prompt(*args, input=pipe, output=DummyOutput(), **kwargs)
                return session

            with patch("pcode.app.create_prompt", prompt):
                task = asyncio.create_task(app.run_async())
                try:
                    await wait_for(lambda: session is not None and session.app.is_running)
                    pipe.send_text("first\r")
                    await asyncio.wait_for(started.wait(), 5)
                    pipe.send_text("/model\r")
                    await wait_for(lambda: app.model == MODELS[1])
                    assert app.controller.turn_running()
                    started.clear()
                    pipe.send_text("second\r")
                    await wait_for(lambda: app.activity.queued_prompts == ["second"])
                    finish.set()
                    await asyncio.wait_for(started.wait(), 5)
                    assert models == [MODELS[0], MODELS[1]]
                    pipe.send_text("/quit\r")
                    await asyncio.wait_for(task, 5)
                finally:
                    if not task.done():
                        task.cancel()
                        await asyncio.gather(task, return_exceptions=True)

    asyncio.run(run())


@pytest.mark.parametrize("selection", [None, MODELS[1]])
def test_modal_result_is_applied_only_on_accept(monkeypatch, selection):
    from contextlib import asynccontextmanager
    from types import SimpleNamespace

    app = PreviewApp(model=MODELS[0], runtime=Mock(agent=None), console=Console(file=StringIO()))
    switch = AsyncMock()
    app.controller.switch_model = switch
    monkeypatch.setattr("pcode.models.active_providers", lambda _: PROVIDERS)
    picker = Mock(run=AsyncMock(return_value=selection))
    factory = Mock(return_value=picker)
    monkeypatch.setattr("pcode.model_ui.ModelPicker", factory)

    @asynccontextmanager
    async def terminal(app):
        yield

    monkeypatch.setattr("pcode.app.suspended_editor", terminal)

    async def run():
        app.output = SimpleNamespace(lock=asyncio.Lock(), flush=AsyncMock())
        app.prompt_session = SimpleNamespace(
            app=SimpleNamespace(input=object(), output=object(), style=None)
        )
        await app.controller.select_model("")
        if selection:
            switch.assert_awaited_once_with(selection)
        else:
            switch.assert_not_called()
        assert factory.call_args.kwargs["current"] == MODELS[0]

    asyncio.run(run())


def test_no_active_provider_does_not_open_modal(monkeypatch):
    buffer = StringIO()
    app = PreviewApp(console=Console(file=buffer))
    monkeypatch.setattr("pcode.models.active_providers", lambda _: set())
    factory = Mock(side_effect=AssertionError("must not open"))
    monkeypatch.setattr("pcode.model_ui.ModelPicker", factory)
    asyncio.run(app.controller.select_model(""))
    assert "No active model providers" in buffer.getvalue()
    factory.assert_not_called()


@pytest.mark.parametrize(
    "query,expected",
    [
        ("anthopus", ["anthropic:claude-opus-5"]),
        ("ANTHOPUS", ["anthropic:claude-opus-5"]),
        ("anth opus", ["anthropic:claude-opus-5"]),
        ("opus anth", ["anthropic:claude-opus-5"]),
        ("claopus", ["anthropic:claude-opus-5"]),
        ("codluna", ["openai-codex:gpt-5.6-luna"]),
        ("openai luna", ["openai-codex:gpt-5.6-luna"]),
        ("sonnet", ["anthropic:claude-sonnet-5"]),
        ("gpt-5.6", ["openai-codex:gpt-5.6-luna"]),
        ("opus luna", []),
    ],
)
def test_filter_combines_provider_and_model_prefixes(query, expected):
    models = [*MODELS, "anthropic:claude-sonnet-5", "anthropic:claude-haiku-4-5"]
    with create_pipe_input() as pipe:
        picker = ModelPicker(models, PROVIDERS, input=pipe, output=DummyOutput())
        picker.selected = 2
        picker.search.text = query
        assert picker.matches == expected
        assert picker.selected == 0
        picker.search.text = ""
        assert picker.matches == models


def test_filter_preserves_newest_first_catalog_order():
    from pcode.models import model_catalog

    current = "anthropic:claude-opus-4-5"
    with create_pipe_input() as pipe:
        picker = ModelPicker(
            model_catalog({"anthropic"}, current),
            {"anthropic"},
            current=current,
            input=pipe,
            output=DummyOutput(),
        )
        picker.search.text = "anthopus"
        assert picker.matches[0] == "anthropic:claude-opus-5"
        assert picker.matches.index("anthropic:claude-opus-4-8") < picker.matches.index(current)
        assert "current" in str(picker.fragments())
