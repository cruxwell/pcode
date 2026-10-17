"""Environment auth and login dispatch without accessing real credentials."""

import asyncio
from io import StringIO
from unittest.mock import Mock

import pytest
from rich.console import Console

from pcode.agent import create_agent
from pcode.app import PreviewApp


@pytest.fixture(autouse=True)
def isolated_credentials(monkeypatch):
    # conftest redirects XDG_CONFIG_HOME, so no stored sign-in is visible here.
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("PCODE_LLM_PROXY", raising=False)


def test_anthropic_can_start_without_key(tmp_path):
    app = PreviewApp(model="anthropic:test-model", workspace=tmp_path)
    asyncio.run(app._initialize_runtime())
    assert isinstance(app.runtime.agent.model, str)
    app.runtime.close()


def test_environment_key_used(monkeypatch, tmp_path):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "synthetic-test-key")
    factory = Mock(return_value="test")
    monkeypatch.setattr("pcode.auth.anthropic_model", factory)
    create_agent("anthropic:test-model", tmp_path)
    factory.assert_called_once_with("anthropic:test-model", "synthetic-test-key")


def test_login_works_from_a_session_on_another_provider():
    buffer = StringIO()
    app = PreviewApp(model="openai-codex:test", runtime=Mock(), console=Console(file=buffer))
    app.handle("/login claude")
    assert app.controller.login_requested == "claude"


def test_login_signs_in_to_claude_code():
    buffer = StringIO()
    app = PreviewApp(console=Console(file=buffer))
    app.handle("/login")
    assert app.controller.login_requested == "claude"
    app.controller.login_requested = None
    app.handle("/login anthropic")
    assert app.controller.login_requested is None
    assert "Usage: /login [claude|openai-codex]" in buffer.getvalue()


def test_unknown_login_source_is_rejected():
    buffer = StringIO()
    app = PreviewApp(console=Console(file=buffer))
    app.handle("/login codex")
    assert app.controller.login_requested is None
    assert "Usage: /login" in buffer.getvalue()
