"""Thinking visibility requests Anthropic thinking without touching other routes."""

import asyncio
import json
import time
from io import StringIO
from types import SimpleNamespace

import httpx2
import pytest
from anthropic import AsyncAnthropic
from pydantic_ai import Agent
from pydantic_ai.models.anthropic import AnthropicModel
from pydantic_ai.providers.anthropic import AnthropicProvider
from rich.console import Console

from pcode.anthropic_oauth import AnthropicOAuthModel, OAuthTokens, write_tokens
from pcode.app import PreviewApp
from pcode.live import AgentRuntime
from pcode.preferences import apply_effort, apply_thinking, save_preferences

SUMMARIZED = {"type": "adaptive", "display": "summarized"}
BUDGET = {"type": "enabled", "budget_tokens": 2048, "display": "summarized"}
UPDATES = {"type": "adaptive", "display": "updates"}


@pytest.mark.parametrize(
    "name, expected, status_line",
    [
        # Think only when asked: the status line never asks, scrollback does.
        ("claude-sonnet-4-5", BUDGET, None),
        ("claude-sonnet-4-6", SUMMARIZED, None),
        ("claude-opus-4-7", SUMMARIZED, None),
        ("claude-opus-5", SUMMARIZED, SUMMARIZED),
        # Writes progress updates: the status line asks for those alone.
        ("claude-opus-5-5", SUMMARIZED, UPDATES),
    ],
)
@pytest.mark.parametrize("auth", ["api-key", "oauth"])
def test_thinking_stream_request_and_persistable_events(
    name, expected, status_line, auth, tmp_path
):
    requests = []
    betas = []

    def handle(request):
        body = json.loads(request.content)
        requests.append(body)
        betas.append(request.headers.get("anthropic-beta", ""))
        events = [
            (
                "message_start",
                {
                    "message": {
                        "id": "msg_test",
                        "type": "message",
                        "role": "assistant",
                        "model": name,
                        "content": [],
                        "usage": {"input_tokens": 1, "output_tokens": 0},
                    }
                },
            ),
        ]
        if "thinking" in body:
            events += [
                (
                    "content_block_start",
                    {
                        "index": 0,
                        "content_block": {
                            "type": "thinking",
                            "thinking": "",
                            "signature": "",
                        },
                    },
                ),
                (
                    "content_block_delta",
                    {
                        "index": 0,
                        "delta": {
                            "type": "thinking_delta",
                            "thinking": "PRIVATE_THINKING",
                        },
                    },
                ),
                (
                    "content_block_delta",
                    {
                        "index": 0,
                        "delta": {
                            "type": "signature_delta",
                            "signature": "test-signature",
                        },
                    },
                ),
                ("content_block_stop", {"index": 0}),
            ]
        events += [
            (
                "content_block_start",
                {
                    "index": 1,
                    "content_block": {
                        "type": "text",
                        "text": "",
                    },
                },
            ),
            (
                "content_block_delta",
                {
                    "index": 1,
                    "delta": {
                        "type": "text_delta",
                        "text": "Hello",
                    },
                },
            ),
            ("content_block_stop", {"index": 1}),
            (
                "message_delta",
                {
                    "delta": {"stop_reason": "end_turn", "stop_sequence": None},
                    "usage": {"output_tokens": 4},
                },
            ),
            ("message_stop", {}),
        ]
        data = "".join(
            f"event: {kind}\ndata: {json.dumps({'type': kind, **payload})}\n\n"
            for kind, payload in events
        )
        return httpx2.Response(200, headers={"content-type": "text/event-stream"}, content=data)

    async def run():
        async with httpx2.AsyncClient(transport=httpx2.MockTransport(handle)) as client:
            provider = AnthropicProvider(
                anthropic_client=AsyncAnthropic(
                    api_key="test-key",
                    auth_token="",
                    http_client=client,
                )
            )
            if auth == "oauth":
                path = tmp_path / "credential.json"
                write_tokens(
                    path,
                    OAuthTokens("synthetic-access", "synthetic-refresh", time.time() + 3600),
                )
                model = AnthropicOAuthModel(f"anthropic:{name}", path=path, http_client=client)
            else:
                model = AnthropicModel(name, provider=provider)
            agent = Agent(model)
            runtime = AgentRuntime(agent)
            app = PreviewApp(
                model=f"anthropic:{name}", runtime=runtime, console=Console(file=StringIO())
            )
            try:
                from pcode.runtime import ThinkingDelta

                wanted = {"off": None, "status-line": status_line, "scrollback": expected}
                for mode in ("off", "scrollback", "status-line", "off"):
                    app.set_thinking_mode(mode)
                    events = [event async for event in runtime.stream("hello")]
                    assert requests[-1]["stream"] is True
                    # The updates display is a beta: its header goes with it, only.
                    updates = wanted[mode] == UPDATES
                    assert ("thinking-display-updates-2026-08-18" in betas[-1]) == updates
                    if wanted[mode] is None:
                        # Off asks for nothing, as before there were modes.
                        assert "thinking" not in requests[-1]
                        assert not any(isinstance(e, ThinkingDelta) for e in events)
                        continue
                    assert requests[-1]["thinking"] == wanted[mode]
                    if "budget_tokens" in wanted[mode]:
                        assert wanted[mode]["budget_tokens"] < requests[-1]["max_tokens"]
                    assert (
                        "".join(e.text for e in events if isinstance(e, ThinkingDelta))
                        == "PRIVATE_THINKING"
                    )
            finally:
                runtime.close()

    asyncio.run(run())


def test_startup_and_toggle_preserve_effort_and_replace_settings():
    save_preferences(show_thinking="scrollback", effort="medium")
    agent = SimpleNamespace(model="anthropic:claude-opus-4-7", model_settings=None)
    app = PreviewApp(
        model=agent.model, runtime=SimpleNamespace(agent=agent), console=Console(file=StringIO())
    )
    assert agent.model_settings == {
        "anthropic_thinking": {"type": "adaptive", "display": "summarized"},
        "anthropic_effort": "medium",
    }
    captured = agent.model_settings
    app.show_thinking("off")
    assert agent.model_settings == {"anthropic_effort": "medium"}
    assert "anthropic_thinking" in captured
    app.set_thinking_mode("scrollback")
    apply_effort(agent, app.model, "high")
    assert agent.model_settings["anthropic_thinking"] == {
        "type": "adaptive",
        "display": "summarized",
    }
    assert "next turn" in app.transcript.console.file.getvalue()


def test_thinking_on_can_open_anthropic_without_credentials(monkeypatch, tmp_path):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("PCODE_ANTHROPIC_AUTH", raising=False)
    save_preferences(show_thinking="scrollback")
    app = PreviewApp(
        model="anthropic:claude-opus-4-7", workspace=tmp_path, console=Console(file=StringIO())
    )
    asyncio.run(app._initialize_runtime())
    try:
        assert app.runtime.agent.model_settings["anthropic_thinking"] == {
            "type": "adaptive",
            "display": "summarized",
        }
    finally:
        app.runtime.close()


def test_switch_uses_current_visibility_not_saved_default(monkeypatch, tmp_path):
    app = PreviewApp(workspace=tmp_path, console=Console(file=StringIO()))
    app.set_thinking_mode("scrollback")
    save_preferences(show_thinking="off")
    monkeypatch.setattr("pcode.agent.create_agent", lambda *args: Agent("test"))

    async def run():
        await app.switch_model("anthropic:claude-sonnet-4-5")
        try:
            assert app.runtime.agent.model_settings["anthropic_thinking"] == {
                "type": "enabled",
                "budget_tokens": 2048,
                "display": "summarized",
            }
        finally:
            app.runtime.close()

    asyncio.run(run())


@pytest.mark.parametrize("model", ["openai-codex:test", "google:test"])
def test_other_routes_are_untouched(model):
    settings = {"existing": "setting"}
    agent = SimpleNamespace(model_settings=settings)
    for mode in ("off", "status-line", "scrollback"):
        apply_thinking(agent, model, mode)
        assert agent.model_settings is settings


@pytest.mark.parametrize("provider", ["openai", "openai-responses"])
def test_openai_summaries_follow_the_mode_on_reasoning_models(provider):
    agent = SimpleNamespace(model_settings={"openai_reasoning_effort": "high"})
    # One summarizer per model: both modes ask for it, and only rendering differs.
    for mode in ("status-line", "scrollback"):
        apply_thinking(agent, f"{provider}:o4-mini", mode)
        assert agent.model_settings == {
            "openai_reasoning_effort": "high",
            "openai_reasoning_summary": "auto",
        }
    # Off never asks: an unverified API organisation gets a 400 for asking.
    apply_thinking(agent, f"{provider}:o4-mini", "off")
    assert agent.model_settings == {"openai_reasoning_effort": "high"}
    # A model that does not reason is never sent a reasoning setting.
    agent = SimpleNamespace(model_settings=None)
    apply_thinking(agent, f"{provider}:gpt-4.1", "scrollback")
    assert agent.model_settings is None


def test_resume_applies_current_thinking_preference(monkeypatch, tmp_path):
    from pcode.sessions import SavedSession

    root = tmp_path / "sessions"
    saved = SavedSession.create("anthropic:claude-opus-4-7", tmp_path, root)
    identity = saved.info.id
    saved.close()
    app = PreviewApp(workspace=tmp_path, session_dir=root, console=Console(file=StringIO()))
    app.set_thinking_mode("scrollback")
    from pydantic_ai.models.test import TestModel

    monkeypatch.setattr(
        "pcode.agent.create_agent",
        lambda *args: Agent(TestModel(profile={"anthropic_supports_adaptive_thinking": True})),
    )

    async def run():
        await app.controller.resume_session(identity)
        try:
            assert app.runtime.agent.model_settings["anthropic_thinking"] == {
                "type": "adaptive",
                "display": "summarized",
            }
        finally:
            app.runtime.close()

    asyncio.run(run())


@pytest.mark.parametrize(
    "name, updates",
    [
        ("claude-opus-5-5", True),
        ("claude-opus-5-5-20260901", True),
        ("claude-sonnet-5-5", True),
        ("claude-fable-5", True),
        ("claude-fable-5-1", True),
        ("claude-mythos-5-1", True),
        # A later model with a longer number is not assumed to support the beta.
        ("claude-fable-50", False),
        ("claude-opus-5-50", False),
        ("claude-mythos-5-10", False),
        ("claude-opus-5", False),
    ],
)
def test_only_models_that_write_progress_updates_get_the_beta(name, updates):
    from pcode.preferences import UPDATES_BETA, thinking_settings

    profile = SimpleNamespace(profile={"anthropic_supports_adaptive_thinking": True})
    wanted = thinking_settings(f"anthropic:{name}", profile, "status-line")
    assert (wanted.get("anthropic_betas") == [UPDATES_BETA]) == updates


@pytest.mark.parametrize(
    "model, mode, warned",
    [
        ("anthropic:claude-opus-4-7", "scrollback", True),
        ("anthropic:claude-opus-4-7", "status-line", False),
        ("anthropic:claude-opus-5-5", "status-line", False),
        ("anthropic:claude-opus-5-5", "scrollback", False),
    ],
)
def test_the_cost_warning_is_only_for_turning_thinking_on(model, mode, warned):
    output = StringIO()
    app = PreviewApp(model=model, console=Console(file=output))
    app.show_thinking(mode)
    assert ("only thinks when asked" in output.getvalue()) == warned


def test_an_unknown_mode_from_a_terminal_is_ignored():
    agent = SimpleNamespace(model="anthropic:claude-opus-4-7", model_settings=None)
    app = PreviewApp(
        model=agent.model, runtime=SimpleNamespace(agent=agent), console=Console(file=StringIO())
    )
    app.controller.set_thinking("scrollback")
    settings = agent.model_settings
    app.controller.set_thinking("on")  # An older terminal's value.
    assert app.activity.thinking_mode == "scrollback"
    assert agent.model_settings is settings
