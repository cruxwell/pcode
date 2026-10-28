"""Exercise native OAuth with a fake HTTP provider, never real accounts or browsers."""

import asyncio
import base64
import hashlib
import json
import warnings
from urllib.parse import parse_qs, urlsplit

import httpx2
import pytest
from fastmcp.client.auth import OAuth
from mcp.shared.auth import AuthorizationCodeResult

from pcode.mcp import MCPState, build_toolset, config_path, mcp_transport
from pcode.mcp_oauth import CredentialStore, SignInRequired, credentials_path

URL = "https://resource.example/mcp"
ISSUER = "https://auth.example"


async def expire_stored_access_token(url=URL):
    """Age the saved access token as a restart hours later would find it."""
    await CredentialStore().put(
        key=f"{url}/token_expiry", value={"expires_at": 1}, collection="mcp-oauth-token-expiry"
    )


def test_basic_token_auth_filter_preserves_other_fields_and_headers():
    from pcode.mcp_oauth import _single_token_auth

    request = httpx2.Request(
        "POST",
        ISSUER + "/token",
        headers={"Authorization": "Basic fake-credentials", "X-Test": "preserved"},
        data={
            "grant_type": "authorization_code",
            "client_id": "fake-client",
            "client_secret": "fake-secret",
            "code": "code+with&encoding",
            "code_verifier": "verifier",
            "redirect_uri": "http://127.0.0.1:1234/callback",
            "resource": URL,
            "empty": "",
        },
        extensions={"test": "preserved"},
    )
    filtered = _single_token_auth(request)
    form = parse_qs(filtered.content.decode(), keep_blank_values=True)
    assert form == {
        key: value
        for key, value in parse_qs(request.content.decode(), keep_blank_values=True).items()
        if key not in {"client_id", "client_secret"}
    }
    assert filtered.headers["Authorization"] == request.headers["Authorization"]
    assert filtered.headers["X-Test"] == "preserved"
    assert filtered.headers["Content-Type"] == request.headers["Content-Type"]
    assert int(filtered.headers["Content-Length"]) == len(filtered.content)
    assert filtered.url == request.url
    assert filtered.method == request.method
    assert filtered.extensions == request.extensions
    assert _single_token_auth(filtered) is filtered  # Safe after an upstream fix, too.
    assert "client_id" in parse_qs(request.content.decode())  # Do not mutate SDK input.


def test_native_oauth_is_constructed_without_network_or_browser(monkeypatch):
    def unexpected(*args, **kwargs):
        pytest.fail("Building a toolset must not authenticate")

    monkeypatch.setattr("webbrowser.open", unexpected)
    monkeypatch.setattr(httpx2.AsyncClient, "send", unexpected)
    with warnings.catch_warnings(record=True) as caught:
        toolset = build_toolset("remote", {"url": URL, "auth": "oauth"})
    auth = mcp_transport(toolset).auth
    assert isinstance(auth, OAuth)
    assert isinstance(auth.token_storage_adapter._key_value_store, CredentialStore)
    assert not caught  # Neither FastMCP's memory-store nor key-value's stability warning.
    assert not auth._initialized
    assert toolset.prefix == "mcp_remote"
    assert not credentials_path().exists()  # Nothing is written before a sign-in.


@pytest.mark.parametrize(
    "entry",
    [
        {"command": "echo", "auth": "oauth"},
        {"url": URL, "auth": "secret-bearer-value"},
        {"url": URL, "auth": {"type": "oauth", "client_secret": "secret-bearer-value"}},
        {"url": URL, "auth": "oauth", "headers": {"Authorization": "secret-bearer-value"}},
        {"url": URL, "auth": "oauth", "headers": {"authorization": "secret-bearer-value"}},
        {"url": URL, "client_id": "fake-client", "client_secret": "secret-bearer-value"},
        {"url": URL, "auth": "oauth", "client_secret": "secret-bearer-value"},
        {"url": URL, "auth": "oauth", "client_id": " ", "client_secret": "secret-bearer-value"},
        {"command": "echo", "client_id": "fake-client", "client_secret": "secret-bearer-value"},
        {"command": "echo", "client_secret": "secret-bearer-value"},
    ],
)
def test_invalid_auth_is_rejected_without_exposing_secrets(entry):
    with pytest.raises(ValueError, match="Invalid MCP server") as error:
        build_toolset("remote", entry)
    assert "secret-bearer-value" not in str(error.value)


def test_oauth_allows_non_auth_headers():
    toolset = build_toolset(
        "remote", {"url": URL, "auth": "oauth", "headers": {"X-Tenant": "test"}}
    )
    assert mcp_transport(toolset).headers == {"X-Tenant": "test"}


def test_credential_store_is_owner_only_and_shared_between_processes(tmp_path):
    from fastmcp.client.auth.oauth import TokenStorageAdapter
    from mcp.shared.auth import OAuthToken

    path = tmp_path / "nested" / "mcp-credentials.json"

    async def run():
        writer = TokenStorageAdapter(CredentialStore(path), URL)
        assert await writer.get_tokens() is None
        assert not path.exists()
        await writer.set_tokens(OAuthToken(access_token="a", refresh_token="r", expires_in=60))
        assert oct(path.stat().st_mode & 0o777) == "0o600"
        # Another process (a fresh store over the same file) sees the tokens and
        # a rotation performed there is visible here without a restart.
        reader = TokenStorageAdapter(CredentialStore(path), URL)
        assert (await reader.get_tokens()).refresh_token == "r"
        assert (await reader.get_token_expiry()) is not None
        await reader.set_tokens(OAuthToken(access_token="b", refresh_token="r2"))
        assert (await writer.get_tokens()).refresh_token == "r2"
        # Other servers' credentials survive one server's sign-out.
        other = TokenStorageAdapter(CredentialStore(path), "https://other.example/mcp")
        await other.set_tokens(OAuthToken(access_token="c"))
        await writer.clear()
        assert await reader.get_tokens() is None
        assert (await other.get_tokens()).access_token == "c"
        assert '"a"' not in path.read_text() and "r2" not in path.read_text()

    asyncio.run(run())


def test_credential_store_rejects_unreadable_file_without_echoing_it(tmp_path):
    path = tmp_path / "mcp-credentials.json"
    path.write_text("{not json: secret-bearer-value")

    async def run():
        with pytest.raises(RuntimeError, match="Cannot read MCP credentials") as error:
            await CredentialStore(path).get(key="k", collection="c")
        assert "secret-bearer-value" not in str(error.value)

    asyncio.run(run())


def test_sign_in_persists_across_enable_cycles_and_processes(monkeypatch):
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"mcpServers": {"remote": {"url": URL, "auth": "oauth"}}}))
    before = path.read_bytes()
    providers = []
    interactive_flags = []

    def configured_toolset(name, raw, *, interactive=True):
        toolset = build_toolset(name, raw, interactive=interactive)
        interactive_flags.append(interactive)
        # One fake authorization server, so tokens minted by one "process" are
        # valid for the next; only the browser/callback plumbing is per toolset.
        provider = providers[0] if providers else FakeMCPOAuthProvider()
        provider.install_toolset(toolset)
        providers.append(provider)
        return toolset

    monkeypatch.setattr("pcode.mcp.build_toolset", configured_toolset)

    async def run():
        provider_holder = providers
        state = MCPState()
        assert state.enabled == {}
        await state.enable("remote")
        provider = provider_holder[0]
        first = mcp_transport(state.enabled["remote"]).auth
        assert provider.browser_visits == 1
        assert provider.registrations == 1
        assert credentials_path().exists()
        assert oct(credentials_path().stat().st_mode & 0o777) == "0o600"
        await state.enable("remote")
        assert mcp_transport(state.enabled["remote"]).auth is first
        # Ordinary subsequent MCP connections use the already authenticated client.
        async with state.enabled["remote"]:
            pass
        assert provider.browser_visits == 1
        state.disable("remote")
        # Re-enable, and a fresh process (new MCPState), reuse the saved sign-in and
        # registration without a browser, including when non-interactive.
        await state.enable("remote")
        assert mcp_transport(state.enabled["remote"]).auth is not first
        await MCPState().enable("remote", interactive=False)
        assert provider.browser_visits == 1
        assert provider.registrations == 1
        assert interactive_flags == [True, True, False]
        # The access token expires: the saved refresh token is used silently, and
        # the rotated tokens are saved for the next process.
        await expire_stored_access_token()
        restarted = MCPState()
        await restarted.enable("remote", interactive=False)
        assert provider.grants[-1] == "refresh_token"
        assert provider.browser_visits == 1
        stored = mcp_transport(restarted.enabled["remote"]).auth.token_storage_adapter
        assert (await stored.get_tokens()).access_token == "fake-refreshed-access-token"
        # Signing out forgets the credentials; the next enable needs a browser.
        await restarted.forget("remote")
        assert restarted.enabled == {}
        with pytest.raises(SignInRequired):
            await MCPState().enable("remote", interactive=False)
        assert provider.browser_visits == 1
        await MCPState().enable("remote")
        assert provider.browser_visits == 2
        assert path.read_bytes() == before
        assert sorted(p.name for p in path.parent.iterdir()) == [
            "mcp-credentials.json",
            "mcp-credentials.json.lock",
            path.name,
        ]

    asyncio.run(run())


def test_forget_rejects_unknown_or_non_oauth_servers():
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"mcpServers": {"local": {"command": "echo"}}}))

    async def run():
        state = MCPState()
        with pytest.raises(ValueError, match="Unknown MCP server"):
            await state.forget("missing")
        with pytest.raises(ValueError, match="does not use OAuth"):
            await state.forget("local")
        assert not credentials_path().exists()

    asyncio.run(run())


class FakeOAuthProvider:
    """Real SDK discovery, DCR, PKCE, exchange, and refresh over a mock transport."""

    def __init__(
        self, token_auth_method="none", *, advertised=ISSUER, issuer=ISSUER, registration=True
    ):
        self.token_auth_method = token_auth_method
        # The authorization server the resource metadata names, and the issuer
        # that server's own metadata reports. Real providers can disagree.
        self.advertised = advertised
        self.issuer = issuer
        self.registration = registration
        self.authorization = None
        self.registrations = 0
        self.grants = []
        self.browser_visits = 0
        self.access_token = "fake-access-token"
        self.callback_started = asyncio.Event()
        self.callback_mode = "success"

    async def redirect(self, url):
        assert url.startswith(ISSUER + "/authorize?")
        self.authorization = parse_qs(urlsplit(url).query)
        self.browser_visits += 1
        assert self.authorization["code_challenge_method"] == ["S256"]
        callback_url = urlsplit(self.authorization["redirect_uri"][0])
        assert callback_url.hostname in {"localhost", "127.0.0.1"}
        assert callback_url.path == "/callback"

    async def callback(self):
        self.callback_started.set()
        if self.callback_mode == "wait":
            await asyncio.Event().wait()
        if self.callback_mode == "denied":
            raise RuntimeError("User denied OAuth access")
        return AuthorizationCodeResult(
            code="fake-authorization-code",
            state="wrong-state"
            if self.callback_mode == "bad-state"
            else self.authorization["state"][0],
        )

    async def http(self, request):
        path = request.url.path
        if str(request.url) == URL:
            if request.headers.get("Authorization") != f"Bearer {self.access_token}":
                return httpx2.Response(
                    401,
                    headers={"WWW-Authenticate": f'Bearer resource_metadata="{ISSUER}/resource"'},
                )
            return httpx2.Response(200, json={"ok": True})
        if path == "/resource" or (
            request.url.host == "resource.example"
            and path.startswith("/.well-known/oauth-protected-resource")
        ):
            return httpx2.Response(
                200, json={"resource": URL, "authorization_servers": [self.advertised]}
            )
        if path.startswith("/.well-known/"):
            return httpx2.Response(
                200,
                json={
                    "issuer": self.issuer,
                    "authorization_endpoint": ISSUER + "/authorize",
                    # Not the SDK's guessed `<origin>/token`, as on real servers.
                    "token_endpoint": ISSUER + "/oauth/token",
                    **(
                        {"registration_endpoint": ISSUER + "/register"} if self.registration else {}
                    ),
                    "response_types_supported": ["code"],
                    "grant_types_supported": ["authorization_code", "refresh_token"],
                    "token_endpoint_auth_methods_supported": ["none"],
                    "code_challenge_methods_supported": ["S256"],
                },
            )
        if path == "/register":
            self.registrations += 1
            if not self.registration:
                return httpx2.Response(400, json={"error": "invalid_request"})
            metadata = json.loads(request.content)
            return httpx2.Response(
                201,
                json={
                    **metadata,
                    "client_id": "fake-client",
                    "token_endpoint_auth_method": self.token_auth_method,
                    **(
                        {"client_secret": "fake-client-secret"}
                        if self.token_auth_method != "none"
                        else {}
                    ),
                },
            )
        if str(request.url) == ISSUER + "/oauth/token":
            form = parse_qs(request.content.decode())
            authorization = request.headers.get("Authorization")
            if self.token_auth_method == "client_secret_basic":
                if "client_id" in form or "client_secret" in form:
                    return httpx2.Response(
                        400,
                        json={
                            "error": "invalid_request",
                            "error_description": (
                                "Client must not use multiple authentication methods"
                            ),
                        },
                    )
                expected = base64.b64encode(b"fake-client:fake-client-secret").decode()
                assert authorization == f"Basic {expected}"
            else:
                assert authorization is None
                assert form["client_id"] == ["fake-client"]
                if self.token_auth_method == "client_secret_post":
                    assert form["client_secret"] == ["fake-client-secret"]
                else:
                    assert "client_secret" not in form
            assert int(request.headers["Content-Length"]) == len(request.content)
            grant = form["grant_type"][0]
            self.grants.append(grant)
            if grant == "authorization_code":
                assert form["code"] == ["fake-authorization-code"]
                challenge = (
                    base64.urlsafe_b64encode(
                        hashlib.sha256(form["code_verifier"][0].encode()).digest()
                    )
                    .rstrip(b"=")
                    .decode()
                )
                assert self.authorization["code_challenge"] == [challenge]
            else:
                assert grant == "refresh_token"
                assert form["refresh_token"] == ["fake-refresh-token"]
                self.access_token = "fake-refreshed-access-token"
            return httpx2.Response(
                200,
                json={
                    "access_token": self.access_token,
                    "token_type": "Bearer",
                    "refresh_token": "fake-refresh-token",
                    "expires_in": 3600,
                },
            )
        pytest.fail(f"Unexpected fake provider request: {request.method} {request.url}")

    def install(self, auth):
        # Replace just browser/callback I/O. OAuth state validation and PKCE stay
        # native, as do pcode's gate on non-interactive sign-in and its callback
        # socket ownership, which the real redirect handler performs before browsing.
        async def redirect(url):
            if not getattr(auth, "interactive", True):
                raise SignInRequired("This MCP server needs a browser sign-in.")
            await auth._reserve_callback()
            await self.redirect(url)

        auth.context.redirect_handler = redirect
        auth.context.callback_handler = self.callback
        return httpx2.MockTransport(self.http)


@pytest.mark.parametrize("method", ["none", "client_secret_post", "client_secret_basic"])
@pytest.mark.parametrize("registered", [False, True])
def test_native_oauth_exchange_reuse_and_refresh(method, registered):
    async def run():
        toolset = build_toolset("remote", {"url": URL, "auth": "oauth"})
        auth = mcp_transport(toolset).auth
        provider = FakeOAuthProvider(method)
        if registered:
            # A failed exchange leaves a valid registration but no tokens. A
            # retry must use it without requiring logout or re-registration.
            from mcp.shared.auth import OAuthClientInformationFull

            await auth.token_storage_adapter.set_client_info(
                OAuthClientInformationFull(
                    client_id="fake-client",
                    client_secret="fake-client-secret" if method != "none" else None,
                    token_endpoint_auth_method=method,
                    redirect_uris=auth.context.client_metadata.redirect_uris,
                )
            )
        transport = provider.install(auth)
        async with httpx2.AsyncClient(auth=auth, transport=transport) as client:
            response = await client.get(URL)
            assert response.status_code == 200
            assert provider.grants == ["authorization_code"]
            assert provider.browser_visits == 1
            assert provider.registrations == (0 if registered else 1)
        # A new HTTP connection reuses the enabled toolset's OAuth object.
        async with httpx2.AsyncClient(auth=auth, transport=transport) as client:
            assert (await client.get(URL)).status_code == 200
            assert provider.browser_visits == 1
            auth.context.token_expiry_time = 1
            assert (await client.get(URL)).status_code == 200
            assert provider.grants == ["authorization_code", "refresh_token"]
            assert provider.browser_visits == 1
        assert (await auth.token_storage_adapter.get_tokens()).access_token == provider.access_token
        # A fresh OAuth instance must reuse the saved registration and its auth
        # method, including when refreshing without an interactive browser.
        await expire_stored_access_token()
        restarted = mcp_transport(
            build_toolset("remote", {"url": URL, "auth": "oauth"}, interactive=False)
        ).auth
        transport = provider.install(restarted)
        async with httpx2.AsyncClient(auth=restarted, transport=transport) as client:
            assert (await client.get(URL)).status_code == 200
        assert provider.registrations == (0 if registered else 1)
        assert provider.browser_visits == 1
        assert provider.grants == ["authorization_code", "refresh_token", "refresh_token"]

    asyncio.run(run())


@pytest.mark.parametrize(("advertised", "issuer"), [(ISSUER + "/", ISSUER), (ISSUER, ISSUER + "/")])
def test_root_issuer_matches_with_or_without_trailing_slash(advertised, issuer):
    """Google's Drive MCP metadata names `https://accounts.google.com/` as its
    authorization server, whose own metadata says `https://accounts.google.com`."""

    async def run():
        auth = mcp_transport(build_toolset("remote", {"url": URL, "auth": "oauth"})).auth
        provider = FakeOAuthProvider(advertised=advertised, issuer=issuer)
        async with httpx2.AsyncClient(auth=auth, transport=provider.install(auth)) as client:
            assert (await client.get(URL)).status_code == 200
        assert provider.grants == ["authorization_code"]

    asyncio.run(run())


@pytest.mark.parametrize(
    ("advertised", "issuer"),
    [(ISSUER + "/tenant", ISSUER), (ISSUER, ISSUER + "/tenant"), (ISSUER, "https://evil.example")],
)
def test_other_issuer_mismatches_are_still_rejected(advertised, issuer):
    from mcp.client.auth.exceptions import OAuthFlowError

    async def run():
        auth = mcp_transport(build_toolset("remote", {"url": URL, "auth": "oauth"})).auth
        provider = FakeOAuthProvider(advertised=advertised, issuer=issuer)
        async with httpx2.AsyncClient(auth=auth, transport=provider.install(auth)) as client:
            with pytest.raises(OAuthFlowError, match="issuer mismatch"):
                await client.get(URL)
        assert provider.browser_visits == 0
        assert provider.registrations == 0

    asyncio.run(run())


PREREGISTERED = {
    "url": URL,
    "auth": "oauth",
    "client_id": "fake-client",
    "client_secret": "${FAKE_CLIENT_SECRET}",
}


@pytest.mark.parametrize("advertised", [ISSUER, ISSUER + "/"])
def test_preregistered_client_skips_dynamic_registration(monkeypatch, advertised):
    """Google offers no dynamic registration: its MCP servers need a client created
    in the Cloud console, configured with its secret kept in the environment."""
    monkeypatch.setenv("FAKE_CLIENT_SECRET", "fake-client-secret")

    async def run():
        auth = mcp_transport(build_toolset("remote", PREREGISTERED)).auth
        provider = FakeOAuthProvider(
            "client_secret_post", advertised=advertised, registration=False
        )
        async with httpx2.AsyncClient(auth=auth, transport=provider.install(auth)) as client:
            assert (await client.get(URL)).status_code == 200
        assert provider.grants == ["authorization_code"]
        # A later process refreshes with the same client and never registers.
        await expire_stored_access_token()
        restarted = mcp_transport(build_toolset("remote", PREREGISTERED, interactive=False)).auth
        transport = provider.install(restarted)
        async with httpx2.AsyncClient(auth=restarted, transport=transport) as client:
            assert (await client.get(URL)).status_code == 200
        assert provider.grants == ["authorization_code", "refresh_token"]
        assert provider.browser_visits == 1
        assert provider.registrations == 0

    asyncio.run(run())


@pytest.mark.parametrize("taken", [False, True])
def test_preregistered_client_redirect_follows_the_reserved_socket(monkeypatch, taken):
    """A pre-registered client may use any loopback port (RFC 8252 section 7.3), so
    a port saved by an earlier process must neither redirect the browser away
    from pcode's listener nor, when now taken, block the sign-in."""
    import socket

    from mcp.shared.auth import OAuthClientInformationFull
    from pydantic import AnyHttpUrl

    monkeypatch.setenv("FAKE_CLIENT_SECRET", "fake-client-secret")

    async def run():
        auth = mcp_transport(build_toolset("remote", PREREGISTERED)).auth
        provider = FakeOAuthProvider("client_secret_post", registration=False)
        transport = provider.install(auth)
        installed = auth.context.redirect_handler

        async def redirect(url):
            await installed(url)
            served = auth._callback_socket.getsockname()[1]
            assert urlsplit(provider.authorization["redirect_uri"][0]).port == served

        auth.context.redirect_handler = redirect
        with socket.socket() as earlier:
            earlier.bind(("127.0.0.1", 0))
            stale_port = earlier.getsockname()[1]
            if taken:
                earlier.listen()
            else:
                earlier.close()  # Free again, as the earlier process left it.
            # An earlier process saved the client with the callback it used then,
            # and its sign-in never finished, so there are no tokens.
            await auth.token_storage_adapter.set_client_info(
                OAuthClientInformationFull(
                    client_id="fake-client",
                    client_secret="fake-client-secret",
                    token_endpoint_auth_method="client_secret_post",
                    redirect_uris=[AnyHttpUrl(f"http://127.0.0.1:{stale_port}/callback")],
                )
            )
            async with httpx2.AsyncClient(auth=auth, transport=transport) as client:
                assert (await client.get(URL)).status_code == 200
        assert provider.browser_visits == 1
        assert provider.registrations == 0

    asyncio.run(run())


@pytest.mark.parametrize("mode", ["denied", "bad-state", "wait"])
def test_native_oauth_failure_or_cancellation_never_exchanges_a_code(mode):
    async def run():
        auth = mcp_transport(build_toolset("remote", {"url": URL, "auth": "oauth"})).auth
        provider = FakeOAuthProvider()
        provider.callback_mode = mode
        async with httpx2.AsyncClient(auth=auth, transport=provider.install(auth)) as client:
            if mode == "wait":
                task = asyncio.create_task(client.get(URL))
                await asyncio.wait_for(provider.callback_started.wait(), 5)
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            else:
                with pytest.raises(Exception, match="denied|State parameter mismatch"):
                    await client.get(URL)
        assert provider.grants == []
        assert await auth.token_storage_adapter.get_tokens() is None
        assert auth._callback_socket is None

    asyncio.run(run())


@pytest.mark.parametrize("cancel", [False, True])
def test_native_loopback_callback_closes_listener(cancel):
    """Exercise FastMCP's actual callback server, not a mock browser or real account."""

    async def run():
        auth = mcp_transport(build_toolset("remote", {"url": URL, "auth": "oauth"})).auth
        callback_url = f"http://localhost:{auth.redirect_port}/callback"
        auth._expected_state = "test-state"
        task = asyncio.create_task(auth.callback_handler())
        try:
            async with httpx2.AsyncClient(trust_env=False, timeout=1) as client:
                async with asyncio.timeout(5):
                    while True:
                        if task.done():
                            await task  # Surface startup errors rather than a polling timeout.
                        try:
                            response = await client.get(callback_url.replace("/callback", "/ready"))
                            if response.status_code == 404:
                                break
                        except httpx2.ConnectError:
                            pass
                        await asyncio.sleep(0.01)
                stale = await client.get(
                    callback_url, params={"code": "old-code", "state": "previous-attempt"}
                )
                assert stale.status_code == 400
                assert not task.done()
                if cancel:
                    task.cancel()
                    with pytest.raises(asyncio.CancelledError):
                        await task
                else:
                    response = await client.get(
                        callback_url, params={"code": "test-code", "state": "test-state"}
                    )
                    assert response.status_code == 200
                    result = await task
                    assert result.code == "test-code"
                    assert result.state == "test-state"
                with pytest.raises(httpx2.ConnectError):
                    await client.get(callback_url)
        finally:
            if not task.done():
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task

    asyncio.run(run())


@pytest.mark.parametrize("outcome", ["success", "denied"])
def test_stale_callbacks_do_not_end_current_sign_in(monkeypatch, tmp_path, outcome):
    """Real loopback callbacks and SDK PKCE with an isolated configuration directory."""
    default_credentials = credentials_path()
    config_dir = tmp_path / "separate-config"
    monkeypatch.setenv("PCODE_CONFIG_DIR", str(config_dir))
    assert credentials_path() == config_dir / "mcp-credentials.json"
    assert config_path() == config_dir / "mcp.json"

    async def run():
        auth = mcp_transport(build_toolset("remote", {"url": URL, "auth": "oauth"})).auth
        provider = FakeOAuthProvider()
        browser_task = None

        async def browser_callback():
            url = provider.authorization["redirect_uri"][0]
            state = provider.authorization["state"][0]
            async with httpx2.AsyncClient(trust_env=False, timeout=5) as client:
                for params in (
                    {"code": "old-code", "state": "previous-attempt"},
                    {"error": "access_denied", "state": "previous-attempt"},
                    {"code": "old-code"},
                    {"code": "old-code", "state": ""},
                    {"code": "old-code", "state": "non-ascii-\u2603"},
                    [("code", "old-code"), ("state", state), ("state", state)],
                ):
                    response = await client.get(url, params=params)
                    assert response.status_code == 400
                    assert "newest sign-in tab" in response.text
                    assert state not in response.text
                    assert response.headers["cache-control"] == "no-store"
                    assert provider.grants == []
                    assert await auth.token_storage_adapter.get_tokens() is None
                params = {"state": state}
                params.update(
                    {"code": "fake-authorization-code"}
                    if outcome == "success"
                    else {"error": "access_denied"}
                )
                response = await client.get(url, params=params)
                assert response.status_code == (200 if outcome == "success" else 400)

        async def redirect(self, url):
            nonlocal browser_task
            await provider.redirect(url)
            browser_task = asyncio.create_task(browser_callback())

        # Keep pcode's redirect handler and the native callback server; replace
        # only FastMCP's external browser/preflight I/O.
        monkeypatch.setattr(OAuth, "redirect_handler", redirect)
        try:
            async with asyncio.timeout(10):
                async with httpx2.AsyncClient(
                    auth=auth, transport=httpx2.MockTransport(provider.http)
                ) as client:
                    if outcome == "success":
                        assert (await client.get(URL)).status_code == 200
                    else:
                        from pcode.diagnostics import error_details

                        with pytest.raises(Exception) as error:
                            await client.get(URL)
                        assert "Access was denied" in str(error_details(error.value))
                await browser_task
            assert provider.grants == (["authorization_code"] if outcome == "success" else [])
            assert auth._callback_socket is None
            assert auth._expected_state is None
            assert credentials_path().exists()
            assert not default_credentials.exists()
        finally:
            if browser_task is not None and not browser_task.done():
                browser_task.cancel()
                await asyncio.gather(browser_task, return_exceptions=True)

    asyncio.run(run())


def test_callback_port_collision_is_replaced_before_registration():
    """Reproduce a port being taken after /mcp enable, before OAuth starts."""
    import signal
    import socket

    async def run():
        auth = mcp_transport(build_toolset("remote", {"url": URL, "auth": "oauth"})).auth
        provider = FakeOAuthProvider()
        browser_task = None
        original_signals = [signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)]
        registered_redirects = []

        async def http(request):
            if request.url.path == "/register":
                redirect = json.loads(request.content)["redirect_uris"][0]
                registered_redirects.append(redirect)
                # This must be an owned listening socket, not a free-port probe.
                with socket.socket() as competing:
                    competing.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                    with pytest.raises(OSError):
                        competing.bind(("127.0.0.1", urlsplit(redirect).port))
            return await provider.http(request)

        async def browser_callback():
            redirect = provider.authorization["redirect_uri"][0]
            assert registered_redirects == [redirect]
            assert urlsplit(redirect).hostname == "127.0.0.1"
            async with httpx2.AsyncClient(trust_env=False, timeout=5) as client:
                response = await client.get(
                    redirect,
                    params={
                        "code": "fake-authorization-code",
                        "state": provider.authorization["state"][0],
                    },
                )
                assert response.status_code == 200

        async def redirect(url):
            nonlocal browser_task
            auth._expected_state = parse_qs(urlsplit(url).query)["state"][0]
            await provider.redirect(url)
            browser_task = asyncio.create_task(browser_callback())

        auth.context.redirect_handler = redirect
        with socket.socket() as occupied:
            occupied.bind(("127.0.0.1", 0))
            occupied.listen()
            old_port = occupied.getsockname()[1]
            auth.redirect_port = old_port
            try:
                async with httpx2.AsyncClient(
                    auth=auth, transport=httpx2.MockTransport(http)
                ) as client:
                    assert (await client.get(URL)).status_code == 200
                await browser_task
                assert auth.redirect_port != old_port
                assert occupied.fileno() != -1  # Never close someone else's listener.
                assert auth._callback_socket is None
                with socket.socket() as reusable:
                    reusable.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                    reusable.bind(("127.0.0.1", auth.redirect_port))
                assert original_signals == [
                    signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)
                ]
            finally:
                if browser_task is not None and not browser_task.done():
                    browser_task.cancel()
                    await asyncio.gather(browser_task, return_exceptions=True)

    asyncio.run(run())


def test_registered_port_collision_is_recoverable_without_browser():
    import socket

    async def run():
        auth = mcp_transport(build_toolset("remote", {"url": URL, "auth": "oauth"})).auth
        provider = FakeOAuthProvider()
        async with httpx2.AsyncClient(auth=auth, transport=provider.install(auth)) as client:
            assert (await client.get(URL)).status_code == 200
        # Model a server requiring a new authorization while the original redirect
        # is registered. Silently changing its port would violate that registration.
        with socket.socket() as occupied:
            occupied.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            occupied.bind(("127.0.0.1", auth.redirect_port))
            occupied.listen()
            with pytest.raises(RuntimeError, match="Retry sign-in"):
                await auth._reserve_callback()
        assert provider.browser_visits == 1
        assert auth._callback_socket is None
        # The stale registration is forgotten so the retry registers on a free port.
        assert await auth.token_storage_adapter.get_client_info() is None
        assert await auth.token_storage_adapter.get_tokens() is None

    asyncio.run(run())


def test_restart_reuses_the_registered_callback_port():
    """A saved registration names a redirect port; a new process must serve that one."""

    async def run():
        provider = FakeOAuthProvider()
        auth = mcp_transport(build_toolset("remote", {"url": URL, "auth": "oauth"})).auth
        async with httpx2.AsyncClient(auth=auth, transport=provider.install(auth)) as client:
            assert (await client.get(URL)).status_code == 200
        registered_port = urlsplit(provider.authorization["redirect_uri"][0]).port
        # A refresh that the server rejects forces a new authorization after restart.
        restarted = mcp_transport(build_toolset("remote", {"url": URL, "auth": "oauth"})).auth
        provider.install(restarted)  # Fake browser/callback only; the transport is below.
        original = provider.http

        async def http(request):
            if request.url.path == "/oauth/token":
                form = parse_qs(request.content.decode())
                if form["grant_type"] == ["refresh_token"]:
                    return httpx2.Response(400, json={"error": "invalid_grant"})
            return await original(request)

        provider.http = http
        transport = httpx2.MockTransport(http)
        await expire_stored_access_token()
        async with httpx2.AsyncClient(auth=restarted, transport=transport) as client:
            assert (await client.get(URL)).status_code == 200
        assert provider.browser_visits == 2
        assert provider.registrations == 1
        assert urlsplit(provider.authorization["redirect_uri"][0]).port == registered_port

    asyncio.run(run())


async def _exercise_callback_startup_failure():
    """Run in a child process: a regressed SystemExit must not kill pytest itself."""
    from unittest.mock import patch

    from pcode.diagnostics import error_details

    toolset = build_toolset("remote", {"url": URL, "auth": "oauth"})
    transport = mcp_transport(toolset)
    auth = transport.auth
    provider = FakeOAuthProvider()
    auth.context.redirect_handler = provider.redirect
    transport.httpx_client_factory = lambda **kwargs: httpx2.AsyncClient(
        transport=httpx2.MockTransport(provider.http), **kwargs
    )

    async def fail_startup(self, sockets=None):
        raise SystemExit(3)

    loop_errors = []
    asyncio.get_running_loop().set_exception_handler(
        lambda loop, context: loop_errors.append(context)
    )
    with patch("uvicorn.Server.serve", fail_startup):
        try:
            async with toolset:
                raise AssertionError("Broken callback server unexpectedly connected")
        except Exception as error:
            assert "callback server could not start" in str(error_details(error))
        else:
            raise AssertionError("Expected a normal, recoverable exception")
    await asyncio.sleep(0)
    assert auth._callback_socket is None
    assert not loop_errors
    print("callback failure handled without terminating pcode")


def test_embedded_callback_systemexit_does_not_kill_mcp_client():
    import subprocess
    import sys
    from pathlib import Path

    code = (
        "import asyncio, runpy; "
        f"ns = runpy.run_path({str(Path(__file__).resolve())!r}); "
        "asyncio.run(ns['_exercise_callback_startup_failure']())"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=15
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "callback failure handled" in result.stdout
    assert "unhandled exception during asyncio.run() shutdown" not in result.stderr


def test_callback_timeout_releases_reserved_port():
    import socket

    from pcode.diagnostics import error_details

    async def run():
        auth = mcp_transport(build_toolset("remote", {"url": URL, "auth": "oauth"})).auth
        auth._callback_timeout = 0.05
        with pytest.raises(Exception) as error:
            await auth.callback_handler()
        assert error_details(error.value)["type"] == "TimeoutError"
        assert auth._callback_socket is None
        with socket.socket() as reusable:
            reusable.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            reusable.bind(("127.0.0.1", auth.redirect_port))

    asyncio.run(run())


def test_non_interactive_enable_never_opens_a_browser(monkeypatch):
    monkeypatch.setattr(
        "webbrowser.open", lambda *a, **k: pytest.fail("non-interactive must not browse")
    )

    async def run():
        auth = mcp_transport(
            build_toolset("remote", {"url": URL, "auth": "oauth"}, interactive=False)
        ).auth
        provider = FakeOAuthProvider()
        transport = provider.install(auth)
        # The fake provider never browses; exercise pcode's own gate here.
        auth.context.redirect_handler = auth.redirect_handler
        async with httpx2.AsyncClient(auth=auth, transport=transport) as client:
            with pytest.raises(SignInRequired):
                await client.get(URL)
        # The MCP session reports only "Connection closed"; the cause stays here.
        assert isinstance(auth.failure, SignInRequired)
        assert provider.browser_visits == 0
        assert provider.grants == []
        assert auth._callback_socket is None
        assert await auth.token_storage_adapter.get_tokens() is None

    asyncio.run(run())


class FakeMCPOAuthProvider(FakeOAuthProvider):
    """Fake OAuth provider plus the real MCP HTTP initialization exchange."""

    async def http(self, request):
        if (
            str(request.url) == URL
            and request.headers.get("Authorization") == f"Bearer {self.access_token}"
        ):
            if request.method != "POST":
                return httpx2.Response(405)
            message = json.loads(request.content)
            if "id" not in message:
                return httpx2.Response(202)
            if message["method"] == "initialize":
                result = {
                    "protocolVersion": message["params"]["protocolVersion"],
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "fake-oauth", "version": "1"},
                }
            elif message["method"] == "tools/list":
                result = {"tools": []}
            else:
                result = {}
            return httpx2.Response(
                200, json={"jsonrpc": "2.0", "id": message["id"], "result": result}
            )
        return await super().http(request)

    def install_toolset(self, toolset):
        transport = mcp_transport(toolset)
        self.install(transport.auth)
        transport.httpx_client_factory = lambda **kwargs: httpx2.AsyncClient(
            transport=httpx2.MockTransport(self.http), **kwargs
        )


def test_enable_allows_browser_sign_in_longer_than_default_init_timeout(monkeypatch):
    """The ordinary five-second initialize deadline must not cancel browser sign-in."""
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"mcpServers": {"remote": {"url": URL, "auth": "oauth"}}}))

    async def run():
        state = MCPState()
        toolset = build_toolset("remote", {"url": URL, "auth": "oauth"})
        provider = FakeMCPOAuthProvider()
        provider.install_toolset(toolset)

        async def slow_browser():
            await asyncio.sleep(5.1)
            return await provider.callback()

        mcp_transport(toolset).auth.context.callback_handler = slow_browser
        monkeypatch.setattr("pcode.mcp.build_toolset", lambda name, raw, **kw: toolset)
        async with asyncio.timeout(10):
            await state.enable("remote")
        assert list(state.enabled.values()) == [toolset]
        assert provider.grants == ["authorization_code"]

    asyncio.run(run())


@pytest.mark.parametrize("mode", ["denied", "bad-state", "wait"])
def test_enable_oauth_failure_or_cancel_leaves_server_off(monkeypatch, mode):
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"mcpServers": {"remote": {"url": URL, "auth": "oauth"}}}))

    async def run():
        state = MCPState()
        toolset = build_toolset("remote", {"url": URL, "auth": "oauth"})
        provider = FakeMCPOAuthProvider()
        provider.callback_mode = mode
        provider.install_toolset(toolset)
        monkeypatch.setattr("pcode.mcp.build_toolset", lambda name, raw, **kw: toolset)
        if mode == "wait":
            task = asyncio.create_task(state.enable("remote"))
            try:
                await asyncio.wait_for(provider.callback_started.wait(), 5)
                assert state.enabled == {}
            finally:
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
        else:
            with pytest.raises(Exception):
                await state.enable("remote")
        assert state.enabled == {}
        assert mcp_transport(toolset).auth._callback_socket is None
        # Retry after failure/cancellation should succeed, without poisoning the client.
        provider.callback_mode = "success"
        await state.enable("remote")
        assert list(state.enabled.values()) == [toolset]

    asyncio.run(run())


@pytest.mark.parametrize("outcome", ["success", "failure", "cancel"])
def test_enable_publishes_only_after_connection_teardown(monkeypatch, outcome):
    from types import SimpleNamespace

    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"mcpServers": {"remote": {"url": URL, "auth": "oauth"}}}))

    async def run():
        exiting = asyncio.Event()
        finish = asyncio.Event()

        class Connection:
            wrapped = SimpleNamespace(
                client=SimpleNamespace(transport=SimpleNamespace(auth=object()))
            )

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                exiting.set()
                await finish.wait()
                if outcome == "failure":
                    raise RuntimeError("Connection teardown failed")

        connection = Connection()
        monkeypatch.setattr("pcode.mcp.build_toolset", lambda name, raw, **kw: connection)
        state = MCPState()
        task = asyncio.create_task(state.enable("remote"))
        try:
            await asyncio.wait_for(exiting.wait(), 5)
            assert state.enabled == {}
            if outcome == "cancel":
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            elif outcome == "failure":
                finish.set()
                with pytest.raises(RuntimeError, match="teardown failed"):
                    await task
            else:
                finish.set()
                await task
            assert list(state.enabled.values()) == ([connection] if outcome == "success" else [])
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    asyncio.run(run())
