"""pcode's own Anthropic browser sign-in.

Every credential, callback, and HTTP response here is synthetic: no browser is
opened, no real token endpoint is called, and no real credential file is read.
Only loopback sockets owned by these tests are used.
"""

import asyncio
import base64
import hashlib
import json
import socket
import stat
import subprocess
import time
from io import StringIO
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import httpx2
import pytest
from anthropic_mock import anthropic_response
from pydantic_ai import Agent
from rich.console import Console

from pcode import anthropic_oauth, auth
from pcode.anthropic_oauth import (
    CLIENT_ID,
    AnthropicOAuthModel,
    OAuthTokens,
    StoredLogin,
    anthropic_auth_source,
    authorization_url,
    credentials_path,
    delete_tokens,
    have_credentials,
    login,
    pkce_pair,
    read_tokens,
    refresh_tokens,
    write_tokens,
)
from pcode.auth import OAUTH_BETAS, OAUTH_PREAMBLE, LoginError, oauth_user_agent

# pcode's own sign-in is off by default (pcode.models.LEGACY_ANTHROPIC_AUTH).
pytestmark = pytest.mark.usefixtures("legacy_anthropic_auth")

MESSAGE = {
    "id": "msg_synthetic",
    "type": "message",
    "role": "assistant",
    "model": "test-model",
    "content": [{"type": "text", "text": "hello"}],
    "stop_reason": "end_turn",
    "usage": {"input_tokens": 1, "output_tokens": 1},
}


@pytest.fixture
def store(tmp_path, monkeypatch):
    path = tmp_path / "credentials.json"
    monkeypatch.setenv("PCODE_CREDENTIALS_FILE", str(path))
    monkeypatch.delenv("PCODE_ANTHROPIC_AUTH", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("PCODE_LLM_PROXY", raising=False)
    return path


def save(path, *, access="synthetic-access", refresh="synthetic-refresh", lifetime=3600):
    write_tokens(path, OAuthTokens(access, refresh, time.time() + lifetime))


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def token_endpoint(responses, recorded):
    """Sync mock transport standing in for Anthropic's token endpoint."""
    queue = list(responses)

    def handle(request):
        recorded.append((str(request.url), json.loads(request.content)))
        status, body = queue.pop(0) if len(queue) > 1 else queue[0]
        return httpx2.Response(status, json=body)

    return httpx2.MockTransport(handle)


@pytest.mark.parametrize(
    ("installed", "expected"),
    [
        # A newer local install unlocks models whose floor is above the pin.
        ("2.9.1 (Claude Code)", "claude-cli/2.9.1"),
        # An older or missing one must not downgrade below the pin.
        ("2.0.9 (Claude Code)", f"claude-cli/{auth.OAUTH_VERSION}"),
        ("unparseable", f"claude-cli/{auth.OAUTH_VERSION}"),
        (None, f"claude-cli/{auth.OAUTH_VERSION}"),
    ],
)
def test_advertised_version_never_falls_below_the_pin(monkeypatch, installed, expected):
    monkeypatch.delenv("PCODE_CLAUDE_VERSION", raising=False)
    found = None if installed is None else "/bin/claude"
    monkeypatch.setattr(auth.shutil, "which", lambda _: found)
    monkeypatch.setattr(
        auth.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(a[0], 0, stdout=installed or ""),
    )
    auth.oauth_user_agent.cache_clear()
    try:
        assert auth.oauth_user_agent() == expected
    finally:
        auth.oauth_user_agent.cache_clear()


@pytest.mark.parametrize(
    ("override", "installed", "expected"),
    [
        # The override raises the floor without a release...
        ("2.4.0", None, "claude-cli/2.4.0"),
        ("2.4.0", "2.1.280 (Claude Code)", "claude-cli/2.4.0"),
        # ...and can also be outrun by a newer local install.
        ("2.4.0", "2.9.1 (Claude Code)", "claude-cli/2.9.1"),
        ("  ", None, f"claude-cli/{auth.OAUTH_VERSION}"),
    ],
)
def test_the_fallback_version_is_configurable(monkeypatch, override, installed, expected):
    monkeypatch.setenv("PCODE_CLAUDE_VERSION", override)
    found = None if installed is None else "/bin/claude"
    monkeypatch.setattr(auth.shutil, "which", lambda _: found)
    monkeypatch.setattr(
        auth.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(a[0], 0, stdout=installed or ""),
    )
    auth.oauth_user_agent.cache_clear()
    try:
        assert auth.oauth_user_agent() == expected
    finally:
        auth.oauth_user_agent.cache_clear()


@pytest.mark.parametrize("value", ["2", "v2.1.280", "latest", "2.1.280-beta"])
def test_an_unusable_version_override_is_reported(monkeypatch, value):
    monkeypatch.setenv("PCODE_CLAUDE_VERSION", value)
    with pytest.raises(LoginError, match="PCODE_CLAUDE_VERSION"):
        auth.oauth_version()


def test_a_broken_claude_executable_falls_back_to_the_pin(monkeypatch):
    monkeypatch.delenv("PCODE_CLAUDE_VERSION", raising=False)
    monkeypatch.setattr(auth.shutil, "which", lambda _: "/bin/claude")

    def explode(*args, **kwargs):
        raise subprocess.TimeoutExpired("claude", auth.VERSION_TIMEOUT_SECONDS)

    monkeypatch.setattr(auth.subprocess, "run", explode)
    auth.oauth_user_agent.cache_clear()
    try:
        assert auth.oauth_user_agent() == f"claude-cli/{auth.OAUTH_VERSION}"
    finally:
        auth.oauth_user_agent.cache_clear()


def test_authorization_url_matches_the_pkce_authorization_request():
    verifier, challenge = pkce_pair()
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    assert challenge == base64.urlsafe_b64encode(digest).decode().rstrip("=")
    assert verifier != pkce_pair()[0]

    url = authorization_url(challenge, verifier, 54545)
    parsed = urlsplit(url)
    fields = {key: value[0] for key, value in parse_qs(parsed.query).items()}
    assert (parsed.scheme, parsed.netloc, parsed.path) == ("https", "claude.ai", "/oauth/authorize")
    assert fields["client_id"] == CLIENT_ID
    assert fields["response_type"] == "code"
    assert fields["code"] == "true"
    assert fields["redirect_uri"] == "http://localhost:54545/callback"
    assert fields["code_challenge"] == challenge
    assert fields["code_challenge_method"] == "S256"
    # This flow echoes the verifier back as state, as Claude Code does.
    assert fields["state"] == verifier
    assert "user:inference" in fields["scope"]


def test_callback_port_override_is_validated(monkeypatch):
    assert anthropic_oauth.callback_port() == anthropic_oauth.DEFAULT_CALLBACK_PORT
    monkeypatch.setenv("PCODE_OAUTH_CALLBACK_PORT", "61234")
    assert anthropic_oauth.callback_port() == 61234
    monkeypatch.setenv("PCODE_OAUTH_CALLBACK_PORT", "not-a-port")
    with pytest.raises(LoginError, match="between 1 and 65535"):
        anthropic_oauth.callback_port()


async def visit(url):
    async with httpx2.AsyncClient(timeout=10) as client:
        return await client.get(url)


def test_browser_sign_in_stores_tokens_and_answers_the_browser(store, monkeypatch):
    monkeypatch.setenv("PCODE_OAUTH_CALLBACK_PORT", str(free_port()))
    monkeypatch.setattr(
        anthropic_oauth,
        "_open_browser",
        lambda url: (_ for _ in ()).throw(AssertionError("no browser in tests")),
    )
    exchanges = []
    transport = token_endpoint(
        [
            (
                200,
                {
                    "access_token": "synthetic-fresh-access",
                    "refresh_token": "synthetic-fresh-refresh",
                    "expires_in": 28800,
                },
            )
        ],
        exchanges,
    )
    shown = []

    async def run():
        task = asyncio.create_task(
            login(notify=shown.append, open_browser=False, transport=transport)
        )
        # Wait for the URL that would have been opened in the browser.
        while not shown:
            await asyncio.sleep(0.01)
        fields = {k: v[0] for k, v in parse_qs(urlsplit(shown[0]).query).items()}
        callback = f"{fields['redirect_uri']}?code=synthetic-code&state={fields['state']}"
        page = await visit(callback)
        assert page.status_code == 200
        assert "Signed in" in page.text
        assert "synthetic" not in page.text
        return await asyncio.wait_for(task, 10), fields["state"]

    tokens, verifier = asyncio.run(run())

    assert tokens.access == "synthetic-fresh-access"
    url, payload = exchanges[0]
    assert url == anthropic_oauth.TOKEN_URL
    assert payload == {
        "client_id": CLIENT_ID,
        "grant_type": "authorization_code",
        "code": "synthetic-code",
        "state": verifier,
        "redirect_uri": f"http://localhost:{anthropic_oauth.callback_port()}/callback",
        "code_verifier": verifier,
    }

    stored = read_tokens(store)
    assert (stored.access, stored.refresh) == ("synthetic-fresh-access", "synthetic-fresh-refresh")
    # Expiry is stored early so a turn never starts on an about-to-die token.
    assert time.time() + 28800 - anthropic_oauth.EARLY_REFRESH_SECONDS - 5 < stored.expires_at
    assert stored.expires_at < time.time() + 28800
    assert stat.S_IMODE(store.stat().st_mode) == 0o600
    assert "synthetic" not in repr(stored)


def test_callback_rejects_a_mismatched_state_and_keeps_waiting(store, monkeypatch):
    monkeypatch.setenv("PCODE_OAUTH_CALLBACK_PORT", str(free_port()))
    exchanges = []
    transport = token_endpoint(
        [(200, {"access_token": "a", "refresh_token": "r", "expires_in": 60})], exchanges
    )
    shown = []

    async def run():
        task = asyncio.create_task(
            login(notify=shown.append, open_browser=False, transport=transport)
        )
        while not shown:
            await asyncio.sleep(0.01)
        fields = {k: v[0] for k, v in parse_qs(urlsplit(shown[0]).query).items()}
        base = fields["redirect_uri"]
        forged = await visit(f"{base}?code=attacker-code&state=wrong-state")
        assert forged.status_code == 400
        assert "state did not match" in forged.text
        denied = await visit(f"{base}?error=access_denied&state={fields['state']}")
        assert denied.status_code == 400
        elsewhere = await visit(f"http://localhost:{anthropic_oauth.callback_port()}/other")
        assert elsewhere.status_code == 400
        assert not task.done()
        await visit(f"{base}?code=real-code&state={fields['state']}")
        return await asyncio.wait_for(task, 10)

    asyncio.run(run())
    assert [payload["code"] for _, payload in exchanges] == ["real-code"]


def test_sign_in_timeout_and_busy_port_are_actionable(store, monkeypatch):
    port = free_port()
    monkeypatch.setenv("PCODE_OAUTH_CALLBACK_PORT", str(port))
    with pytest.raises(LoginError, match="timed out"):
        asyncio.run(login(open_browser=False, timeout=0.2))
    assert not store.exists()

    listener = socket.socket()
    try:
        listener.bind(("127.0.0.1", port))
        listener.listen()
        with pytest.raises(LoginError, match="PCODE_OAUTH_CALLBACK_PORT"):
            asyncio.run(login(open_browser=False, timeout=5))
    finally:
        listener.close()


@pytest.mark.parametrize(
    "data",
    [
        [],
        {},
        {"anthropic": []},
        {"anthropic": {"type": "api_key", "key": "synthetic"}},
        {"anthropic": {"type": "oauth", "access": "", "refresh": "r", "expires_at": 1}},
        {"anthropic": {"type": "oauth", "access": "a b", "refresh": "r", "expires_at": 1}},
        {"anthropic": {"type": "oauth", "access": "a", "refresh": "", "expires_at": 1}},
        {"anthropic": {"type": "oauth", "access": "a", "refresh": "r"}},
        {"anthropic": {"type": "oauth", "access": "a", "refresh": "r", "expires_at": True}},
        {"anthropic": {"type": "oauth", "access": "a", "refresh": "r", "expires_at": "soon"}},
    ],
)
def test_unusable_stored_credentials_are_rejected_without_echoing_them(store, data):
    store.write_text(json.dumps(data))
    with pytest.raises(LoginError, match="No usable pcode Anthropic login") as error:
        read_tokens(store)
    assert "synthetic" not in str(error.value)

    store.write_text('malformed "synthetic-secret"')
    with pytest.raises(LoginError) as error:
        read_tokens(store)
    assert "synthetic-secret" not in str(error.value)


def test_expired_credentials_refresh_and_rewrite_the_file(store):
    save(store, access="synthetic-old", refresh="synthetic-refresh", lifetime=-10)
    exchanges = []
    transport = token_endpoint(
        # Anthropic may omit a rotated refresh token; the stored one must survive.
        [(200, {"access_token": "synthetic-renewed", "expires_in": 3600})],
        exchanges,
    )

    async def run():
        return await StoredLogin(store, transport=transport)()

    token = asyncio.run(run())
    assert token.token == "synthetic-renewed"
    assert "synthetic-renewed" not in repr(token)
    assert exchanges[0][1] == {
        "client_id": CLIENT_ID,
        "grant_type": "refresh_token",
        "refresh_token": "synthetic-refresh",
    }
    stored = read_tokens(store)
    assert (stored.access, stored.refresh) == ("synthetic-renewed", "synthetic-refresh")
    assert stat.S_IMODE(store.stat().st_mode) == 0o600


def test_valid_credentials_are_used_as_is_unless_a_refresh_is_forced(store):
    save(store, access="synthetic-valid")
    exchanges = []
    transport = token_endpoint(
        [(200, {"access_token": "synthetic-forced", "refresh_token": "r2", "expires_in": 3600})],
        exchanges,
    )
    provider = StoredLogin(store, transport=transport)

    async def run():
        assert (await provider()).token == "synthetic-valid"
        assert exchanges == []
        # The SDK sets force_refresh after a 401 so a stale token is replaced.
        return await provider(force_refresh=True)

    assert asyncio.run(run()).token == "synthetic-forced"
    assert read_tokens(store).access == "synthetic-forced"


def test_a_concurrent_refresh_is_not_repeated(store):
    save(store, access="synthetic-old", lifetime=-10)
    exchanges = []
    transport = token_endpoint(
        [(200, {"access_token": "synthetic-renewed", "refresh_token": "r2", "expires_in": 3600})],
        exchanges,
    )

    def refresh_once():
        return refresh_tokens(store, transport=transport).access

    async def run():
        return await asyncio.gather(
            asyncio.to_thread(refresh_once),
            asyncio.to_thread(refresh_once),
        )

    assert asyncio.run(run()) == ["synthetic-renewed", "synthetic-renewed"]
    # The second caller re-reads the file behind the lock instead of refreshing.
    assert len(exchanges) == 1


@pytest.mark.parametrize(
    "response, expected",
    [
        ((401, {"error": "invalid_grant"}), "rejected the sign-in"),
        ((500, {"error": "boom"}), "token endpoint failed"),
        ((200, {"error": "nope"}), "unusable token response"),
    ],
)
def test_refresh_failures_are_reported_without_bodies(store, response, expected):
    save(store, access="synthetic-old", refresh="synthetic-refresh", lifetime=-10)
    before = store.read_bytes()
    with pytest.raises(LoginError, match=expected) as error:
        refresh_tokens(store, transport=token_endpoint([response], []))
    assert "invalid_grant" not in str(error.value)
    assert "synthetic" not in str(error.value)
    # A failed refresh must not destroy the credential that may still refresh.
    assert store.read_bytes() == before


def test_requests_carry_bearer_auth_and_claude_code_wire_markers(store, monkeypatch):
    # Unrelated auth and endpoint variables must not redirect or override this.
    monkeypatch.setenv("ANTHROPIC_API_KEY", "synthetic-unrelated-key")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "synthetic-unrelated-token")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://unrelated.invalid")
    save(store, access="synthetic-access")
    requests = []

    def handle(request):
        requests.append(request)
        # A 401 makes the SDK invalidate its token cache and retry once.
        if len(requests) == 1:
            return httpx2.Response(401, json={"error": {"message": "expired"}})
        return anthropic_response(request, MESSAGE)

    exchanges = []
    transport = token_endpoint(
        [(200, {"access_token": "synthetic-rotated", "refresh_token": "r2", "expires_in": 3600})],
        exchanges,
    )

    async def run():
        async with httpx2.AsyncClient(transport=httpx2.MockTransport(handle)) as client:
            model = AnthropicOAuthModel(
                "anthropic:test-model", http_client=client, transport=transport
            )
            agent = Agent(model, instructions="Keep original instructions.")
            result = await agent.run("hello")
            assert result.output == "hello"

    asyncio.run(run())

    assert [request.headers["authorization"] for request in requests] == [
        "Bearer synthetic-access",
        "Bearer synthetic-rotated",
    ]
    for request in requests:
        assert request.url.host == "api.anthropic.com"
        assert "x-api-key" not in request.headers
        assert OAUTH_BETAS <= set(
            flag.strip() for flag in request.headers["anthropic-beta"].split(",")
        )
        assert request.headers["user-agent"] == oauth_user_agent()
        assert request.headers["x-app"] == "cli"
        payload = json.loads(request.content)
        assert payload["model"] == "test-model"
        assert payload["system"][0]["text"] == OAUTH_PREAMBLE
        assert "Keep original instructions." in json.dumps(payload["system"])
        assert "synthetic" not in json.dumps(payload)
    # The 401 retry refreshed the credential and stored the rotated token.
    assert len(exchanges) == 1
    assert read_tokens(store).access == "synthetic-rotated"


def test_subscription_requests_send_the_2025_web_tool_versions(store):
    """The endpoint accepts the 20260209 versions but only runs them inside code_execution."""
    from pydantic_ai.capabilities import WebFetch, WebSearch

    save(store, access="synthetic-access")
    requests = []

    def handle(request):
        requests.append(json.loads(request.content))
        return anthropic_response(request, MESSAGE)

    async def run():
        async with httpx2.AsyncClient(transport=httpx2.MockTransport(handle)) as client:
            model = AnthropicOAuthModel("anthropic:claude-opus-4-6", http_client=client)
            assert model.profile.get("anthropic_supports_dynamic_filtering") is False
            await Agent(model, capabilities=[WebSearch(), WebFetch()]).run("hello")

    asyncio.run(run())
    (payload,) = requests
    assert [tool["type"] for tool in payload["tools"]] == [
        "web_search_20250305",
        "web_fetch_20250910",
    ]


def test_building_the_model_without_a_stored_login_fails_early(store):
    with pytest.raises(LoginError, match="Run /login"):
        AnthropicOAuthModel("anthropic:test-model")


def test_auth_source_prefers_the_stored_login_but_environment_wins(store, monkeypatch):
    assert not have_credentials()
    assert anthropic_auth_source() == "api-key"
    monkeypatch.setenv("ANTHROPIC_API_KEY", "synthetic-key")
    assert anthropic_auth_source() == "api-key"
    save(store)
    assert have_credentials()
    assert anthropic_auth_source() == "oauth"
    monkeypatch.setenv("PCODE_ANTHROPIC_AUTH", "api-key")
    assert anthropic_auth_source() == "api-key"
    monkeypatch.setenv("PCODE_ANTHROPIC_AUTH", "oauth")
    assert anthropic_auth_source() == "oauth"


def test_stored_login_enables_the_anthropic_picker_without_reading_it(store, monkeypatch):
    from pathlib import Path

    from pcode.models import ENV_PROVIDERS, active_providers

    monkeypatch.setattr("pcode.models.shutil.which", lambda _: None)
    # A machine that has used Claude Code has ~/.claude, which enables `claude:`.
    monkeypatch.setattr("pcode.models.claude_code_configured", lambda: False)
    # The developer's shell may hold real provider keys (AWS_PROFILE, etc.).
    for requirements in ENV_PROVIDERS.values():
        for group in requirements:
            for name in group:
                monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("CODEX_HOME", str(store.parent / "absent-codex"))
    assert active_providers(None) == set()
    save(store)
    # Resolution may consult pcode's own non-secret preferences; guard only the
    # token store, so this stays a statement about credentials, not file I/O.
    read_text = Path.read_text

    def guarded(self, *args, **kwargs):
        if self == store:
            pytest.fail("must not read the stored credential")
        return read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", guarded)
    assert active_providers(None) == {"anthropic"}


def test_stored_login_is_used_for_new_agents(store, monkeypatch, tmp_path):
    from pcode.agent import create_agent

    save(store)
    key = lambda *_: pytest.fail("must not use an environment key")  # noqa: E731
    monkeypatch.setattr("pcode.auth.anthropic_model", key)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "synthetic-unrelated-key")
    agent = create_agent("anthropic:test-model", tmp_path)
    assert isinstance(agent.model, AnthropicOAuthModel)


def test_login_command_switches_the_running_model_and_reports_storage(store, monkeypatch):
    from pcode.app import PreviewApp

    save(store)
    runtime = SimpleNamespace(agent=SimpleNamespace(model="original"), history=["existing"])
    buffer = StringIO()
    app = PreviewApp(model="anthropic:test-model", runtime=runtime, console=Console(file=buffer))
    opened = []

    async def fake_login(**kwargs):
        kwargs["notify"]("https://claude.ai/oauth/authorize?synthetic=1")
        opened.append(kwargs)
        return read_tokens(store)

    monkeypatch.setattr(anthropic_oauth, "login", fake_login)
    app.handle("/login")
    assert app.controller.login_requested == "anthropic"
    asyncio.run(app.controller.perform_login())

    assert app.controller.login_requested is None
    assert opened and isinstance(runtime.agent.model, AnthropicOAuthModel)
    assert runtime.history == ["existing"]
    output = buffer.getvalue()
    assert "claude.ai/oauth/authorize" in output
    # The path may wrap; continuation rows hang under the note's text.
    assert str(credentials_path()) in "".join(line.strip() for line in output.splitlines())
    assert "synthetic-access" not in output


def test_failed_login_keeps_the_previous_model(store, monkeypatch):
    from pcode.app import PreviewApp

    runtime = SimpleNamespace(agent=SimpleNamespace(model="original"))
    buffer = StringIO()
    app = PreviewApp(model="anthropic:test-model", runtime=runtime, console=Console(file=buffer))

    async def fail(**kwargs):
        raise LoginError("Anthropic sign-in timed out. Run /login to try again.")

    monkeypatch.setattr(anthropic_oauth, "login", fail)
    app.handle("/login anthropic")
    asyncio.run(app.controller.perform_login())
    assert runtime.agent.model == "original"
    assert "timed out" in buffer.getvalue()


def test_logout_removes_the_stored_login(store, monkeypatch):
    from pcode.app import PreviewApp

    buffer = StringIO()
    app = PreviewApp(
        model="anthropic:test-model", runtime=SimpleNamespace(), console=Console(file=buffer)
    )
    app.handle("/logout")
    assert "No stored Anthropic login" in buffer.getvalue()

    save(store)
    monkeypatch.setenv("PCODE_ANTHROPIC_AUTH", "oauth")
    app.handle("/logout")
    assert not store.exists()
    assert "PCODE_ANTHROPIC_AUTH" not in __import__("os").environ
    assert "Removed pcode's stored Anthropic login" in buffer.getvalue()
    assert delete_tokens(store) is False


def test_callback_page_is_self_contained_and_escapes_its_text():
    from pcode.oauth_pages import callback_page

    ok = callback_page("Signed in", "You can close this tab.", ok=True)
    bad = callback_page("Sign-in failed", "<script>alert(1)</script>", ok=False)

    # One request, one document: nothing may be fetched after the server stops.
    for page in (ok, bad):
        assert page.startswith("<!doctype html>")
        assert "<link" not in page and "src=" not in page and "http://" not in page
    assert "#10b981" in ok and "#ef4444" in bad
    assert "<script>" not in bad
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in bad
