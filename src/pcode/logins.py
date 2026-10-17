"""Provider sign-in and sign-out for a session: `/login` and `/logout`.

`Logins` is a collaborator of `SessionController`, which holds one as
`controller.logins`. The commands only record what was asked for
(`controller.login_requested` / `logout_requested`); the controller's command
loop runs the browser flow once the handler returns.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pcode.controller import SessionController


class Logins:
    """Signs a session in to, and out of, the providers pcode can store logins for."""

    def __init__(self, controller: SessionController) -> None:
        self.controller = controller

    @property
    def view(self):
        return self.controller.view

    def login(self, argument: str) -> None:
        # Signing in does not require the conversation to already be on that
        # provider; a session on another provider keeps its own model.
        from pcode.models import login_sources

        sources = login_sources()
        source = argument.strip() or sources[0]
        if source not in sources:
            self.view.note(f"Usage: /login [{'|'.join(sources)}]")
            return
        self.controller.login_requested = source

    def logout(self, argument: str) -> None:
        from pcode.models import logout_sources

        sources = logout_sources()
        source = argument.strip() or sources[0]
        if source not in sources:
            self.view.note(f"Usage: /logout [{'|'.join(sources)}]")
            return
        self.controller.logout_requested = source

    async def perform_login(self, source: str | None) -> None:
        if source == "openai-codex":
            await self.login_codex()
        else:
            await self.login_claude()

    async def login_claude(self) -> None:
        """Run Claude Code's own sign-in for `claude:` models, with the CLI they run."""
        from pcode.auth import LoginError
        from pcode.claude_sdk import LOGIN_ENV, MISSING_SDK, cli_path
        from pcode.claude_sdk.login import claude_login
        from pcode.models import claude_sdk_installed

        if not claude_sdk_installed():
            self.view.error(MISSING_SDK)
            return
        try:
            self.view.note(
                "Signing in to Claude Code with `claude auth login`. "
                "Finish in the browser (Ctrl+C cancels)."
            )
            # Scrubbed as the requests are, so an API key cannot pass for the login.
            status = await claude_login(self.view.note, cli_path(), extra_env=LOGIN_ENV)
            plan = status.get("subscriptionType")
            self.view.note(
                "Signed in to Claude Code"
                + (f" ({plan} plan)" if plan else "")
                + ". claude: models use it from their next request; pcode stores nothing."
            )
        except asyncio.CancelledError:
            self.view.note("Claude sign-in cancelled.")
            raise
        except LoginError as error:
            self.view.error(str(error))
        except Exception:
            self.view.error("Claude sign-in failed. No credential details were logged.")

    async def login_codex(self) -> None:
        from pcode.agent import codex_model
        from pcode.auth import LoginError
        from pcode.codex_login import credentials_path, login

        controller = self.controller
        self.view.note(
            "Sign in with your ChatGPT account in the browser. "
            "If no browser opens, visit this URL (Ctrl+C cancels):"
        )
        try:
            await login(notify=self.view.note)
            # Codex credentials are read when the model is built, so a Codex
            # conversation must rebuild its model to adopt the new sign-in.
            codex = (controller.model or "").startswith("openai-codex:")
            if codex and hasattr(controller.runtime, "agent"):
                controller.runtime.agent.model = await asyncio.to_thread(
                    codex_model, controller.model
                )
            self.view.note(
                f"Signed in to OpenAI Codex. Credentials are stored in {credentials_path()} "
                "(owner-only) and refreshed automatically; /logout openai-codex removes them."
            )
        except asyncio.CancelledError:
            self.view.note("OpenAI Codex sign-in cancelled.")
            raise
        except LoginError as error:
            self.view.error(str(error))
        except Exception:
            self.view.error("OpenAI Codex sign-in failed. No credential details were logged.")

    async def perform_logout(self) -> None:
        from pcode.auth import LoginError
        from pcode.codex_login import credentials_path, delete_credentials

        self.controller.logout_requested = None
        try:
            removed = await asyncio.to_thread(delete_credentials, credentials_path())
        except LoginError as error:
            self.view.error(str(error))
            return
        self.view.note(
            "Removed pcode's stored OpenAI Codex login. "
            "New models fall back to the CLI login, if present; that login was not removed. "
            "The current model retains its in-memory token until it expires."
            if removed
            else "No stored pcode OpenAI Codex login to remove. CLI login is unchanged."
        )
