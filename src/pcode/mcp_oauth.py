"""Keep native OAuth, but own the loopback socket and the credential file.

FastMCP 4.0.4 probes/closes a port at construction, then Uvicorn binds it much
later. A collision raises SystemExit inside its background task. Reserve the
actual socket before registration and pass it to an embedded, signal-free server.

Tokens and the dynamic client registration outlive the process in one
owner-only JSON file, like pcode's Codex sign-in, so a server can be enabled
at startup without a browser round trip. No keychain: it locks over SSH, prompts
after upgrades, and other tools sharing the item have wiped MCP sign-ins.
"""

import asyncio
import json
import logging
import os
import secrets
import socket
import tempfile
from contextlib import aclosing, nullcontext
from pathlib import Path
from urllib.parse import parse_qs, parse_qsl, urlencode, urlsplit

import anyio
import httpx2
import mcp.client.auth.oauth2 as sdk_oauth
from fastmcp.client.auth import OAuth
from fastmcp.client.oauth_callback import OAuthCallbackResult, create_oauth_callback_server
from filelock import FileLock
from key_value.aio.stores.base import BaseStore
from mcp.client.auth.exceptions import OAuthFlowError
from mcp.client.auth.utils import (
    build_oauth_authorization_server_metadata_discovery_urls,
    build_protected_resource_metadata_discovery_urls,
    create_oauth_metadata_request,
    credentials_match_issuer,
    handle_auth_metadata_response,
    handle_protected_resource_response,
    issuers_match,
    validate_metadata_issuer,
)
from mcp.shared.auth import AuthorizationCodeResult, OAuthMetadata
from pydantic import AnyHttpUrl
from starlette.requests import Request
from starlette.responses import PlainTextResponse
from uvicorn import Server

from pcode.preferences import preferences_path

_HOST = "127.0.0.1"
logger = logging.getLogger(__name__)


class SignInRequired(RuntimeError):
    """The server wants a browser login, which a non-interactive enable must not start."""


def _validate_metadata_issuer(oauth_metadata: OAuthMetadata, expected_issuer: str) -> None:
    """Accept a root issuer written with or without its trailing slash.

    The SDK treats `https://host` and `https://host/` as one server when binding
    stored credentials and on its legacy discovery path, but compares strictly
    when resource metadata names the server. Google's MCP servers name
    `https://accounts.google.com/` there while Google's metadata says
    `https://accounts.google.com`, so every sign-in failed. Any other mismatch
    still fails the SDK's own check.
    """
    if not issuers_match(str(oauth_metadata.issuer), expected_issuer):
        validate_metadata_issuer(oauth_metadata, expected_issuer)


sdk_oauth.validate_metadata_issuer = _validate_metadata_issuer


def credentials_path() -> Path:
    return preferences_path().with_name("mcp-credentials.json")


class CredentialStore(BaseStore):
    """FastMCP's AsyncKeyValue over one 0600 JSON file, keyed by server URL.

    Every operation re-reads the file under a lock, so concurrent pcode processes
    see each other's refreshed tokens instead of clobbering them with stale ones.
    """

    def __init__(self, path: Path | None = None) -> None:
        super().__init__(stable_api=True)
        self._path = path or credentials_path()
        self._lock = FileLock(f"{self._path}.lock", mode=0o600)

    def _read(self) -> dict:
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except OSError, ValueError:
            raise RuntimeError(f"Cannot read MCP credentials: {self._path}") from None
        return data if isinstance(data, dict) else {}

    def _write(self, data: dict) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        name = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", dir=self._path.parent, delete=False, encoding="utf-8"
            ) as file:
                name = file.name
                # Restrict before any token bytes reach the filesystem.
                os.chmod(file.fileno(), 0o600)
                json.dump(data, file, indent=2)
                file.write("\n")
            os.replace(name, self._path)
            name = None
        finally:
            if name is not None:
                os.unlink(name)

    async def _get_managed_entry(self, *, collection: str, key: str):
        with self._lock:
            raw = self._read().get(collection, {}).get(key)
        if not isinstance(raw, dict):
            return None
        return self._serialization_adapter.load_dict(data=raw)

    async def _put_managed_entry(self, *, collection: str, key: str, managed_entry) -> None:
        with self._lock:
            data = self._read()
            data.setdefault(collection, {})[key] = self._serialization_adapter.dump_dict(
                entry=managed_entry
            )
            self._write(data)

    async def _delete_managed_entry(self, *, key: str, collection: str) -> bool:
        with self._lock:
            data = self._read()
            removed = data.get(collection, {}).pop(key, None) is not None
            if removed:
                self._write(data)
        return removed

    async def _get_collection_keys(self, *, collection: str, limit: int | None = None) -> list[str]:
        with self._lock:
            keys = list(self._read().get(collection, {}))
        return keys[:limit] if limit is not None else keys


class _CallbackServer(Server):
    def capture_signals(self):
        # This is a component of pcode, not a process-wide web server. Ctrl+C must
        # keep reaching pcode's cancellation handler, even with multiple MCP clients.
        return nullcontext()

    async def serve(self, sockets=None):
        try:
            await super().serve(sockets=sockets)
        except SystemExit:
            # Catch inside the spawned task: catching outside a TaskGroup is too
            # late to stop asyncio treating SystemExit as process termination.
            raise RuntimeError("MCP OAuth callback server could not start.") from None
        finally:
            for listener in getattr(self, "servers", []):
                listener.close()
                await listener.wait_closed()


def _single_token_auth(request: httpx2.Request) -> httpx2.Request:
    """Keep Basic credentials exclusively in the header of native token requests.

    The SDK removes client_secret for Basic auth but leaves client_id in the
    form, which strict servers reject as a second authentication method. Limit
    this workaround to token builders; resource requests remain untouched.
    """
    if request.headers.get("Authorization", "").partition(" ")[0].lower() != "basic":
        return request
    form = parse_qsl(request.content.decode(), keep_blank_values=True)
    filtered = [(key, value) for key, value in form if key not in {"client_id", "client_secret"}]
    if filtered == form:
        return request
    headers = request.headers.copy()
    del headers["Content-Length"]
    return httpx2.Request(
        request.method,
        request.url,
        content=urlencode(filtered).encode(),
        headers=headers,
        extensions=request.extensions,
    )


class LoopbackOAuth(OAuth):
    """FastMCP handles protocol/security; this adapter owns callback I/O lifetime."""

    def __init__(
        self,
        store: CredentialStore | None = None,
        *,
        interactive: bool = True,
        client_id: str | None = None,
        client_secret: str | None = None,
    ):
        super().__init__(
            client_name="pcode",
            callback_host=_HOST,
            token_storage=store or CredentialStore(),
            client_id=client_id,
            client_secret=client_secret,
        )
        self.interactive = interactive
        # Only a dynamic registration pins a callback port. A client created
        # ahead of time may use any loopback port (RFC 8252 section 7.3), so the
        # socket pcode reserves decides, not a port saved by an earlier process.
        self._preregistered = client_id is not None
        self._callback_socket: socket.socket | None = None
        self._flow_lock = asyncio.Lock()
        self._expected_state: str | None = None
        # The error that ended the latest sign-in attempt, until a request gets
        # through. A call that triggers it sees only "Connection closed" from the
        # MCP session, so the caller reads the real cause here (see
        # `NamedServer.call_tool`). Shared by concurrent calls, so never cleared on
        # entry or on read.
        self.failure: Exception | None = None

    async def _exchange_token_authorization_code(
        self, auth_code: str, code_verifier: str
    ) -> httpx2.Request:
        return _single_token_auth(
            await super()._exchange_token_authorization_code(auth_code, code_verifier)
        )

    async def _refresh_token(self) -> httpx2.Request:
        return _single_token_auth(await super()._refresh_token())

    def _adopt_registered_port(self, registered) -> int:
        """A registration from an earlier process names its own callback port; the
        authorization and token requests must present that same redirect URI."""
        if registered is not None and registered.redirect_uris:
            port = registered.redirect_uris[0].port
            if port and port != self.redirect_port:
                self.redirect_port = port
                self.context.client_metadata.redirect_uris = [
                    AnyHttpUrl(f"http://{_HOST}:{port}/callback")
                ]
        return self.redirect_port

    async def _initialize(self) -> None:
        # The SDK builds the authorization URL from client_metadata before calling
        # redirect_handler, so the saved registration's port must be adopted here.
        await super()._initialize()
        if not self._preregistered:
            self._adopt_registered_port(self.context.client_info)

    async def _auth_flow(self, request):
        # The SDK's redirect-following wrapper drives this, so discovery requests
        # follow same-origin redirects exactly as the SDK's own discovery does.
        for flow in (self._discover_before_refresh(), super()._auth_flow(request)):
            async with aclosing(flow):
                try:
                    outgoing = await anext(flow)
                    while True:
                        response = yield outgoing
                        outgoing = await flow.asend(response)
                except StopAsyncIteration:
                    pass

    async def _discover_before_refresh(self):
        """Find the real token endpoint before refreshing tokens loaded from disk.

        The SDK discovers server metadata only after a 401, so a refresh right
        after a restart posts to a guessed `<origin>/token`. Servers whose token
        endpoint lives elsewhere answer with an HTML page (or a 404), the refresh
        fails, and an otherwise valid sign-in turns into a browser round trip.
        Mirrors the SDK's discovery without a challenge. Any doubt changes no
        state, so the SDK's own fallback and 401 handling still apply.
        """
        if not self._initialized:
            await self._initialize()
        context = self.context
        if (
            context.oauth_metadata is not None
            or context.is_token_valid()
            or not context.can_refresh_token()
        ):
            return
        prm = auth_server_url = None
        try:
            for url in build_protected_resource_metadata_discovery_urls(None, context.server_url):
                response = yield create_oauth_metadata_request(url)
                if response.status_code >= 500 or response.status_code == 429:
                    raise OAuthFlowError(f"resource metadata: HTTP {response.status_code}")
                if prm := await handle_protected_resource_response(response):
                    await self._validate_resource_match(prm)
                    auth_server_url = self._select_authorization_server(
                        [str(server) for server in prm.authorization_servers]
                    )
                    break
            expected = auth_server_url or self._expected_issuer()
            if context.client_info is not None and not credentials_match_issuer(
                context.client_info, expected, context.client_metadata_url
            ):
                raise OAuthFlowError("stored registration belongs to another server")
            for url in build_oauth_authorization_server_metadata_discovery_urls(
                auth_server_url, context.server_url
            ):
                response = yield create_oauth_metadata_request(url)
                ok, asm = await handle_auth_metadata_response(response)
                if not ok:
                    raise OAuthFlowError(f"server metadata: HTTP {response.status_code}")
                if asm is not None:
                    if auth_server_url is None and issuers_match(str(asm.issuer), expected):
                        expected = str(asm.issuer)
                    _validate_metadata_issuer(asm, expected)
                    break
            else:
                raise OAuthFlowError("server metadata not found")
        except OAuthFlowError as error:
            logger.debug("Token endpoint discovery before refresh gave up: %s", error)
            return
        context.protected_resource_metadata = prm
        context.auth_server_url = auth_server_url
        context.oauth_metadata = asm

    async def _reserve_callback(self) -> socket.socket:
        if self._callback_socket is not None:
            return self._callback_socket
        registered = (
            None if self._preregistered else await self.token_storage_adapter.get_client_info()
        )
        port = self._adopt_registered_port(registered)
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind((_HOST, port))
            except OSError:
                if registered is not None:
                    # A registered redirect URI cannot be silently changed. Forget
                    # the registration (its tokens are already unusable here) so the
                    # next attempt registers afresh on a free port.
                    await self.token_storage_adapter.clear()
                    self.context.client_info = None
                    raise RuntimeError(
                        "MCP OAuth callback port is unavailable. Retry sign-in."
                    ) from None
                sock.bind((_HOST, 0))
            sock.listen()
            sock.setblocking(False)
            self.redirect_port = sock.getsockname()[1]
            self.context.client_metadata.redirect_uris = [
                AnyHttpUrl(f"http://{_HOST}:{self.redirect_port}/callback")
            ]
            self._callback_socket = sock
            return sock
        except BaseException:
            sock.close()
            raise

    def _close_callback(self) -> None:
        self._expected_state = None
        if self._callback_socket is not None:
            self._callback_socket.close()
            self._callback_socket = None

    async def async_auth_flow(self, request):
        # The SDK already serializes its protocol flow. Include our reservation
        # in that lifetime too, so concurrent requests cannot change redirect URIs.
        async with self._flow_lock:
            try:
                if await self.token_storage_adapter.get_tokens() is None:
                    await self._reserve_callback()
                async with aclosing(super().async_auth_flow(request)) as flow:
                    try:
                        outgoing = await anext(flow)
                        while True:
                            response = yield outgoing
                            outgoing = await flow.asend(response)
                    except StopAsyncIteration:
                        self.failure = None
                        return
            except Exception as error:
                self.failure = error
                raise
            finally:
                self._close_callback()

    async def redirect_handler(self, authorization_url: str) -> None:
        if not self.interactive:
            raise SignInRequired("This MCP server needs a browser sign-in.")
        # Refresh can fail and require a new login even when the flow began with
        # tokens. Verify socket ownership before opening a browser in that case.
        await self._reserve_callback()
        states = parse_qs(urlsplit(authorization_url).query).get("state", [])
        if len(states) != 1 or not states[0]:
            raise RuntimeError("MCP authorization URL has no unique state parameter.")
        self._expected_state = states[0]
        await super().redirect_handler(authorization_url)

    async def callback_handler(self) -> AuthorizationCodeResult:
        sock = await self._reserve_callback()
        try:
            result = OAuthCallbackResult()
            ready = anyio.Event()
            stopped = anyio.Event()
            server = _CallbackServer(
                create_oauth_callback_server(
                    port=self.redirect_port,
                    host=_HOST,
                    server_url=self.mcp_url,
                    result_container=result,
                    result_ready=ready,
                ).config
            )
            callback_app = server.config.app
            expected_state = self._expected_state

            async def checked_callback(scope, receive, send):
                if scope["type"] == "http" and scope["path"] == "/callback":
                    states = Request(scope).query_params.getlist("state")
                    if (
                        expected_state is None
                        or len(states) != 1
                        or not secrets.compare_digest(states[0].encode(), expected_state.encode())
                    ):
                        # A late redirect from an earlier attempt must not claim
                        # this listener or cancel the current sign-in. The SDK
                        # still validates state, issuer, and PKCE after this gate.
                        response = PlainTextResponse(
                            "This callback does not match the current sign-in. "
                            "Close this tab and use the newest sign-in tab.",
                            status_code=400,
                            headers={"Cache-Control": "no-store"},
                        )
                        await response(scope, receive, send)
                        return
                await callback_app(scope, receive, send)

            server.config.app = checked_callback
            server.config.timeout_graceful_shutdown = 1

            async def serve():
                try:
                    await server.serve(sockets=[sock])
                finally:
                    stopped.set()

            async with anyio.create_task_group() as group:
                group.start_soon(serve)
                try:
                    with anyio.fail_after(self._callback_timeout):
                        await ready.wait()
                    if result.error:
                        raise result.error
                    return AuthorizationCodeResult(
                        code=result.code, state=result.state, iss=result.iss
                    )
                finally:
                    server.should_exit = True
                    # Shield cleanup from the cancelled model turn. Bound the wait
                    # and explicitly close listeners even if startup/shutdown fails.
                    with anyio.move_on_after(2, shield=True):
                        await stopped.wait()
                    group.cancel_scope.cancel()
        finally:
            self._close_callback()
