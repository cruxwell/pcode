"""Context rewrites must survive restart/checkout without replaying tool effects."""

import asyncio
from copy import deepcopy

import pytest
from pydantic_ai import Agent
from pydantic_ai.exceptions import ModelHTTPError, UnexpectedModelBehavior
from pydantic_ai.messages import (
    ModelRequest,
    ModelResponse,
    SystemPromptPart,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models.function import DeltaToolCall, FunctionModel
from pydantic_ai.usage import RequestUsage, RunUsage
from pydantic_ai_harness.compaction import ClearToolResults
from pydantic_ai_harness.planning import PlanItem
from pydantic_ai_harness.step_persistence import ContinuableSnapshot, is_provider_valid

from pcode.agent import create_coder
from pcode.compaction import (
    MARKER,
    CompactionError,
    auto_compaction_threshold,
    context_estimate,
    effective_window,
    summarize,
)
from pcode.context_usage import context_label
from pcode.live import AgentRuntime
from pcode.preferences import parse_token_count
from pcode.sessions import SavedSession

SUMMARY = "## Goal and constraints\nFix auth.\n## Verification\nTests failed; not yet fixed."


@pytest.mark.parametrize(
    "window,max_output,expected",
    [
        # 16_384 reserve is >10% of a 128k window, so it (not 90%) binds:
        # 128_000 - 16_384 = 111_616, below the ~115.2k that 90% would allow.
        (128_000, 4_096, 111_616),
        # For larger windows the 16_384 floor reserve is under 10%, so 90%
        # of the window becomes the binding constraint.
        (200_000, 4_096, 180_000),
        (256_000, 4_096, 230_400),
        # 1M windows must use ~90% (900k), not the old ~80% (800k).
        (1_000_000, 4_096, 900_000),
        # A large resolved output budget pushes the reserve above the 16_384
        # floor, so the output ceiling becomes the binding constraint.
        (128_000, 32_000, 96_000),
    ],
)
def test_auto_compaction_threshold_uses_ninety_percent_unless_reserve_binds(
    window, max_output, expected
):
    assert auto_compaction_threshold(window, max_output) == expected


def test_auto_compaction_threshold_small_window_reserve_stays_proportional():
    # window // 5 caps the reserve well below window // 2 for small windows,
    # so a tiny window still leaves most of itself usable, and the reserve
    # can never consume more than half the window.
    window = 16_000
    threshold = auto_compaction_threshold(window, max_output_tokens=0)
    assert threshold == min(int(window * 0.9), window - window // 5)
    assert window - threshold <= window // 2


@pytest.mark.parametrize(
    "text,tokens", [("200k", 200_000), ("1.5M", 1_500_000), ("150_000", 150_000), ("50k", 50_000)]
)
def test_parse_token_count(text, tokens):
    assert parse_token_count(text) == tokens


@pytest.mark.parametrize("text", ["", "k", "abc", "49k", "-200k", "nan", "inf"])
def test_parse_token_count_rejects(text):
    with pytest.raises(ValueError):
        parse_token_count(text)


def history():
    return [
        ModelRequest(
            parts=[SystemPromptPart("Do not commit secrets."), UserPromptPart("Fix auth")]
        ),
        ModelResponse(parts=[ToolCallPart("read_file", {"path": "auth.py"}, "read-1")]),
        ModelRequest(parts=[ToolReturnPart("read_file", "important failure " * 6000, "read-1")]),
        ModelResponse(parts=[TextPart("Investigating " * 6000)]),
        ModelRequest(parts=[UserPromptPart("Preserve test failures.")]),
        ModelResponse(parts=[TextPart("Still working")], usage=RequestUsage(input_tokens=50000)),
    ]


def summary_model(calls, *, output=SUMMARY):
    async def stream(messages, info):
        calls.append((deepcopy(messages), info))
        yield output

    return FunctionModel(stream_function=stream)


def test_summarizer_is_tool_free_focused_incremental_and_pair_safe(monkeypatch):
    monkeypatch.setenv("PCODE_CONTEXT_WINDOW", "100000")

    async def run():
        calls = []
        source = history()
        original = deepcopy(source)
        usage = RunUsage()
        result = await summarize(
            source, model=summary_model(calls), focus="Keep {literal} test failures", usage=usage
        )
        assert result.changed and result.after < result.before
        assert source == original
        assert is_provider_valid(result.messages)
        assert usage.requests == 1
        assert not calls[0][1].function_tools
        prompt = calls[0][0][-1].parts[0].content
        assert "Keep {literal} test failures" in prompt
        assert "important failure" in prompt
        assert "Do not commit secrets." in str(result.messages)
        assert "Still working" in str(result.messages)
        assert context_estimate(result.messages) == result.after
        assert "~" in context_label("test:local", result.messages)
        # Recompaction anchors the previous summary instead of erasing it.
        second_source = result.messages + history()[1:]
        second = await summarize(second_source, model=summary_model(calls))
        assert second.changed
        assert "<previous-summary>" in calls[-1][0][-1].parts[0].content
        assert SUMMARY in calls[-1][0][-1].parts[0].content
        # A genuine later response replaces the estimate with measured usage.
        second.messages.append(
            ModelResponse(parts=[TextPart("ok")], usage=RequestUsage(input_tokens=99))
        )
        assert context_label("test:local", second.messages) == " · 99/100k"

    asyncio.run(run())


# How Harness before 0.66 stored a summary, which saved histories still hold.
LEGACY_SUMMARY_PREFIX = "Summary of previous conversation:\n\n"


@pytest.mark.parametrize("merged", [True, False])
def test_recompaction_updates_the_newest_summary_and_keeps_only_its_own(monkeypatch, merged):
    from pydantic_ai.messages import CompactionPart

    monkeypatch.setenv("PCODE_CONTEXT_WINDOW", "100000")
    newest = SystemPromptPart(LEGACY_SUMMARY_PREFIX + "newest summary")
    oldest = SystemPromptPart(LEGACY_SUMMARY_PREFIX + "oldest summary")
    first = UserPromptPart("Fix auth")
    # A compaction leaves its summary in a request of its own; pydantic-ai
    # merges that into the kept first user request on the next run.
    if merged:
        start = [ModelRequest(parts=[newest, oldest, first])]
    else:
        start = [ModelRequest(parts=[newest]), ModelRequest(parts=[oldest, first])]

    async def run():
        calls = []
        source = [*start, *history()[1:]]
        result = await summarize(source, model=summary_model(calls))
        previous = calls[-1][0][-1].parts[0].content.split("<previous-summary>")[-1]
        assert "newest summary" in previous
        assert "oldest summary" not in calls[-1][0][-1].parts[0].content
        parts = [part for message in result.messages for part in message.parts]
        # The legacy summaries are replaced by one summary in Harness's own form.
        assert [p.content for p in parts if isinstance(p, CompactionPart)] == [SUMMARY.strip()]
        assert not any(
            isinstance(p, SystemPromptPart) and p.content.startswith(LEGACY_SUMMARY_PREFIX)
            for p in parts
        )
        assert "Fix auth" in str(result.messages)
        assert is_provider_valid(result.messages)

    asyncio.run(run())


def test_unmeasured_history_tokens_are_not_carried_forward_as_overhead(monkeypatch):
    from pydantic_ai_harness.compaction import estimate_token_count

    from pcode.compaction import summary_index

    monkeypatch.setenv("PCODE_CONTEXT_WINDOW", "1000000")

    async def run():
        source = history()
        # The provider billed four times what the character heuristic sees, as
        # with thinking whose signature is billed but whose text is summarized.
        source[-1].usage = RequestUsage(input_tokens=4 * estimate_token_count(source))
        result = await summarize(source, model=summary_model([]))
        assert result.changed
        # The kept tail is scaled by the measured density; the fresh summary
        # is not, and the ~150k gap is not padded on as overhead.
        at = summary_index(result.messages)
        summary = result.messages[at : at + 1]
        kept = result.messages[:at] + result.messages[at + 1 :]
        assert result.after == estimate_token_count(summary) + 4 * estimate_token_count(kept)

    asyncio.run(run())


def test_media_in_the_kept_tail_is_counted_apart_from_text_density(monkeypatch):
    from pydantic_ai.messages import BinaryContent

    from pcode.compaction import MEDIA_TOKENS, summary_index, text_and_media

    monkeypatch.setenv("PCODE_CONTEXT_WINDOW", "1000000")
    shot = BinaryContent(data=b"\x89PNG" + b"x" * 400_000, media_type="image/png")

    async def run():
        # Two images the heuristic counts as nothing, in a history the
        # provider measured at its text plus those images.
        source = [
            *history(),
            ModelRequest(parts=[UserPromptPart(["Why this layout?", shot, shot])]),
            ModelResponse(parts=[TextPart("Looked")]),
        ]
        text, media = text_and_media(source)
        assert media == 2
        source[-1].usage = RequestUsage(input_tokens=text + 2 * MEDIA_TOKENS)
        result = await summarize(source, model=summary_model([]))
        assert "Why this layout?" in str(result.messages[summary_index(result.messages) + 1 :])
        # The same history without the images: they add their own count and
        # nothing else, rather than inflating the text's measured density.
        plain = deepcopy(source)
        plain[-2].parts[0].content = ["Why this layout?"]
        plain[-1].usage = RequestUsage(input_tokens=text)
        without = await summarize(plain, model=summary_model([]))
        assert result.after - without.after == 2 * MEDIA_TOKENS
        # A tool return's image bytes are not read as text either.
        returned = ModelRequest(parts=[ToolReturnPart("screenshot", ["page", shot], "s")])
        tokens, media = text_and_media([returned])
        assert tokens < 10 and media == 1

    asyncio.run(run())


def test_oversized_summary_request_retries_with_tighter_caps(monkeypatch):
    """Nothing upstream bounds the summary request, so it can be too large itself.

    Compaction would then fail at the one moment it has to succeed: when history
    no longer fits. The retry must shrink what it sends, not just try again.
    """
    monkeypatch.setenv("PCODE_CONTEXT_WINDOW", "100000")
    prompts = []

    async def stream(messages, info):
        prompt = messages[-1].parts[0].content
        prompts.append(prompt)
        if len(prompt) > 20_000:
            raise ModelHTTPError(400, "test", body={"error": {"message": "prompt is too long"}})
        yield SUMMARY

    async def run():
        # A long run of older work, so what is summarized is too big on its own.
        source = history()
        source[1:1] = [
            ModelResponse(parts=[TextPart("Earlier work " * 6000)]),
            ModelRequest(parts=[UserPromptPart("Continue.")]),
        ]
        result = await summarize(source, model=FunctionModel(stream_function=stream))
        assert result.changed
        summary = [
            part.content
            for message in result.messages
            for part in message.parts
            if isinstance(getattr(part, "content", None), str) and SUMMARY in part.content
        ]
        assert summary, "the retry must still produce a real summary"

    asyncio.run(run())
    # The first attempt is rejected for size; a later one sends strictly less.
    assert len(prompts) > 1
    assert len(prompts[-1]) < len(prompts[0])
    assert len(prompts[-1]) <= 20_000


def test_summary_failure_that_is_not_about_size_still_surfaces(monkeypatch):
    """The fallback chain must not turn a real provider outage into silence."""
    monkeypatch.setenv("PCODE_CONTEXT_WINDOW", "100000")
    attempts = 0

    async def stream(messages, info):
        nonlocal attempts
        attempts += 1
        raise ModelHTTPError(503, "test", body={"error": {"message": "upstream down"}})
        yield ""  # pragma: no cover - generator protocol only

    async def run():
        with pytest.raises(ModelHTTPError):
            await summarize(history(), model=FunctionModel(stream_function=stream))

    asyncio.run(run())
    assert attempts == 3, "every tightening step is tried before giving up"


def test_small_history_is_noop_without_model_request():
    async def run():
        calls = []
        source = [ModelRequest(parts=[UserPromptPart("Hello")])]
        result = await summarize(source, model=summary_model(calls))
        assert not result.changed
        assert not calls

    asyncio.run(run())


@pytest.mark.parametrize("output", ["", "Huge summary " * 30000])
def test_invalid_or_unhelpful_summary_does_not_replace_history(output):
    async def run():
        source = history()
        original = deepcopy(source)
        with pytest.raises((CompactionError, UnexpectedModelBehavior)):
            await summarize(source, model=summary_model([], output=output))
        assert source == original

    asyncio.run(run())


@pytest.mark.parametrize("saved", [False, True])
def test_manual_checkpoint_preserves_plan_branches_restart_and_failed_next_turn(tmp_path, saved):
    async def run():
        calls = []
        session = (
            SavedSession.create("test:local", tmp_path, tmp_path / "sessions") if saved else None
        )
        runtime = AgentRuntime(Agent(summary_model(calls)), session)
        original = history()
        parent = "original"
        record = {"kind": "turn_started", "run_id": parent, "parent_id": None, "prompt": "Fix auth"}
        runtime.tree.consume(record)
        runtime.tree.consume({"kind": "turn_completed"})
        runtime.tree.nodes[parent].history = deepcopy(original)
        runtime.history = deepcopy(original)
        if session:
            session.append("turn_started", run_id=parent, parent_id=None, prompt="Fix auth")
            await session.store.save_snapshot(
                ContinuableSnapshot(run_id=parent, step_index=1, messages=original)
            )
            session.append("turn_completed", run_id=parent)
        plan = [PlanItem(content="Fix auth", status="in_progress", id="auth-task")]
        await runtime.plan_store.set_items(plan)
        result = await runtime.compact("Keep failures")
        compact_id = runtime.tree.active
        assert compact_id != parent
        assert runtime.tree.nodes[compact_id].kind == "compaction"
        assert ((compact_id, True), "") not in runtime.tree.rows()
        assert all(key != (compact_id, True) for key, _ in runtime.tree.rows())
        assert runtime.turns == 0  # A summary is not a user turn.
        assert runtime.input_tokens > 0
        compacted = deepcopy(result.messages)
        await runtime.navigate(parent)
        assert runtime.history == original
        await runtime.navigate(compact_id)
        assert runtime.history == compacted
        assert (await runtime.plan_store.get_items())[0].id == "auth-task"
        assert len(calls) == 1  # Navigation never calls models or tools.
        if session:
            identity, root = session.info.id, session.directory.parent
            runtime.close()
            reopened = SavedSession.open(identity, root)
            runtime = AgentRuntime(Agent(summary_model(calls)), reopened)
            await runtime.restore()
            assert runtime.history == compacted
            assert (await runtime.plan_store.get_items())[0].id == "auth-task"

        failed_inputs = []

        async def failing(messages, info):
            failed_inputs.append(deepcopy(messages))
            raise RuntimeError("provider down")
            yield "unreachable"

        runtime.replace_agent(Agent(FunctionModel(stream_function=failing)))
        with pytest.raises(RuntimeError, match="provider down"):
            async for _ in runtime.stream("Continue"):
                pass
        # Keep the new request for /continue, but never the failed response.
        assert runtime.history == failed_inputs[0]
        assert runtime.history[-1].parts[0].content == "Continue"
        await runtime.navigate(parent)
        assert runtime.history == original
        runtime.close()

    asyncio.run(run())


def test_manual_cancellation_and_persistence_failure_leave_context_selected(tmp_path, monkeypatch):
    async def run():
        started = asyncio.Event()

        async def slow(messages, info):
            started.set()
            await asyncio.Event().wait()
            yield SUMMARY

        session = SavedSession.create("test:local", tmp_path, tmp_path / "sessions")
        runtime = AgentRuntime(Agent(FunctionModel(stream_function=slow)), session)
        runtime.history = history()
        original = deepcopy(runtime.history)
        task = asyncio.create_task(runtime.compact())
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert runtime.history == original and runtime.tree.active is None
        runtime.replace_agent(Agent(summary_model([])))

        def fail(*args, **kwargs):
            raise OSError("disk full")

        monkeypatch.setattr(session, "append", fail)
        with pytest.raises(OSError, match="disk full"):
            await runtime.compact()
        assert runtime.history == original and runtime.tree.active is None
        runtime.close()

    asyncio.run(run())


def test_auto_compacts_inside_tool_loop_and_saves_before_next_request(tmp_path, monkeypatch):
    monkeypatch.setenv("PCODE_CONTEXT_WINDOW", "16000")

    async def run():
        summaries = []
        main_calls = []
        effects = []
        session = SavedSession.create("test:local", tmp_path, tmp_path / "sessions")

        async def stream(messages, info):
            if "context summarization assistant" in (info.instructions or ""):
                summaries.append(deepcopy(messages))
                assert not info.function_tools
                yield SUMMARY
            elif not summaries:
                # Reads until the history needs compacting. Harness keeps the
                # newest result verbatim, so only the older ones are summarized.
                main_calls.append(deepcopy(messages))
                call_id = f"tool-{len(main_calls)}"
                yield {0: DeltaToolCall(name="read", json_args="{}", tool_call_id=call_id)}
            else:
                main_calls.append(deepcopy(messages))
                assert "Tests failed; not yet fixed." in str(messages)
                assert is_provider_valid(messages)
                # Tracking sits inside compaction: the footer shows the compacted request.
                assert "Tests failed; not yet fixed." in str(runtime.context_history)
                assert any(MARKER in (m.metadata or {}) for m in runtime.context_history)
                snapshot = await session.store.latest_snapshot(run_id=runtime.tree.active)
                assert "Tests failed; not yet fixed." in str(snapshot.messages)
                raise RuntimeError("next request failed")

        agent = Agent(FunctionModel(stream_function=stream))

        @agent.tool_plain
        def read() -> str:
            effects.append("read")
            return "diagnostic result " * 1500

        runtime = AgentRuntime(agent, session)
        runtime.auto_compact = True
        notices = []
        runtime.compaction_notice = notices.append
        with pytest.raises(RuntimeError, match="next request failed"):
            async for _ in runtime.stream("Find auth bug"):
                pass
        # Two results pass the threshold; the first is summarized, the second kept.
        assert effects == ["read", "read"]
        assert len(summaries) == 1
        assert len(main_calls) == 3
        assert "Tests failed; not yet fixed." in str(runtime.history)
        assert any("automatically" in text for text in notices)
        runtime.close()

    asyncio.run(run())


@pytest.mark.parametrize("limit", [None, 50_000])
def test_token_cap_compacts_before_the_window_fills(monkeypatch, limit):
    monkeypatch.setenv("PCODE_CONTEXT_WINDOW", "1000000")

    windows = []
    real = summarize

    async def spy(*args, window=None, **kwargs):
        windows.append(window)
        return await real(*args, window=window, **kwargs)

    monkeypatch.setattr("pcode.compaction.summarize", spy)

    async def run():
        async def stream(messages, info):
            if "context summarization assistant" in (info.instructions or ""):
                yield SUMMARY
            else:
                yield "continuing"

        runtime = AgentRuntime(Agent(FunctionModel(stream_function=stream)))
        # Twice the cap: past it, but nowhere near 90% of the window.
        runtime.history = history() * 2
        runtime.auto_compact = True
        runtime.auto_compact_limit = limit
        async for _ in runtime.stream("Continue"):
            pass
        # Summary and kept history are budgeted from the cap, not the 1m window.
        assert windows == ([50_000] if limit else [])

    asyncio.run(run())


def test_autocompact_change_mid_turn_applies_to_the_next_request(monkeypatch):
    monkeypatch.setenv("PCODE_CONTEXT_WINDOW", "1000000")

    async def run():
        summaries = []

        async def stream(messages, info):
            if "context summarization assistant" in (info.instructions or ""):
                summaries.append(messages)
                yield SUMMARY
            elif not any(isinstance(p, ToolReturnPart) for p in messages[-1].parts):
                yield {0: DeltaToolCall(name="read", json_args="{}", tool_call_id="tool-1")}
            else:
                yield "done"

        agent = Agent(FunctionModel(stream_function=stream))

        @agent.tool_plain
        def read() -> str:
            # As `/autocompact 50k` typed while this tool runs would.
            runtime.auto_compact, runtime.auto_compact_limit = True, 50_000
            return "diagnostic result " * 1500

        runtime = AgentRuntime(agent)
        runtime.history = history() * 2
        runtime.auto_compact = False
        async for _ in runtime.stream("Continue"):
            pass
        assert len(summaries) == 1

    asyncio.run(run())


def test_window_override_and_unknown_policy(monkeypatch, tmp_path):
    monkeypatch.delenv("PCODE_CONTEXT_WINDOW", raising=False)
    assert effective_window("proxy:unknown") is None
    monkeypatch.setenv("PCODE_CONTEXT_WINDOW", "32000")
    assert effective_window("proxy:unknown") == 32000
    monkeypatch.setenv("PCODE_CONTEXT_WINDOW", "bogus")
    with pytest.raises(CompactionError, match="positive token"):
        effective_window("proxy:unknown")
    assert not any(isinstance(c, ClearToolResults) for c in create_coder(tmp_path).capabilities)


def test_marker_survives_suffix_growth():
    messages = history()
    messages[-1].metadata = {MARKER: {"tokens": 1234}}
    messages.append(ModelRequest(parts=[UserPromptPart("New input " * 100)]))
    assert 1234 < context_estimate(messages) < 50000


@pytest.mark.parametrize("cancel", [False, True])
def test_unsaved_auto_failure_retains_settled_tool_effects(monkeypatch, cancel):
    monkeypatch.setenv("PCODE_CONTEXT_WINDOW", "16000")

    async def run():
        started = asyncio.Event()
        effects = []
        main = []

        async def stream(messages, info):
            if "context summarization assistant" in (info.instructions or ""):
                started.set()
                if cancel:
                    await asyncio.Event().wait()
                raise CompactionError("summary service unavailable")
            main.append(messages)
            yield {0: DeltaToolCall(name="edit", json_args="{}", tool_call_id="edit-1")}

        agent = Agent(FunctionModel(stream_function=stream))

        @agent.tool_plain
        def edit():
            effects.append("changed file")
            return "edited file diagnostics " * 5000

        runtime = AgentRuntime(agent)
        runtime.auto_compact = True

        async def turn():
            async for _ in runtime.stream("Fix auth"):
                pass

        task = asyncio.create_task(turn())
        await started.wait()
        if cancel:
            task.cancel()
        with pytest.raises(asyncio.CancelledError if cancel else CompactionError):
            await task
        assert effects == ["changed file"]
        assert "edited file diagnostics" in str(runtime.history)
        assert "Fix auth" in str(runtime.history)
        assert is_provider_valid(runtime.history)
        selected = runtime.tree.active
        await runtime.navigate(None)
        await runtime.navigate(selected)
        assert "edited file diagnostics" in str(runtime.history)
        runtime.auto_compact = False
        runtime.replace_agent(Agent(summary_model([])))
        async for _ in runtime.stream("Continue from the completed edit"):
            pass
        assert effects == ["changed file"]

    asyncio.run(run())


def test_auto_success_survives_restart_and_does_not_recompact_stale_usage(tmp_path, monkeypatch):
    monkeypatch.setenv("PCODE_CONTEXT_WINDOW", "16000")

    async def run():
        summaries = []
        main = []

        async def stream(messages, info):
            if "context summarization assistant" in (info.instructions or ""):
                summaries.append(messages)
                yield SUMMARY
            else:
                main.append(deepcopy(messages))
                yield "continuing from summary"

        saved = SavedSession.create("test:local", tmp_path, tmp_path / "sessions")
        runtime = AgentRuntime(Agent(FunctionModel(stream_function=stream)), saved)
        runtime.history = history()
        runtime.auto_compact = True
        for prompt in ("Continue", "What next?"):
            async for _ in runtime.stream(prompt):
                pass
        assert len(summaries) == 1
        assert len(main) == 2
        # The journal is the only record that a turn lost its own early context.
        started = [r for r in saved.records() if r["kind"] == "turn_started"]
        (marker,) = [r for r in saved.records() if r["kind"] == "auto_compacted"]
        assert marker["run_id"] in {r["run_id"] for r in started}
        assert marker["before"] > marker["after"]
        assert "Tests failed; not yet fixed." in str(runtime.history)
        assert runtime.input_tokens > 0
        compacted = deepcopy(runtime.history)
        expected_usage = (runtime.input_tokens, runtime.output_tokens)
        identity, root = saved.info.id, saved.directory.parent
        runtime.close()
        reopened = AgentRuntime(Agent(summary_model([])), SavedSession.open(identity, root))
        await reopened.restore()
        assert reopened.history == compacted
        assert (reopened.input_tokens, reopened.output_tokens) == expected_usage
        reopened.close()

    asyncio.run(run())


def test_schema_growth_is_added_to_existing_external_overhead():
    from pydantic_ai.models import ModelRequestParameters
    from pydantic_ai.tools import ToolDefinition

    from pcode.compaction import SCHEMAS, schema_tokens

    parameters = ModelRequestParameters(
        function_tools=[ToolDefinition(name="new_mcp_tool", description="large schema " * 1500)]
    )
    messages = [
        ModelResponse(
            parts=[TextPart("Short message")],
            usage=RequestUsage(input_tokens=10000),
            metadata={MARKER: {"tokens": 10000, "schema_tokens": 1000}},
        )
    ]
    assert context_estimate(messages, parameters) == 10000 + schema_tokens(parameters) - 1000
    messages[-1].metadata = {SCHEMAS: 1000}
    assert context_estimate(messages, parameters) == 10000 + schema_tokens(parameters) - 1000


def test_override_display_and_safe_compaction_errors(monkeypatch):
    from pcode.live import error_message

    monkeypatch.setenv("PCODE_CONTEXT_WINDOW", "32000")
    assert context_label("proxy:unknown", []) == " · 0/32k"
    assert context_label("anthropic:claude-sonnet-4-6", []) == " · 0/32k"
    error = CompactionError("Not enough room. Use /compact with a focus or /new.")
    assert error_message(error) == str(error)


def test_a_cut_through_a_spanning_native_call_drops_its_orphans(monkeypatch):
    """A `code_execution` call can open in one response and finish in a later
    one; summarizing the first must not leave the second's results behind."""
    from pydantic_ai.messages import NativeToolCallPart, NativeToolReturnPart

    monkeypatch.setenv("PCODE_CONTEXT_WINDOW", "100000")
    caller = {"anthropic_caller": {"tool_id": "srvtoolu_exec", "type": "code_execution_20260120"}}
    source = [
        ModelRequest(parts=[UserPromptPart("Backfill the metrics")]),
        ModelResponse(
            parts=[
                NativeToolCallPart(
                    "code_execution", {"code": "x"}, "srvtoolu_exec", provider_name="anthropic"
                ),
                TextPart("Investigating " * 6000),
                ToolCallPart("shell", {"command": "ls"}, "toolu_1"),
            ]
        ),
        ModelRequest(parts=[ToolReturnPart("shell", "listing", "toolu_1")]),
        ModelResponse(
            parts=[
                NativeToolCallPart(
                    "web_fetch",
                    {"url": "https://example.com"},
                    "srvtoolu_fetch",
                    provider_name="anthropic",
                    provider_details=caller,
                ),
                NativeToolReturnPart(
                    "web_fetch", {"type": "x"}, "srvtoolu_fetch", provider_name="anthropic"
                ),
                NativeToolReturnPart(
                    "code_execution", {"stdout": ""}, "srvtoolu_exec", provider_name="anthropic"
                ),
                TextPart("Fetched. " * 6000),
                ToolCallPart("shell", {"command": "pwd"}, "toolu_2"),
            ]
        ),
        ModelRequest(parts=[ToolReturnPart("shell", "/repo", "toolu_2")]),
        ModelResponse(parts=[TextPart("Done")], usage=RequestUsage(input_tokens=50000)),
    ]

    async def run():
        result = await summarize(source, model=summary_model([]))
        assert result.changed
        parts = [part for message in result.messages for part in message.parts]
        ids = {getattr(part, "tool_call_id", None) for part in parts}
        # The cut fell between the two responses, or this test proves nothing.
        assert "toolu_2" in ids and "toolu_1" not in ids
        assert not ids & {"srvtoolu_exec", "srvtoolu_fetch"}
        assert "Fetched." in str(result.messages)

    asyncio.run(run())
