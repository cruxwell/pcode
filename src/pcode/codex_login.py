"""pcode's own OpenAI Codex sign-in: `/login openai-codex` and `/logout openai-codex`.

The flow itself is Pydantic AI's `OpenAICodexOAuthFlow` (PKCE authorization
code against the public Codex client, callback pinned by OpenAI's registration
to `http://localhost:1455/auth/callback`). pcode owns the browser and the
credential file, an owner-only JSON file in `config_dir()`, and hands that
file to the provider as an `OpenAICodexCredentialSource`, so refreshed tokens
are written back and a login survives restarts.

The Codex CLI's `$CODEX_HOME/auth.json` remains a read-only fallback when pcode
has no login of its own (see `credential_source`). pcode never writes that
file: the CLI requires an `id_token` there that this flow does not keep, so a
pcode-written `auth.json` would be unreadable by the CLI.
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import webbrowser
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic_ai.exceptions import UserError

from pcode.auth import LoginError
from pcode.preferences import config_dir

if TYPE_CHECKING:
    # Imported where used: the provider loads the OpenAI SDK (~50 MB), and every
    # session's model picker and sandbox only need `credentials_path`.
    from pydantic_ai.providers.openai_codex import (
        OpenAICodexCredentials,
        OpenAICodexCredentialSource,
        OpenAICodexOAuthFlow,
    )

LOGIN_TIMEOUT_SECONDS = 300.0
_MAX_FILE_BYTES = 1024 * 1024


def credentials_path() -> Path:
    override = os.environ.get("PCODE_CODEX_CREDENTIALS_FILE", "").strip()
    if override:
        return Path(override).expanduser()
    return config_dir() / "codex-credentials.json"


def have_credentials(path: Path | None = None) -> bool:
    """Existence check only; the picker must never read a stored token."""
    try:
        return (path if path is not None else credentials_path()).is_file()
    except OSError:
        return False


def _token(value: object) -> str | None:
    if not isinstance(value, str) or not value or any(char.isspace() for char in value):
        return None
    return value


def read_credentials(path: Path) -> OpenAICodexCredentials:
    """Load pcode's own stored Codex credential, never the CLI's auth file."""
    from pydantic_ai.providers.openai_codex import OpenAICodexCredentials

    try:
        with path.open("r", encoding="utf-8-sig") as source:
            contents = source.read(_MAX_FILE_BYTES + 1)
        if len(contents) > _MAX_FILE_BYTES:
            raise ValueError
        data = json.loads(contents)
        entry = data.get("openai-codex") if isinstance(data, dict) else None
        if not isinstance(entry, dict) or entry.get("type") != "oauth":
            raise ValueError
        access = _token(entry.get("access"))
        refresh = _token(entry.get("refresh"))
        account = _token(entry.get("account_id"))
        if access is None or refresh is None or account is None:
            raise ValueError
        return OpenAICodexCredentials(
            access_token=access, refresh_token=refresh, account_id=account
        )
    except Exception:
        raise LoginError(
            "No usable pcode OpenAI Codex login. Run /login openai-codex to sign in "
            "with your ChatGPT account. Remove pcode's login with /logout openai-codex "
            "to fall back to the CLI's credential."
        ) from None


def write_credentials(path: Path, credentials: OpenAICodexCredentials) -> None:
    """Replace the credential file atomically, owner-readable only."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    payload = {
        "openai-codex": {
            "type": "oauth",
            "access": credentials.access_token,
            "refresh": credentials.refresh_token,
            "account_id": credentials.account_id,
        }
    }
    name = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as file:
            name = file.name
            # Restrict before any token bytes reach the filesystem.
            os.chmod(file.fileno(), 0o600)
            json.dump(payload, file, indent=2)
            file.write("\n")
        os.replace(name, path)
        name = None
    finally:
        if name is not None:
            Path(name).unlink(missing_ok=True)


def delete_credentials(path: Path) -> bool:
    try:
        path.unlink()
        return True
    except FileNotFoundError:
        return False
    except OSError:
        raise LoginError(f"Could not remove the stored OpenAI Codex login: {path}") from None


class CredentialStore:
    """pcode's credential file as the provider's storage, so refreshes persist.

    An `OpenAICodexCredentialSource`, a protocol, so it need not subclass it.
    """

    def __init__(self, path: Path | None = None) -> None:
        self.path = path if path is not None else credentials_path()

    async def load(self) -> OpenAICodexCredentials:
        return await asyncio.to_thread(read_credentials, self.path)

    async def save(self, credentials: OpenAICodexCredentials) -> None:
        await asyncio.to_thread(write_credentials, self.path, credentials)


if TYPE_CHECKING:
    _conforms: OpenAICodexCredentialSource = CredentialStore()


def credential_source(path: Path | None = None) -> CredentialStore | None:
    """pcode's own store when a login exists, else `None` for the CLI's `auth.json`."""
    path = path if path is not None else credentials_path()
    return CredentialStore(path) if have_credentials(path) else None


def _open_browser(url: str) -> None:
    try:
        webbrowser.open(url)
    except Exception:
        # A headless machine is expected; the URL was already displayed.
        pass


async def login(
    *,
    path: Path | None = None,
    notify=None,
    open_browser: bool = True,
    timeout: float = LOGIN_TIMEOUT_SECONDS,
    flow: OpenAICodexOAuthFlow | None = None,
) -> OpenAICodexCredentials:
    """Run the browser sign-in and store the resulting credential."""
    from pydantic_ai.providers.openai_codex import OpenAICodexOAuthFlow

    path = path if path is not None else credentials_path()
    flow = flow if flow is not None else OpenAICodexOAuthFlow()
    url = flow.authorization_url()
    if notify is not None:
        notify(url)
    if open_browser:
        # Opening a browser can block on some desktops; never stall the terminal.
        await asyncio.to_thread(_open_browser, url)
    try:
        async with asyncio.timeout(timeout):
            credentials = await flow.exchange_code_from_callback()
    except TimeoutError:
        raise LoginError(
            "OpenAI Codex sign-in timed out. Run /login openai-codex to try again."
        ) from None
    except OSError:
        # The callback port is pinned by OpenAI's client registration.
        raise LoginError(
            "Port 1455 is already in use, so the sign-in callback cannot start. "
            "Close the other login (for example a running `codex login`) and try again."
        ) from None
    except UserError:
        raise LoginError(
            "OpenAI Codex authorization failed. Try /login openai-codex again."
        ) from None
    except Exception:
        raise LoginError(
            "OpenAI Codex sign-in failed. No credential details were logged."
        ) from None
    await asyncio.to_thread(write_credentials, path, credentials)
    return credentials
