import asyncio
import json
import sqlite3
import stat
import subprocess
import sys
from copy import deepcopy

import pytest
from pydantic_ai import Agent
from pydantic_ai.capabilities import LocalWorkspace
from pydantic_ai.exceptions import ModelHTTPError
from pydantic_ai.messages import (
    ModelRequest,
    ModelResponse,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models.function import DeltaToolCall, FunctionModel
from pydantic_ai_harness import Coder
from pydantic_ai_harness.step_persistence import ContinuableSnapshot, RunRecord, ToolEffectRecord

from pcode.diagnostics import error_details
from pcode.live import AgentRuntime, error_message
from pcode.runtime import Message
from pcode.sessions import (
    SavedSession,
    SessionBusy,
    SessionError,
    list_sessions,
    resolve_session,
)


def test_round_trip_session_keeps_model_messages_and_transcript(tmp_path):
    root = tmp_path / "sessions"
    saved = SavedSession.create("test:local", tmp_path, root)
    identity = saved.info.id
    requests = []

    async def model(messages, info):
        requests.append(messages)
        yield "A saved answer."

    agent = Agent(FunctionModel(stream_function=model))

    async def run():
        runtime = AgentRuntime(agent, saved)
        _ = [event async for event in runtime.stream("first question")]
        history = runtime.history
        runtime.close()
        reopened = SavedSession.open(identity[:8], root)
        try:
            restored = AgentRuntime(agent, reopened)
            await restored.restore()
            assert restored.history == history
            assert restored.turns == 1
            assert restored.conversation_id == identity
            _ = [event async for event in restored.stream("follow up")]
            assert len(requests[-1]) > len(requests[0])
            assert restored.turns == 2
            records = list(reopened.transcript_records())
            assert sum(r["kind"] == "Message" for r in records) == 2
            assert [r["prompt"] for r in records if r["kind"] == "turn_started"] == [
                "first question",
                "follow up",
            ]
        finally:
            reopened.close()

    try:
        asyncio.run(run())
        assert resolve_session("latest", root).name == identity
        assert list_sessions(root)[0].turns == 2
        check = subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    "import asyncio, sys; from pathlib import Path; "
                    "from pcode.sessions import SavedSession; "
                    "s=SavedSession.open(sys.argv[1], Path(sys.argv[2])); "
                    "print(len(asyncio.run(s.recover()))); s.close()"
                ),
                identity,
                str(root),
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        assert int(check.stdout.strip()) > 0
        assert stat.S_IMODE((root / identity).stat().st_mode) == 0o700
        for name in ("session.json", "steps.sqlite3", "transcript.jsonl"):
            assert stat.S_IMODE((root / identity / name).stat().st_mode) == 0o600
    finally:
        saved.close()


def test_http_failure_after_tool_keeps_tool_result_and_diagnostics(tmp_path, monkeypatch):
    marker = "private-test-api-value-should-not-be-in-errors"
    monkeypatch.setenv("EXAMPLE_API_KEY", marker)
    (tmp_path / "README.md").write_text("a test repository")
    saved = SavedSession.create("test:local", tmp_path, tmp_path / "sessions")
    calls = 0

    async def model(messages, info):
        nonlocal calls
        calls += 1
        if calls == 1:
            yield {0: DeltaToolCall(name="read_file", json_args='{"path":"README.md"}')}
        elif calls == 2:
            raise ModelHTTPError(
                400,
                "test:local",
                body={
                    "error": {
                        "message": f"Unsupported request parameter; api_key={marker}",
                        "param": "prompt_cache_breakpoint",
                        "code": "invalid_parameter",
                    }
                },
            )
        else:
            results = [p for m in messages for p in m.parts if isinstance(p, ToolReturnPart)]
            assert any("a test repository" in str(p.content) for p in results)
            yield "Continued without calling the file tool again."

    runtime = AgentRuntime(
        Agent(
            FunctionModel(stream_function=model), capabilities=[LocalWorkspace(tmp_path), Coder()]
        ),
        saved,
    )

    async def run():
        with pytest.raises(ModelHTTPError) as raised:
            _ = [event async for event in runtime.stream("Read the README")]
        assert "Unsupported request parameter" in error_message(raised.value)
        assert marker not in error_message(raised.value)
        assert runtime.history
        assert saved.info.status == "failed"
        records = list(saved.transcript_records())
        failure = next(r for r in records if r["kind"] == "turn_failed")
        assert failure["error"]["provider_param"] == "prompt_cache_breakpoint"
        assert marker not in json.dumps(failure)
        with sqlite3.connect(saved.directory / "steps.sqlite3") as connection:
            errors = connection.execute(
                "SELECT error FROM events WHERE error IS NOT NULL"
            ).fetchall()
        assert errors and marker not in str(errors)
        runtime.close()
        reopened = SavedSession.open(saved.info.id, saved.directory.parent)
        recovered = AgentRuntime(runtime.agent, reopened)
        try:
            await recovered.restore()
            events = [event async for event in recovered.stream("Continue")]
            assert Message("Continued without calling the file tool again.") in events
            assert calls == 3
        finally:
            recovered.close()

    try:
        asyncio.run(run())
    finally:
        saved.close()


@pytest.mark.parametrize("tool_name", ["write_file", "edit_file", "run_command", "unknown_tool"])
@pytest.mark.parametrize("journaled", [False, True])
def test_resume_without_checkpoint_allows_interrupted_tools(tmp_path, tool_name, journaled):
    saved = SavedSession.create("test:local", tmp_path, tmp_path / "sessions")

    async def run():
        await saved.store.register_run(RunRecord(run_id="crashed", conversation_id=saved.info.id))
        await saved.store.record_tool_effect(
            ToolEffectRecord(
                run_id="crashed",
                tool_call_id="call-1",
                tool_name=tool_name,
                status="started",
            )
        )
        if journaled:
            saved.append("turn_started", run_id="crashed", prompt="interrupted turn")
        assert await saved.recover() == []
        effects = await saved.store.list_unresolved_tool_effects(run_id="crashed")
        assert len(effects) == 1
        assert effects[0].status == "started"

    try:
        asyncio.run(run())
    finally:
        saved.close()


def test_session_lock_and_path_validation(tmp_path):
    saved = SavedSession.create("test:local", tmp_path, tmp_path / "sessions")
    try:
        with pytest.raises(SessionError, match="already open"):
            SavedSession.open(saved.info.id, saved.directory.parent)
        with pytest.raises(SessionError, match="Use a session ID"):
            resolve_session("../../outside", saved.directory.parent)
    finally:
        saved.close()
    reopened = SavedSession.open(saved.info.id, saved.directory.parent)
    reopened.close()


def test_latest_is_scoped_to_the_given_workspace(tmp_path):
    root = tmp_path / "sessions"
    here, elsewhere = tmp_path / "here", tmp_path / "elsewhere"
    here.mkdir()
    elsewhere.mkdir()
    mine = SavedSession.create("test:local", here, root)
    mine.close()
    # Newer, but belongs to another checkout.
    other = SavedSession.create("test:local", elsewhere, root)
    other.close()

    assert resolve_session("latest", root).name == other.info.id
    assert resolve_session("latest", root, here).name == mine.info.id
    assert resolve_session("latest", root, elsewhere).name == other.info.id
    with pytest.raises(SessionError, match="No saved session for"):
        resolve_session("latest", root, tmp_path)


@pytest.mark.parametrize("tail", [b'{"kind":"TextDelta","text":"torn', b'{"text":"\xf0\x9f'])
def test_torn_journal_does_not_hide_future_events(tmp_path, tail):
    saved = SavedSession.create("test:local", tmp_path, tmp_path / "sessions")
    try:
        saved.event(Message("before crash"))
        with (saved.directory / "transcript.jsonl").open("ab") as file:
            file.write(tail)
        saved.event(Message("after restart"))
        assert [r["markdown"] for r in saved.transcript_records()] == [
            "before crash",
            "after restart",
        ]
    finally:
        saved.close()


def test_new_session_preserves_old_one(tmp_path):
    async def model(messages, info):
        yield "answer"

    saved = SavedSession.create("test:local", tmp_path, tmp_path / "sessions")
    runtime = AgentRuntime(Agent(FunctionModel(stream_function=model)), saved)
    identity = saved.info.id
    runtime.reset()
    try:
        assert runtime.session is None
        assert runtime.history == []
        assert len(list_sessions(saved.directory.parent)) == 1
        reopened = SavedSession.open(identity, saved.directory.parent)
        reopened.close()

        async def submit():
            return [event async for event in runtime.stream("new question")]

        asyncio.run(submit())
        assert runtime.session.info.id != identity
        assert runtime.conversation_id == runtime.session.info.id
        assert len(list_sessions(saved.directory.parent)) == 2
    finally:
        runtime.close()


def test_diagnostics_redact_provider_secrets_and_keep_failure_reason(monkeypatch):
    monkeypatch.setenv("EXAMPLE_TOKEN", "example-token-with-sensitive-value")
    error = ModelHTTPError(
        400,
        "example",
        body={
            "error": {
                "message": (
                    "Bad parameter; Bearer example-token-with-sensitive-value; refresh_token=hidden"
                ),
                "code": "invalid_parameter",
                "param": "prompt_cache_breakpoint",
                "headers": {"Authorization": "must not be recorded"},
            }
        },
    )
    detail = error_details(error)
    encoded = json.dumps(detail)
    assert "example-token-with-sensitive-value" not in encoded
    assert "hidden" not in encoded
    assert "must not be recorded" not in encoded
    assert detail["provider_param"] == "prompt_cache_breakpoint"


@pytest.mark.parametrize(
    "tool_name",
    [
        "read_file",
        "list_directory",
        "search_files",
        "find_files",
        "file_info",
        "read_tool_result",
        "write_file",
        "edit_file",
        "run_command",
        "unknown_tool",
    ],
)
@pytest.mark.parametrize("checkpoint_in_prior_run", [False, True])
@pytest.mark.parametrize("journaled", [False, True])
def test_resume_abandons_interrupted_tools_without_replaying(
    tmp_path, tool_name, checkpoint_in_prior_run, journaled
):
    saved = SavedSession.create("test:local", tmp_path, tmp_path / "sessions")

    async def run():
        if journaled:
            saved.append("turn_started", run_id="prior", prompt="prior turn")
            saved.append("turn_completed")
            saved.append("turn_started", run_id="crashed", prompt="interrupted turn")
        history = [ModelRequest(parts=[UserPromptPart("saved checkpoint")])]
        await saved.store.register_run(RunRecord(run_id="prior", conversation_id=saved.info.id))
        await saved.store.register_run(RunRecord(run_id="crashed", conversation_id=saved.info.id))
        await saved.store.save_snapshot(
            ContinuableSnapshot(
                run_id="prior" if checkpoint_in_prior_run else "crashed",
                step_index=0,
                messages=history,
            )
        )
        await saved.store.save_snapshot(
            ContinuableSnapshot(
                run_id="crashed",
                step_index=1,
                state="interrupted",
                messages=[
                    *history,
                    ModelResponse(parts=[ToolCallPart(tool_name, {}, "call-1")]),
                ],
            )
        )
        await saved.store.record_tool_effect(
            ToolEffectRecord(
                run_id="crashed",
                tool_call_id="call-1",
                tool_name=tool_name,
                status="started",
            )
        )
        assert await saved.recover() == history
        # Recovery must not rewrite the historical effect as successful.
        effects = await saved.store.list_unresolved_tool_effects(run_id="crashed")
        assert len(effects) == 1
        assert effects[0].status == "started"
        # Mixed batches also resume without rewriting any unresolved effects.
        await saved.store.record_tool_effect(
            ToolEffectRecord(
                run_id="crashed",
                tool_call_id="write-1",
                tool_name="write_file",
                status="started",
            )
        )
        assert await saved.recover() == history
        effects = await saved.store.list_unresolved_tool_effects(run_id="crashed")
        assert {effect.tool_call_id: effect.status for effect in effects} == {
            "call-1": "started",
            "write-1": "started",
        }

    try:
        asyncio.run(run())
    finally:
        saved.close()


def test_reopened_session_continues_without_replaying_interrupted_write(tmp_path):
    saved = SavedSession.create("test:local", tmp_path, tmp_path / "sessions")
    requests = []
    tool_calls = []

    async def model(messages, info):
        requests.append(messages)
        yield "Ready to continue."

    agent = Agent(FunctionModel(stream_function=model))

    @agent.tool_plain
    def write_file(path: str, content: str) -> str:
        tool_calls.append(path)
        return "written"

    async def run():
        runtime = AgentRuntime(agent, saved)
        _ = [event async for event in runtime.stream("first question")]
        history = list(runtime.history)
        await saved.store.register_run(RunRecord(run_id="crashed", conversation_id=saved.info.id))
        saved.append("turn_started", run_id="crashed", prompt="interrupted write")
        await saved.store.save_snapshot(
            ContinuableSnapshot(
                run_id="crashed",
                step_index=0,
                state="interrupted",
                messages=[
                    *history,
                    ModelResponse(
                        parts=[
                            ToolCallPart(
                                "write_file", {"path": "file.txt", "content": "test"}, "write-1"
                            )
                        ]
                    ),
                ],
            )
        )
        effect = ToolEffectRecord(
            run_id="crashed", tool_call_id="write-1", tool_name="write_file", status="started"
        )
        await saved.store.record_tool_effect(effect)
        runtime.close()
        reopened = SavedSession.open(saved.info.id, saved.directory.parent)
        restored = AgentRuntime(agent, reopened)
        try:
            await restored.restore()
            assert restored.history == history
            events = [event async for event in restored.stream("Continue")]
            assert Message("Ready to continue.") in events
            assert len(requests) == 2
            assert not any(
                isinstance(part, ToolCallPart) for message in requests[-1] for part in message.parts
            )
            assert tool_calls == []
            assert (
                await reopened.store.get_tool_effect(run_id="crashed", tool_call_id="write-1")
                == effect
            )
        finally:
            restored.close()

    try:
        asyncio.run(run())
    finally:
        saved.close()


@pytest.mark.parametrize("outcome", ["done", "error", "cancel"])
def test_thinking_persists_and_replays_after_reopen_even_when_hidden(tmp_path, outcome):
    from io import StringIO

    from pydantic_ai.models.function import DeltaThinkingPart
    from rich.console import Console

    from pcode.app import PreviewApp

    root = tmp_path / "sessions"
    saved = SavedSession.create("test:local", tmp_path, root)
    identity = saved.info.id
    # More than the old 8-KB live preview limit, with a distinctive beginning/end.
    thought = "FIRST_THOUGHT\n" + "thinking text\n" * 1000 + "LAST_THOUGHT"

    async def model(messages, info):
        yield {0: DeltaThinkingPart(content=thought[:100])}
        yield {0: DeltaThinkingPart(content=thought[100:], signature="OPAQUE_SIGNATURE")}
        if outcome == "error":
            raise RuntimeError("deliberate model failure")
        if outcome == "cancel":
            raise asyncio.CancelledError
        yield "Public answer"

    async def run():
        agent = Agent(FunctionModel(stream_function=model))
        runtime = AgentRuntime(agent, saved)
        try:
            try:
                _ = [event async for event in runtime.stream("Question")]
            except RuntimeError, asyncio.CancelledError:
                assert outcome != "done"
        finally:
            runtime.close()
        reopened = SavedSession.open(identity, root)
        runtime = AgentRuntime(agent, reopened)
        try:
            records = list(reopened.transcript_records())
            thinking = [r for r in records if r["kind"] in ("Thinking", "thinking_partial")]
            assert len(thinking) == 1
            assert thinking[0]["text"] == thought
            assert "OPAQUE_SIGNATURE" not in repr(list(reopened.records()))
            app = PreviewApp(model="test:local", runtime=runtime, console=Console(file=StringIO()))
            app.activity.thinking_mode = "off"
            app.replay()
            assert "FIRST_THOUGHT" not in repr(app.transcript.replay())
            app.set_thinking_mode("scrollback")
            replay = repr(app.transcript.replay())
            assert replay.count("FIRST_THOUGHT") == 1
            assert replay.count("LAST_THOUGHT") == 1
            app.set_thinking_mode("off")
            assert "FIRST_THOUGHT" not in repr(app.transcript.replay())
        finally:
            runtime.close()

    asyncio.run(run())


def test_thinking_completion_does_not_repeat_deltas_across_interleaved_tool_records():
    from types import SimpleNamespace

    records = [
        {"kind": "ThinkingDelta", "text": "first"},
        {"kind": "ToolSummary", "name": "read_file"},
        {"kind": "ThinkingDelta", "text": " second"},
        {"kind": "Thinking", "text": "first second"},
        {"kind": "Message", "markdown": "Answer"},
        {"kind": "ThinkingDelta", "text": "interrupted"},
        {"kind": "turn_cancelled"},
    ]
    saved = SimpleNamespace(active_records=lambda end=None: iter(records))
    assert list(SavedSession.transcript_records(saved)) == [
        {"kind": "thinking_partial", "text": "first"},
        {"kind": "ToolSummary", "name": "read_file"},
        {"kind": "thinking_partial", "text": " second"},
        {"kind": "Message", "markdown": "Answer"},
        {"kind": "thinking_partial", "text": "interrupted"},
        {"kind": "turn_cancelled"},
    ]


def test_resumed_history_draws_without_a_runtime(tmp_path):
    """The journal is a millisecond read; the provider stack behind the runtime is seconds."""
    from io import StringIO

    from rich.console import Console

    from pcode.app import PreviewApp

    root = tmp_path / "sessions"
    saved = SavedSession.create("test:local", tmp_path, root)
    identity = saved.info.id

    async def model(messages, info):
        yield "SAVED_ANSWER"

    async def run():
        runtime = AgentRuntime(Agent(FunctionModel(stream_function=model)), saved)
        try:
            _ = [event async for event in runtime.stream("SAVED_QUESTION")]
        finally:
            runtime.close()

    asyncio.run(run())
    reopened = SavedSession.open(identity, root)
    try:
        # No runtime at all: the offline preview stands in until the real one
        # finishes importing, exactly as it does during startup.
        app = PreviewApp(
            model="test:local",
            saved_session=reopened,
            resume=True,
            console=Console(file=StringIO(), width=80, color_system=None),
        )
        assert getattr(app.runtime, "session", None) is None
        app.replay(reopened)
        stream = StringIO()
        console = Console(file=stream, width=80, color_system=None)
        for objects, end, soft_wrap in app.transcript.replay():
            console.print(*objects, end=end, soft_wrap=soft_wrap)
        shown = stream.getvalue()
        assert "SAVED_QUESTION" in shown
        assert "SAVED_ANSWER" in shown
    finally:
        reopened.close()


def snapshot_rows(saved):
    with sqlite3.connect(saved.directory / "steps.sqlite3") as connection:
        return connection.execute("select count(*) from snapshots").fetchone()[0]


def test_steps_keep_a_bounded_number_of_checkpoints_per_turn(tmp_path):
    """Every step stores the whole history again; only the newest is ever read."""
    from pydantic_ai_harness.step_persistence import ContinuableSnapshot

    from pcode.sessions import SNAPSHOTS_PER_RUN

    saved = SavedSession.create("test:local", tmp_path, tmp_path / "sessions")
    try:

        async def write():
            for step in range(12):
                await saved.store.save_snapshot(
                    ContinuableSnapshot(
                        run_id="run-1",
                        step_index=step,
                        conversation_id=saved.info.id,
                        messages=[ModelRequest(parts=[UserPromptPart(content=f"step {step}")])],
                    )
                )
            return await saved.store.latest_snapshot(run_id="run-1")

        latest = asyncio.run(write())
        assert snapshot_rows(saved) <= SNAPSHOTS_PER_RUN + 1
        assert latest.messages[0].parts[0].content == "step 11"
    finally:
        saved.close()


def growing_history(turns):
    """A conversation as it stands after each turn, every one a prefix of the next."""
    history, states = [], []
    for turn in range(turns):
        history = [
            *history,
            ModelRequest(parts=[UserPromptPart(content=f"question {turn} " + "q" * 2_000)]),
            ModelResponse(parts=[ToolCallPart(tool_name="read_file", args={"path": f"{turn}.md"})]),
        ]
        states.append(history)
    return states


def test_snapshots_store_each_message_once_and_read_back_whole(tmp_path):
    """Every turn's checkpoint repeats the history before it; only new messages cost space."""
    saved = SavedSession.create("test:local", tmp_path, tmp_path / "sessions")
    states = growing_history(6)
    try:

        async def write_and_read():
            for turn, messages in enumerate(states):
                await saved.store.save_snapshot(
                    ContinuableSnapshot(
                        run_id=f"run-{turn}",
                        step_index=0,
                        conversation_id=saved.info.id,
                        messages=messages,
                    )
                )
            latest = [await saved.store.latest_snapshot(run_id=f"run-{t}") for t in range(6)]
            listed = await saved.store.list_snapshots(run_id="run-3")
            return latest, listed

        latest, listed = asyncio.run(write_and_read())
        assert [snapshot.messages for snapshot in latest] == states
        assert [snapshot.messages for snapshot in listed] == [states[3]]
        with sqlite3.connect(saved.directory / "steps.sqlite3") as connection:
            stored = connection.execute("select count(*) from snapshot_messages").fetchone()[0]
            rows = [text for (text,) in connection.execute("select messages from snapshots")]
        assert stored == len(states[-1])
        assert all(len(text) < 1_000 for text in rows)
    finally:
        saved.close()


def _legacy_rows(saved, states):
    """Rows as pcode stored them before Harness did: hashes, with bodies in `pcode_messages`."""
    import hashlib
    import zlib

    from pydantic_ai.messages import ModelMessagesTypeAdapter

    directory = saved.directory
    # The schema as a session already in use has it.
    asyncio.run(
        saved.store.save_snapshot(
            ContinuableSnapshot(run_id="seed", step_index=0, messages=[], conversation_id="x")
        )
    )
    saved.close()
    with sqlite3.connect(directory / "steps.sqlite3") as connection:
        connection.execute("delete from snapshots")
        connection.execute(
            "create table pcode_messages (hash text primary key, body blob not null) without rowid"
        )
        # A message nothing references any more, as a pruned checkpoint leaves.
        connection.execute("insert into pcode_messages values ('orphan', x'00')")
        for turn, messages in enumerate(states):
            hashes = []
            for message in json.loads(ModelMessagesTypeAdapter.dump_json(messages)):
                text = json.dumps(message, separators=(",", ":"), sort_keys=True)
                key = hashlib.blake2b(text.encode(), digest_size=16).hexdigest()
                hashes.append(key)
                connection.execute(
                    "insert or ignore into pcode_messages values (?, ?)",
                    (key, zlib.compress(text.encode())),
                )
            connection.execute(
                "insert into snapshots (run_id, step_index, timestamp, state, messages)"
                " values (?, 0, '2026-01-01T00:00:00+00:00', 'complete', ?)",
                (f"run-{turn}", json.dumps({"pcode_message_refs": hashes})),
            )


def test_a_session_pcode_stored_itself_moves_to_harness_and_reads_back(tmp_path):
    root = tmp_path / "sessions"
    saved = SavedSession.create("test:local", tmp_path, root)
    directory = saved.directory
    states = growing_history(4)
    _legacy_rows(saved, states)

    reopened = SavedSession.open(directory.name, root)
    try:

        async def read():
            return [await reopened.store.latest_snapshot(run_id=f"run-{t}") for t in range(4)]

        assert [snapshot.messages for snapshot in asyncio.run(read())] == states
        with sqlite3.connect(directory / "steps.sqlite3") as connection:
            tables = {name for (name,) in connection.execute("select name from sqlite_master")}
            stored = connection.execute("select count(*) from snapshot_messages").fetchone()[0]
        assert "pcode_messages" not in tables
        assert stored == len(states[-1])
    finally:
        reopened.close()


def test_a_legacy_store_missing_a_message_is_left_untouched_and_still_opens(tmp_path):
    root = tmp_path / "sessions"
    saved = SavedSession.create("test:local", tmp_path, root)
    directory = saved.directory
    _legacy_rows(saved, growing_history(2))
    with sqlite3.connect(directory / "steps.sqlite3") as connection:
        connection.execute("delete from pcode_messages where hash != 'orphan'")
        before = connection.execute("select seq, messages from snapshots").fetchall()
    reopened = SavedSession.open(directory.name, root)
    try:
        with pytest.raises(ValueError):
            asyncio.run(reopened.store.latest_snapshot(run_id="run-0"))
    finally:
        reopened.close()
    with sqlite3.connect(directory / "steps.sqlite3") as connection:
        assert connection.execute("select seq, messages from snapshots").fetchall() == before
        assert connection.execute("select count(*) from pcode_messages").fetchone()[0] == 1


def test_compacting_stores_whole_history_rows_once_and_keeps_them_readable(tmp_path):
    from pydantic_ai.messages import ModelMessagesTypeAdapter

    from pcode.sessions import PrivateStepStore, compact_snapshots

    root = tmp_path / "sessions"
    saved = SavedSession.create("test:local", tmp_path, root)
    directory = saved.directory
    states = growing_history(30)
    # Rows as pcode wrote them before messages were stored once: each one the
    # whole history, written past the store as an older version would have.
    asyncio.run(
        saved.store.save_snapshot(
            ContinuableSnapshot(run_id="seed", step_index=0, messages=[], conversation_id="x")
        )
    )
    saved.close()
    with sqlite3.connect(directory / "steps.sqlite3") as connection:
        connection.execute("delete from snapshots")
        for turn, messages in enumerate(states[:-1]):
            connection.execute(
                "insert into snapshots (run_id, step_index, timestamp, state, messages)"
                " values (?, 0, '2026-01-01T00:00:00+00:00', 'complete', ?)",
                (f"run-{turn}", ModelMessagesTypeAdapter.dump_json(messages).decode()),
            )
    # The newest turn written by this version, so one store mixes both forms.
    store = PrivateStepStore(database=directory / "steps.sqlite3")
    asyncio.run(
        store.save_snapshot(
            ContinuableSnapshot(
                run_id=f"run-{len(states) - 1}",
                step_index=0,
                conversation_id="x",
                messages=states[-1],
            )
        )
    )

    async def read():
        return [await store.latest_snapshot(run_id=f"run-{t}") for t in range(len(states))]

    def stored_hashes():
        with sqlite3.connect(directory / "steps.sqlite3") as connection:
            return {key for (key,) in connection.execute("select sha256 from snapshot_messages")}

    before, after = compact_snapshots(directory)
    assert after < before / 4
    hashes = stored_hashes()
    assert len(hashes) == len(states[-1])
    assert [snapshot.messages for snapshot in asyncio.run(read())] == states

    # A second pass reads every row as references and must keep what they name.
    compact_snapshots(directory)
    assert stored_hashes() == hashes
    assert [snapshot.messages for snapshot in asyncio.run(read())] == states


def test_compacting_an_old_session_frees_space_and_keeps_what_resume_reads(tmp_path):
    from pydantic_ai_harness.step_persistence import ContinuableSnapshot

    from pcode.sessions import compact_snapshots

    root = tmp_path / "sessions"
    saved = SavedSession.create("test:local", tmp_path, root)
    directory, identity = saved.directory, saved.info.id
    # One write through the store creates the schema; the rest go in behind it
    # to stand in for an unbounded store, as every session predating the bound.
    asyncio.run(
        saved.store.save_snapshot(
            ContinuableSnapshot(run_id="run-0", step_index=0, messages=[], conversation_id=identity)
        )
    )
    bulky = "x" * 20_000
    with sqlite3.connect(directory / "steps.sqlite3") as connection:
        for run in ("run-1", "run-2"):
            for step in range(40):
                connection.execute(
                    "insert into snapshots (run_id, step_index, timestamp, state, messages)"
                    " values (?, ?, '2026-01-01T00:00:00Z', 'complete', ?)",
                    (run, step, f'{{"step": {step}, "pad": "{bulky}"}}'),
                )
    saved.close()

    # A session held open elsewhere is left alone.
    reopened = SavedSession.open(identity, root)
    try:
        held_before, held_after = compact_snapshots(directory)
        assert held_before == held_after
    finally:
        reopened.close()

    before, after = compact_snapshots(directory)
    assert after < before / 2
    with sqlite3.connect(directory / "steps.sqlite3") as connection:
        rows = connection.execute(
            "select run_id, max(step_index) from snapshots group by run_id"
        ).fetchall()
    # Both turns still restore, from the step they actually settled at.
    assert rows == [("run-0", 0), ("run-1", 39), ("run-2", 39)]


def test_compacted_session_still_resumes_and_navigates_to_an_earlier_turn(tmp_path):
    from pcode.sessions import compact_snapshots

    root = tmp_path / "sessions"
    saved = SavedSession.create("test:local", tmp_path, root)
    (tmp_path / "README.md").write_text("a test repository\n")
    calls = 0

    async def model(messages, info):
        nonlocal calls
        calls += 1
        # A tool per turn settles extra steps, so a turn holds several snapshots.
        if not [p for m in messages for p in m.parts if isinstance(p, ToolReturnPart)]:
            yield {0: DeltaToolCall(name="read_file", json_args='{"path":"README.md"}')}
        else:
            yield f"Answer {calls}."

    runtime = AgentRuntime(
        Agent(
            FunctionModel(stream_function=model), capabilities=[LocalWorkspace(tmp_path), Coder()]
        ),
        saved,
    )

    async def run():
        for prompt in ("First question", "Second question"):
            async for _ in runtime.stream(prompt):
                pass
        expected = deepcopy(runtime.history)
        first, second = list(runtime.tree.nodes)
        identity, directory = saved.info.id, saved.directory
        # Stand in for a session written before the per-turn bound existed. They
        # are interrupted steps, the case where the retain set widens to keep
        # each turn's newest settled one as well.
        with sqlite3.connect(directory / "steps.sqlite3") as connection:
            for run in (first, second):
                for step in range(20):
                    connection.execute(
                        "insert into snapshots (run_id, step_index, timestamp, state, messages)"
                        " values (?, ?, '2026-01-01T00:00:00Z', 'interrupted', '[]')",
                        (run, 100 + step),
                    )
        before_rows = snapshot_rows(saved)
        runtime.close()

        assert compact_snapshots(directory) != (0, 0)

        reopened = SavedSession.open(identity, root)
        # Space freed is page-granular and a small store may not shrink; rows do.
        assert snapshot_rows(reopened) < before_rows
        resumed = AgentRuntime(runtime.agent, reopened)
        try:
            # --continue / /resume: the newest turn's settled history comes back.
            await resumed.restore()
            assert resumed.history == expected
            # /tree back to the first turn, which keeps its own newest checkpoint.
            await resumed.navigate(first)
            assert resumed.history and resumed.history != expected
            await resumed.navigate(second)
            assert resumed.history == expected
            async for _ in resumed.stream("Third question"):
                pass
            assert "Answer" in str(resumed.history[-1])
        finally:
            resumed.close()

    try:
        asyncio.run(run())
    finally:
        saved.close()


def test_sessions_compact_reports_space_and_needs_the_listing(tmp_path, monkeypatch, capsys):
    import sys

    from pcode.app import main

    root = tmp_path / "sessions"
    saved = SavedSession.create("test:local", tmp_path, root)
    saved.close()
    monkeypatch.setattr(
        sys, "argv", ["pcode", "--sessions", "--compact", "--session-dir", str(root)]
    )
    main()
    # Long temp paths wrap the line, so compare words, not layout.
    assert "MB freed" in " ".join(capsys.readouterr().out.split())

    monkeypatch.setattr(sys, "argv", ["pcode", "--compact"])
    with pytest.raises(SystemExit) as raised:
        main()
    assert raised.value.code == 2


def latest_prompt(messages) -> str:
    return next(
        part.content
        for message in reversed(messages)
        if isinstance(message, ModelRequest)
        for part in message.parts
        if isinstance(part, UserPromptPart)
    )


def test_opening_a_busy_session_continues_a_copy_from_its_last_safe_step(tmp_path):
    """`--continue` and `/resume` on a session mid-turn elsewhere get a copy, not a refusal."""
    root = tmp_path / "sessions"
    (tmp_path / "README.md").write_text("a test repository\n")
    saved = SavedSession.create("test:local", tmp_path, root)
    identity = saved.info.id
    calls = []

    async def run():
        blocked, release = asyncio.Event(), asyncio.Event()

        async def model(messages, info):
            prompt = latest_prompt(messages)
            returned = any(isinstance(part, ToolReturnPart) for part in messages[-1].parts)
            calls.append((prompt, returned))
            if prompt == "Second question" and not returned:
                # A settled tool step, so the running turn has a mid-turn checkpoint.
                yield {0: DeltaToolCall(name="read_file", json_args='{"path":"README.md"}')}
                return
            if prompt == "Second question" and not blocked.is_set():
                blocked.set()  # Only the source blocks; the copy answers straight away.
                await release.wait()
            yield f"Answer to {prompt}."

        agent = Agent(
            FunctionModel(stream_function=model), capabilities=[LocalWorkspace(tmp_path), Coder()]
        )
        runtime = AgentRuntime(agent, saved)
        _ = [event async for event in runtime.stream("First question")]
        settled = deepcopy(runtime.history)
        (first,) = runtime.tree.nodes

        async def second():
            return [event async for event in runtime.stream("Second question")]

        running = asyncio.create_task(second())
        await blocked.wait()
        before = (saved.directory / "transcript.jsonl").read_bytes()

        with pytest.raises(SessionBusy):
            SavedSession.open(identity, root)
        copy = SavedSession.open(identity[:8], root, fork_if_open=True)
        try:
            assert copy.forked_from == identity
            assert copy.info.id != identity
            assert copy.info.workspace == saved.info.workspace
            assert {info.id for info in list_sessions(root)} == {identity, copy.info.id}
            # The running turn is copied as a crash leaves it: active, from its
            # last settled step, which is past the tool it already ran.
            running_turn = copy.tree.active
            assert copy.tree.nodes[running_turn].parent == first
            assert copy.tree.nodes[running_turn].status == "interrupted"
            recovered = await copy.recover()
            assert recovered[: len(settled)] == settled
            assert any(isinstance(part, ToolReturnPart) for part in recovered[-1].parts)

            resumed = AgentRuntime(agent, copy)
            await resumed.restore()
            start = len(calls)
            events = [event async for event in resumed.stream(None)]  # /continue
            assert any(
                isinstance(event, Message) and event.markdown == "Answer to Second question."
                for event in events
            )
            # It carried on after the tool rather than running it again.
            assert calls[start:] == [("Second question", True)]
            # The last finished turn is still one /tree selection away.
            await resumed.navigate(first)
            assert resumed.history == settled
        finally:
            copy.close()

        # The source was only read, never written.
        assert (saved.directory / "transcript.jsonl").read_bytes() == before
        release.set()
        await running
        runtime.close()
        reopened = SavedSession.open(identity, root)
        try:
            assert len(reopened.tree.nodes) == 2  # Nothing from the copy.
        finally:
            reopened.close()

    asyncio.run(run())


def test_a_busy_first_turn_with_no_settled_step_copies_its_prompt_for_resend(tmp_path):
    root = tmp_path / "sessions"
    saved = SavedSession.create("test:local", tmp_path, root)
    try:
        saved.append("turn_started", prompt="Still running", run_id="r1", parent_id=None)
        copy = SavedSession.open(saved.info.id, root, fork_if_open=True)
        try:
            assert asyncio.run(copy.recover()) == []
            runtime = AgentRuntime(Agent("test"), copy)
            assert runtime.resend_prompt() == "Still running"
        finally:
            copy.close()
    finally:
        saved.close()
