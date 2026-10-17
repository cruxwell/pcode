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
    monkeypatch.delenv("PCODE_ANTHROPIC_AUTH", raising=False)


@pytest.mark.usefixtures("legacy_anthropic_auth")
def test_anthropic_can_start_without_key(tmp_path):
    app = PreviewApp(model="anthropic:test-model", workspace=tmp_path)
    asyncio.run(app._initialize_runtime())
    assert app.handle("/login") is False
    assert app.controller.login_requested == "anthropic"
    app.runtime.close()


def test_environment_key_used(monkeypatch, tmp_path):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "synthetic-test-key")
    factory = Mock(return_value="test")
    monkeypatch.setattr("pcode.auth.anthropic_model", factory)
    create_agent("anthropic:test-model", tmp_path)
    factory.assert_called_once_with("anthropic:test-model", "synthetic-test-key")


def test_stored_sign_in_is_ignored_while_legacy_auth_is_off(monkeypatch, tmp_path):
    """ANTHROPIC_API_KEY keeps working whatever an earlier /login left behind."""
    credentials = tmp_path / "credentials.json"
    credentials.write_text("{}")
    monkeypatch.setenv("PCODE_CREDENTIALS_FILE", str(credentials))
    monkeypatch.setenv("PCODE_ANTHROPIC_AUTH", "oauth")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "synthetic-test-key")
    factory = Mock(return_value="test")
    monkeypatch.setattr("pcode.auth.anthropic_model", factory)
    create_agent("anthropic:test-model", tmp_path)
    factory.assert_called_once_with("anthropic:test-model", "synthetic-test-key")


@pytest.mark.usefixtures("legacy_anthropic_auth")
def test_unknown_auth_source_is_rejected(monkeypatch, tmp_path):
    monkeypatch.setenv("PCODE_ANTHROPIC_AUTH", "something-else")
    with pytest.raises(ValueError, match="api-key or oauth"):
        create_agent("anthropic:test-model", tmp_path)


@pytest.mark.usefixtures("legacy_anthropic_auth")
def test_login_works_from_a_non_anthropic_session():
    """Signing in stores a credential; it does not require an Anthropic model."""
    buffer = StringIO()
    app = PreviewApp(model="openai-codex:test", runtime=Mock(), console=Console(file=buffer))
    app.handle("/login anthropic")
    assert app.controller.login_requested == "anthropic"
    assert "Anthropic only" not in buffer.getvalue()


@pytest.mark.usefixtures("legacy_anthropic_auth")
def test_preview_login_requests_browser_sign_in():
    app = PreviewApp(console=Console(file=StringIO()))
    app.handle("/login")
    assert app.controller.login_requested == "anthropic"


def test_login_signs_in_to_claude_code_while_legacy_auth_is_off():
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
