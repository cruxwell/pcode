"""`antigravity:` models against a scripted `agy acp` (`tests/fake_agy.py`)."""

import asyncio
import json
import os
import sys
from pathlib import Path

import anyio
import pytest
from pydantic_ai import Agent
from pydantic_ai.messages import (
    BinaryContent,
    ModelRequest,
    ModelResponse,
    NativeToolCallPart,
    NativeToolReturnPart,
    TextPart,
    ThinkingPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models import ModelRequestParameters

from pcode import antigravity, antigravity_tools
from pcode.antigravity import AntigravityError, AntigravityModel, cached_models, replay_text
from pcode.claude_sdk.workspace import CWD_SETTING


@pytest.fixture
def fake_agy(tmp_path, monkeypatch):
    """Put the fake server on PATH as `agy`; returns a reader for what it received."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    script = bin_dir / "agy"
    fake = Path(__file__).with_name("fake_agy.py")
    script.write_text(f'#!/bin/sh\nexec {sys.executable} -I {fake} "$@"\n')
    script.chmod(0o755)
    log = tmp_path / "agy.jsonl"
    monkeypatch.setenv("FAKE_AGY_LOG", str(log))
    monkeypatch.setenv("PATH", f"{bin_dir}:/usr/bin:/bin")
    # The fake reports every GEMINI*/GOOGLE* variable it sees; drop the
    # developer's own so only the synthetic ones below are in play.
    for name in list(os.environ):
        if name.startswith(("GEMINI", "GOOGLE")):
            monkeypatch.delenv(name)
    for name in (
        "GEMINI_API_KEY",
        "GOOGLE_CLOUD_PROJECT",
        "GOOGLE_CLOUD_QUOTA_PROJECT",
        "GOOGLE_GEMINI_BASE_URL",
    ):
        monkeypatch.setenv(name, "synthetic")

    def received(method: str | None = None) -> list[dict]:
        if not log.exists():
            return []
        entries = [json.loads(line) for line in log.read_text().splitlines()]
        return [e for e in entries if method is None or e.get("method") == method]

    return received


def agent_for(model: AntigravityModel, workspace: Path) -> Agent:
    return Agent(model, model_settings={CWD_SETTING: str(workspace)})


def run(coro):
    async def main():
        try:
            return await coro
        finally:
            await antigravity.shutdown()

    return asyncio.run(main())


def prompts(received) -> list[list[dict]]:
    return [e["params"]["prompt"] for e in received("session/prompt")]


def test_one_turn_maps_text_thinking_tools_and_usage(fake_agy, tmp_path):
    async def go():
        return await agent_for(AntigravityModel("fake-flash"), tmp_path).run("hello")

    result = run(go())
    response = result.all_messages()[-1]
    kinds = [type(part) for part in response.parts]
    assert kinds == [ThinkingPart, NativeToolCallPart, NativeToolReturnPart, TextPart]
    call, back = response.parts[1], response.parts[2]
    # Antigravity's capitalized arguments, lower-cased for pcode's tool rows.
    assert call.args["path"] == "a.txt" and call.provider_name == "antigravity"
    assert back.outcome == "success" and back.content == "Edited a.txt"
    assert result.output == "got: hello"
    assert response.usage.input_tokens == 13 and response.usage.cache_read_tokens == 3
    assert response.provider_response_id == "s-1#1"
    # Allowed once, with the allow option rather than the first one offered.
    answers = [e for e in fake_agy() if e.get("id") == "perm"]
    assert answers[0]["result"]["outcome"]["optionId"] == "yes"
    # No API key, Cloud project or endpoint: those would bill or route elsewhere.
    assert fake_agy()[0]["env"] == []
    # Background sub-agents can't forward permission requests; see `Connection.start`.
    assert fake_agy()[0]["argv"] == ["acp", "--dangerously-skip-permissions"]
    new = fake_agy("session/new")[0]["params"]
    assert new["cwd"] == str(tmp_path)
    # pcode's delegation, as an MCP server agy may call; see antigravity_tools.
    assert [server["name"] for server in new["mcpServers"]] == ["pcode"]
    assert fake_agy("session/set_config_option")[0]["params"] == {
        "sessionId": "s-1",
        "configId": "model",
        "value": "fake-flash",
    }
    # pcode's own tools are never offered: the agent loop ends after one request.
    assert len(prompts(fake_agy)) == 1
    assert cached_models() == ("fake-neon", "fake-flash", "fake-fixed")


def test_follow_up_continues_the_session_and_a_new_model_resumes_it(fake_agy, tmp_path):
    async def go():
        model = AntigravityModel("fake-neon")
        first = await agent_for(model, tmp_path).run("one")
        second = await agent_for(model, tmp_path).run("two", message_history=first.all_messages())
        await antigravity.shutdown()
        # As after a restart: a fresh model given the saved history.
        return await agent_for(AntigravityModel("fake-neon"), tmp_path).run(
            "three", message_history=second.all_messages()
        )

    third = run(go())
    assert len(fake_agy("session/new")) == 1
    assert [e["params"]["sessionId"] for e in fake_agy("session/resume")] == ["s-1"]
    # Only the new message each time: no replay.
    assert [p for p in prompts(fake_agy)] == [
        [{"type": "text", "text": text}] for text in ("one", "two", "three")
    ]
    assert third.all_messages()[-1].provider_response_id == "s-1#3"


def test_a_rewind_replays_rather_than_resuming_a_session_with_later_turns(fake_agy, tmp_path):
    async def go():
        model = AntigravityModel("fake-neon")
        first = await agent_for(model, tmp_path).run("one")
        await agent_for(model, tmp_path).run("two", message_history=first.all_messages())
        # Back to after "one": the session also holds "two", which this history dropped.
        return await agent_for(AntigravityModel("fake-neon"), tmp_path).run(
            "branch", message_history=first.all_messages()
        )

    result = run(go())
    assert fake_agy("session/resume") == []
    assert len(fake_agy("session/new")) == 2
    last = prompts(fake_agy)[-1]
    assert "[User]\none" in last[0]["text"] and "two" not in last[0]["text"]
    assert result.all_messages()[-1].provider_response_id == "s-2#1"


def test_another_models_history_is_replayed_tools_included(fake_agy, tmp_path):
    earlier = [
        ModelRequest(parts=[UserPromptPart("remember zebra")]),
        ModelResponse(parts=[ToolCallPart("shell", {"command": "ls"}, "c1")], provider_name="x"),
        ModelRequest(parts=[ToolReturnPart("shell", "a.txt", "c1")]),
        ModelResponse(parts=[TextPart("noted")], provider_name="google"),
    ]

    run(agent_for(AntigravityModel("fake-neon"), tmp_path).run("and now?", message_history=earlier))
    assert fake_agy("session/resume") == []
    replayed, latest = prompts(fake_agy)[-1]
    assert "replayed below as a transcript" in replayed["text"]
    assert "[User]\nremember zebra" in replayed["text"]
    assert '[Assistant ran shell: {"command": "ls"}]' in replayed["text"]
    assert "[shell returned: a.txt]" in replayed["text"]
    assert latest == {"type": "text", "text": "and now?"}


def test_what_pcode_tells_any_model_is_sent_as_text(fake_agy, tmp_path):
    from pydantic_ai_harness._background_completion import make_background_result

    # A turn another model was running when /model switched, and a worker's result.
    history = [
        ModelRequest(parts=[UserPromptPart("fix it")]),
        ModelResponse(parts=[ToolCallPart("shell", {"command": "ls"}, "c1")]),
        ModelRequest(
            parts=[
                ToolReturnPart("shell", "ok", "c1"),
                make_background_result("delegate_task", "c2", "the report"),
                UserPromptPart("steer"),
            ]
        ),
    ]
    settings = {CWD_SETTING: str(tmp_path)}
    run(AntigravityModel("fake-neon").request(history, settings, ModelRequestParameters()))
    texts = [block["text"] for block in prompts(fake_agy)[0]]
    assert texts[1:] == [
        "[pcode] shell returned:\nok",
        "[pcode] Result of the delegate_task call that was running:",
        "the report",
        "steer",
    ]


def test_a_continuation_with_no_message_says_continue(fake_agy, tmp_path):
    from pcode.live import CANCELLED_MARKER

    kept = ModelRequest(parts=[UserPromptPart("fix it"), UserPromptPart(CANCELLED_MARKER)])
    settings = {CWD_SETTING: str(tmp_path)}
    run(AntigravityModel("fake-neon").request([kept], settings, ModelRequestParameters()))
    texts = [block["text"] for block in prompts(fake_agy)[0]]
    assert "fix it" in texts[0] and texts[1:] == ["Continue."]
    # A history that ends in a reply has nothing to answer.
    answered = [ModelRequest(parts=[UserPromptPart("x")]), ModelResponse(parts=[TextPart("y")])]
    with pytest.raises(AntigravityError, match="nothing to answer"):
        run(AntigravityModel("fake-neon").request(answered, None, None))


def test_unsupported_attachments_are_refused(fake_agy):
    pdf = BinaryContent(b"%PDF", media_type="application/pdf")
    request = ModelRequest(parts=[UserPromptPart(["read this", pdf])])
    with pytest.raises(AntigravityError, match="text and images here, not BinaryContent"):
        run(AntigravityModel("fake-neon").request([request], None, None))


def test_images_are_sent_as_image_blocks(fake_agy, tmp_path):
    png = BinaryContent(b"\x89PNG", media_type="image/png")
    run(agent_for(AntigravityModel("fake-neon"), tmp_path).run(["look", png]))
    assert prompts(fake_agy)[0][1] == {"type": "image", "mimeType": "image/png", "data": "iVBORw=="}


def test_never_chooses_always_allow(fake_agy, tmp_path):
    run(agent_for(AntigravityModel("fake-neon"), tmp_path).run("ALWAYS"))
    answers = [e for e in fake_agy() if e.get("id") == "perm"]
    assert answers[0]["result"]["outcome"]["optionId"] == "no"


def test_a_refused_turn_leaves_the_session_where_it_was(fake_agy, tmp_path):
    async def go():
        model = AntigravityModel("fake-neon")
        first = await agent_for(model, tmp_path).run("one")
        with pytest.raises(AntigravityError, match="agy acp --login"):
            await agent_for(model, tmp_path).run("AUTH", message_history=first.all_messages())
        return await agent_for(model, tmp_path).run("two", message_history=first.all_messages())

    result = run(go())
    assert len(fake_agy("session/new")) == 1
    assert result.all_messages()[-1].provider_response_id == "s-1#2"


def test_a_crash_mid_turn_fails_it_and_the_next_turn_starts_over(fake_agy, tmp_path):
    async def go():
        model = AntigravityModel("fake-neon")
        first = await agent_for(model, tmp_path).run("one")
        with pytest.raises(AntigravityError, match="exited"):
            await agent_for(model, tmp_path).run("CRASH", message_history=first.all_messages())
        return await agent_for(model, tmp_path).run("two", message_history=first.all_messages())

    run(go())
    # The crashed session saw a turn this history doesn't have, so it isn't resumed.
    assert fake_agy("session/resume") == []
    assert len(fake_agy("session/new")) == 2
    assert "[User]\none" in prompts(fake_agy)[-1][0]["text"]


def test_a_session_that_cannot_resume_is_replayed(fake_agy, tmp_path):
    antigravity._record_turn("gone-1", 1)
    earlier = [
        ModelRequest(parts=[UserPromptPart("one")]),
        ModelResponse(
            parts=[TextPart("ok")], provider_name="antigravity", provider_response_id="gone-1#1"
        ),
    ]
    run(agent_for(AntigravityModel("fake-neon"), tmp_path).run("two", message_history=earlier))
    assert [e["params"]["sessionId"] for e in fake_agy("session/resume")] == ["gone-1"]
    assert "[User]\none" in prompts(fake_agy)[-1][0]["text"]


def test_an_unknown_model_names_the_offered_ones(fake_agy, tmp_path):
    with pytest.raises(
        AntigravityError, match="Models this account offers: fake-neon, fake-flash, fake-fixed"
    ):
        run(agent_for(AntigravityModel("missing"), tmp_path).run("hi"))


def interrupted(history, prompt: str) -> list:
    """`history` as pcode keeps it after cancelling `prompt`: the request, no reply."""
    from pcode.live import CANCELLED_MARKER

    request = ModelRequest(parts=[UserPromptPart(prompt), UserPromptPart(CANCELLED_MARKER)])
    return [*history, request]


def test_cancel_stops_the_turn_and_the_next_prompt_continues_it(fake_agy, tmp_path):
    async def go():
        model = AntigravityModel("fake-neon")
        agent = agent_for(model, tmp_path)
        first = await agent.run("one")
        # Level-triggered, as pcode's runs are cancelled.
        with anyio.move_on_after(1):
            await agent.run("SLOW", message_history=first.all_messages())
        # The next prompt waits for the cancelled turn to really end, then
        # continues the same session, which already saw "SLOW".
        kept = interrupted(first.all_messages(), "SLOW")
        return await agent.run("after", message_history=kept)

    result = run(go())
    assert fake_agy("session/cancel")
    assert result.output == "got: after"
    assert len(fake_agy("session/new")) == 1
    assert prompts(fake_agy)[-1] == [{"type": "text", "text": "after"}]
    assert result.all_messages()[-1].provider_response_id == "s-1#3"


def test_a_rewind_past_a_cancelled_turn_replays(fake_agy, tmp_path):
    async def go():
        model = AntigravityModel("fake-neon")
        agent = agent_for(model, tmp_path)
        first = await agent.run("one")
        with anyio.move_on_after(1):
            await agent.run("SLOW", message_history=first.all_messages())
        # Without the cancelled request: the session holds a turn this history lacks.
        return await agent.run("after", message_history=first.all_messages())

    run(go())
    assert len(fake_agy("session/new")) == 2
    assert "[User]\none" in prompts(fake_agy)[-1][0]["text"]


def test_a_new_session_replays_the_interrupted_request_too(fake_agy, tmp_path):
    history = interrupted(
        [
            ModelRequest(parts=[UserPromptPart("one")]),
            ModelResponse(parts=[TextPart("ok")], provider_name="google"),
        ],
        "half-done ask",
    )
    run(agent_for(AntigravityModel("fake-neon"), tmp_path).run("after", message_history=history))
    replayed, latest = prompts(fake_agy)[-1]
    assert "[User]\nhalf-done ask" in replayed["text"]
    assert latest == {"type": "text", "text": "after"}


def test_two_cancels_in_a_row_still_continue_the_session(fake_agy, tmp_path):
    async def go():
        model = AntigravityModel("fake-neon")
        agent = agent_for(model, tmp_path)
        first = await agent.run("one")
        kept = first.all_messages()
        for ask in ("SLOW a", "SLOW b"):
            with anyio.move_on_after(1):
                await agent.run(ask, message_history=kept)
            kept = interrupted(kept, ask)
        # pcode keeps both requests, merged into one with two markers.
        return await agent.run("after", message_history=kept)

    result = run(go())
    assert len(fake_agy("session/new")) == 1
    assert result.all_messages()[-1].provider_response_id == "s-1#4"


def test_a_cancelled_first_turn_still_continues_the_session(fake_agy, tmp_path):
    async def go():
        agent = agent_for(AntigravityModel("fake-neon"), tmp_path)
        with anyio.move_on_after(1):
            await agent.run("SLOW")
        return await agent.run("after", message_history=interrupted([], "SLOW"))

    result = run(go())
    assert len(fake_agy("session/new")) == 1
    assert prompts(fake_agy)[-1] == [{"type": "text", "text": "after"}]
    assert result.all_messages()[-1].provider_response_id == "s-1#2"


def test_a_fork_that_moved_the_session_on_is_not_continued(fake_agy, tmp_path):
    async def go():
        original = AntigravityModel("fake-neon")
        first = await agent_for(original, tmp_path).run("one")
        # Another pcode process resumes the same session and adds a turn.
        await agent_for(AntigravityModel("fake-neon"), tmp_path).run(
            "fork", message_history=first.all_messages()
        )
        return await agent_for(original, tmp_path).run("mine", message_history=first.all_messages())

    result = run(go())
    assert len(fake_agy("session/new")) == 2
    assert "[User]\none" in prompts(fake_agy)[-1][0]["text"]
    assert result.all_messages()[-1].provider_response_id == "s-2#1"


def test_an_unwritable_state_directory_still_continues_live(fake_agy, tmp_path, monkeypatch):
    blocked = tmp_path / "not-a-directory"
    blocked.write_text("")
    monkeypatch.setenv("XDG_STATE_HOME", str(blocked))

    async def go():
        model = AntigravityModel("fake-neon")
        first = await agent_for(model, tmp_path).run("one")
        return await agent_for(model, tmp_path).run("two", message_history=first.all_messages())

    run(go())
    assert len(fake_agy("session/new")) == 1


def test_the_ledger_keeps_every_writers_turn(tmp_path, monkeypatch):
    import subprocess

    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    writer = (
        "import sys; from pcode import antigravity\n"
        "for n in range(150): antigravity._record_turn(f'{sys.argv[1]}-{n}', n)\n"
    )
    procs = [subprocess.Popen([sys.executable, "-c", writer, tag]) for tag in "abc"]
    assert all(p.wait(60) == 0 for p in procs)
    ledger = json.loads((tmp_path / "pcode" / "antigravity-sessions.json").read_text())
    assert len(ledger) == 450


def test_shutdown_lets_agy_exit_on_its_own(fake_agy, tmp_path):
    async def go():
        model = AntigravityModel("fake-neon")
        await agent_for(model, tmp_path).run("one")
        process = model._session.connection.process
        await antigravity.shutdown()
        return process.returncode

    # Closing stdin ends it, not a kill, so agy can save what --continue resumes.
    assert asyncio.run(go()) == 0


def test_replacing_or_closing_the_runtime_stops_its_process(fake_agy, tmp_path):
    from pcode.live import AgentRuntime

    async def stopped(connection) -> bool:
        for _ in range(100):
            if not connection.alive:
                return True
            await asyncio.sleep(0.05)
        return False

    async def go():
        model = AntigravityModel("fake-neon")
        runtime = AgentRuntime(agent_for(model, tmp_path))
        await runtime.agent.run("one")
        connection = model._session.connection
        runtime.replace_agent(Agent(AntigravityModel("fake-flash")))  # /model
        replaced = await stopped(connection)
        second = runtime.agent.model
        await runtime.agent.run("two", model_settings={CWD_SETTING: str(tmp_path)})
        runtime.close()  # /resume
        return replaced, await stopped(second._session.connection)

    assert run(go()) == (True, True)


def test_malformed_messages_do_not_stop_the_reader(fake_agy, tmp_path):
    async def go():
        model = AntigravityModel("fake-neon")
        result = await agent_for(model, tmp_path).run("BADLINE")
        return result.output, model._session.connection.alive

    assert run(go()) == ("got: BADLINE", True)


async def streamed(agent, prompt, history=None):
    """Run as pcode does: streamed, which is what hands the model its run context."""
    from pydantic_ai.run import AgentRunResultEvent

    async with agent.run_stream_events(prompt, message_history=history) as events:
        async for event in events:
            if isinstance(event, AgentRunResultEvent):
                result = event.result
    return result


def steering_agent(model, workspace, queue: list):
    from pcode.steering import STEERING_REMINDER, Steering

    def take():
        taken = list(queue)
        queue.clear()
        return taken

    steering = Steering(take, lambda: bool(queue), reminder=STEERING_REMINDER)
    return Agent(model, model_settings={CWD_SETTING: str(workspace)}, capabilities=[steering])


def test_a_message_sent_mid_turn_steers_it(fake_agy, tmp_path):
    queue: list = []

    async def go():
        agent = steering_agent(AntigravityModel("fake-neon"), tmp_path, queue)
        run = asyncio.create_task(streamed(agent, "SLOW"))
        for _ in range(200):
            if prompts(fake_agy):
                break
            await asyncio.sleep(0.02)
        queue.append("also do this")
        return await run

    result = run(go())
    # A second prompt on the same session, which ends the first: agy's steering.
    assert prompts(fake_agy) == [
        [{"type": "text", "text": "SLOW"}],
        [{"type": "text", "text": "also do this"}],
    ]
    response = result.all_messages()[-1]
    texts = [p.content for p in response.parts if isinstance(p, TextPart)]
    assert "".join(texts) == "workinggot: also do this"
    # Both prompts' usage, one turn.
    assert response.usage.output_tokens == 6 and response.provider_response_id == "s-1#1"
    # The history records what Antigravity was told, and no pcode reminder.
    request = result.all_messages()[-2]
    assert [p.content for p in request.parts if isinstance(p, UserPromptPart)] == [
        "SLOW",
        "also do this",
    ]


def test_deferred_steering_commits_at_step_boundary_mid_text_and_mid_tool():
    """AntigravityStream sends steering immediately, but defers the transcript commit
    until the in-flight step (and any running tools) finishes."""
    from pydantic_ai.messages import PartEndEvent, PartStartEvent

    from pcode.antigravity import AntigravityStream
    from pcode.steering import Steering

    async def exercise_mid_text():
        events_log: list[str] = []
        queue = ["say MANGO"]
        updates: asyncio.Queue = asyncio.Queue()
        loop = asyncio.get_running_loop()
        r1 = loop.create_future()
        r2 = loop.create_future()

        class FakeConn:
            def begin(self, method, params):
                events_log.append(f"rpc:{params['prompt'][0]['text']}")
                return r2

        class FakeSession:
            session_id = "s-mid-text"
            connection = FakeConn()
            daemon_tools: set[str] = set()

        def take(*, defer_commit=False):
            taken = list(queue)
            queue.clear()
            if not defer_commit:
                events_log.append(f"commit:{taken}")
            return taken

        def commit(msgs):
            events_log.append(f"commit:{msgs}")

        steering = Steering(take, lambda: bool(queue), commit_messages=commit)

        class FakeCtx:
            messages = [ModelRequest(parts=[UserPromptPart("count")])]
            capabilities = {"steering": steering}

        stream = AntigravityStream(
            model_request_parameters=ModelRequestParameters(),
            model_id="fake-neon",
            run_context=FakeCtx(),
        )
        stream.session = FakeSession()
        stream.reply = r1
        stream.replies = [r1]

        async def feed():
            # Step 1 starts streaming text; steer arrives mid-text.
            updates.put_nowait(
                {"sessionUpdate": "agent_message_chunk", "content": {"text": "1, 2, "}}
            )
            await asyncio.sleep(0.05)
            # Step 1 finishes streaming AFTER steer was already sent.
            updates.put_nowait({"sessionUpdate": "agent_message_chunk", "content": {"text": "3."}})
            updates.put_nowait({"sessionUpdate": "usage_update", "used": 100})
            await asyncio.sleep(0.05)
            # Step 2 replies having seen the steer.
            updates.put_nowait(
                {"sessionUpdate": "agent_message_chunk", "content": {"text": "MANGO"}}
            )
            updates.put_nowait({"sessionUpdate": "usage_update", "used": 120})
            r2.set_result({"stopReason": "end_turn"})
            updates.put_nowait(r2)

        async def _get_event_iterator():
            async for ev in stream._events(updates, stream.session):
                yield ev

        stream._get_event_iterator = _get_event_iterator
        feeder = asyncio.create_task(feed())
        async for ev in stream:
            if isinstance(ev, PartEndEvent) and isinstance(ev.part, TextPart):
                events_log.append(f"part_end:{ev.part.content}")
            elif isinstance(ev, PartStartEvent) and isinstance(ev.part, TextPart):
                events_log.append(f"part_start:{ev.part.content}")
        await feeder
        return events_log

    log = run(exercise_mid_text())
    # RPC was sent immediately; commit waited until step 1's TextPart ended before step 2.
    assert "rpc:say MANGO" in log
    assert (
        log.index("part_end:1, 2, 3.")
        < log.index("commit:['say MANGO']")
        < log.index("part_start:MANGO")
    )

    async def exercise_mid_tool():
        events_log: list[str] = []
        queue = ["say PINEAPPLE"]
        updates: asyncio.Queue = asyncio.Queue()
        loop = asyncio.get_running_loop()
        r1 = loop.create_future()
        r2 = loop.create_future()

        class FakeConn:
            def begin(self, method, params):
                events_log.append(f"rpc:{params['prompt'][0]['text']}")
                return r2

        class FakeSession:
            session_id = "s-mid-tool"
            connection = FakeConn()
            daemon_tools: set[str] = set()

        def take(*, defer_commit=False):
            taken = list(queue)
            queue.clear()
            return taken

        def commit(msgs):
            events_log.append(f"commit:{msgs}")

        steering = Steering(take, lambda: bool(queue), commit_messages=commit)

        class FakeCtx:
            messages = [ModelRequest(parts=[UserPromptPart("run bash")])]
            capabilities = {"steering": steering}

        stream = AntigravityStream(
            model_request_parameters=ModelRequestParameters(),
            model_id="fake-neon",
            run_context=FakeCtx(),
        )
        stream.session = FakeSession()
        stream.reply = r1
        stream.replies = [r1]

        async def feed():
            # Step 1 emits usage_update + tool_call (in_progress).
            updates.put_nowait({"sessionUpdate": "usage_update", "used": 50})
            updates.put_nowait(
                {
                    "sessionUpdate": "tool_call",
                    "toolCallId": "t1",
                    "status": "in_progress",
                    "title": "sleep",
                }
            )
            await asyncio.sleep(0.05)
            # Tool completes -> steering should commit right after tool completes, before step 2!
            updates.put_nowait(
                {
                    "sessionUpdate": "tool_call_update",
                    "toolCallId": "t1",
                    "status": "completed",
                    "title": "sleep",
                }
            )
            await asyncio.sleep(0.05)
            updates.put_nowait(
                {"sessionUpdate": "agent_message_chunk", "content": {"text": "PINEAPPLE"}}
            )
            updates.put_nowait({"sessionUpdate": "usage_update", "used": 80})
            r2.set_result({"stopReason": "end_turn"})
            updates.put_nowait(r2)

        async def _get_event_iterator():
            async for ev in stream._events(updates, stream.session):
                yield ev

        stream._get_event_iterator = _get_event_iterator
        feeder = asyncio.create_task(feed())
        async for ev in stream:
            if isinstance(ev, PartStartEvent) and isinstance(ev.part, NativeToolReturnPart):
                events_log.append("tool_return")
            elif isinstance(ev, PartStartEvent) and isinstance(ev.part, TextPart):
                events_log.append(f"part_start:{ev.part.content}")
        await feeder
        return events_log

    tool_log = run(exercise_mid_tool())
    assert "rpc:say PINEAPPLE" in tool_log
    assert (
        tool_log.index("tool_return")
        < tool_log.index("commit:['say PINEAPPLE']")
        < tool_log.index("part_start:PINEAPPLE")
    )


def test_a_steered_turn_that_is_cancelled_still_continues(fake_agy, tmp_path):
    queue: list = []

    async def go():
        model = AntigravityModel("fake-neon")
        agent = steering_agent(model, tmp_path, queue)
        first = await agent.run("one")
        with anyio.move_on_after(1.5):
            run = asyncio.create_task(streamed(agent, "SLOW", first.all_messages()))
            for _ in range(200):
                if len(prompts(fake_agy)) == 2:
                    break
                await asyncio.sleep(0.02)
            queue.append("SLOW too")
            await run
        kept = interrupted(first.all_messages(), "SLOW")
        kept[-1].parts.insert(1, UserPromptPart("SLOW too"))  # Before the marker.
        return await agent.run("after", message_history=kept)

    result = run(go())
    assert len(fake_agy("session/new")) == 1
    assert prompts(fake_agy)[-1] == [{"type": "text", "text": "after"}]
    assert result.output == "got: after"


def test_a_steer_reaches_the_copy_pcode_restores_after_a_cancel(fake_agy, tmp_path):
    from pcode.retries import RequestCheckpoint
    from pcode.steering import Steering

    queue: list = []
    checkpoint = RequestCheckpoint()

    async def go():
        def take():
            taken = list(queue)
            queue.clear()
            return taken

        agent = Agent(
            AntigravityModel("fake-neon"),
            model_settings={CWD_SETTING: str(tmp_path)},
            capabilities=[Steering(take, lambda: bool(queue)), checkpoint],
        )
        with anyio.move_on_after(1.5):
            run_ = asyncio.create_task(streamed(agent, "SLOW"))
            for _ in range(200):
                if prompts(fake_agy):
                    break
                await asyncio.sleep(0.02)
            queue.append("SLOW too")
            await run_

    run(go())
    restored = checkpoint.messages[-1]
    assert [p.content for p in restored.parts if isinstance(p, UserPromptPart)] == [
        "SLOW",
        "SLOW too",
    ]


def test_pcode_notices_reach_it_like_any_model():
    from types import SimpleNamespace

    from pcode.background_delegation import AbandonedDelegations
    from pcode.isolated_delegation import WorkspaceSubAgents

    request = ModelRequest(parts=[UserPromptPart("hi")])
    ctx = SimpleNamespace(messages=[request])
    context = SimpleNamespace(model=AntigravityModel("fake-neon"), messages=[request])
    ids = ["w1"]
    asyncio.run(AbandonedDelegations(ids).before_model_request(ctx, context))
    delegation = WorkspaceSubAgents.__new__(WorkspaceSubAgents)
    delegation.hub = SimpleNamespace(take_questions=lambda: ["Worker w2 asks: which file?"])
    asyncio.run(delegation.before_model_request(ctx, context))
    assert ids == [] and len(request.parts) == 3
    assert request.parts[2].content == "Worker w2 asks: which file?"


def test_retiring_drops_unshown_output_after_the_turn():
    shown: list[str] = []

    async def go():
        model = AntigravityModel("fake-neon")
        model.listener = shown.append
        model._idle_update({"sessionUpdate": "agent_message_chunk", "content": {"text": "late"}})
        model.retire()
        await asyncio.sleep(antigravity.IDLE_QUIET + 0.2)

    asyncio.run(go())
    assert shown == []


@pytest.fixture
def subagent_models(monkeypatch):
    """Set the `subagent_models` default, and a fixed /model catalog."""
    monkeypatch.setattr(
        antigravity_tools,
        "offered_models",
        lambda: ["claude:fable", "openai-codex:gpt"],
    )

    def use(*names):
        monkeypatch.setattr("pcode.preferences.subagent_models", lambda: list(names))

    use()
    return use


def delegating_agent(model, workspace, ran: list):
    """An agent whose `delegate_task` records its calls, shaped like pcode's."""
    agent = Agent(model, model_settings={CWD_SETTING: str(workspace)})

    @agent.tool_plain
    def delegate_task(
        agent_name: str, task: str, model: str | None = None, run_in_background: bool = False
    ):
        """Delegate a task."""
        ran.append({"task": task, "model": model, "background": run_in_background})
        assert agent_name == "worker"  # Filled in: Antigravity can't know pcode's names.
        return f"report on {task} from {model}"

    @agent.tool_plain
    def read_file(path: str):
        """Not offered to Antigravity."""
        raise AssertionError("read_file is not offered")

    return agent


def mcp_prompt(name: str, arguments: dict) -> str:
    return "MCP " + json.dumps({"name": name, "arguments": arguments})


def test_antigravity_can_delegate_to_a_worker_on_another_provider(
    fake_agy, tmp_path, subagent_models
):
    ran: list = []

    async def go():
        model = AntigravityModel("fake-neon")
        agent = delegating_agent(model, tmp_path, ran)
        prompt = mcp_prompt(
            "delegate_task", {"task": "review", "model": "claude:fable", "run_in_background": True}
        )
        return await streamed(agent, prompt)

    result = run(go())
    assert ran == [{"task": "review", "model": "claude:fable", "background": True}]
    assert result.output == "tool said: report on review from claude:fable"
    [listed] = [e for e in fake_agy() if "mcp_tools" in e]
    assert listed["mcp_discover"]["error"]["code"] == -32601
    [tool] = listed["mcp_tools"]
    assert tool["name"] == "delegate_task"
    assert "run_in_background" in tool["inputSchema"]["properties"]
    schema = tool["inputSchema"]
    assert schema["properties"]["model"]["description"] == (
        "Optional model to run the sub-agent on, as provider:model, such as: "
        "claude:fable, openai-codex:gpt. A bare model name (no provider:) resolves against "
        "the session's provider first. Omit it to use the session's current model "
        "(antigravity:fake-neon)."
    )
    assert schema["required"] == ["task"]


def test_omitted_model_defaults_to_antigravitys_own_current_model(
    fake_agy, tmp_path, subagent_models
):
    ran: list = []

    async def go():
        agent = delegating_agent(AntigravityModel("fake-neon"), tmp_path, ran)
        return await streamed(agent, mcp_prompt("delegate_task", {"task": "x"}))

    result = run(go())
    assert ran == [{"task": "x", "model": None, "background": False}]
    assert result.output == "tool said: report on x from None"
    [listed] = [e for e in fake_agy() if "mcp_tools" in e]
    assert listed["mcp_tools"][0]["inputSchema"]["required"] == ["task"]


def test_explicit_antigravity_worker_model_is_passed_through(fake_agy, tmp_path, subagent_models):
    ran: list = []

    async def go():
        agent = delegating_agent(AntigravityModel("fake-neon"), tmp_path, ran)
        arguments = {"task": "x", "model": "antigravity:fake-flash"}
        return await streamed(agent, mcp_prompt("delegate_task", arguments))

    result = run(go())
    assert ran == [{"task": "x", "model": "antigravity:fake-flash", "background": False}]
    assert result.output == "tool said: report on x from antigravity:fake-flash"


def test_only_delegation_is_offered_and_only_during_a_turn(fake_agy, tmp_path, subagent_models):
    ran: list = []

    async def go():
        agent = delegating_agent(AntigravityModel("fake-neon"), tmp_path, ran)
        hidden = await streamed(agent, mcp_prompt("read_file", {"path": "x"}))
        # Unstreamed, the model has no run to execute a call in.
        unstreamed = await agent.run(mcp_prompt("delegate_task", {"task": "x"}))
        return hidden.output, unstreamed.output

    hidden, unstreamed = run(go())
    assert "no tool named 'read_file'" in hidden
    assert antigravity_tools.NO_TURN in unstreamed
    assert ran == []


def test_retiring_closes_the_tool_bridge(fake_agy, tmp_path, subagent_models):
    async def go():
        model = AntigravityModel("fake-neon")
        await streamed(delegating_agent(model, tmp_path, []), "hello")
        socket_dir = Path(model.tools._dir)
        assert (socket_dir / "mcp.sock").exists()
        model.retire()
        for _ in range(100):
            if not socket_dir.exists():
                break
            await asyncio.sleep(0.02)
        return socket_dir

    assert not run(go()).exists()


def test_each_turns_calls_run_in_that_turns_context(fake_agy, tmp_path, subagent_models):
    from contextvars import ContextVar

    turn: ContextVar[str] = ContextVar("turn", default="none")

    async def go():
        model = AntigravityModel("fake-neon")
        agent = Agent(model, model_settings={CWD_SETTING: str(tmp_path)})

        @agent.tool_plain
        def delegate_task(agent_name: str, task: str, model: str | None = None):
            """Delegate."""
            return turn.get()

        outputs = []
        history = None
        for name in ("one", "two"):
            turn.set(name)
            result = await streamed(
                agent, mcp_prompt("delegate_task", {"task": "x", "model": "claude:fable"}), history
            )
            history = result.all_messages()
            outputs.append(result.output)
        return outputs

    # The server started in turn one, but turn two's call sees turn two.
    assert run(go()) == ["tool said: one", "tool said: two"]


def test_a_call_agy_cancels_stops_its_worker(fake_agy, tmp_path, subagent_models):
    stopped: list = []

    async def go():
        model = AntigravityModel("fake-neon")
        agent = Agent(model, model_settings={CWD_SETTING: str(tmp_path)})

        @agent.tool_plain
        async def delegate_task(agent_name: str, task: str, model: str | None = None):
            """Delegate."""
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                stopped.append(task)
                raise

        arguments = {"task": "slow", "model": "claude:fable"}
        call = {"name": "delegate_task", "arguments": arguments, "cancel_after": 0.3}
        return await streamed(agent, "MCP " + json.dumps(call))

    result = run(go())
    assert stopped == ["slow"] and result.output == "tool said: gave up"
    # No reply to a call agy cancelled.
    assert [e["mcp_after_cancel"] for e in fake_agy() if "mcp_after_cancel" in e] == [""]


def test_shutdown_removes_the_socket(fake_agy, tmp_path, subagent_models):
    async def go():
        model = AntigravityModel("fake-neon")
        await streamed(delegating_agent(model, tmp_path, []), "hello")
        return Path(model.tools._dir)

    assert not run(go()).exists()


def test_a_crashed_sessions_socket_directory_is_swept(tmp_path, monkeypatch):
    import os
    import time

    monkeypatch.setattr(antigravity_tools, "_socket_root", lambda: tmp_path)
    stale, fresh = tmp_path / "pcode-agy-old", tmp_path / "pcode-agy-new"
    stale.mkdir()
    fresh.mkdir()
    old = time.time() - antigravity_tools.STALE_AFTER - 5
    os.utime(stale, (old, old))
    antigravity_tools.sweep_stale()
    # Nothing answers on the old one; the new one may still be starting.
    assert not stale.exists() and fresh.exists()


def test_steering_at_a_turns_start_is_sent_with_the_prompt(fake_agy, tmp_path):
    from pcode.steering import STEERING_REMINDER

    queue = ["queued early"]
    run(streamed(steering_agent(AntigravityModel("fake-neon"), tmp_path, queue), "hello"))
    texts = [b["text"] for b in prompts(fake_agy)[0]]
    assert texts == ["hello", "queued early", STEERING_REMINDER]


def test_output_after_the_turn_is_shown(fake_agy, tmp_path, monkeypatch):
    monkeypatch.setattr(antigravity, "IDLE_QUIET", 0.1)
    shown: list[str] = []

    async def go():
        model = AntigravityModel("fake-neon")
        model.listener = shown.append
        await agent_for(model, tmp_path).run("LATER")
        for _ in range(100):
            if len(shown) == 2:
                break
            await asyncio.sleep(0.05)

    run(go())
    assert shown == [
        "Antigravity, after the turn:\nThe sub-agent found 6 files.",
        "Antigravity, after the turn, ran wc -l",
    ]
    # Shown, never answered: pcode sent no prompt of its own.
    assert len(prompts(fake_agy)) == 1


def test_the_runtime_shows_output_after_the_turn_as_a_lasting_note():
    from pcode.live import AgentRuntime

    model = AntigravityModel("fake-neon")
    runtime = AgentRuntime(Agent(model))
    notes: list[str] = []
    runtime.retained_notice = notes.append
    model.listener("hello")
    assert notes == ["hello"]


def test_replay_text_lists_turns_and_tool_calls():
    text = replay_text(
        [
            ModelRequest(parts=[UserPromptPart("fix it")]),
            ModelResponse(
                parts=[NativeToolCallPart("run_command", {"command": "make"}), TextPart("done")]
            ),
        ]
    )
    assert "[User]\nfix it" in text and "[Assistant]\ndone" in text
    assert '[Assistant ran run_command: {"command": "make"}]' in text
    assert antigravity.REPLAY_OMITTED not in text
    assert text.endswith("</conversation>")


def test_replay_text_caps_total_bytes_and_keeps_newest_turns():
    history = []
    for i in range(40):
        history.append(ModelRequest(parts=[UserPromptPart(f"turn-{i:02d} " + "u" * 120)]))
        history.append(ModelResponse(parts=[TextPart(f"reply-{i:02d} " + "a" * 120)]))

    capped = replay_text(history, max_bytes=1200)
    assert len(capped.encode("utf-8")) <= 1200
    assert antigravity.REPLAY_OMITTED in capped
    assert capped.endswith("</conversation>")
    assert "turn-39" in capped and "reply-39" in capped
    assert "turn-00" not in capped and "reply-00" not in capped
    # Kept entries remain in chronological order.
    assert capped.index("turn-39") < capped.index("reply-39")


def test_replay_text_clips_oversized_entry_safely_for_multibyte_utf8():
    # Single newest entry larger than the entire budget, containing 4-byte emoji.
    huge = "🔥" * 500
    history = [
        ModelRequest(parts=[UserPromptPart("old turn")]),
        ModelRequest(parts=[UserPromptPart(huge)]),
    ]
    capped = replay_text(history, max_bytes=600)
    assert len(capped.encode("utf-8")) <= 600
    assert antigravity.REPLAY_OMITTED in capped
    assert "[User]\n🔥" in capped
    assert " …\n</conversation>" in capped
    assert "old turn" not in capped

    # Oversized entry right before a short newest entry keeps the newest entry
    # and clips the oversized entry into the remaining budget.
    history_with_tail = [*history, ModelResponse(parts=[TextPart("short tail")])]
    capped_tail = replay_text(history_with_tail, max_bytes=600)
    assert len(capped_tail.encode("utf-8")) <= 600
    assert "short tail" in capped_tail
    assert "[User]\n🔥" in capped_tail
    assert capped_tail.endswith("</conversation>")

    # Budget too small even for the wrapper returns empty string.
    assert replay_text(history, max_bytes=20) == ""

    # Bare ellipsis is never returned when a multi-byte char can't fit ahead of " …".
    assert antigravity._clip_bytes("🔥" * 10, 5) == ""

    # Tiny budget just above omitted_header + footer falls back cleanly to
    # REPLAY_OMITTED without emitting mangled role-tag stubs like "[Use" or "[Ass".
    header = f"{antigravity.REPLAY_INTRO}\n\n<conversation>\n"
    footer = "\n</conversation>"
    minimal_bytes = len(f"{header}{antigravity.REPLAY_OMITTED}{footer}".encode("utf-8"))
    omitted_base = len(f"{header}{antigravity.REPLAY_OMITTED}\n\n{footer}".encode("utf-8"))
    # Minimal-only band (budget <= 0, minimal <= max_bytes).
    minimal_only = replay_text(history, max_bytes=minimal_bytes)
    assert minimal_only == f"{header}{antigravity.REPLAY_OMITTED}{footer}"
    # Tiny positive budget below MIN_CLIP_BYTES also omits cleanly.
    tiny_budget = replay_text(history, max_bytes=omitted_base + 8)
    assert tiny_budget == f"{header}{antigravity.REPLAY_OMITTED}{footer}"

    # Oversized entry right before a kept tail that leaves < MIN_CLIP_BYTES is
    # dropped whole instead of prepending a stub.
    tail = "[Assistant]\nshort tail"
    exact_tail_budget = omitted_base + len(tail.encode("utf-8")) + 10
    no_stub = replay_text(history_with_tail, max_bytes=exact_tail_budget)
    assert len(no_stub.encode("utf-8")) <= exact_tail_budget
    assert tail in no_stub
    assert "[User]" not in no_stub

    # max_bytes=None disables the cap completely.
    unbounded = replay_text(history, max_bytes=None)
    assert "old turn" in unbounded and huge in unbounded
    assert antigravity.REPLAY_OMITTED not in unbounded


def test_non_side_huge_prompt_and_images_replay_budget(fake_agy, tmp_path, monkeypatch):
    monkeypatch.setattr(antigravity, "REPLAY_MAX_BYTES", 1200)
    monkeypatch.setattr(antigravity, "PROMPT_BUDGET_BYTES", 2000)
    earlier = [
        ModelRequest(parts=[UserPromptPart("earlier context")]),
        ModelResponse(parts=[TextPart("noted")], provider_name="google"),
    ]

    async def go():
        # Huge text prompt omits replay on a brand-new session and never triggers
        # auto-compaction on the 0-turn session.
        model = AntigravityModel("fake-neon")
        model.compact_at, model.used = 10, 500
        await agent_for(model, tmp_path).run("x" * 2100, message_history=earlier)

        # A large image attachment beside short text does not wipe out replay.
        img = BinaryContent(b"\x89PNG" + b"\x00" * 5000, media_type="image/png")
        await agent_for(AntigravityModel("fake-neon"), tmp_path).run(
            ["look", img], message_history=earlier
        )

    run(go())
    sent = prompts(fake_agy)
    # First prompt: no `/compact`, no replay block, just the huge prompt.
    assert len(sent) == 2
    assert sent[0] == [{"type": "text", "text": "x" * 2100}]
    # Second prompt: replay + text + image all present.
    assert len(sent[1]) == 3
    assert "[User]\nearlier context" in sent[1][0]["text"]
    assert sent[1][1] == {"type": "text", "text": "look"}
    assert sent[1][2]["type"] == "image"


def test_btw_caps_replayed_transcript_and_shrinks_for_large_prompts(
    fake_agy, tmp_path, monkeypatch
):
    from pydantic_ai.models.function import FunctionModel

    from pcode.agent import side_model
    from pcode.live import AgentRuntime

    monkeypatch.setattr(antigravity, "REPLAY_MAX_BYTES", 1200)
    monkeypatch.setattr(antigravity, "PROMPT_BUDGET_BYTES", 2000)

    async def dummy(messages, info):
        yield "ok"

    async def go():
        runtime = AgentRuntime(
            Agent(
                FunctionModel(stream_function=dummy),
                model_settings={CWD_SETTING: str(tmp_path)},
            )
        )
        runtime.context_history = [
            msg
            for i in range(30)
            for msg in (
                ModelRequest(parts=[UserPromptPart(f"ask-{i:02d} " + "x" * 100)]),
                ModelResponse(parts=[TextPart(f"ans-{i:02d} " + "y" * 100)]),
            )
        ]
        short_reply = await runtime.aside(
            "status update", model=side_model("antigravity:fake-flash")
        )
        # A medium-large question shrinks replay so replay + question stays within
        # PROMPT_BUDGET_BYTES.
        medium_question = ("m" * 600) + " mid-end"
        medium_reply = await runtime.aside(
            medium_question, model=side_model("antigravity:fake-flash")
        )
        # A question larger than PROMPT_BUDGET_BYTES omits replay rather than
        # pushing the prompt itself over the ceiling.
        huge_question = ("z" * 2100) + " big-end"
        huge_reply = await runtime.aside(huge_question, model=side_model("antigravity:fake-flash"))
        return short_reply, medium_reply, huge_reply

    short_reply, medium_reply, huge_reply = run(go())
    assert "Question: status update" in short_reply.answer
    assert "mid-end" in medium_reply.answer
    assert "big-end" in huge_reply.answer
    sent = prompts(fake_agy)
    # First /btw: capped replay + intact side question.
    assert len(sent[0]) == 2
    replayed_block, question_block = sent[0][0]["text"], sent[0][1]["text"]
    assert len(replayed_block.encode("utf-8")) <= 1200
    assert antigravity.REPLAY_OMITTED in replayed_block
    assert "ans-29" in replayed_block and "ask-00" not in replayed_block
    assert replayed_block.endswith("</conversation>")
    assert "Question: status update" in question_block
    # Second /btw: replay shrunk below REPLAY_MAX_BYTES so total stays <= PROMPT_BUDGET_BYTES.
    assert len(sent[1]) == 2
    mid_replay, mid_question = sent[1][0]["text"], sent[1][1]["text"]
    assert len(mid_replay.encode("utf-8")) < 1200
    assert len(mid_replay.encode("utf-8")) + len(mid_question.encode("utf-8")) <= 2000
    assert mid_replay.endswith("</conversation>")
    assert "mid-end" in mid_question
    # Third /btw: prompt alone exceeded PROMPT_BUDGET_BYTES, so replay was omitted.
    assert len(sent[2]) == 1
    assert "big-end" in sent[2][0]["text"]


def test_replay_text_does_not_double_encode_json_string_tool_args():
    raw_json = '{"path": "src/app.py", "lines": [1, 20]}'
    text = replay_text(
        [
            ModelRequest(parts=[UserPromptPart("inspect app")]),
            ModelResponse(
                parts=[
                    ToolCallPart("read_file", raw_json, tool_call_id="tc-1"),
                    NativeToolCallPart("run_command", {"command": "make"}),
                    TextPart("checked"),
                ]
            ),
        ]
    )
    assert f"[Assistant ran read_file: {raw_json}]" in text
    assert '\\"path\\"' not in text
    assert '[Assistant ran run_command: {"command": "make"}]' in text


def test_replay_text_tiered_compaction_condenses_older_tools_before_dropping_turns(tmp_path):
    from dataclasses import dataclass

    from pydantic_ai.messages import CapabilityPartRendering, DataCapabilityPart, OperatorSource

    @dataclass(kw_only=True)
    class FakeBgResult(
        DataCapabilityPart[str], namespace="test_replay", tag="bg_result", _register=False
    ):
        tool_name: str = "delegate_task"
        outcome: str = "success"

    history: list = [
        ModelRequest(parts=[UserPromptPart("early-user-requirements")]),
        ModelResponse(
            parts=[
                ToolCallPart(
                    "read_file",
                    '{"path": "old.py", "pad": "' + ("c" * 300) + '", "end": "OLD_CALL_TAIL"}',
                    tool_call_id="old-1",
                ),
                TextPart("early-architectural-plan"),
            ]
        ),
        ModelRequest(
            parts=[
                ToolReturnPart(
                    "read_file",
                    "OLD_RET_HEAD " + ("r" * 1400) + " OLD_RET_TAIL",
                    tool_call_id="old-1",
                ),
                FakeBgResult(
                    content="BG_WORKER_REPORT " + ("w" * 350) + " BG_WORKER_TAIL",
                    source=OperatorSource(),
                    rendering=CapabilityPartRendering(voice="data", tag="bg_result"),
                ),
            ]
        ),
        ModelResponse(
            parts=[
                ToolCallPart("grep", '{"q": "' + ("g" * 300) + '"}', tool_call_id="old-2"),
                TextPart("early-follow-up"),
            ]
        ),
        ModelRequest(
            parts=[
                ToolReturnPart(
                    "grep", "GREP_HEAD " + ("m" * 1400) + " GREP_TAIL", tool_call_id="old-2"
                )
            ]
        ),
        ModelResponse(parts=[TextPart("older-summary")]),
    ]
    # Add REPLAY_RECENT_MESSAGES (12) recent messages, including a recent large tool call/return.
    for i in range(5):
        history.append(ModelRequest(parts=[UserPromptPart(f"recent-user-{i}")]))
        history.append(ModelResponse(parts=[TextPart(f"recent-asst-{i}")]))
    history.append(
        ModelResponse(
            parts=[
                ToolCallPart(
                    "read_file",
                    '{"path": "new.py", "pad": "' + ("n" * 240) + '", "end": "RECENT_CALL_TAIL"}',
                    tool_call_id="new-1",
                )
            ]
        )
    )
    history.append(
        ModelRequest(
            parts=[
                ToolReturnPart(
                    "read_file",
                    "RECENT_RET_HEAD " + ("z" * 600) + " RECENT_RET_TAIL",
                    tool_call_id="new-1",
                )
            ]
        )
    )

    full = replay_text(history, max_bytes=None)
    spilled: list[str] = []
    spill_file = tmp_path / "spill.txt"

    def record_spill(text: str) -> Path:
        spilled.append(text)
        spill_file.write_text(text, encoding="utf-8")
        return spill_file

    # Budget smaller than full (~5.5 KB), large enough for Pass 1 (~3.4 KB).
    max_bytes = len(full.encode("utf-8")) - 1500
    compacted = replay_text(history, max_bytes=max_bytes, spill=record_spill)
    assert len(compacted.encode("utf-8")) <= max_bytes
    assert antigravity.REPLAY_CONDENSED in compacted
    assert antigravity.REPLAY_OMITTED not in compacted
    assert str(spill_file) in compacted
    assert spilled == [full]
    # Earlier user prompts, assistant prose, and DataCapabilityPart stay intact.
    assert "early-user-requirements" in compacted
    assert "early-architectural-plan" in compacted
    assert "BG_WORKER_REPORT" in compacted and "BG_WORKER_TAIL" in compacted
    # Older tool calls/returns are condensed rather than dropped whole.
    assert "OLD_RET_HEAD" in compacted and "OLD_RET_TAIL" not in compacted
    assert "OLD_CALL_TAIL" not in compacted
    # Newest 12 messages remain at full fidelity.
    assert "RECENT_CALL_TAIL" in compacted
    assert "RECENT_RET_TAIL" in compacted


def test_replay_text_pass_two_drops_older_tools_before_dropping_prose():
    history: list = []
    # 10 older messages heavy with tool calls/returns plus essential user/assistant prose.
    for i in range(5):
        history.append(
            ModelRequest(
                parts=[
                    UserPromptPart(f"older-user-{i}"),
                    ToolReturnPart(
                        "old_tool",
                        f"OLD_TOOL_RET_{i} " + ("x" * 180),
                        tool_call_id=f"old-{i}",
                    ),
                ]
            )
        )
        history.append(
            ModelResponse(
                parts=[
                    ToolCallPart(
                        "old_tool",
                        f'{{"i": {i}, "pad": "' + ("y" * 120) + '"}',
                        tool_call_id=f"old-{i}",
                    ),
                    TextPart(f"older-prose-{i}"),
                ]
            )
        )
    # 12 newest messages kept at full fidelity.
    for i in range(6):
        history.append(ModelRequest(parts=[UserPromptPart(f"recent-u-{i}")]))
        history.append(
            ModelResponse(
                parts=[
                    ToolCallPart("recent_tool", f'{{"step": {i}}}', tool_call_id=f"r-{i}"),
                    TextPart(f"recent-a-{i}"),
                ]
            )
        )

    # Pass 1 keeps all 10 older tool calls/returns (~2.2 KB of condensed tool text);
    # pick a budget that fits Pass 2 (all prose + 12 recent messages) but not Pass 1.
    full = replay_text(history, max_bytes=None)
    pass1_approx = len(full.encode("utf-8"))
    # Older tool calls (~160 B) + returns (~220 B) across 5 turns add ~1,900 B even after Pass 1.
    budget = pass1_approx - 1200
    dropped = replay_text(history, max_bytes=budget)
    assert len(dropped.encode("utf-8")) <= budget
    assert antigravity.REPLAY_TOOLS_DROPPED in dropped
    assert antigravity.REPLAY_OMITTED not in dropped
    # Older user prompts, older prose, and recent tools are preserved.
    for i in range(5):
        assert f"older-user-{i}" in dropped
        assert f"older-prose-{i}" in dropped
    assert "[Assistant ran old_tool:" not in dropped
    assert "[old_tool returned:" not in dropped
    assert '[Assistant ran recent_tool: {"step": 5}]' in dropped


def test_replay_spill_file_permissions_and_plain_side_skip(fake_agy, tmp_path, monkeypatch):
    import stat

    state_home = tmp_path / "state"
    monkeypatch.setenv("XDG_STATE_HOME", str(state_home))
    monkeypatch.setattr(antigravity, "REPLAY_MAX_BYTES", 1500)
    monkeypatch.setattr(antigravity, "PROMPT_BUDGET_BYTES", 4000)

    short_earlier = [
        ModelRequest(parts=[UserPromptPart("hi")]),
        ModelResponse(parts=[TextPart("hello")], provider_name="google"),
    ]
    large_earlier: list = []
    for i in range(10):
        large_earlier.append(ModelRequest(parts=[UserPromptPart(f"turn-{i} " + "u" * 60)]))
        large_earlier.append(
            ModelResponse(
                parts=[
                    ToolCallPart(
                        "read_file", '{"path": "' + ("p" * 200) + '"}', tool_call_id=f"t{i}"
                    ),
                    TextPart(f"reply-{i} " + "a" * 60),
                ],
                provider_name="google",
            )
        )
        large_earlier.append(
            ModelRequest(
                parts=[ToolReturnPart("read_file", f"ret-{i} " + "r" * 400, tool_call_id=f"t{i}")]
            )
        )
    large_earlier.append(ModelResponse(parts=[TextPart("all done")], provider_name="google"))

    replays_dir = state_home / "pcode" / "replays"

    async def go():
        # 1. Full replay fits: no spill file written.
        await agent_for(AntigravityModel("fake-neon"), tmp_path).run(
            "next", message_history=short_earlier
        )
        assert not replays_dir.exists() or list(replays_dir.glob("*.txt")) == []

        # 2. Plain side completion (no tools): skips spilling even when truncated/condensed.
        plain_model = AntigravityModel("fake-neon", side=True)
        await Agent(
            plain_model,
            instructions="Summarize.",
            model_settings={CWD_SETTING: str(tmp_path)},
        ).run("go", message_history=large_earlier)
        assert not replays_dir.exists() or list(replays_dir.glob("*.txt")) == []

        # 3. Main session with oversized replay: writes private 0o600 spill file.
        await agent_for(AntigravityModel("fake-neon"), tmp_path).run(
            "continue", message_history=large_earlier
        )

    run(go())
    spills = list(replays_dir.glob("*.txt"))
    assert len(spills) == 1
    spill_path = spills[0]
    assert stat.S_IMODE(replays_dir.stat().st_mode) == 0o700
    assert stat.S_IMODE(spill_path.stat().st_mode) == 0o600
    assert spill_path.read_text(encoding="utf-8") == replay_text(large_earlier, max_bytes=None)
    sent = prompts(fake_agy)
    # Third prompt is the main session with oversized replay; its replay block cites the spill file.
    assert str(spill_path) in sent[2][0]["text"]
    assert len(sent[2][0]["text"].encode("utf-8")) <= 1500

    # Unwritable state directory fails gracefully and returns None.
    blocked = tmp_path / "blocked"
    blocked.write_text("not a dir")
    monkeypatch.setenv("XDG_STATE_HOME", str(blocked))
    assert antigravity._spill_replay("transcript") is None


def test_replay_spill_sweeps_files_older_than_24_hours(tmp_path, monkeypatch):
    import stat
    import time

    state_home = tmp_path / "state"
    monkeypatch.setenv("XDG_STATE_HOME", str(state_home))
    replays_dir = state_home / "pcode" / "replays"
    replays_dir.mkdir(parents=True)

    stale = replays_dir / "stale.txt"
    fresh = replays_dir / "fresh.txt"
    other = replays_dir / "keep.log"
    stale.write_text("old")
    fresh.write_text("recent")
    other.write_text("unrelated")

    now = time.time()
    old_mtime = now - antigravity.REPLAY_SPILL_TTL_SECONDS - 3600
    recent_mtime = now - 3600
    os.utime(stale, (old_mtime, old_mtime))
    os.utime(fresh, (recent_mtime, recent_mtime))
    os.utime(other, (old_mtime, old_mtime))

    written = antigravity._spill_replay("new full transcript")
    assert written is not None and written.exists()
    assert written.read_text(encoding="utf-8") == "new full transcript"
    assert stat.S_IMODE(written.stat().st_mode) == 0o600
    assert not stale.exists()
    assert fresh.exists()
    assert other.exists()


def test_compact_asks_antigravity_and_is_not_a_turn(fake_agy, tmp_path):
    async def go():
        model = AntigravityModel("fake-neon")
        nothing = await model.compact([], str(tmp_path))
        first = await agent_for(model, tmp_path).run("one")
        sizes = await model.compact(first.all_messages(), str(tmp_path), "keep the plan")
        await agent_for(model, tmp_path).run("two", message_history=first.all_messages())
        return nothing, sizes

    nothing, sizes = run(go())
    assert nothing is None and sizes == (42, 800)
    # The same session carries on after it: no replay, no new turn in the stamps.
    texts = [blocks[0]["text"] for blocks in prompts(fake_agy)]
    assert texts == ["one", "/compact keep the plan", "two"]
    assert len(fake_agy("session/new")) == 2  # One was the empty conversation's.


def test_auto_compaction_runs_before_the_prompt_and_not_again_until_it_grows(fake_agy, tmp_path):
    notices: list[str] = []

    async def go():
        model = AntigravityModel("fake-neon")
        model.compaction_notice = notices.append
        agent = agent_for(model, tmp_path)
        history = (await agent.run("one")).all_messages()
        model.compact_at, model.used = 500, 900
        history = (await agent.run("two", message_history=history)).all_messages()
        model.used = 900  # The fake reported 800 left: barely grown since.
        await agent.run("three", message_history=history)

    run(go())
    texts = [blocks[0]["text"] for blocks in prompts(fake_agy)]
    assert texts == ["one", "/compact", "two", "three"]
    assert notices == ["Compacting context automatically…"]


def test_autocompact_tells_antigravity_when_to_compact():
    from types import SimpleNamespace

    from pcode.compaction import AutoCompaction

    model = AntigravityModel("fake-flash")
    model._usage_update({"size": 1_000_000})
    runtime = SimpleNamespace(
        auto_compact=True, auto_compact_limit=200_000, compaction_notice=lambda text: None
    )
    capability = AutoCompaction(runtime, SimpleNamespace(run_id="r"))
    context = SimpleNamespace(model=model, model_settings={}, messages=[])
    asyncio.run(capability.before_model_request(None, context))
    assert model.compact_at == 200_000
    runtime.auto_compact = False
    asyncio.run(capability.before_model_request(None, context))
    assert model.compact_at is None


def test_plan_mode_puts_the_session_in_antigravitys_own(fake_agy, tmp_path):
    async def go():
        model = AntigravityModel("fake-neon")
        agent = agent_for(model, tmp_path)
        model.plan_mode = True
        history = (await agent.run("one")).all_messages()
        history = (await agent.run("two", message_history=history)).all_messages()
        model.plan_mode = False
        await agent.run("three", message_history=history)

    run(go())
    assert [e["params"]["modeId"] for e in fake_agy("session/set_mode")] == ["plan", "default"]


def test_a_plain_side_job_is_sent_its_instructions_and_its_process_expires(
    fake_agy, tmp_path, monkeypatch
):
    monkeypatch.setattr(antigravity, "SIDE_IDLE", 0.1)

    async def go():
        model = AntigravityModel("fake-neon", side=True)
        settings = {CWD_SETTING: str(tmp_path)}
        await Agent(model, instructions="Write a title.", model_settings=settings).run("hello")
        connection = model._session.connection
        for _ in range(100):
            if not connection.alive:
                break
            await asyncio.sleep(0.05)
        return connection.alive

    assert run(go()) is False
    assert [block["text"] for block in prompts(fake_agy)[0]] == ["Write a title.", "hello"]


def test_side_uses_and_workers_get_sessions_of_their_own(fake_agy):
    from pcode.agent import side_model, subagent_menu, worker_model
    from pcode.compaction import CompactionError, summarize

    assert side_model("antigravity:fake-neon").model._side
    assert worker_model("antigravity:fake-neon").model.system == "antigravity"
    assert "antigravity:fake-neon" in subagent_menu(["antigravity:fake-neon"])[0]
    # The conversation's own session compacts itself instead (`/compact`).
    with pytest.raises(CompactionError, match="use /compact"):
        asyncio.run(summarize([], model=AntigravityModel("fake-neon")))


def test_btw_runs_in_separate_session_with_replay_and_reuses_for_follow_up(fake_agy, tmp_path):
    from pcode.antigravity import SIDE_REPLAY_INTRO
    from pcode.live import AgentRuntime

    async def go():
        runtime = AgentRuntime(agent_for(AntigravityModel("fake-neon"), tmp_path))
        _ = [event async for event in runtime.stream("main task")]
        reports: list[tuple[str, str]] = []
        first = await runtime.aside("why?", report=lambda ans, act: reports.append((ans, act)))
        follow = await runtime.aside("more?", after=first)
        # Main conversation continues on its own session, untouched by /btw.
        _ = [event async for event in runtime.stream("next main")]
        return first, follow, reports

    first, follow, reports = run(go())
    assert "Question: why?" in first.answer
    assert "Question: more?" in follow.answer
    # Native tool call from fake_agy surfaced in the side question's activity.
    assert any("a.txt" in act for _, act in reports)
    # Two sessions: s-1 for main (with pcode delegation MCP), s-2 for /btw (no MCP).
    new_sessions = fake_agy("session/new")
    assert len(new_sessions) == 2
    assert [s["name"] for s in new_sessions[0]["params"]["mcpServers"]] == ["pcode"]
    assert new_sessions[1]["params"]["mcpServers"] == []
    # Never resumed s-1 for the side question.
    assert fake_agy("session/resume") == []
    sent = fake_agy("session/prompt")
    # 0: main task (s-1), 1: /btw first (s-2, replayed), 2: /btw follow-up (s-2, no replay),
    # 3: next main (s-1, no replay).
    assert [e["params"]["sessionId"] for e in sent] == ["s-1", "s-2", "s-2", "s-1"]
    side_first_prompt = sent[1]["params"]["prompt"]
    assert len(side_first_prompt) == 2
    assert SIDE_REPLAY_INTRO in side_first_prompt[0]["text"]
    assert "[User]\nmain task" in side_first_prompt[0]["text"]
    assert "Question: why?" in side_first_prompt[1]["text"]
    # Follow-up reused s-2 directly without replaying.
    assert len(sent[2]["params"]["prompt"]) == 1
    assert "Question: more?" in sent[2]["params"]["prompt"][0]["text"]
    # Main turn 2 reused s-1 directly without replaying.
    assert sent[3]["params"]["prompt"] == [{"type": "text", "text": "next main"}]


def test_btw_runs_in_parallel_while_main_turn_is_in_flight(fake_agy, tmp_path):
    from pcode.live import AgentRuntime

    async def go():
        runtime = AgentRuntime(agent_for(AntigravityModel("fake-neon"), tmp_path))
        stream = runtime.stream("SLOW")
        # Consume until the slow main turn is actively streaming (holding its lock).
        async for event in stream:
            if getattr(event, "text", None):
                break
        side = await runtime.aside("what is running?")
        await stream.aclose()
        return side

    side = run(go())
    assert "Question: what is running?" in side.answer
    sent = fake_agy("session/prompt")
    assert [e["params"]["sessionId"] for e in sent] == ["s-1", "s-2"]
    side_prompt = sent[1]["params"]["prompt"]
    # The in-flight main prompt ("SLOW") is in the replayed transcript, not the side prompt.
    assert len(side_prompt) == 2
    assert "[User]\nSLOW" in side_prompt[0]["text"]
    assert "SLOW" not in side_prompt[1]["text"]
    assert "Question: what is running?" in side_prompt[1]["text"]


def test_btw_explicit_antigravity_model_handles_mid_tool_loop_context(fake_agy, tmp_path):
    from pydantic_ai.models.function import FunctionModel

    from pcode.agent import side_model
    from pcode.live import AgentRuntime

    async def dummy(messages, info):
        yield "ok"

    async def go():
        runtime = AgentRuntime(
            Agent(
                FunctionModel(stream_function=dummy),
                model_settings={CWD_SETTING: str(tmp_path)},
            )
        )
        runtime.context_history = [
            ModelRequest(parts=[UserPromptPart("fix bug")]),
            ModelResponse(parts=[ToolCallPart("read_file", {"path": "x.py"}, "c1")]),
            ModelRequest(parts=[ToolReturnPart("read_file", "print(1)", "c1")]),
        ]
        chosen = side_model("antigravity:fake-flash")
        return await runtime.aside("is x.py right?", model=chosen)

    reply = run(go())
    assert "Question: is x.py right?" in reply.answer
    assert fake_agy("session/set_config_option")[0]["params"] == {
        "sessionId": "s-1",
        "configId": "model",
        "value": "fake-flash",
    }
    side_prompt = prompts(fake_agy)[0]
    assert len(side_prompt) == 2
    assert "[read_file returned: print(1)]" in side_prompt[0]["text"]
    assert "Question: is x.py right?" in side_prompt[1]["text"]


def test_btw_retires_side_process_on_failure_close_and_summarize(fake_agy, tmp_path):
    from pcode.agent import side_model
    from pcode.aside import Asides, SideReply
    from pcode.live import AgentRuntime

    async def go():
        runtime = AgentRuntime(agent_for(AntigravityModel("fake-neon"), tmp_path))
        # Explicit $antigravity: model that fails mid-turn retires its connection.
        chosen = side_model("antigravity:fake-flash")
        with pytest.raises(AntigravityError):
            await runtime.aside("CRASH", model=chosen)
        await asyncio.gather(*list(antigravity._background), return_exceptions=True)
        assert len(antigravity._connections) == 0

        # Live thread retired on Asides.close().
        asides = Asides()
        aside = asides.start("ok?", lambda _: runtime.aside("ok?"))
        await asides._tasks[aside.id]
        assert len(antigravity._connections) == 1
        await asides.close()
        await asyncio.gather(*list(antigravity._background), return_exceptions=True)
        assert len(antigravity._connections) == 0

        # Summarizing a restored thread (reply.model is None) retires its fresh side model.
        restored = SideReply(answer="ans", messages=[ModelRequest(parts=[UserPromptPart("hi")])])
        summary = await runtime.summarize_aside(restored)
        assert "as my next message." in summary
        await asyncio.gather(*list(antigravity._background), return_exceptions=True)
        assert len(antigravity._connections) == 0

    run(go())


def test_provider_shows_when_agy_is_installed(monkeypatch, tmp_path):
    from pcode.models import active_providers, model_catalog

    monkeypatch.setattr(
        "pcode.models.shutil.which", lambda name: "/bin/agy" if name == "agy" else None
    )
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    assert "antigravity" in active_providers(None)
    # Before any session: one default to pick.
    assert model_catalog({"antigravity"}) == ["antigravity:gemini-4-neon"]
    antigravity._remember_models(
        {"configOptions": [{"id": "model", "options": [{"value": "fake-flash"}]}]}
    )
    assert model_catalog({"antigravity"}) == ["antigravity:fake-flash"]


def test_missing_cli_is_explained(monkeypatch):
    monkeypatch.setattr("pcode.antigravity.shutil.which", lambda _: None)
    with pytest.raises(ValueError, match="not installed"):
        antigravity.antigravity_model("antigravity:fake")


def test_background_command_holds_turn_open_until_followup_ends(fake_agy, tmp_path, monkeypatch):
    monkeypatch.setattr(antigravity, "DEFERRED_FINALIZE_DEBOUNCE", 0.05)
    shown: list[str] = []

    async def go():
        model = AntigravityModel("fake-neon")
        model.listener = shown.append
        return await agent_for(model, tmp_path).run("BGCMD")

    result = run(go())
    assert "Waiting for background work..." in result.output
    assert "All tests passed." in result.output
    parts = [p for m in result.all_messages() for p in m.parts]
    assert any(
        isinstance(p, NativeToolReturnPart)
        and p.tool_call_id == "bg-1"
        and p.content == "all passed"
        for p in parts
    )
    assert shown == []


def test_daemon_command_does_not_hold_turn_open(fake_agy, tmp_path):
    async def go():
        model = AntigravityModel("fake-neon")
        return await agent_for(model, tmp_path).run("BGDAEMON")

    result = run(go())
    assert result.output == "Waiting for background work..."


def test_schedule_tool_holds_turn_open(fake_agy, tmp_path, monkeypatch):
    monkeypatch.setattr(antigravity, "DEFERRED_FINALIZE_DEBOUNCE", 0.05)
    shown: list[str] = []

    async def go():
        model = AntigravityModel("fake-neon")
        model.listener = shown.append
        return await agent_for(model, tmp_path).run("BGSCHED")

    result = run(go())
    assert "Waiting for background work..." in result.output
    assert "All tests passed." in result.output
    assert shown == []


def test_steering_during_held_turn_resumes_prompt_lifecycle(fake_agy, tmp_path):
    queue: list[str] = []

    async def go():
        model = AntigravityModel("fake-neon")
        agent = steering_agent(model, tmp_path, queue)
        task = asyncio.create_task(streamed(agent, "BGHANG"))
        for _ in range(200):
            if (
                model._session is not None
                and model._session.turns == 1
                and not model._session.connection.pending
                and model._session.connection.updates is not None
            ):
                await asyncio.sleep(0.01)
                break
            await asyncio.sleep(0.01)
        queue.append("COMPLETE_BG steer while held")
        return await task

    result = run(go())
    assert "got: COMPLETE_BG steer while held" in result.output
    assert len(prompts(fake_agy)) == 2


def test_cancelling_held_turn_sends_session_cancel_and_recovers(fake_agy, tmp_path):
    async def go():
        model = AntigravityModel("fake-neon")
        agent = agent_for(model, tmp_path)
        task = asyncio.create_task(streamed(agent, "BGHANG"))
        for _ in range(200):
            if (
                model._session is not None
                and model._session.turns == 1
                and not model._session.connection.pending
                and model._session.connection.updates is not None
            ):
                await asyncio.sleep(0.01)
                break
            await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        # Next turn still works (lock released).
        return await agent.run("after cancel")

    result = run(go())
    assert "got: after cancel" in result.output
    assert any(m.get("method") == "session/cancel" for m in fake_agy())


def test_usage_update_records_context_window_and_caches_it(fake_agy, tmp_path):
    from pcode.context_usage import context_label, context_window

    model = AntigravityModel("fake-flash")
    assert context_window(model) is None
    assert context_window("antigravity:fake-flash") is None

    result = run(agent_for(model, tmp_path).run("hello"))
    assert context_window(model) == 1_048_576
    assert context_window("antigravity:fake-flash") == 1_048_576
    # Billing usage keeps the prompt reply total (13), while the footer context
    # meter uses ACP `usage_update.used` (42) rather than the multi-call sum.
    response = result.all_messages()[-1]
    assert response.usage.input_tokens == 13
    assert response.provider_details["context_used"] == 42
    assert context_label(model, result.all_messages()) == " · 42/1.05m"

    # A fresh model instance (as after a restart) reloads the cached window from disk.
    antigravity._windows_loaded_path = None
    antigravity._windows = {}
    fresh = AntigravityModel("fake-flash")
    assert context_window(fresh) == 1_048_576
    assert context_label(fresh, []) == " · 0/1.05m"


def test_usage_update_used_fallback_when_reply_omits_usage(fake_agy, tmp_path):
    from pcode.context_usage import context_label

    model = AntigravityModel("fake-flash")
    result = run(agent_for(model, tmp_path).run("NO_REPLY_USAGE"))
    response = result.all_messages()[-1]
    # Context fill lives in `provider_details["context_used"]`, never stuffed
    # into billing `usage.input_tokens` (which would look like uncached tokens).
    assert response.usage.input_tokens == 0
    assert response.provider_details["context_used"] == 42
    assert context_label(model, result.all_messages()) == " · 42/1.05m"


def test_continuation_turn_does_not_count_context_used_as_uncached_usage():
    from pydantic_ai.models import ModelRequestParameters

    from pcode.antigravity import AntigravityStream

    stream = AntigravityStream(
        model_request_parameters=ModelRequestParameters(),
        model_id="fake-flash",
        continuation=True,
    )
    list(stream._map({"sessionUpdate": "usage_update", "used": 150_000, "size": 1_048_576}))
    stream._finish({"stopReason": "end_turn"})
    assert stream.provider_details == {"stop_reason": "end_turn", "context_used": 150_000}
    assert stream._usage.input_tokens == 0
    assert stream._usage.cache_read_tokens == 0


def test_usage_update_ignores_malformed_and_handles_idle_updates(monkeypatch):
    from pydantic_ai.models import ModelRequestParameters

    from pcode.antigravity import AntigravityStream
    from pcode.context_usage import context_window

    model = AntigravityModel("fake-idle")
    stream = AntigravityStream(model_request_parameters=ModelRequestParameters())
    for bad in (0, -1, True, "1048576", 1.5, None):
        model._idle_update({"sessionUpdate": "usage_update", "size": bad})
    for bad in (-1, True, "42", 1.5, None):
        list(stream._map({"sessionUpdate": "usage_update", "used": bad}))
    assert context_window(model) is None
    assert stream.context_used is None

    model._idle_update({"sessionUpdate": "usage_update", "size": 200_000, "used": 1000})
    assert context_window(model) == 200_000
    monkeypatch.setenv("PCODE_CONTEXT_WINDOW", "100000")
    assert context_window(model) == 100_000


def test_the_compact_cap_applies_to_antigravity_too():
    from types import SimpleNamespace

    from pcode.controller import SessionController

    model = AntigravityModel("fake-flash")
    model._usage_update({"size": 1_048_576})
    runtime = SimpleNamespace(
        agent=SimpleNamespace(model=model),
        auto_compact=True,
        auto_compact_limit=100_000,
        history=[],
        context_history=None,
    )
    controller = object.__new__(SessionController)
    controller.model = "antigravity:fake-flash"
    controller.runtime = runtime
    controller.startup_pending = False
    controller.startup_error = None
    assert controller.compact_cap() == 100_000
    assert "100k" in controller.autocompact_state()


def test_session_info_update_titles_antigravity_session_end_to_end(fake_agy, tmp_path, monkeypatch):
    from io import StringIO
    from types import SimpleNamespace

    from rich.console import Console

    from pcode.app import PreviewApp
    from pcode.live import AgentRuntime
    from pcode.preferences import save_preferences
    from pcode.sessions import SavedSession, list_sessions
    from pcode.ui import TerminalOutput

    save_preferences(session_naming="on")
    asked = []

    async def fail_suggest(*args, **kwargs):
        asked.append(args)
        raise AssertionError("antigravity: should not call suggest_title")

    monkeypatch.setattr("pcode.session_naming.suggest_title", fail_suggest)

    root = tmp_path / "sessions"
    model = AntigravityModel("fake-flash")
    console = Console(file=StringIO())
    app = PreviewApp(workspace=tmp_path, session_dir=root, console=console)
    app.output = TerminalOutput(console, SimpleNamespace(invalidate=lambda: None))
    app.model = "antigravity:fake-flash"
    app.runtime = AgentRuntime(
        agent_for(model, tmp_path),
        session_factory=lambda: SavedSession.create("antigravity:fake-flash", tmp_path, root),
    )

    try:
        run(app.controller.run_turn("TITLE fix the parser"))
        assert asked == []
        assert app.controller.naming_task is None
        assert app.controller.session_title() == "Fix parser bug"
        (info,) = list_sessions(root)
        assert info.title == "Fix parser bug"
    finally:
        if app.runtime.session is not None:
            app.runtime.session.close()


def test_session_info_update_after_turn_and_guards(tmp_path):
    from io import StringIO

    from rich.console import Console

    from pcode.app import PreviewApp
    from pcode.live import AgentRuntime
    from pcode.preferences import save_preferences
    from pcode.sessions import SavedSession, list_sessions

    root = tmp_path / "sessions"
    model = AntigravityModel("fake-flash")
    app = PreviewApp(workspace=tmp_path, session_dir=root, console=Console(file=StringIO()))
    app.model = "antigravity:fake-flash"
    app.runtime = AgentRuntime(Agent(model))
    saved = SavedSession.create("antigravity:fake-flash", tmp_path, root)
    saved.save_info()
    app.runtime.session = saved
    app.runtime.title_notice = app.controller.apply_provider_title

    try:
        # Preference off ignores provider titles.
        save_preferences(session_naming="off")
        model._idle_update({"sessionUpdate": "session_info_update", "title": "Ignored when off"})
        assert saved.info.title is None

        # Malformed or blank titles are ignored.
        save_preferences(session_naming="on")
        for bad in (None, 123, "", "   ", '""'):
            model._idle_update({"sessionUpdate": "session_info_update", "title": bad})
        assert saved.info.title is None

        # Idle session_info_update sets and persists the cleaned title.
        model._idle_update({"sessionUpdate": "session_info_update", "title": "  Late idle title! "})
        assert saved.info.title == "Late idle title"
        assert list_sessions(root)[0].title == "Late idle title"

        # Existing title is not overwritten by a second update.
        model._idle_update({"sessionUpdate": "session_info_update", "title": "Second title"})
        assert saved.info.title == "Late idle title"

        # Existing /rename name blocks provider titles.
        saved.info.title = None
        app.controller.rename("User name")
        model._idle_update({"sessionUpdate": "session_info_update", "title": "After rename"})
        assert saved.info.title is None
        assert saved.info.name == "User name"

        # cancel_naming detaches title_notice so late updates don't hit a new session.
        saved.info.name = None
        app.controller.cancel_naming()
        model._idle_update({"sessionUpdate": "session_info_update", "title": "After cancel"})
        assert saved.info.title is None

        # retire() clears title_listener on the model.
        assert model.title_listener is not None
        model.retire()
        assert model.title_listener is None
    finally:
        saved.close()


def test_session_info_update_after_turn_end_to_end_and_reset(fake_agy, tmp_path):
    from io import StringIO
    from types import SimpleNamespace

    from rich.console import Console

    from pcode.app import PreviewApp
    from pcode.live import AgentRuntime
    from pcode.preferences import save_preferences
    from pcode.sessions import SavedSession, list_sessions
    from pcode.ui import TerminalOutput

    save_preferences(session_naming="on")
    root = tmp_path / "sessions"
    model = AntigravityModel("fake-flash")
    console = Console(file=StringIO())
    app = PreviewApp(workspace=tmp_path, session_dir=root, console=console)
    app.output = TerminalOutput(console, SimpleNamespace(invalidate=lambda: None))
    app.model = "antigravity:fake-flash"
    app.runtime = AgentRuntime(
        agent_for(model, tmp_path),
        session_factory=lambda: SavedSession.create("antigravity:fake-flash", tmp_path, root),
    )

    async def exercise():
        await app.controller.run_turn("LATER_TITLE check post-turn naming")
        # Give the reader a moment to deliver the post-reply session_info_update.
        for _ in range(100):
            if app.controller.session_title():
                break
            await asyncio.sleep(0.01)
        assert app.controller.session_title() == "Late idle title"
        assert list_sessions(root)[0].title == "Late idle title"

        # /new resets the runtime and detaches the old connection's idle handler
        # immediately, while keeping title_listener wired for the next session.
        connection = model._session.connection
        assert connection.idle is not None
        app.runtime.reset()
        assert connection.idle is None
        assert model.title_listener is not None

    try:
        run(exercise())
    finally:
        if app.runtime.session is not None:
            app.runtime.session.close()


def test_bare_rename_works_on_antigravity_and_retires_side_model(fake_agy, tmp_path):
    from io import StringIO
    from types import SimpleNamespace

    from rich.console import Console

    from pcode.app import PreviewApp
    from pcode.live import AgentRuntime
    from pcode.sessions import SavedSession, list_sessions
    from pcode.ui import TerminalOutput

    root = tmp_path / "sessions"
    model = AntigravityModel("fake-flash")
    console = Console(file=StringIO())
    app = PreviewApp(workspace=tmp_path, session_dir=root, console=console)
    app.output = TerminalOutput(console, SimpleNamespace(invalidate=lambda: None))
    app.model = "antigravity:fake-flash"
    app.runtime = AgentRuntime(
        agent_for(model, tmp_path),
        session_factory=lambda: SavedSession.create("antigravity:fake-flash", tmp_path, root),
    )

    async def exercise():
        await app.controller.run_turn("fix the parser")
        main_session_id = model._session.session_id
        app.controller.rename("")
        assert app.controller.rename_task is not None
        await app.controller.rename_task
        assert app.controller.session_title()
        assert list_sessions(root)[0].name == app.controller.session_title()
        # Main conversation's ACP session is untouched; rename ran in its own side session.
        assert model._session.session_id == main_session_id
        sessions = [m for m in fake_agy() if m.get("method") == "session/new"]
        assert len(sessions) == 2

    try:
        run(exercise())
    finally:
        if app.runtime.session is not None:
            app.runtime.session.close()


def test_browser_tools_screenshots_and_instructions_are_bridged_when_enabled(fake_agy, tmp_path):
    import base64

    from pydantic_ai.capabilities import Capability
    from pydantic_ai.messages import ToolReturn

    png = b"\x89PNG\r\n\x1a\nfake"
    started: list[str] = []

    def navigate(url: str) -> str:
        """Open a URL."""
        return f"opened {url}"

    def screenshot(full_page: bool = False) -> ToolReturn:
        """Capture a screenshot."""
        return ToolReturn(
            "Screenshot captured (CSS pixels). URL: https://example.com",
            content=[BinaryContent(data=png, media_type="image/png")],
        )

    def read_file(path: str) -> str:
        """Read a file."""
        return path

    class FakeBrowser(Capability):
        async def before_tool_execute(self, ctx, *, call, tool_def, args):
            if tool_def.name in {"navigate", "screenshot"}:
                started.append(tool_def.name)
            return args

    browser_cap = FakeBrowser(
        id="browser",
        tools=[navigate, screenshot],
        instructions="Use browser_open for sign-in pages.",
    )
    ext_browser = Capability(id="ext.browser", instructions="Delegate multi-step flows to browser.")
    file_cap = Capability(id="file_tools", tools=[read_file], instructions="Never bridged.")

    async def go():
        agent = Agent(
            AntigravityModel("fake-neon"),
            model_settings={CWD_SETTING: str(tmp_path)},
            capabilities=[browser_cap, ext_browser, file_cap],
        )
        return await streamed(agent, mcp_prompt("screenshot", {}))

    result = run(go())
    assert started == ["screenshot"]
    assert result.output == "tool said: Screenshot captured (CSS pixels). URL: https://example.com"
    [listed] = [e for e in fake_agy() if "mcp_tools" in e]
    by_name = {t["name"]: t for t in listed["mcp_tools"]}
    assert set(by_name) == {"navigate", "screenshot"}
    assert "browser_open" in by_name["navigate"]["description"]
    instructions = listed["mcp_initialize"]["instructions"]
    assert "Use browser_open for sign-in pages." in instructions
    assert "Delegate multi-step flows to browser." in instructions
    assert "Never bridged." not in instructions
    [called] = [e for e in fake_agy() if "mcp_result" in e]
    content = called["mcp_result"]["result"]["content"]
    assert content == [
        {"type": "text", "text": "Screenshot captured (CSS pixels). URL: https://example.com"},
        {"type": "image", "data": base64.b64encode(png).decode(), "mimeType": "image/png"},
    ]


def test_core_tools_bridged_extensions_and_opt_in_extension_are_exposed(fake_agy, tmp_path):
    from pydantic_ai.capabilities import Capability

    from pcode.ext import ExtensionAPI, ExtensionUI

    def task_worktree(action: str) -> dict:
        """Manage task worktrees."""
        return {"action": action}

    def advisor(question: str) -> str:
        """Consult advisor."""
        return f"advice: {question}"

    def read_tool_result(handle: str) -> str:
        """Read spilled output."""
        return f"slice:{handle}"

    def search_sessions(query: str) -> list:
        """Search sessions."""
        return [query]

    def list_hosts() -> list:
        """List hosts."""
        return ["h1"]

    def shell(command: str) -> str:
        """Run shell."""
        return command

    ext_opt_in = ExtensionAPI("custom_on", tmp_path, ExtensionUI())
    ext_opt_in.bridge_to_acp = True
    ext_opt_in.instructions("Use ticket_lookup for issue IDs.")

    @ext_opt_in.tool
    def ticket_lookup(ticket_id: str) -> str:
        """Look up a ticket."""
        return f"ticket:{ticket_id}"

    ext_opt_out = ExtensionAPI("custom_off", tmp_path, ExtensionUI())

    @ext_opt_out.tool
    def local_only() -> str:
        """Not bridged."""
        return "nope"

    advisor_guidance = Capability(id="advisor_guidance", instructions="Consult advisor wisely.")
    advisor_guidance.bridge_to_acp = True

    caps = [
        Capability(id="sub_agents", tools=[task_worktree]),
        Capability(id="advisor", tools=[advisor]),
        advisor_guidance,
        Capability(
            id="tool_output_limits",
            tools=[read_tool_result],
            instructions="Read spilled handles with read_tool_result.",
        ),
        Capability(
            id="ext.session_history",
            tools=[search_sessions],
            instructions="Search saved history.",
        ),
        Capability(id="ext.conductor", tools=[list_hosts], instructions="Inspect hosts."),
        Capability(id="shell_tools", tools=[shell]),
        *ext_opt_in.capabilities(),
        *ext_opt_out.capabilities(),
    ]

    async def go():
        agent = Agent(
            AntigravityModel("fake-neon"),
            model_settings={CWD_SETTING: str(tmp_path)},
            capabilities=caps,
        )
        ok = await streamed(agent, mcp_prompt("ticket_lookup", {"ticket_id": "ENG-7"}))
        denied = await streamed(agent, mcp_prompt("local_only", {}), ok.all_messages())
        return ok.output, denied.output

    ok_out, denied_out = run(go())
    assert ok_out == "tool said: ticket:ENG-7"
    assert "no tool named 'local_only'" in denied_out
    [listed] = [e for e in fake_agy() if "mcp_tools" in e][:1]
    names = {t["name"] for t in listed["mcp_tools"]}
    assert names == {
        "task_worktree",
        "advisor",
        "read_tool_result",
        "search_sessions",
        "list_hosts",
        "ticket_lookup",
    }
    instructions = listed["mcp_initialize"]["instructions"]
    assert "Consult advisor wisely." in instructions
    assert "Read spilled handles with read_tool_result." in instructions
    assert "Search saved history." in instructions
    assert "Inspect hosts." in instructions
    assert "Use ticket_lookup for issue IDs." in instructions


def test_worker_bridges_scoped_tools_and_excludes_parent_only_tools(
    fake_agy, tmp_path, subagent_models
):
    from pydantic_ai.capabilities import Capability

    from pcode.antigravity_tools import DELEGATION_PROMPT_NOTE, WORKER_DELEGATION_PROMPT_NOTE
    from pcode.ext import ExtensionAPI, ExtensionUI

    def delegate_task(task: str) -> str:
        """Delegate."""
        return task

    def manage_worker(action: str) -> str:
        """Manage workers."""
        return action

    def advisor(question: str) -> str:
        """Consult advisor."""
        return question

    def ask_parent(question: str) -> str:
        """Ask parent."""
        return f"asked:{question}"

    def read_tool_result(handle: str) -> str:
        """Read spilled output."""
        return f"slice:{handle}"

    def read_session(session_id: str) -> str:
        """Read saved session."""
        return f"session:{session_id}"

    def search_sessions(query: str) -> list:
        """Search saved sessions."""
        return [query]

    def list_hosts() -> list:
        """List hosts."""
        return ["h1"]

    def inspect_host(host_id: str) -> dict:
        """Inspect host."""
        return {"host_id": host_id}

    def ask_host(target: str, question: str) -> str:
        """Ask host."""
        return f"{target}:{question}"

    def send_to_host(target: str, prompt: str) -> str:
        """Send to host."""
        return f"{target}:{prompt}"

    ext_opt_in = ExtensionAPI("worker_ext", tmp_path, ExtensionUI())
    ext_opt_in.bridge_to_acp = True

    @ext_opt_in.tool
    def ticket_lookup(ticket_id: str) -> str:
        """Look up a ticket."""
        return f"ticket:{ticket_id}"

    advisor_guidance = Capability(id="advisor_guidance", instructions="Consult advisor wisely.")
    advisor_guidance.bridge_to_acp = True

    caps = [
        Capability(id="sub_agents", tools=[delegate_task, manage_worker]),
        Capability(id="advisor", tools=[advisor]),
        advisor_guidance,
        Capability(id="child_channel", tools=[ask_parent], instructions="Ask parent when stuck."),
        Capability(id="tool_output_limits", tools=[read_tool_result]),
        Capability(
            id="ext.session_history",
            tools=[search_sessions, read_session],
            instructions="Search saved history.",
        ),
        Capability(
            id="ext.conductor",
            tools=[list_hosts, inspect_host, ask_host, send_to_host],
            instructions="Use list_hosts and inspect_host.",
        ),
        *ext_opt_in.capabilities(),
    ]

    async def go():
        agent = Agent(
            AntigravityModel("fake-neon", worker=True),
            model_settings={CWD_SETTING: str(tmp_path)},
            capabilities=caps,
        )
        ok = await streamed(agent, mcp_prompt("read_session", {"session_id": "abc12345"}))
        asked = await streamed(
            agent, mcp_prompt("ask_parent", {"question": "which branch?"}), ok.all_messages()
        )
        denied = await streamed(
            agent, mcp_prompt("ask_host", {"target": "h1", "question": "hi"}), asked.all_messages()
        )
        return ok.output, asked.output, denied.output

    ok_out, asked_out, denied_out = run(go())
    assert ok_out == "tool said: session:abc12345"
    assert asked_out == "tool said: asked:which branch?"
    assert "no tool named 'ask_host'" in denied_out
    [listed] = [e for e in fake_agy() if "mcp_tools" in e][:1]
    names = {t["name"] for t in listed["mcp_tools"]}
    assert names == {
        "delegate_task",
        "manage_worker",
        "ask_parent",
        "read_tool_result",
        "search_sessions",
        "read_session",
        "list_hosts",
        "inspect_host",
        "ticket_lookup",
    }
    instructions = listed["mcp_initialize"]["instructions"]
    assert "Search saved history." in instructions
    assert "Ask parent when stuck." in instructions
    assert "Consult advisor wisely." not in instructions
    new_calls = fake_agy("session/new")
    assert len(new_calls) == 1
    meta = new_calls[0]["params"].get("_meta") or {}
    append = meta.get("antigravity", {}).get("systemPrompt", {}).get("append", "")
    assert WORKER_DELEGATION_PROMPT_NOTE in append
    assert DELEGATION_PROMPT_NOTE not in append


def test_side_btw_bridges_scoped_tools_and_respects_aside_guard(fake_agy, tmp_path):
    from pydantic_ai.capabilities import Capability

    from pcode.aside_guard import AsideGuard

    def delegate_task(task: str) -> str:
        """Delegate."""
        return task

    def manage_worker(action: str, worker_id: str = "") -> dict:
        """Manage workers."""
        return {"action": action, "worker_id": worker_id}

    def advisor(question: str) -> str:
        """Consult advisor."""
        return question

    def read_session(session_id: str) -> str:
        """Read saved session."""
        return f"session:{session_id}"

    def list_hosts() -> list:
        """List hosts."""
        return ["h1"]

    def ask_host(target: str, question: str) -> str:
        """Ask host."""
        return f"{target}:{question}"

    caps = [
        AsideGuard(),
        Capability(id="sub_agents", tools=[delegate_task, manage_worker]),
        Capability(id="advisor", tools=[advisor]),
        Capability(id="ext.session_history", tools=[read_session]),
        Capability(id="ext.conductor", tools=[list_hosts, ask_host]),
    ]

    async def go():
        agent = Agent(
            AntigravityModel("fake-neon", side=True),
            model_settings={CWD_SETTING: str(tmp_path)},
            capabilities=caps,
        )
        ok_read = await streamed(agent, mcp_prompt("read_session", {"session_id": "s99"}))
        ok_list = await streamed(
            agent, mcp_prompt("manage_worker", {"action": "list"}), ok_read.all_messages()
        )
        refused_stop = await streamed(
            agent,
            mcp_prompt("manage_worker", {"action": "stop", "worker_id": "w1"}),
            ok_list.all_messages(),
        )
        refused_host = await streamed(
            agent,
            mcp_prompt("ask_host", {"target": "h1", "question": "hi"}),
            refused_stop.all_messages(),
        )
        return ok_read.output, ok_list.output, refused_stop.output, refused_host.output

    ok_read, ok_list, refused_stop, refused_host = run(go())
    assert ok_read == "tool said: session:s99"
    assert '"action": "list"' in ok_list
    assert "unavailable in a side question" in refused_stop
    assert "no tool named 'ask_host'" in refused_host
    [listed] = [e for e in fake_agy() if "mcp_tools" in e][:1]
    names = {t["name"] for t in listed["mcp_tools"]}
    assert names == {"manage_worker", "read_session", "list_hosts"}


def test_plain_side_completion_bridges_no_mcp_tools(fake_agy, tmp_path):
    async def go():
        model = AntigravityModel("fake-neon", side=True)
        await Agent(
            model,
            instructions="Write a title.",
            model_settings={CWD_SETTING: str(tmp_path)},
        ).run("hello")

    run(go())
    new_calls = fake_agy("session/new")
    assert len(new_calls) == 1
    assert new_calls[0]["params"]["mcpServers"] == []


def test_delegate_task_lists_browser_subagent_without_notes(fake_agy, tmp_path, subagent_models):
    from types import SimpleNamespace

    from pydantic_ai.capabilities import Capability

    subagent_models("claude:fable")
    shown: list[str] = []
    ran: list[dict] = []

    def delegate_task(agent_name: str, task: str, model: str | None = None) -> str:
        """Delegate."""
        ran.append({"agent_name": agent_name, "task": task, "model": model})
        return "done"

    sub_cap = Capability(id="sub_agents", tools=[delegate_task])
    sub_cap.agents = [
        SimpleNamespace(resolved_name="worker"),
        SimpleNamespace(resolved_name="browser"),
    ]

    async def go():
        model = AntigravityModel("fake-neon")
        model.listener = shown.append
        agent = Agent(
            model,
            model_settings={CWD_SETTING: str(tmp_path)},
            capabilities=[sub_cap],
        )
        return await streamed(
            agent, mcp_prompt("delegate_task", {"agent_name": "browser", "task": "check login"})
        )

    result = run(go())
    assert result.output == "tool said: done"
    assert ran == [{"agent_name": "browser", "task": "check login", "model": None}]
    assert shown == []
    [listed] = [e for e in fake_agy() if "mcp_tools" in e]
    agent_desc = listed["mcp_tools"][0]["inputSchema"]["properties"]["agent_name"]["description"]
    assert "worker, browser" in agent_desc


def test_dynamic_tool_change_resumes_session_while_compact_reuses(fake_agy, tmp_path):
    from pydantic_ai.capabilities import Capability, DynamicCapability

    advisor_on = [False]

    def advisor(question: str) -> str:
        """Ask advisor."""
        return f"advisor:{question}"

    def resolve(ctx):
        if not advisor_on[0]:
            return None
        cap = Capability(id="advisor", tools=[advisor], instructions="Advisor is on.")
        cap.bridge_to_acp = True
        return cap

    async def go():
        model = AntigravityModel("fake-neon")
        agent = Agent(
            model,
            model_settings={CWD_SETTING: str(tmp_path)},
            capabilities=[DynamicCapability(resolve)],
        )
        first = await streamed(agent, "turn one")
        second = await streamed(agent, "turn two", first.all_messages())
        # /compact runs without a turn RunContext: must reuse the live session.
        await model.compact(second.all_messages(), str(tmp_path))
        assert len(fake_agy("session/resume")) == 0
        # Turning advisor on mid-session changes the bridged tool signature, so
        # the next turn resumes the same ACP session to reload MCP tools.
        advisor_on[0] = True
        third = await streamed(
            agent,
            mcp_prompt("advisor", {"question": "plan?"}),
            second.all_messages(),
        )
        return third.output

    out = run(go())
    assert out == "tool said: advisor:plan?"
    assert len(fake_agy("session/new")) == 1
    resumes = fake_agy("session/resume")
    assert len(resumes) == 1
    assert resumes[0]["params"]["sessionId"] == "s-1"
    [listed] = [e for e in fake_agy() if "mcp_tools" in e]
    assert [t["name"] for t in listed["mcp_tools"]] == ["advisor"]
    assert "Advisor is on." in listed["mcp_initialize"]["instructions"]


def test_dynamic_toggle_off_and_non_core_dynamic_combined_and_prefixed_capabilities(
    fake_agy, tmp_path
):
    from dataclasses import dataclass, replace

    from pydantic_ai.capabilities import (
        AbstractCapability,
        Capability,
        CombinedCapability,
        DynamicCapability,
        PrefixTools,
    )
    from pydantic_ai.messages import InstructionPart

    from pcode.ext import ExtensionAPI, ExtensionUI

    enabled = [True]

    def dyn_ping(label: str) -> str:
        """Dynamic non-core tool."""
        return f"pong:{label}"

    def comb_tool() -> str:
        """Combined tool."""
        return "comb"

    def prefixed_tool() -> str:
        """Prefixed tool."""
        return "pref"

    @dataclass
    class ReplacingCap(AbstractCapability):
        step: int = 0

        async def for_run(self, ctx):
            return replace(self, step=self.step + 1)

        def get_instructions(self):
            return [
                InstructionPart(content="InstructionPart guidance."),
                lambda: "Zero-arg callable guidance.",
            ]

    ext = ExtensionAPI("rich", tmp_path, ExtensionUI())
    ext.bridge_to_acp = True
    ext.add_capability(
        DynamicCapability(
            lambda ctx: (
                Capability(id="dyn", tools=[dyn_ping], instructions="Dynamic guidance.")
                if enabled[0]
                else None
            ),
            id="rich.dyn",
        )
    )
    ext.add_capability(
        CombinedCapability([Capability(id="comb", tools=[comb_tool])], id="rich.comb")
    )
    ext.add_capability(
        PrefixTools(Capability(id="pref", tools=[prefixed_tool]), prefix="extp", id="rich.pref")
    )
    ext.add_capability(ReplacingCap(id="rich.rep"))

    async def go():
        agent = Agent(
            AntigravityModel("fake-neon"),
            model_settings={CWD_SETTING: str(tmp_path)},
            capabilities=ext.capabilities(),
        )
        first = await streamed(agent, mcp_prompt("dyn_ping", {"label": "ok"}))
        # Toggle the dynamic capability off: next turn must resume and drop `dyn_ping`.
        enabled[0] = False
        second = await streamed(agent, mcp_prompt("extp_prefixed_tool", {}), first.all_messages())
        return first.output, second.output

    first_out, second_out = run(go())
    assert first_out == "tool said: pong:ok"
    assert second_out == "tool said: pref"
    assert len(fake_agy("session/new")) == 1
    assert len(fake_agy("session/resume")) == 1
    listed = [e for e in fake_agy() if "mcp_tools" in e]
    assert {t["name"] for t in listed[0]["mcp_tools"]} == {
        "dyn_ping",
        "comb_tool",
        "extp_prefixed_tool",
    }
    assert {t["name"] for t in listed[1]["mcp_tools"]} == {"comb_tool", "extp_prefixed_tool"}
    inst0 = listed[0]["mcp_initialize"]["instructions"]
    assert "Dynamic guidance." in inst0
    assert "InstructionPart guidance." in inst0
    assert "Zero-arg callable guidance." in inst0
    inst1 = listed[1]["mcp_initialize"]["instructions"]
    assert "Dynamic guidance." not in inst1
    assert "InstructionPart guidance." in inst1


def test_cold_compact_then_streamed_turn_resumes_to_load_mcp_tools(fake_agy, tmp_path):
    async def go():
        model = AntigravityModel("fake-neon")
        agent = delegating_agent(model, tmp_path, [])
        # Turn 1 unstreamed (no RunContext -> tools_signature=None), then cold /compact,
        # then a streamed turn (with RunContext and tools -> must resume to load MCP tools).
        first = await agent.run("hello")
        await model.compact(first.all_messages(), str(tmp_path))
        assert len(fake_agy("session/resume")) == 0
        second = await streamed(
            agent,
            mcp_prompt("delegate_task", {"task": "check", "model": "claude:fable"}),
            first.all_messages(),
        )
        return second.output

    out = run(go())
    assert out == "tool said: report on check from claude:fable"
    assert len(fake_agy("session/resume")) == 1


def test_content_conversion_handles_image_only_and_non_image_binary():
    import base64

    from pydantic_ai.messages import ToolReturn

    png = b"\x89PNG\r\n"
    image_only = antigravity_tools._content(
        ToolReturn(return_value=None, content=[BinaryContent(data=png, media_type="image/png")])
    )
    assert image_only == [
        {"type": "image", "data": base64.b64encode(png).decode(), "mimeType": "image/png"}
    ]
    pdf = antigravity_tools._content(BinaryContent(data=b"%PDF-1.7", media_type="application/pdf"))
    assert pdf == [{"type": "text", "text": "[binary content: application/pdf, 8 bytes]"}]


def test_antigravity_workers_run_in_isolated_sessions_and_allow_extension_subagents(
    fake_agy, tmp_path
):
    from pydantic_ai_harness.sub_agents import SubAgent

    from pcode.background_delegation import BackgroundDelegation
    from pcode.delegation import DelegationReporting, stream_child_activity
    from pcode.ext import ExtensionAPI
    from pcode.isolated_delegation import WorkspaceSubAgents
    from pcode.live import AgentRuntime
    from pcode.runtime import ToolStarted, ToolSummary, WorkerStatus
    from pcode.tool_panel import ToolHistory
    from pcode.workers import Workers

    api = ExtensionAPI("test", tmp_path, None)
    api.subagent(Agent(AntigravityModel("fake-neon"), name="ext_worker"))
    assert [sub.resolved_name for sub in api.subagents] == ["ext_worker"]

    async def go():
        parent_model = AntigravityModel("fake-neon")
        worker = Agent(None, name="worker", model_settings={CWD_SETTING: str(tmp_path)})
        sub_agents = WorkspaceSubAgents(
            workspace=tmp_path,
            worker_factory=None,
            agents=[SubAgent(worker)],
            agent_folders=None,
            event_stream_handler=stream_child_activity,
        )
        parent = Agent(
            parent_model,
            model_settings={CWD_SETTING: str(tmp_path)},
            capabilities=[sub_agents, DelegationReporting(), BackgroundDelegation()],
        )
        runtime = AgentRuntime(parent)
        panel = ToolHistory()
        workers = Workers()
        live_panel_seen: list[int] = []
        live_workers_seen: list[int] = []
        events = []
        prompt = mcp_prompt("delegate_task", {"task": "check repo", "purpose": "checking repo"})
        async for event in runtime.stream(prompt):
            events.append(event)
            if isinstance(event, ToolStarted | ToolSummary):
                panel.record(event)
                workers.record(event)
            elif isinstance(event, WorkerStatus):
                workers.record(event)
            if panel.delegates:
                live_panel_seen.append(len(panel.delegates))
            if any(w.running for w in workers.items):
                live_workers_seen.append(workers.running())
        return events, panel, workers, live_panel_seen, live_workers_seen

    events, panel, workers, live_panel_seen, live_workers_seen = run(go())
    # Parent session (s-1) and isolated worker session (s-2): never deadlocked on parent's lock.
    new_sessions = fake_agy("session/new")
    assert len(new_sessions) == 2
    assert [s["name"] for s in new_sessions[0]["params"]["mcpServers"]] == ["pcode"]
    assert new_sessions[1]["params"]["mcpServers"] == []
    # Worker appeared live in BOTH the panel and /agents during the turn, then settled.
    assert live_panel_seen and live_workers_seen
    assert panel.delegates == []
    assert len(workers.items) == 1
    assert workers.items[0].worker_id == "w1"
    assert not workers.items[0].running
    assert not workers.items[0].failed
    # Native mcp_pcode_delegate_task rows are suppressed (no duplicate tool summaries).
    started_names = [e.name for e in events if isinstance(e, ToolStarted)]
    summary_names = [e.name for e in events if isinstance(e, ToolSummary)]
    assert "delegate_task" in started_names and "mcp_pcode_delegate_task" not in started_names
    assert "delegate_task" in summary_names and "mcp_pcode_delegate_task" not in summary_names
    # Antigravity worker's own native tool call surfaced through stream_child_activity.
    assert any(entry.kind == "tool" for entry in workers.items[0].entries)


def test_antigravity_foreground_worker_auto_detaches_before_mcp_timeout(
    fake_agy, tmp_path, monkeypatch
):
    from pydantic_ai.models.function import FunctionModel
    from pydantic_ai_harness.sub_agents import ModelOption, SubAgent

    from pcode import background_delegation
    from pcode.background_delegation import BackgroundDelegation
    from pcode.delegation import DelegationReporting, stream_child_activity
    from pcode.isolated_delegation import WorkspaceSubAgents
    from pcode.live import AgentRuntime
    from pcode.runtime import RunStatus, ToolStarted, ToolSummary, WorkerStatus
    from pcode.tool_panel import ToolHistory
    from pcode.workers import Workers

    monkeypatch.setattr(background_delegation, "MCP_DETACH_SECONDS", 0.05)
    monkeypatch.setattr(background_delegation, "POLL_SECONDS", 0.01)

    release = asyncio.Event()

    async def slow_worker(messages, info):
        await release.wait()
        yield "slow worker finished"

    async def go():
        parent_model = AntigravityModel("fake-neon")
        worker = Agent(FunctionModel(stream_function=slow_worker), name="worker")
        sub_agents = WorkspaceSubAgents(
            workspace=tmp_path,
            worker_factory=None,
            agents=[SubAgent(worker)],
            models={"slow:m": ModelOption(FunctionModel(stream_function=slow_worker))},
            agent_folders=None,
            event_stream_handler=stream_child_activity,
        )
        parent = Agent(
            parent_model,
            model_settings={CWD_SETTING: str(tmp_path)},
            capabilities=[sub_agents, DelegationReporting(), BackgroundDelegation()],
        )
        runtime = AgentRuntime(parent)
        panel = ToolHistory()
        workers = Workers()
        saw_still_live_after_detach = False
        prompt = mcp_prompt("delegate_task", {"task": "long job", "model": "slow:m"})

        async for event in runtime.stream(prompt):
            if isinstance(event, ToolStarted | ToolSummary):
                panel.record(event)
                workers.record(event)
            elif isinstance(event, WorkerStatus):
                workers.record(event)
            elif (
                isinstance(event, RunStatus) and "sub-agent" in event.text and not release.is_set()
            ):
                # Reached DelegatesPending AFTER auto-detach; worker must still be live.
                saw_still_live_after_detach = bool(panel.delegates) and workers.running() == 1
                release.set()
        return panel, workers, saw_still_live_after_detach

    panel, workers, saw_still_live = run(go())
    assert saw_still_live
    assert panel.delegates == []
    assert len(workers.items) == 1 and not workers.items[0].running
    # Antigravity received the detach receipt first, then the delivered result as the next prompt.
    sent = prompts(fake_agy)
    assert len(sent) == 2
    assert "slow worker finished" in "\n".join(b.get("text", "") for b in sent[1])
    [mcp_res] = [e["mcp_result"] for e in fake_agy() if "mcp_result" in e]
    assert "client tool timeout" in mcp_res["result"]["content"][0]["text"]


def test_antigravity_worker_events_stream_live_while_turn_is_running(fake_agy, tmp_path):
    from pydantic_ai.models.function import FunctionModel
    from pydantic_ai_harness.sub_agents import ModelOption, SubAgent

    from pcode.background_delegation import BackgroundDelegation
    from pcode.delegation import DelegationReporting, stream_child_activity
    from pcode.isolated_delegation import WorkspaceSubAgents
    from pcode.live import AgentRuntime
    from pcode.runtime import ChildText, ToolStarted, ToolSummary, WorkerStatus
    from pcode.tool_panel import ToolHistory
    from pcode.workers import Workers

    release = asyncio.Event()
    saw_live_while_blocked = False

    async def gated_worker(messages, info):
        yield "working on it..."
        await release.wait()
        yield " all done"

    async def go():
        nonlocal saw_live_while_blocked
        parent_model = AntigravityModel("fake-neon")
        worker = Agent(FunctionModel(stream_function=gated_worker), name="worker")
        sub_agents = WorkspaceSubAgents(
            workspace=tmp_path,
            worker_factory=None,
            agents=[SubAgent(worker)],
            models={"fast:m": ModelOption(FunctionModel(stream_function=gated_worker))},
            agent_folders=None,
            event_stream_handler=stream_child_activity,
        )
        parent = Agent(
            parent_model,
            model_settings={CWD_SETTING: str(tmp_path)},
            capabilities=[sub_agents, DelegationReporting(), BackgroundDelegation()],
        )
        runtime = AgentRuntime(parent)
        panel = ToolHistory()
        workers = Workers()
        prompt = mcp_prompt(
            "delegate_task", {"task": "inspect", "purpose": "inspecting", "model": "fast:m"}
        )
        async for event in runtime.stream(prompt):
            if isinstance(event, ToolStarted | ToolSummary):
                panel.record(event)
                workers.record(event)
            elif isinstance(event, WorkerStatus | ChildText):
                workers.record(event)
            # Only release the worker AFTER its start and prose reached the live UI!
            if (
                not release.is_set()
                and panel.delegates
                and workers.items
                and any(e.kind == "text" for e in workers.items[0].entries)
            ):
                saw_live_while_blocked = True
                release.set()
        return panel, workers

    panel, workers = run(go())
    assert saw_live_while_blocked
    assert panel.delegates == []
    assert len(workers.items) == 1 and not workers.items[0].running


def test_non_tool_data_capability_parts_are_not_sent_to_antigravity():
    from pydantic_ai.messages import CapabilitySource
    from pydantic_ai_harness.planning import PlanReminderPart

    request = ModelRequest(
        parts=[
            UserPromptPart("hi"),
            PlanReminderPart(
                content="internal plan",
                source=CapabilitySource("planning", authority="application"),
            ),
        ]
    )
    blocks, _ = antigravity.user_prompt([request])
    assert blocks == [{"type": "text", "text": "hi"}]


def test_asked_matches_rendered_non_user_and_continue_segments():
    from pydantic_ai_harness._background_completion import make_background_result

    from pcode.live import CANCELLED_MARKER

    parts = [
        UserPromptPart(CANCELLED_MARKER),
        make_background_result("delegate_task", "c1", "worker done"),
        UserPromptPart(CANCELLED_MARKER),
    ]
    assert antigravity._asked(parts) == [
        antigravity.CONTINUE,
        "[pcode] Result of the delegate_task call that was running:\nworker done",
    ]


def test_delegation_arguments_replace_empty_or_none_agent_name():
    assert (
        antigravity_tools._delegation_arguments({"agent_name": None, "task": "x"})["agent_name"]
        == "worker"
    )
    assert (
        antigravity_tools._delegation_arguments({"agent_name": "   ", "task": "x"})["agent_name"]
        == "worker"
    )
    assert "model" not in antigravity_tools._delegation_arguments({"model": "  ", "task": "x"})


def test_non_antigravity_parent_can_delegate_to_antigravity_worker(fake_agy, tmp_path):
    from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
    from pydantic_ai.models.function import FunctionModel
    from pydantic_ai_harness.sub_agents import ModelOption, SubAgent

    from pcode.delegation import DelegationReporting, stream_child_activity
    from pcode.isolated_delegation import WorkspaceSubAgents

    calls = 0

    def parent_fn(messages, info):
        nonlocal calls
        calls += 1
        if calls == 1:
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        "delegate_task",
                        {
                            "agent_name": "worker",
                            "task": "inspect files",
                            "model": "antigravity:fake-neon",
                        },
                    )
                ]
            )
        return ModelResponse(parts=[TextPart("parent done")])

    async def go():
        worker = Agent(None, name="worker", model_settings={CWD_SETTING: str(tmp_path)})
        sub_agents = WorkspaceSubAgents(
            workspace=tmp_path,
            worker_factory=None,
            agents=[SubAgent(worker)],
            models={"antigravity:fake-neon": ModelOption(AntigravityModel("fake-neon"))},
            agent_folders=None,
            event_stream_handler=stream_child_activity,
        )
        parent = Agent(
            FunctionModel(parent_fn),
            model_settings={CWD_SETTING: str(tmp_path)},
            capabilities=[sub_agents, DelegationReporting()],
        )
        return await parent.run("delegate to agy")

    result = run(go())
    assert result.output == "parent done"
    new_sessions = fake_agy("session/new")
    assert len(new_sessions) == 1
    assert new_sessions[0]["params"]["mcpServers"] == []


def test_continue_worker_reuses_antigravity_session_and_retires_on_switch(fake_agy, tmp_path):
    from pydantic_ai.models.function import FunctionModel
    from pydantic_ai_harness.sub_agents import ModelOption, SubAgent

    from pcode.background_delegation import BackgroundDelegation
    from pcode.delegation import DelegationReporting, stream_child_activity
    from pcode.isolated_delegation import WorkspaceSubAgents
    from pcode.live import AgentRuntime
    from pcode.worker_channel import WorkerHub

    retired: list[AntigravityModel] = []
    orig_retire = AntigravityModel.retire

    def track_retire(self):
        retired.append(self)
        orig_retire(self)

    async def other_model(messages, info):
        yield "switched reply"

    AntigravityModel.retire = track_retire  # type: ignore[method-assign]
    try:

        async def go():
            hub = WorkerHub()
            parent_model = AntigravityModel("fake-neon")
            worker = Agent(None, name="worker", model_settings={CWD_SETTING: str(tmp_path)})
            sub_agents = WorkspaceSubAgents(
                workspace=tmp_path,
                worker_factory=None,
                agents=[SubAgent(worker)],
                models={"other:m": ModelOption(FunctionModel(stream_function=other_model))},
                agent_folders=None,
                event_stream_handler=stream_child_activity,
                hub=hub,
            )
            parent = Agent(
                parent_model,
                model_settings={CWD_SETTING: str(tmp_path)},
                capabilities=[sub_agents, DelegationReporting(), BackgroundDelegation()],
            )
            runtime = AgentRuntime(parent)
            # Turn 1: initial antigravity worker -> opens worker session s-2.
            async for _ in runtime.stream(mcp_prompt("delegate_task", {"task": "step one"})):
                pass
            handle = hub.get("w1")
            assert handle is not None and isinstance(handle.worker_model, AntigravityModel)
            first_model = handle.worker_model
            # Turn 2: continue_worker on same antigravity model -> reuses s-2 (no new session).
            async for _ in runtime.stream(
                mcp_prompt("delegate_task", {"task": "step two", "continue_worker": "w1"})
            ):
                pass
            assert handle.worker_model is first_model
            assert len(fake_agy("session/new")) == 2
            # Turn 3: continue_worker switching to a non-Antigravity model -> retires first_model.
            async for _ in runtime.stream(
                mcp_prompt(
                    "delegate_task",
                    {"task": "step three", "continue_worker": "w1", "model": "other:m"},
                )
            ):
                pass
            assert handle.worker_model is None
            assert first_model in retired

        run(go())
    finally:
        AntigravityModel.retire = orig_retire  # type: ignore[method-assign]


def test_bridged_delegate_task_cancel_settles_as_interrupted(fake_agy, tmp_path):
    from pydantic_ai.models.function import FunctionModel
    from pydantic_ai_harness.sub_agents import ModelOption, SubAgent

    from pcode.background_delegation import BackgroundDelegation
    from pcode.delegation import DelegationReporting, stream_child_activity
    from pcode.isolated_delegation import WorkspaceSubAgents
    from pcode.live import AgentRuntime
    from pcode.runtime import ToolStarted, ToolSummary, WorkerStatus
    from pcode.tool_panel import ToolHistory
    from pcode.workers import Workers

    async def blocking_worker(messages, info):
        await asyncio.Future()
        yield "never"

    async def go():
        parent_model = AntigravityModel("fake-neon")
        worker = Agent(FunctionModel(stream_function=blocking_worker), name="worker")
        sub_agents = WorkspaceSubAgents(
            workspace=tmp_path,
            worker_factory=None,
            agents=[SubAgent(worker)],
            models={"hang:m": ModelOption(FunctionModel(stream_function=blocking_worker))},
            agent_folders=None,
            event_stream_handler=stream_child_activity,
        )
        parent = Agent(
            parent_model,
            model_settings={CWD_SETTING: str(tmp_path)},
            capabilities=[sub_agents, DelegationReporting(), BackgroundDelegation()],
        )
        runtime = AgentRuntime(parent)
        panel = ToolHistory()
        workers = Workers()
        summaries: list[ToolSummary] = []
        prompt = "MCP " + json.dumps(
            {
                "name": "delegate_task",
                "arguments": {"task": "wait forever", "model": "hang:m"},
                "cancel_after": 0.05,
            }
        )
        async for event in runtime.stream(prompt):
            if isinstance(event, ToolStarted):
                panel.record(event)
                workers.record(event)
            elif isinstance(event, ToolSummary):
                panel.record(event)
                workers.record(event)
                summaries.append(event)
            elif isinstance(event, WorkerStatus):
                workers.record(event)
        return panel, workers, summaries

    panel, workers, summaries = run(go())
    assert panel.delegates == []
    assert len(workers.items) == 1 and not workers.items[0].running
    delegation_summaries = [s for s in summaries if s.name == "delegate_task"]
    assert len(delegation_summaries) == 1
    assert delegation_summaries[0].outcome == "interrupted"
    assert delegation_summaries[0].failed


def test_post_settle_continuation_streams_bridged_delegate_task(fake_agy, tmp_path, monkeypatch):
    from pydantic_ai_harness.sub_agents import SubAgent

    from pcode.background_delegation import BackgroundDelegation
    from pcode.delegation import DelegationReporting, stream_child_activity
    from pcode.isolated_delegation import WorkspaceSubAgents

    monkeypatch.setattr(antigravity, "DEFERRED_FINALIZE_DEBOUNCE", 0.04)
    model = AntigravityModel("fake-flash")
    worker = Agent(None, name="worker", model_settings={CWD_SETTING: str(tmp_path)})
    sub_agents = WorkspaceSubAgents(
        workspace=tmp_path,
        worker_factory=None,
        agents=[SubAgent(worker)],
        agent_folders=None,
        event_stream_handler=stream_child_activity,
    )
    app, _, buffer = _controller_app(
        model,
        tmp_path,
        extra_capabilities=[sub_agents, DelegationReporting(), BackgroundDelegation()],
    )

    async def exercise():
        consumer = asyncio.create_task(app.controller.consume())
        try:
            payload = json.dumps(
                {"name": "delegate_task", "arguments": {"task": "check late", "purpose": "late"}}
            )
            app.controller.submit(f"LATER_MCP {payload}", "queue")
            for _ in range(250):
                if (
                    "late mcp:" in buffer.getvalue()
                    and not app.activity.busy
                    and not app.activity.queued_prompts
                ):
                    break
                await asyncio.sleep(0.02)
            assert "late mcp:" in buffer.getvalue()
            assert "got: check late" in buffer.getvalue()
            assert len(app.activity.workers.items) == 1
            assert not app.activity.workers.items[0].running
        finally:
            app.controller.running = False
            consumer.cancel()
            await asyncio.gather(consumer, return_exceptions=True)

    run(exercise())


def _controller_app(model: AntigravityModel, tmp_path, *, extra_capabilities=()):
    from io import StringIO
    from types import SimpleNamespace

    from rich.console import Console

    from pcode.app import PreviewApp
    from pcode.live import AgentRuntime
    from pcode.sessions import SavedSession
    from pcode.ui import TerminalOutput

    class _DirectOutput(TerminalOutput):
        def print(self, *objects, end="\n", transient=False) -> None:
            self.console.print(*objects, end=end)

        async def flush(self, *, drain: bool = False) -> None:
            pass

    root = tmp_path / "sessions"
    buffer = StringIO()
    console = Console(file=buffer, force_terminal=False, width=100)
    app = PreviewApp(
        model="antigravity:fake-flash", workspace=tmp_path, session_dir=root, console=console
    )
    app.output = _DirectOutput(console, SimpleNamespace(invalidate=lambda: None))
    app.model = "antigravity:fake-flash"
    app.runtime = AgentRuntime(
        Agent(model, capabilities=list(extra_capabilities)),
        session_factory=lambda: SavedSession.create("antigravity:fake-flash", tmp_path, root),
    )
    app.controller.ready.set()
    return app, root, buffer


def test_active_non_execute_tool_pauses_quiet_timer_while_holding(fake_agy, tmp_path, monkeypatch):
    monkeypatch.setattr(antigravity, "DEFERRED_FINALIZE_DEBOUNCE", 0.04)
    monkeypatch.setattr(antigravity, "OPEN_REPLY_QUIET_LIMIT", 0.05)

    async def go():
        model = AntigravityModel("fake-neon")
        return await agent_for(model, tmp_path).run("BGDELEGATE")

    result = run(go())
    assert "Delegation finished cleanly." in result.output
    parts = [p for m in result.all_messages() for p in m.parts]
    assert any(
        isinstance(p, NativeToolReturnPart) and p.tool_call_id == "mcp-1" and p.content == "LGTM"
        for p in parts
    )


def test_invoke_subagent_holds_turn_open_until_wakeup_settles(fake_agy, tmp_path, monkeypatch):
    monkeypatch.setattr(antigravity, "DEFERRED_FINALIZE_DEBOUNCE", 0.04)
    model = AntigravityModel("fake-flash")
    app, _root, buffer = _controller_app(model, tmp_path)
    dropped_idle_mid_wait = False

    async def exercise():
        nonlocal dropped_idle_mid_wait
        consumer = asyncio.create_task(app.controller.consume())
        try:
            app.controller.submit("SUBAGENT_HOLD review diff", "queue")
            saw_waiting = False
            for _ in range(250):
                text = buffer.getvalue()
                if "Waiting for the code review subagent." in text:
                    saw_waiting = True
                    if "Review addressed and merged." not in text and not app.activity.busy:
                        dropped_idle_mid_wait = True
                if (
                    "Review addressed and merged." in text
                    and not app.activity.busy
                    and not app.activity.queued_prompts
                ):
                    break
                await asyncio.sleep(0.01)
            assert saw_waiting
            assert not dropped_idle_mid_wait
            assert "Review addressed and merged." in buffer.getvalue()
            # Completed in a single turn with no extra prompt or continuation turn.
            assert len(prompts(fake_agy)) == 1
            assert model._session is not None and model._session.turns == 1
        finally:
            app.controller.running = False
            consumer.cancel()
            await asyncio.gather(consumer, return_exceptions=True)

    run(exercise())


def test_invoke_subagent_followed_by_another_tool_does_not_hold(fake_agy, tmp_path, monkeypatch):
    monkeypatch.setattr(antigravity, "DEFERRED_FINALIZE_DEBOUNCE", 0.04)

    async def go():
        model = AntigravityModel("fake-neon")
        return await asyncio.wait_for(
            agent_for(model, tmp_path).run("SUBAGENT_THEN_TOOL"), timeout=2.0
        )

    result = run(go())
    assert "All done." in result.output


def test_failed_invoke_subagent_does_not_hold(fake_agy, tmp_path, monkeypatch):
    monkeypatch.setattr(antigravity, "DEFERRED_FINALIZE_DEBOUNCE", 0.04)

    async def go():
        model = AntigravityModel("fake-neon")
        return await asyncio.wait_for(agent_for(model, tmp_path).run("SUBAGENT_FAIL"), timeout=2.0)

    result = run(go())
    assert "Could not launch subagent." in result.output


def test_chained_invoke_subagent_during_held_wakeup_keeps_holding(fake_agy, tmp_path, monkeypatch):
    monkeypatch.setattr(antigravity, "DEFERRED_FINALIZE_DEBOUNCE", 0.04)

    async def go():
        model = AntigravityModel("fake-neon")
        return await asyncio.wait_for(agent_for(model, tmp_path).run("SUBAGENT_CHAIN"), timeout=3.0)

    result = run(go())
    texts = [
        p.content
        for m in result.all_messages()
        for p in m.parts
        if isinstance(p, TextPart) and p.content
    ]
    joined = "".join(texts)
    assert "Waiting for the code review subagent." in joined
    assert "Waiting for second reviewer." in result.output
    assert "Second review clean." in result.output


def test_cross_turn_daemon_update_does_not_clear_subagent_hold(fake_agy, tmp_path, monkeypatch):
    monkeypatch.setattr(antigravity, "DEFERRED_FINALIZE_DEBOUNCE", 0.04)

    async def go():
        model = AntigravityModel("fake-neon")
        agent = agent_for(model, tmp_path)
        first = await asyncio.wait_for(agent.run("BGDAEMON"), timeout=2.0)
        second = await asyncio.wait_for(
            agent.run("SUBAGENT_HOLD DAEMON_PING", message_history=first.all_messages()),
            timeout=3.0,
        )
        assert model._session is not None and model._session.turns == 2
        return second

    result = run(go())
    assert "Review addressed and merged." in result.output


def test_cancelling_held_subagent_turn_sends_cancel_and_quarantines(
    fake_agy, tmp_path, monkeypatch
):
    monkeypatch.setattr(antigravity, "DEFERRED_FINALIZE_DEBOUNCE", 0.04)

    async def go():
        model = AntigravityModel("fake-neon")
        agent = agent_for(model, tmp_path)
        task = asyncio.create_task(agent.run("SUBAGENT_HANG"))
        # Under load, startup can outlast a fixed sleep: wait for the prompt to go out.
        for _ in range(500):
            if prompts(fake_agy):
                break
            await asyncio.sleep(0.01)
        await asyncio.sleep(0.15)  # Settled and holding for the subagent.
        assert not task.done()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert model._wake_quarantined

    run(go())
    assert any(m.get("method") == "session/cancel" for m in fake_agy())


def test_post_settle_wake_opens_continuation_turn_end_to_end(fake_agy, tmp_path, monkeypatch):
    from pcode.sessions import first_prompt, list_sessions, session_turns

    monkeypatch.setattr(antigravity, "DEFERRED_FINALIZE_DEBOUNCE", 0.04)
    model = AntigravityModel("fake-flash")
    app, root, buffer = _controller_app(model, tmp_path)

    async def exercise():
        consumer = asyncio.create_task(app.controller.consume())
        try:
            app.controller.submit("LATER_WAKE check post-settle continuation", "queue")
            for _ in range(250):
                if (
                    "Committed after background check." in buffer.getvalue()
                    and not app.activity.busy
                    and not app.activity.queued_prompts
                ):
                    break
                await asyncio.sleep(0.02)
            assert "Committed after background check." in buffer.getvalue()
            assert "Antigravity, after the turn" not in buffer.getvalue()
            # No extra session/prompt was sent to agy for the continuation.
            assert len(prompts(fake_agy)) == 1
            # A follow-up user turn still reuses the warm live session (no replay).
            app.controller.submit("follow up after wake", "queue")
            for _ in range(250):
                if (
                    "got: follow up after wake" in buffer.getvalue()
                    and not app.activity.busy
                    and not app.activity.queued_prompts
                ):
                    break
                await asyncio.sleep(0.02)
            assert "got: follow up after wake" in buffer.getvalue()
            assert len(prompts(fake_agy)) == 2
            assert sum(m.get("method") == "session/new" for m in fake_agy()) == 1
        finally:
            app.controller.running = False
            consumer.cancel()
            await asyncio.gather(consumer, return_exceptions=True)

    run(exercise())
    saved = list_sessions(root)[0]
    assert first_prompt(saved, root) == "LATER_WAKE check post-settle continuation"
    turns = session_turns(saved, root)
    assert turns is not None
    # The continuation folded onto turn 1 rather than adding a blank user turn.
    assert len(turns) == 2
    assert turns[0].prompt == "LATER_WAKE check post-settle continuation"
    assert any(
        "Committed after background check." in block
        for block in turns[0].blocks
        if isinstance(block, str)
    )
    assert any(
        getattr(block, "name", None) == "run_command"
        and "git commit" in getattr(block, "detail", "")
        for block in turns[0].blocks
    )
    # Tree view omits an empty user: row for the continuation node.
    tree_rows = [text for _, text in app.runtime.tree.rows()]
    assert not any(text.endswith("user: ") for text in tree_rows)


def test_post_settle_continuation_allows_bridged_mcp_calls(fake_agy, tmp_path, monkeypatch):
    from pydantic_ai import FunctionToolset
    from pydantic_ai.capabilities import AbstractCapability

    monkeypatch.setattr(antigravity, "DEFERRED_FINALIZE_DEBOUNCE", 0.04)
    seen: list[str] = []
    toolset = FunctionToolset()

    @toolset.tool_plain
    def ping_bridge(note: str) -> str:
        seen.append(note)
        return f"pong:{note}"

    class _PingCap(AbstractCapability):
        bridge_to_acp = True

        def get_toolset(self):
            return toolset

    model = AntigravityModel("fake-flash")
    app, _, buffer = _controller_app(model, tmp_path, extra_capabilities=(_PingCap(),))

    async def exercise():
        consumer = asyncio.create_task(app.controller.consume())
        try:
            payload = json.dumps({"name": "ping_bridge", "arguments": {"note": "from-wake"}})
            app.controller.submit(f"LATER_MCP {payload}", "queue")
            for _ in range(300):
                if (
                    "late mcp: pong:from-wake" in buffer.getvalue()
                    and not app.activity.busy
                    and not app.activity.queued_prompts
                ):
                    break
                await asyncio.sleep(0.02)
            assert "late mcp: pong:from-wake" in buffer.getvalue()
            assert seen == ["from-wake"]
            assert len(prompts(fake_agy)) == 1
        finally:
            app.controller.running = False
            consumer.cancel()
            await asyncio.gather(consumer, return_exceptions=True)

    run(exercise())


def test_post_settle_continuation_supports_steering_and_cancel(fake_agy, tmp_path, monkeypatch):
    monkeypatch.setattr(antigravity, "DEFERRED_FINALIZE_DEBOUNCE", 0.04)

    # 1. Steering during a continuation turn sends a real prompt and completes.
    model_steer = AntigravityModel("fake-flash")
    app_steer, _, buf_steer = _controller_app(model_steer, tmp_path)

    async def steer_exercise():
        consumer = asyncio.create_task(app_steer.controller.consume())
        try:
            app_steer.controller.submit("LATER_HANG start", "queue")
            for _ in range(250):
                if "Continuing in background..." in buf_steer.getvalue():
                    break
                await asyncio.sleep(0.02)
            assert "Continuing in background..." in buf_steer.getvalue()
            app_steer.controller.submit("COMPLETE_BG steer continuation", "steering")
            for _ in range(250):
                if (
                    "got: COMPLETE_BG steer continuation" in buf_steer.getvalue()
                    and not app_steer.activity.busy
                ):
                    break
                await asyncio.sleep(0.02)
            assert "got: COMPLETE_BG steer continuation" in buf_steer.getvalue()
        finally:
            app_steer.controller.running = False
            consumer.cancel()
            await asyncio.gather(consumer, return_exceptions=True)

    run(steer_exercise())

    # 2. Cancelling a continuation turn sends session/cancel and recovers for the next turn.
    model_cancel = AntigravityModel("fake-flash")
    app_cancel, _, buf_cancel = _controller_app(model_cancel, tmp_path)

    async def cancel_exercise():
        consumer = asyncio.create_task(app_cancel.controller.consume())
        try:
            app_cancel.controller.submit("LATER_HANG cancel me", "queue")
            for _ in range(250):
                if "Continuing in background..." in buf_cancel.getvalue():
                    break
                await asyncio.sleep(0.02)
            app_cancel.controller.cancel()
            for _ in range(200):
                if not app_cancel.activity.busy and app_cancel.controller.live_task is None:
                    break
                await asyncio.sleep(0.02)
            app_cancel.controller.submit("fresh after cancel", "queue")
            for _ in range(250):
                if (
                    "got: fresh after cancel" in buf_cancel.getvalue()
                    and not app_cancel.activity.busy
                ):
                    break
                await asyncio.sleep(0.02)
            assert "got: fresh after cancel" in buf_cancel.getvalue()
            assert any(m.get("method") == "session/cancel" for m in fake_agy())
        finally:
            app_cancel.controller.running = False
            consumer.cancel()
            await asyncio.gather(consumer, return_exceptions=True)

    run(cancel_exercise())


def test_cancel_quarantines_trailing_updates_until_next_prompt(fake_agy, tmp_path, monkeypatch):
    monkeypatch.setattr(antigravity, "DEFERRED_FINALIZE_DEBOUNCE", 0.04)
    model = AntigravityModel("fake-flash")
    app, _, buffer = _controller_app(model, tmp_path)

    async def exercise():
        consumer = asyncio.create_task(app.controller.consume())
        try:
            app.controller.submit("CANCEL_TAIL slow run", "queue")
            for _ in range(200):
                if "working" in buffer.getvalue():
                    break
                await asyncio.sleep(0.01)
            app.controller.cancel()
            for _ in range(200):
                if not app.activity.busy and app.controller.live_task is None:
                    break
                await asyncio.sleep(0.01)
            # Wait past fake_agy's trailing post-cancel frame: must not reopen a turn.
            await asyncio.sleep(0.12)
            assert "stale trailing cancel frame" not in buffer.getvalue()
            assert not model.has_pending_wake()
            # A subsequent normal prompt lifts quarantine so late wakes work again.
            app.controller.submit("LATER_WAKE after cancel", "queue")
            for _ in range(250):
                if (
                    "Committed after background check." in buffer.getvalue()
                    and not app.activity.busy
                ):
                    break
                await asyncio.sleep(0.02)
            assert "Committed after background check." in buffer.getvalue()
        finally:
            app.controller.running = False
            consumer.cancel()
            await asyncio.gather(consumer, return_exceptions=True)

    run(exercise())


def test_host_preserves_last_prompt_and_stays_busy_for_provider_wake(tmp_path):
    from types import SimpleNamespace

    from pcode.host import HostEntry, SessionHost

    entry = HostEntry(
        id="h1",
        pid=1,
        model="antigravity:fake-flash",
        workspace=str(tmp_path),
    )
    host = SessionHost(entry, tmp_path / "hosts")
    host.turn_began("initial user prompt")
    assert host.entry.last_prompt == "initial user prompt"
    assert host.entry.title == "initial user prompt"
    # Provider continuation passes an empty prompt and must not clobber last_prompt/title.
    host.turn_began("")
    assert host.entry.last_prompt == "initial user prompt"
    assert host.entry.title == "initial user prompt"
    # Host reports busy while a provider wake is pending.
    host.controller.runtime = SimpleNamespace(has_provider_wake=lambda: True)
    assert "Antigravity" in host.busy_with()


def test_daemon_updates_do_not_trigger_post_settle_wake():
    daemons: set[str] = set()
    start = {
        "sessionUpdate": "tool_call",
        "toolCallId": "d-1",
        "kind": "execute",
        "rawInput": {"CommandLine": "npm run dev", "IsDaemon": True},
        "status": "in_progress",
    }
    end = {
        "sessionUpdate": "tool_call_update",
        "toolCallId": "d-1",
        "status": "completed",
    }
    assert not antigravity._is_wake_update(start, daemon_tools=daemons)
    assert "d-1" in daemons
    assert not antigravity._is_wake_update(end, daemon_tools=daemons)


def test_continue_after_continuation_does_not_leak_placeholder_and_history_omits_blank_user(
    fake_agy, tmp_path, monkeypatch
):
    from pcode.history import TurnReader
    from pcode.sessions import list_sessions, session_records

    monkeypatch.setattr(antigravity, "DEFERRED_FINALIZE_DEBOUNCE", 0.04)
    model = AntigravityModel("fake-flash")
    app, root, buffer = _controller_app(model, tmp_path)

    async def exercise():
        consumer = asyncio.create_task(app.controller.consume())
        try:
            app.controller.submit("initial turn before wake", "queue")
            for _ in range(250):
                if (
                    "got: initial turn before wake" in buffer.getvalue()
                    and not app.activity.busy
                    and not app.activity.queued_prompts
                ):
                    break
                await asyncio.sleep(0.01)
            # Attach a pending `!command` exchange between turns, then trigger a
            # post-settle wake: the continuation must preserve pending_shell.
            shell_msg = ModelRequest.user_text_prompt("!pwd -> /tmp")
            app.runtime.context.pending_shell = [shell_msg]
            model._idle_update(
                {
                    "sessionUpdate": "agent_message_chunk",
                    "content": {"type": "text", "text": "Committed after background check."},
                }
            )
            model._idle_update({"sessionUpdate": "usage_update", "used": 140, "size": 1_048_576})
            for _ in range(250):
                if (
                    "Committed after background check." in buffer.getvalue()
                    and not app.activity.busy
                    and not app.activity.queued_prompts
                ):
                    break
                await asyncio.sleep(0.02)
            assert "Committed after background check." in buffer.getvalue()
            assert app.runtime.context.pending_shell == [shell_msg]
            app.runtime.context.pending_shell = []
            # /continue after a completed provider continuation must send "Continue.",
            # never the synthetic PROVIDER_CONTINUATION_PROMPT placeholder.
            app.controller.submit("", "continue")
            for _ in range(250):
                if (
                    len(prompts(fake_agy)) >= 2
                    and not app.activity.busy
                    and not app.activity.queued_prompts
                ):
                    break
                await asyncio.sleep(0.02)
            assert len(prompts(fake_agy)) == 2
            assert prompts(fake_agy)[-1][-1]["text"] == antigravity.CONTINUE
            sent_texts = [
                block.get("text", "")
                for blocks in prompts(fake_agy)
                for block in blocks
                if block.get("type") == "text"
            ]
            assert not any(antigravity.PROVIDER_CONTINUATION_PROMPT in t for t in sent_texts)
        finally:
            app.controller.running = False
            consumer.cancel()
            await asyncio.gather(consumer, return_exceptions=True)

    run(exercise())
    saved = list_sessions(root)[0]
    reader = TurnReader()
    for record in session_records(saved, root):
        reader.accept(record)
    assert not any(block == "User: " for turn in reader.turns.values() for block in turn.blocks)


def test_navigate_drops_mismatched_antigravity_session_and_clears_wake(
    fake_agy, tmp_path, monkeypatch
):
    monkeypatch.setattr(antigravity, "DEFERRED_FINALIZE_DEBOUNCE", 0.04)
    model = AntigravityModel("fake-flash")
    app, _, _ = _controller_app(model, tmp_path)

    async def exercise():
        await app.controller.run_turn("first turn")
        first_node = app.runtime.tree.active
        await app.controller.run_turn("second turn")
        assert model._session is not None and model._session.turns == 2
        # Simulate a pending post-turn wake before rewinding to turn 1.
        model._wake_buffer.append(
            {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "late"}}
        )
        model._wake_pending = True
        await app.runtime.navigate(first_node)
        assert not model.has_pending_wake(app.runtime.history)
        assert not model._wake_buffer

    run(exercise())


def test_effort_sets_thought_level_across_turns_and_restores_default(fake_agy, tmp_path):
    async def go():
        model = AntigravityModel("fake-neon")
        agent = agent_for(model, tmp_path)
        # Default model starts on high; asking for high sends no config RPC.
        agent.model_settings = {CWD_SETTING: str(tmp_path), "antigravity_effort": "high"}
        first = await agent.run("one")
        # Lowering to low updates the live session.
        agent.model_settings = {CWD_SETTING: str(tmp_path), "antigravity_effort": "low"}
        second = await agent.run("two", message_history=first.all_messages())
        # Same effort again sends no duplicate RPC.
        third = await agent.run("three", message_history=second.all_messages())
        # Returning to default restores the model's initial thought_level (high).
        agent.model_settings = {CWD_SETTING: str(tmp_path)}
        await agent.run("four", message_history=third.all_messages())

    run(go())
    thought_calls = [
        e["params"]["value"]
        for e in fake_agy("session/set_config_option")
        if e["params"]["configId"] == "thought_level"
    ]
    assert thought_calls == ["low", "high"]
    assert antigravity.cached_thought_levels("fake-neon") == ("low", "medium", "high", "xhigh")


def test_effort_applies_after_session_resume(fake_agy, tmp_path):
    async def go():
        model = AntigravityModel("fake-neon")
        agent = agent_for(model, tmp_path)
        agent.model_settings = {CWD_SETTING: str(tmp_path), "antigravity_effort": "low"}
        first = await agent.run("one")
        await antigravity.shutdown()
        resumed = Agent(
            AntigravityModel("fake-neon"),
            model_settings={CWD_SETTING: str(tmp_path), "antigravity_effort": "xhigh"},
        )
        await resumed.run("two", message_history=first.all_messages())

    run(go())
    assert [e["params"]["sessionId"] for e in fake_agy("session/resume")] == ["s-1"]
    configs = [
        (e["params"]["configId"], e["params"]["value"])
        for e in fake_agy("session/set_config_option")
    ]
    assert configs == [("thought_level", "low"), ("model", "fake-neon"), ("thought_level", "xhigh")]


def test_effort_clamps_to_nearest_offered_thought_level(fake_agy, tmp_path):
    async def go():
        agent = Agent(
            AntigravityModel("fake-flash"),
            model_settings={CWD_SETTING: str(tmp_path), "antigravity_effort": "xhigh"},
        )
        return await agent.run("hi")

    result = run(go())
    assert result.output == "got: hi"
    configs = [
        (e["params"]["configId"], e["params"]["value"])
        for e in fake_agy("session/set_config_option")
    ]
    # fake-flash offers low/medium/high, so xhigh clamps to high instead of failing.
    assert configs == [("model", "fake-flash"), ("thought_level", "high")]
    assert antigravity.cached_thought_levels("fake-flash") == ("low", "medium", "high")


def test_model_without_thought_level_skips_rpc_and_gates_effort(fake_agy, tmp_path):
    from io import StringIO
    from types import SimpleNamespace

    from rich.console import Console

    from pcode.app import PreviewApp
    from pcode.preferences import effort_setting, model_efforts, save_model_effort

    async def go():
        model = AntigravityModel("fake-fixed")
        agent = Agent(
            model,
            model_settings={CWD_SETTING: str(tmp_path), "antigravity_effort": "high"},
        )
        await agent.run("hi")
        return model

    model = run(go())
    assert model.thought_levels == ()
    assert antigravity.cached_thought_levels("fake-fixed") == ()
    assert [
        e
        for e in fake_agy("session/set_config_option")
        if e["params"]["configId"] == "thought_level"
    ] == []
    # Both the live model and a fresh lookup via the disk cache report no effort control.
    assert effort_setting("antigravity:fake-fixed", model) is None
    assert effort_setting("antigravity:fake-fixed", None) is None

    save_model_effort("antigravity:fake-fixed", "high")
    output = StringIO()
    runtime = SimpleNamespace(
        agent=SimpleNamespace(model=model, model_settings={"antigravity_effort": "high"})
    )
    app = PreviewApp(
        model="antigravity:fake-fixed",
        runtime=runtime,
        console=Console(file=output, color_system=None),
    )
    assert app.current_effort() == "n/a"
    app.handle("/effort high")
    assert "has no effort control" in output.getvalue()
    app.handle("/effort default")
    assert model_efforts()["antigravity:fake-fixed"] == "default"
    assert "antigravity_effort" not in runtime.agent.model_settings
    assert "Cleared the saved effort" in output.getvalue()


def test_side_model_and_btw_apply_antigravity_effort(fake_agy, tmp_path):
    from io import StringIO
    from types import SimpleNamespace

    from rich.console import Console

    from pcode.agent import side_model, with_effort
    from pcode.app import PreviewApp
    from pcode.aside import SideTarget
    from pcode.live import AgentRuntime

    async def go():
        agent = agent_for(AntigravityModel("fake-neon"), tmp_path)
        runtime = AgentRuntime(agent)
        # Explicit $antigravity:model+low side question.
        chosen = side_model("antigravity:fake-flash", "low")
        first = await runtime.aside("quick check?", model=chosen)
        # /btw +low on the conversation's own Antigravity model (model=None).
        own_settings = with_effort(
            "antigravity:fake-neon", agent.model, agent.model_settings, "low"
        )
        second = await runtime.aside("own check?", settings=own_settings)
        return first, second

    first, second = run(go())
    assert "Question: quick check?" in first.answer
    assert "Question: own check?" in second.answer
    configs = [
        (e["params"]["configId"], e["params"]["value"])
        for e in fake_agy("session/set_config_option")
    ]
    assert configs == [("model", "fake-flash"), ("thought_level", "low"), ("thought_level", "low")]

    # Gated model refuses /btw +high and /btw $antigravity:fake-fixed+high.
    antigravity._remember_thought_levels("fake-fixed", ())
    app = PreviewApp(
        model="antigravity:fake-fixed",
        runtime=SimpleNamespace(agent=SimpleNamespace(model=None, model_settings={})),
        console=Console(file=StringIO(), color_system=None),
    )
    with pytest.raises(ValueError, match="has no effort control"):
        app.controller.check_effort(SideTarget("", "high"))
    with pytest.raises(ValueError, match="has no effort control"):
        app.controller.check_effort(SideTarget("antigravity:fake-fixed", "high"))


def test_borrowed_turn_effort_applies_for_one_turn_then_restores(fake_agy, tmp_path):
    from pcode.agent import with_effort

    async def go():
        model = AntigravityModel("fake-neon")
        agent = Agent(
            model,
            model_settings={CWD_SETTING: str(tmp_path), "antigravity_effort": "medium"},
        )
        first = await agent.run("turn one")
        borrowed = with_effort("antigravity:fake-neon", model, agent.model_settings, "xhigh")
        with agent.override(model_settings=borrowed):
            second = await agent.run("turn two", message_history=first.all_messages())
        await agent.run("turn three", message_history=second.all_messages())

    run(go())
    thought_calls = [
        e["params"]["value"]
        for e in fake_agy("session/set_config_option")
        if e["params"]["configId"] == "thought_level"
    ]
    assert thought_calls == ["medium", "xhigh", "medium"]


def test_resume_default_effort_restores_cached_model_default(fake_agy, tmp_path):
    async def go():
        # Fresh session records fake-neon's built-in default ("high") and runs at "low".
        first = await Agent(
            AntigravityModel("fake-neon"),
            model_settings={CWD_SETTING: str(tmp_path), "antigravity_effort": "low"},
        ).run("one")
        await antigravity.shutdown()
        # Resumed session where agy preserved "low", then user clears to /effort default.
        resumed = AntigravityModel("fake-neon")
        second = await Agent(
            resumed,
            model_settings={CWD_SETTING: str(tmp_path), "antigravity_effort": "low"},
        ).run("two", message_history=first.all_messages())
        await Agent(
            resumed,
            model_settings={CWD_SETTING: str(tmp_path)},
        ).run("three", message_history=second.all_messages())

    run(go())
    assert antigravity.cached_default_thought_level("fake-neon") == "high"
    thought_calls = [
        e["params"]["value"]
        for e in fake_agy("session/set_config_option")
        if e["params"]["configId"] == "thought_level"
    ]
    # Turn 1 sets low; resumed turn 2 sets low; turn 3 (/effort default) restores high.
    assert thought_calls == ["low", "low", "high"]


def test_fit_thought_level_and_corrupted_models_cache_self_heal(monkeypatch, tmp_path):
    from pcode.preferences import effort_setting

    assert antigravity._fit_thought_level("medium", ("low", "high")) == "low"
    assert antigravity._fit_thought_level("xhigh", ("low", "medium", "high")) == "high"
    assert antigravity._fit_thought_level("bogus", ("low", "high")) is None
    assert antigravity._fit_thought_level("high", ()) is None

    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    cache = antigravity.models_cache()
    cache.parent.mkdir(parents=True, exist_ok=True)
    # Corrupted non-dict `thought_levels` must not crash cache reads or writes.
    cache.write_text('{"models": ["fake-neon"], "thought_levels": ["bad"]}')
    assert antigravity.cached_thought_levels("fake-neon") is None
    antigravity._remember_thought_levels("fake-fixed", ())
    assert antigravity.cached_thought_levels("fake-fixed") == ()
    assert effort_setting("antigravity:fake-fixed") is None
    # Updating the cache invalidates preferences' stat-keyed mirror immediately.
    antigravity._remember_thought_levels("fake-fixed", ("low", "high"))
    assert effort_setting("antigravity:fake-fixed") == "antigravity_effort"


def test_stale_list_models_cache_is_ignored(monkeypatch, tmp_path):
    from pcode.models import model_catalog

    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    cache = antigravity.models_cache()
    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text('["gemini-4-neon-high", "gemini-4-neon-low"]')
    assert cached_models() == ()
    assert model_catalog({"antigravity"}) == ["antigravity:gemini-4-neon"]


def test_antigravity_tool_started_emits_while_tool_is_still_running(
    fake_agy, tmp_path, monkeypatch
):
    """ToolStarted + Running status arrive on `tool_call`, not after `completed`."""
    from pcode.live import AgentRuntime
    from pcode.runtime import RunStatus, ToolStarted, ToolSummary

    gate = tmp_path / "release.gate"
    monkeypatch.setenv("FAKE_AGY_GATE", str(gate))
    model = AntigravityModel("fake-flash")
    runtime = AgentRuntime(agent_for(model, tmp_path))

    async def exercise():
        seen: list = []
        started_before_gate = False
        status_before_gate = ""
        async for event in runtime.stream("GATE_TOOL"):
            seen.append(event)
            if isinstance(event, RunStatus) and "pytest" in event.text and not gate.exists():
                started_before_gate = any(
                    isinstance(e, ToolStarted) and e.call_id == "gate-1" for e in seen
                )
                status_before_gate = event.text
                # Wait briefly so elapsed_seconds reflects real runtime before completion.
                await asyncio.sleep(0.03)
                gate.write_text("go")
        return seen, started_before_gate, status_before_gate

    seen, started_before_gate, status_before_gate = run(exercise())
    assert started_before_gate
    assert status_before_gate == "Running run_command · pytest…"
    starts = [e for e in seen if isinstance(e, ToolStarted) and e.call_id == "gate-1"]
    summaries = [e for e in seen if isinstance(e, ToolSummary) and e.call_id == "gate-1"]
    assert len(starts) == 1
    assert starts[0].detail == "pytest"
    assert starts[0].command == "pytest"
    assert len(summaries) == 1
    assert summaries[0].command == "pytest"
    assert summaries[0].elapsed_seconds >= 0.02

    from io import StringIO

    from rich.console import Console

    from pcode.ui import Transcript

    stream = StringIO()
    transcript = Transcript(
        Console(file=stream, width=80, color_system=None),
        preferences={"group_tools": "on"},
    )
    transcript.tool_result(summaries[0])
    transcript.settle_tools()
    lines = stream.getvalue().splitlines()
    assert len(lines) == 1
    assert lines[0].startswith("✓ Run shell · ") and lines[0].endswith(" · pytest")


def test_antigravity_parallel_tools_both_report_running_before_completion(
    fake_agy, tmp_path, monkeypatch
):
    from pcode.live import AgentRuntime
    from pcode.runtime import RunStatus, ToolStarted, ToolSummary

    gate = tmp_path / "parallel.gate"
    monkeypatch.setenv("FAKE_AGY_GATE", str(gate))
    model = AntigravityModel("fake-flash")
    runtime = AgentRuntime(agent_for(model, tmp_path))

    async def exercise():
        seen: list = []
        saw_two_running = False
        async for event in runtime.stream("PARALLEL_TOOLS"):
            seen.append(event)
            if (
                isinstance(event, RunStatus)
                and "Running 2 tools" in event.text
                and not gate.exists()
            ):
                saw_two_running = True
                gate.write_text("go")
        return seen, saw_two_running

    seen, saw_two_running = run(exercise())
    assert saw_two_running
    assert [(e.call_id, e.detail) for e in seen if isinstance(e, ToolStarted)] == [
        ("par-1", "a.py"),
        ("par-2", "pytest"),
    ]
    assert [e.call_id for e in seen if isinstance(e, ToolSummary)] == ["par-1", "par-2"]


def test_antigravity_late_raw_input_updates_running_tool_without_duplicating(
    fake_agy, tmp_path, monkeypatch
):
    from pcode.live import AgentRuntime
    from pcode.runtime import ToolStarted, ToolSummary

    gate = tmp_path / "late.gate"
    monkeypatch.setenv("FAKE_AGY_GATE", str(gate))
    model = AntigravityModel("fake-flash")
    runtime = AgentRuntime(agent_for(model, tmp_path))

    async def exercise():
        seen: list = []
        async for event in runtime.stream("LATE_INPUT"):
            seen.append(event)
            if isinstance(event, ToolStarted) and event.call_id == "late-1" and not gate.exists():
                await asyncio.sleep(0.03)
                gate.write_text("go")
        return seen

    seen = run(exercise())
    starts = [e for e in seen if isinstance(e, ToolStarted) and e.call_id == "late-1"]
    summaries = [e for e in seen if isinstance(e, ToolSummary) and e.call_id == "late-1"]
    # Initial start on title-only `tool_call`, then restated once `rawInput` arrives.
    assert [e.detail for e in starts] == ["command unavailable", "make test"]
    assert [e.command for e in starts] == ["", "make test"]
    assert starts[0].started_at == starts[1].started_at
    assert "Run tests" in starts[1].arguments
    assert len(summaries) == 1
    assert summaries[0].detail == "make test"
    assert summaries[0].command == "make test"
    assert summaries[0].elapsed_seconds >= 0.02
    # Text streamed around the late update and tool return must all settle into messages.
    from pcode.runtime import Message

    messages = [e.markdown for e in seen if isinstance(e, Message)]
    assert messages == ["before input ", "after input ", "done"]
    # Tree and tool archive keep one entry per call_id, updated in place.
    node = runtime.tree.nodes[runtime.tree.active]
    assert len(node.tools) == 1
    assert node.tools[0]["label"] == "make test"
    assert node.tools[0]["outcome"] == "completed"
    assert len(runtime.inspections.calls) == 1
    assert runtime.inspections.calls[0].detail == "make test"


def test_session_new_appends_pcode_system_prompt_when_advertised(fake_agy, tmp_path, monkeypatch):
    from pydantic_ai.capabilities import Capability
    from pydantic_ai.models.test import TestModel

    from pcode import advisor as advisor_mod
    from pcode.agent import PCODE_CLIENT_INSTRUCTIONS

    monkeypatch.setattr(advisor_mod, "_local_model", lambda _name: TestModel())

    def delegate_task(task: str) -> str:
        """Delegate a task."""
        return task

    async def go():
        # Plain agent: client instructions only, no delegation or advisor notes.
        await streamed(agent_for(AntigravityModel("fake-neon"), tmp_path), "hello")
        # Agent with delegation and real advisor_capability (Dynamic -> Combined).
        agent = Agent(
            AntigravityModel("fake-neon"),
            model_settings={CWD_SETTING: str(tmp_path)},
            capabilities=[
                Capability(id="delegation", tools=[delegate_task]),
                advisor_mod.advisor_capability(advisor_mod.AdvisorChoice("openai:gpt-5")),
            ],
        )
        await streamed(agent, "hello")

    run(go())
    new_calls = fake_agy("session/new")
    assert len(new_calls) == 2
    plain_append = new_calls[0]["params"]["_meta"]["antigravity"]["systemPrompt"]["append"]
    assert PCODE_CLIENT_INSTRUCTIONS in plain_append
    assert "mcp_pcode_delegate_task" not in plain_append
    assert "advisor" not in plain_append
    assert "edit_file" not in plain_append

    full_append = new_calls[1]["params"]["_meta"]["antigravity"]["systemPrompt"]["append"]
    assert PCODE_CLIENT_INSTRUCTIONS in full_append
    assert "mcp_pcode_delegate_task" in full_append
    assert "invoke_subagent" in full_append
    assert "task panel" in full_append
    assert "openai:gpt-5" in full_append
    assert "edit_file" not in full_append


def test_system_prompt_append_skipped_for_side_worker_and_unadvertised(
    fake_agy, tmp_path, monkeypatch
):
    async def go():
        for kwargs in ({"side": True}, {"worker": True}):
            model = AntigravityModel("fake-neon", **kwargs)
            await Agent(model, model_settings={CWD_SETTING: str(tmp_path)}).run("hello")
        monkeypatch.setenv("FAKE_AGY_NO_SYSTEM_PROMPT_APPEND", "1")
        await streamed(agent_for(AntigravityModel("fake-neon"), tmp_path), "hello")

    run(go())
    new_calls = fake_agy("session/new")
    assert len(new_calls) == 3
    for call in new_calls:
        assert "_meta" not in call["params"]


def test_session_new_appends_ancestor_and_non_native_repo_instructions(
    fake_agy, tmp_path, monkeypatch
):
    from pydantic_ai.capabilities import LocalWorkspace

    from pcode.repo_context import create_repo_context

    home = tmp_path / "home"
    parent = home / "work"
    repo = parent / "repo"
    sub = repo / "pkg"
    (repo / ".git").mkdir(parents=True)
    sub.mkdir(parents=True)

    (home / "AGENTS.md").write_text("HOME_AGENTS_RULE\n", encoding="utf-8")
    (parent / "AGENTS.md").write_text("PARENT_AGENTS_RULE\n", encoding="utf-8")
    (repo / "AGENTS.md").write_text("REPO_AGENTS_RULE\n", encoding="utf-8")
    (repo / "CLAUDE.md").write_text("REPO_CLAUDE_RULE\n", encoding="utf-8")
    (repo / "AGENTS.local.md").write_text("REPO_LOCAL_RULE\n", encoding="utf-8")
    (sub / "AGENTS.md").write_text("SUB_AGENTS_RULE\n", encoding="utf-8")
    # Identical copy of an AGENTS.md that agy loads natively should not be duplicated.
    (sub / "CLAUDE.md").write_text("SUB_AGENTS_RULE\n", encoding="utf-8")

    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
    monkeypatch.setattr(
        "pcode.repo_context.load_preferences", lambda: {"repo_context_walk_up": "on"}
    )

    async def go():
        agent = Agent(
            AntigravityModel("fake-neon"),
            model_settings={CWD_SETTING: str(sub)},
            capabilities=[LocalWorkspace(sub), create_repo_context(sub)],
        )
        await streamed(agent, "hello")

    run(go())
    new_calls = fake_agy("session/new")
    assert len(new_calls) == 1
    append = new_calls[0]["params"]["_meta"]["antigravity"]["systemPrompt"]["append"]
    assert "HOME_AGENTS_RULE" in append
    assert "PARENT_AGENTS_RULE" in append
    assert "REPO_CLAUDE_RULE" in append
    assert "REPO_LOCAL_RULE" in append
    # Repo and subdir AGENTS.md (plus identical CLAUDE.md) are loaded natively by agy.
    assert "REPO_AGENTS_RULE" not in append
    assert "SUB_AGENTS_RULE" not in append
    # Ancestor-first order is preserved.
    assert append.index("HOME_AGENTS_RULE") < append.index("PARENT_AGENTS_RULE")
    assert append.index("PARENT_AGENTS_RULE") < append.index("REPO_CLAUDE_RULE")
    assert append.index("REPO_CLAUDE_RULE") < append.index("REPO_LOCAL_RULE")


def test_session_new_repo_instructions_respect_walk_up_off_and_worktree_dedupe(
    fake_agy, tmp_path, monkeypatch
):
    from pydantic_ai.capabilities import LocalWorkspace

    from pcode.repo_context import create_repo_context

    home = tmp_path / "home"
    mainline = home / "repo"
    wt = mainline / ".worktrees" / "feat"
    (mainline / ".git").mkdir(parents=True)
    wt.mkdir(parents=True)
    # Worktree .git is a file, not a directory.
    (wt / ".git").write_text("gitdir: ../../.git/worktrees/feat\n", encoding="utf-8")

    (home / "AGENTS.md").write_text("HOME_RULE\n", encoding="utf-8")
    (mainline / "AGENTS.md").write_text("SHARED_REPO_RULE\n", encoding="utf-8")
    (mainline / "AGENTS.local.md").write_text("MAINLINE_LOCAL_RULE\n", encoding="utf-8")
    (wt / "AGENTS.md").write_text("SHARED_REPO_RULE\n", encoding="utf-8")
    (wt / "CLAUDE.local.md").write_text("WT_LOCAL_RULE\n", encoding="utf-8")

    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))

    async def go():
        monkeypatch.setattr(
            "pcode.repo_context.load_preferences", lambda: {"repo_context_walk_up": "on"}
        )
        await streamed(
            Agent(
                AntigravityModel("fake-neon"),
                model_settings={CWD_SETTING: str(wt)},
                capabilities=[LocalWorkspace(wt), create_repo_context(wt)],
            ),
            "hello",
        )
        monkeypatch.setattr(
            "pcode.repo_context.load_preferences", lambda: {"repo_context_walk_up": "off"}
        )
        await streamed(
            Agent(
                AntigravityModel("fake-neon"),
                model_settings={CWD_SETTING: str(wt)},
                capabilities=[LocalWorkspace(wt), create_repo_context(wt)],
            ),
            "hello",
        )

    run(go())
    new_calls = fake_agy("session/new")
    assert len(new_calls) == 2

    walk_on = new_calls[0]["params"]["_meta"]["antigravity"]["systemPrompt"]["append"]
    assert "HOME_RULE" in walk_on
    assert "MAINLINE_LOCAL_RULE" in walk_on
    assert "WT_LOCAL_RULE" in walk_on
    # Neither the worktree's nor mainline's identical AGENTS.md is duplicated.
    assert "SHARED_REPO_RULE" not in walk_on

    walk_off = new_calls[1]["params"]["_meta"]["antigravity"]["systemPrompt"]["append"]
    assert "HOME_RULE" not in walk_off
    assert "MAINLINE_LOCAL_RULE" not in walk_off
    assert "SHARED_REPO_RULE" not in walk_off
    assert "WT_LOCAL_RULE" in walk_off


def test_session_new_repo_instructions_outside_git_repo(fake_agy, tmp_path, monkeypatch):
    from pydantic_ai.capabilities import LocalWorkspace

    from pcode.repo_context import create_repo_context

    home = tmp_path / "home"
    scratch = home / "scratch"
    scratch.mkdir(parents=True)
    (home / "AGENTS.md").write_text("HOME_OUTSIDE_GIT\n", encoding="utf-8")
    (scratch / "AGENTS.md").write_text("SCRATCH_LOCAL_AGENTS\n", encoding="utf-8")

    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
    monkeypatch.setattr(
        "pcode.repo_context.load_preferences", lambda: {"repo_context_walk_up": "on"}
    )

    async def go():
        await streamed(
            Agent(
                AntigravityModel("fake-neon"),
                model_settings={CWD_SETTING: str(scratch)},
                capabilities=[LocalWorkspace(scratch), create_repo_context(scratch)],
            ),
            "hello",
        )

    run(go())
    new_calls = fake_agy("session/new")
    assert len(new_calls) == 1
    append = new_calls[0]["params"]["_meta"]["antigravity"]["systemPrompt"]["append"]
    assert "HOME_OUTSIDE_GIT" in append
    assert "SCRATCH_LOCAL_AGENTS" not in append


def test_session_new_repo_instructions_symlinks_gemini_all_native_and_combined(
    fake_agy, tmp_path, monkeypatch
):
    from pydantic_ai.capabilities import CombinedCapability, LocalWorkspace

    from pcode.repo_context import create_repo_context

    home = tmp_path / "home"
    # 1. Repo with only native AGENTS.md + CLAUDE.md -> AGENTS.md symlink + GEMINI.md copy.
    native_only = home / "native_only"
    (native_only / ".git").mkdir(parents=True)
    (native_only / "AGENTS.md").write_text("NATIVE_AGENTS\n", encoding="utf-8")
    (native_only / "CLAUDE.md").symlink_to("AGENTS.md")
    (native_only / "GEMINI.md").write_text("NATIVE_GEMINI\n", encoding="utf-8")
    (native_only / "AGENTS.local.md").write_text("NATIVE_GEMINI\n", encoding="utf-8")

    # 2. CombinedCapability wiring + multi-turn reuse when AGENTS.local.md changes.
    combined_repo = home / "combined"
    (combined_repo / ".git").mkdir(parents=True)
    (home / "AGENTS.md").write_text("HOME_COMBINED\n", encoding="utf-8")
    (combined_repo / "AGENTS.local.md").write_text("LOCAL_V1\n", encoding="utf-8")

    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
    monkeypatch.setattr(
        "pcode.repo_context.load_preferences", lambda: {"repo_context_walk_up": "on"}
    )

    async def go():
        # All-native repo (with no home AGENTS.md yet? Remove temporarily via walk_up=off).
        monkeypatch.setattr(
            "pcode.repo_context.load_preferences", lambda: {"repo_context_walk_up": "off"}
        )
        await streamed(
            Agent(
                AntigravityModel("fake-neon"),
                model_settings={CWD_SETTING: str(native_only)},
                capabilities=[LocalWorkspace(native_only), create_repo_context(native_only)],
            ),
            "hello",
        )
        monkeypatch.setattr(
            "pcode.repo_context.load_preferences", lambda: {"repo_context_walk_up": "on"}
        )
        agent = Agent(
            AntigravityModel("fake-neon"),
            model_settings={CWD_SETTING: str(combined_repo)},
            capabilities=[
                LocalWorkspace(combined_repo),
                CombinedCapability([create_repo_context(combined_repo)]),
            ],
        )
        first = await streamed(agent, "turn 1")
        # Editing AGENTS.local.md mid-session must not drop/recreate the live agy session.
        (combined_repo / "AGENTS.local.md").write_text("LOCAL_V2\n", encoding="utf-8")
        await streamed(agent, "turn 2", history=first.all_messages())

    run(go())
    new_calls = fake_agy("session/new")
    assert len(new_calls) == 2

    native_append = new_calls[0]["params"]["_meta"]["antigravity"]["systemPrompt"]["append"]
    assert "<context-file" not in native_append
    assert "NATIVE_AGENTS" not in native_append
    assert "NATIVE_GEMINI" not in native_append

    combined_append = new_calls[1]["params"]["_meta"]["antigravity"]["systemPrompt"]["append"]
    assert "HOME_COMBINED" in combined_append
    assert "LOCAL_V1" in combined_append


def test_worker_session_forwards_only_non_native_repo_instructions(fake_agy, tmp_path, monkeypatch):
    from pydantic_ai.capabilities import LocalWorkspace

    from pcode.agent import PCODE_CLIENT_INSTRUCTIONS
    from pcode.repo_context import create_repo_context

    home = tmp_path / "home"
    repo = home / "repo"
    (repo / ".git").mkdir(parents=True)
    (home / "AGENTS.md").write_text("HOME_WORKER_RULE\n", encoding="utf-8")
    (repo / "AGENTS.md").write_text("REPO_NATIVE_RULE\n", encoding="utf-8")
    (repo / "AGENTS.local.md").write_text("REPO_WORKER_LOCAL\n", encoding="utf-8")

    monkeypatch.setattr(Path, "home", staticmethod(lambda: home))
    monkeypatch.setattr(
        "pcode.repo_context.load_preferences", lambda: {"repo_context_walk_up": "on"}
    )

    async def go():
        agent = Agent(
            AntigravityModel("fake-neon", worker=True),
            model_settings={CWD_SETTING: str(repo)},
            capabilities=[LocalWorkspace(repo), create_repo_context(repo)],
        )
        await streamed(agent, "hello")

    run(go())
    new_calls = fake_agy("session/new")
    assert len(new_calls) == 1
    append = new_calls[0]["params"]["_meta"]["antigravity"]["systemPrompt"]["append"]
    assert "HOME_WORKER_RULE" in append
    assert "REPO_WORKER_LOCAL" in append
    assert "REPO_NATIVE_RULE" not in append
    assert PCODE_CLIENT_INSTRUCTIONS not in append


def test_subagent_wait_gives_up_after_its_limit(fake_agy, tmp_path, monkeypatch):
    # A subagent that never reports must not hold the turn (and spinner) forever.
    monkeypatch.setattr(antigravity, "DEFERRED_FINALIZE_DEBOUNCE", 0.04)
    monkeypatch.setattr(antigravity, "SUBAGENT_WAIT_LIMIT", 0.2)

    async def go():
        model = AntigravityModel("fake-neon")
        started = asyncio.get_running_loop().time()
        result = await asyncio.wait_for(agent_for(model, tmp_path).run("SUBAGENT_HANG"), 3.0)
        return result, asyncio.get_running_loop().time() - started

    result, elapsed = run(go())
    assert "Waiting for the code review subagent." in result.output
    assert elapsed >= 0.2


def test_tool_args_normalizes_command_line_variants():
    from pcode.antigravity import _tool_args

    assert _tool_args({"rawInput": {"CommandLine": "git status"}})["command"] == "git status"
    assert _tool_args({"rawInput": {"command_line": "make test"}})["command"] == "make test"
    assert (
        _tool_args({"rawInput": {"CommandLine": "", "command_line": "pytest -q"}})["command"]
        == "pytest -q"
    )


def test_native_failed_command_and_interrupted_child_preserve_command():
    from types import SimpleNamespace

    from pydantic_ai.messages import NativeToolCallPart, NativeToolReturnPart

    from pcode.runtime import ToolStarted, ToolSummary
    from pcode.stream_events import EventTranslator

    translator = EventTranslator(
        runtime=None,  # type: ignore[arg-type]
        context=SimpleNamespace(run_id="r1"),  # type: ignore[arg-type]
        plan_items=[],
        plan_preview=None,
        edit_preview=None,
    )
    call = NativeToolCallPart(
        "run_command",
        {"command": "pytest -q", "purpose": "tests"},
        tool_call_id="c1",
        provider_name="antigravity",
    )
    started = [e for e in translator.on_native_tool_call(call) if isinstance(e, ToolStarted)]
    assert len(started) == 1
    assert started[0].command == "pytest -q"
    assert started[0].purpose == "tests"
    assert started[0].execution == "foreground"

    ret = NativeToolReturnPart(
        "run_command",
        "[stderr]\nboom\n[exit code: 1]",
        tool_call_id="c1",
        provider_name="antigravity",
        outcome="failed",
    )
    summaries = [e for e in translator.on_native_tool_return(ret) if isinstance(e, ToolSummary)]
    assert len(summaries) == 1
    assert summaries[0].failed is True
    assert summaries[0].command == "pytest -q"
    assert summaries[0].purpose == "tests"
    assert summaries[0].error == "boom"

    # Interrupted child shell call under a delegate keeps its command and purpose.
    translator.child_tools["d1:s1"] = ToolStarted(
        "run_command",
        "make test",
        "d1:s1",
        parent_call_id="d1",
        command="make test",
        purpose="suite",
    )
    interrupted = [
        e
        for e in translator.on_delegation_end(
            SimpleNamespace(tool_call_id="d1", outcome="timed_out")  # type: ignore[arg-type]
        )
        if isinstance(e, ToolSummary)
    ]
    assert len(interrupted) == 1
    assert interrupted[0].command == "make test"
    assert interrupted[0].purpose == "suite"
