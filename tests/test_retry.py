import asyncio
from io import StringIO

import httpx2
import pytest
from pydantic_ai import Agent
from pydantic_ai.exceptions import ModelAPIError
from pydantic_ai.messages import ModelResponse
from pydantic_ai.models.function import FunctionModel
from rich.console import Console

from pcode.app import PreviewApp
from pcode.diagnostics import transient
from pcode.live import AgentRuntime
from pcode.preferences import save_preferences
from pcode.sessions import SessionError


def dropped_connection() -> ModelAPIError:
    """The failure recorded in real sessions: a stream that ends mid-body."""
    error = ModelAPIError("test:local", "Connection error.")
    error.__cause__ = httpx2.RemoteProtocolError(
        "peer closed connection without sending complete message body"
    )
    return error


def streamed_overload() -> ModelAPIError:
    from openai import APIError

    message = "Our servers are currently overloaded. Please try again later."
    error = ModelAPIError("test:local", message)
    error.__cause__ = APIError(
        message, request=httpx2.Request("POST", "https://example.com"), body=None
    )
    return error


@pytest.mark.parametrize("field", ["code", "type", "message"])
def test_streamed_overload_classification(field):
    from openai import APIError

    value = (
        "Our servers are currently overloaded. Please try again later."
        if field == "message"
        else "overloaded_error"
    )
    error = APIError(
        "Provider failure",
        request=httpx2.Request("POST", "https://example.com"),
        body={"error": {field: value}},
    )
    assert transient(error)
    error.status_code = 429
    assert not transient(error)


def test_overload_matching_does_not_retry_unrelated_errors():
    assert transient(streamed_overload())
    assert not transient(ValueError(str(streamed_overload())))
    assert not transient(ModelAPIError("test:local", "Invalid request; try again later."))
    error = streamed_overload()
    error.__cause__.status_code = 401
    assert not transient(error)


def test_transient_classification_ignores_answered_requests():
    assert transient(dropped_connection())
    answered = ModelAPIError("test:local", "Rate limited.")
    answered.status_code = 429
    assert not transient(answered)
    assert not transient(ValueError("bad model name"))


def run(runtime, prompt):
    async def drive():
        return [event async for event in runtime.stream(prompt)]

    return asyncio.run(drive())


def failing_model(failures, error=dropped_connection):
    calls = []

    async def model(messages, info):
        calls.append(len(messages))
        if len(calls) <= failures:
            raise error()
        yield "done"

    return model, calls


def test_dropped_connection_is_retried_three_times_by_default():
    model, calls = failing_model(3)
    runtime = AgentRuntime(Agent(FunctionModel(stream_function=model)))
    try:
        run(runtime, "hello")
    finally:
        runtime.close()
    assert runtime.retry_attempts == 3
    assert len(calls) == 4


def test_retries_are_bounded_by_the_saved_preference():
    save_preferences(retry_attempts="0")
    model, calls = failing_model(1)
    runtime = AgentRuntime(Agent(FunctionModel(stream_function=model)))
    try:
        with pytest.raises(ModelAPIError):
            run(runtime, "hello")
    finally:
        runtime.close()
    assert runtime.retry_attempts == 0
    assert len(calls) == 1


def test_repeated_drops_stop_at_the_configured_limit():
    save_preferences(retry_attempts="2")
    model, calls = failing_model(5)
    runtime = AgentRuntime(Agent(FunctionModel(stream_function=model)))
    try:
        with pytest.raises(ModelAPIError):
            run(runtime, "hello")
    finally:
        runtime.close()
    assert len(calls) == 3


@pytest.mark.parametrize(
    "error",
    [lambda: ModelAPIError("test:local", "Invalid request."), lambda: ValueError("bad model")],
)
def test_answered_and_programming_errors_are_not_retried(error):
    model, calls = failing_model(1, error)
    runtime = AgentRuntime(Agent(FunctionModel(stream_function=model)))
    try:
        with pytest.raises(Exception):
            run(runtime, "hello")
    finally:
        runtime.close()
    assert len(calls) == 1


def test_cancellation_is_never_retried():
    calls = []

    async def model(messages, info):
        calls.append(1)
        raise asyncio.CancelledError
        yield "unreachable"

    runtime = AgentRuntime(Agent(FunctionModel(stream_function=model)))
    try:
        with pytest.raises(asyncio.CancelledError):
            run(runtime, "hello")
    finally:
        runtime.close()
    assert len(calls) == 1


def test_resume_continues_history_without_adding_a_user_message():
    seen = []

    async def model(messages, info):
        seen.append(list(messages))
        yield "done"

    runtime = AgentRuntime(Agent(FunctionModel(stream_function=model)))
    try:
        run(runtime, "hello")
        run(runtime, None)
    finally:
        runtime.close()
    assert len(seen) == 2
    # The resumed request asks again from the settled history rather than
    # appending an empty or duplicated prompt.
    assert seen[1] == runtime.history[: len(seen[1])]
    assert not isinstance(seen[1][-1], ModelResponse)


def test_resume_without_history_is_refused():
    async def model(messages, info):
        yield "unreachable"

    runtime = AgentRuntime(Agent(FunctionModel(stream_function=model)))
    try:
        with pytest.raises(SessionError, match="no earlier prompt"):
            run(runtime, None)
    finally:
        runtime.close()


class StubRuntime:
    def __init__(self, history):
        self.history = history

    def resend_prompt(self):
        if not self.history:
            raise SessionError("There is no earlier prompt to resend.")
        return "earlier prompt"


def preview(history):
    return PreviewApp(
        model="test:local",
        runtime=StubRuntime(history),
        console=Console(file=StringIO()),
    )


@pytest.mark.parametrize("name", ["/continue", "/retry"])
def test_continue_routes_to_a_live_run(name):
    app = preview(["earlier turn"])
    assert app.handle(name) is False
    assert app.activity.queued_prompts == ["earlier prompt"]
    assert app.activity.queued_modes == ["continue"]


def test_continue_is_refused_without_history_or_with_arguments():
    output = StringIO()
    app = PreviewApp(
        model="test:local", runtime=StubRuntime([]), console=Console(file=output, width=160)
    )
    assert app.handle("/continue") is False
    assert not app.activity.queued_prompts
    app.runtime.history = ["earlier turn"]
    assert app.handle("/continue now") is False
    assert not app.activity.queued_prompts
    assert "no earlier prompt" in output.getvalue()
    assert "Usage: /continue" in output.getvalue()


@pytest.mark.parametrize("saved", [False, True])
@pytest.mark.parametrize("error", [dropped_connection, streamed_overload])
def test_retry_preserves_tools_and_exact_request_after_partial_stream(tmp_path, saved, error):
    from copy import deepcopy

    from pydantic_ai.messages import UserPromptPart
    from pydantic_ai.models.function import DeltaToolCall

    from pcode.sessions import SavedSession

    save_preferences(retry_attempts="2")
    calls, effects = [], []

    async def model(messages, info):
        calls.append(deepcopy(messages))
        if len(calls) == 1:
            yield {0: DeltaToolCall(name="write_once", json_args="{}")}
        elif len(calls) < 4:
            yield "Abandoned partial answer"
            raise error()
        else:
            yield "done"

    agent = Agent(FunctionModel(stream_function=model))

    @agent.tool_plain
    def write_once() -> str:
        effects.append("written")
        return "written"

    session = SavedSession.create("test:local", tmp_path, tmp_path / "sessions") if saved else None
    runtime = AgentRuntime(agent, session)
    notices = []
    runtime.retry_notice = notices.append
    try:
        run(runtime, "make one change")
        assert effects == ["written"]
        assert len(calls) == 4
        assert len(notices) == 2
        for messages in calls[1:]:
            prompts = [
                p.content for m in messages for p in m.parts if isinstance(p, UserPromptPart)
            ]
            assert prompts == ["make one change"]
            assert "Abandoned partial answer" not in str(messages)
            assert "written" in str(messages)
        assert runtime.resend_prompt() == "make one change"
    finally:
        runtime.close()


def test_continue_after_restart_preserves_first_failed_prompt(tmp_path):
    from pcode.sessions import SavedSession

    save_preferences(retry_attempts="0")
    model, calls = failing_model(1)
    session = SavedSession.create("test:local", tmp_path, tmp_path / "sessions")
    runtime = AgentRuntime(Agent(FunctionModel(stream_function=model)), session)
    try:
        with pytest.raises(ModelAPIError):
            run(runtime, "original request")
        identity, root = session.info.id, session.directory.parent
        runtime.close()
        session = SavedSession.open(identity, root)
        restored = AgentRuntime(Agent(FunctionModel(stream_function=model)), session)
        asyncio.run(restored.restore())
        assert restored.resend_prompt() == "original request"
        run(restored, None)
        assert len(calls) == 2
        assert calls == [1, 1]
    finally:
        session.close()


@pytest.mark.parametrize("following", [False, True])
def test_continue_uses_send_pipeline_and_original_prompt_spinner(following):
    from unittest.mock import patch

    from prompt_toolkit.input import create_pipe_input
    from prompt_toolkit.output import DummyOutput
    from pydantic_ai.messages import UserPromptPart

    from pcode.ui import create_prompt

    save_preferences(retry_attempts="0")

    async def drive():
        release = asyncio.Event()
        started = asyncio.Event()
        calls = []

        async def model(messages, info):
            calls.append(
                [p.content for m in messages for p in m.parts if isinstance(p, UserPromptPart)]
            )
            if len(calls) == 1:
                raise dropped_connection()
            started.set()
            await release.wait()
            yield "done"

        runtime = AgentRuntime(Agent(FunctionModel(stream_function=model)))
        app = PreviewApp(model="test:local", runtime=runtime, console=Console(file=StringIO()))

        app.send_mode = "queue"

        async def wait(predicate):
            async with asyncio.timeout(5):
                while not predicate():
                    await asyncio.sleep(0.01)

        with create_pipe_input() as pipe:
            session = None

            def prompt(*args, **kwargs):
                nonlocal session
                session = create_prompt(*args, input=pipe, output=DummyOutput(), **kwargs)
                return session

            with patch("pcode.app.create_prompt", prompt):
                task = asyncio.create_task(app.run_async())
                try:
                    await wait(lambda: session is not None and session.app.is_running)
                    pipe.send_text("make the requested change\r")
                    await wait(
                        lambda: app.activity.prompt_state == "failed" and not app.activity.busy
                    )
                    assert app.activity.turn_outcome == "failed"
                    pipe.send_text("/continue\r" + ("next message\r" if following else ""))
                    await asyncio.wait_for(started.wait(), 5)
                    queued = ["next message"] if following else []
                    await wait(
                        lambda: (
                            app.activity.prompt_state == "running"
                            and app.activity.queued_prompts == queued
                        )
                    )
                    assert app.activity.busy
                    assert app.activity.prompt == "make the requested change"
                    assert app.activity.prompt_kind == "user"
                    assert app.activity.prompt_state == "running"
                    fragments = app.activity.status_fragments("⠋", 100)
                    # The row spins for the resent turn without echoing its prompt.
                    assert fragments[0] == ("class:activity.spinner", "⠋ ")
                    assert "make the requested change" not in "".join(t for _, t in fragments)
                    assert app.activity.queued_prompts == (["next message"] if following else [])
                    # A second resend while busy must not queue or steer anything.
                    pipe.send_text("/continue\rdraft")
                    await wait(lambda: session.default_buffer.text == "draft")
                    release.set()
                    await wait(lambda: not app.activity.busy)
                    assert calls[:2] == [
                        ["make the requested change"],
                        ["make the requested change"],
                    ]
                    assert len(calls) == (3 if following else 2)
                    if following:
                        assert calls[2] == ["make the requested change", "next message"]
                    assert app.activity.prompt_state == "done"
                    assert app.activity.turn_outcome == "done"
                    assert session.default_buffer.text == "draft"
                    pipe.send_text("\x15/quit\r")
                    await asyncio.wait_for(task, 5)
                finally:
                    if not task.done():
                        task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                    runtime.close()

    asyncio.run(drive())


@pytest.mark.parametrize("saved", [False, True])
def test_continue_refuses_interrupted_tool_effects(tmp_path, saved):
    from pydantic_ai.models.function import DeltaToolCall

    from pcode.sessions import SavedSession

    async def drive():
        changed = asyncio.Event()
        effects = []

        async def model(messages, info):
            yield {0: DeltaToolCall(name="change_then_wait", json_args="{}")}

        agent = Agent(FunctionModel(stream_function=model))

        @agent.tool_plain
        async def change_then_wait() -> str:
            effects.append("changed")
            changed.set()
            await asyncio.Event().wait()
            return "unreachable"

        session = (
            SavedSession.create("test:local", tmp_path, tmp_path / "sessions") if saved else None
        )
        runtime = AgentRuntime(agent, session)

        async def turn():
            return [event async for event in runtime.stream("make a change")]

        task = asyncio.create_task(turn())
        try:
            await asyncio.wait_for(changed.wait(), 5)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            with pytest.raises(SessionError, match="explicit next step"):
                runtime.resend_prompt()
            assert effects == ["changed"]
            if saved:
                identity, root = session.info.id, session.directory.parent
                runtime.close()
                session = SavedSession.open(identity, root)
                runtime = AgentRuntime(agent, session)
                await runtime.restore()
                with pytest.raises(SessionError, match="explicit next step"):
                    runtime.resend_prompt()
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            runtime.close()

    asyncio.run(drive())


def test_failed_resend_setup_does_not_duplicate_original_prompt(tmp_path, monkeypatch):
    from pydantic_ai.messages import UserPromptPart

    from pcode.sessions import SavedSession

    prompts = []

    async def model(messages, info):
        prompts.append(
            [p.content for m in messages for p in m.parts if isinstance(p, UserPromptPart)]
        )
        yield "done"

    session = SavedSession.create("test:local", tmp_path, tmp_path / "sessions")
    runtime = AgentRuntime(Agent(FunctionModel(stream_function=model)), session)
    try:
        run(runtime, "original request")
        refresh = runtime.refresh_context

        async def broken_setup():
            raise OSError("setup failed")

        monkeypatch.setattr(runtime, "refresh_context", broken_setup)
        with pytest.raises(OSError):
            run(runtime, None)
        monkeypatch.setattr(runtime, "refresh_context", refresh)
        run(runtime, None)
        assert prompts == [["original request"], ["original request"]]
    finally:
        runtime.close()
