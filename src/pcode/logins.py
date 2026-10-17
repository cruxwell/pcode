"""Provider sign-in and sign-out for a session: `/login` and `/logout`.

`Logins` is a collaborator of `SessionController`, which holds one as
`controller.logins`. The commands only record what was asked for
(`controller.login_requested` / `logout_requested`); the controller's command
loop runs the browser flow once the handler returns.
"""

from __future__ import annotations

import asyncio
import os
from typing import TYPE_CHECKING

from pcode.preferences import load_preferences

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
        # Signing in stores a credential; it does not require the conversation to
        # already be on Anthropic. A non-Anthropic session keeps its own model.
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
        if source == "openai-codex":
            self.controller.logout_requested = source
            return
        if source != "anthropic":
            self.view.note(f"Usage: /logout [{'|'.join(sources)}]")
            return
        self.logout_anthropic()

    def logout_anthropic(self) -> None:
        from pcode.anthropic_oauth import credentials_path, delete_tokens
        from pcode.auth import LoginError

        try:
            removed = delete_tokens(credentials_path())
        except LoginError as error:
            self.view.error(str(error))
            return
        if os.environ.get("PCODE_ANTHROPIC_AUTH", "").strip() == "oauth":
            del os.environ["PCODE_ANTHROPIC_AUTH"]
        # The stored sign-in is gone; a saved "oauth" choice would now resolve
        # to a credential that no longer exists.
        if load_preferences().get("anthropic_auth") == "oauth":
            self.controller.forget_defaults("anthropic_auth")
        if not removed:
            self.view.note("No stored Anthropic login to remove.")
            return
        self.view.note(
            "Removed pcode's stored Anthropic login. This conversation keeps its current "
            "model until the token expires; use /login again or set ANTHROPIC_API_KEY."
        )

    async def perform_login(self, source: str | None) -> None:
        if source == "openai-codex":
            await self.login_codex()
        elif source == "claude":
            await self.login_claude()
        else:
            await self.login_anthropic()

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

    async def login_anthropic(self) -> None:
        from pcode.anthropic_oauth import AnthropicOAuthModel, credentials_path, login
        from pcode.auth import LoginError

        controller = self.controller
        self.view.note(
            "Opening claude.ai to sign in with your Anthropic account. "
            "If no browser opens, visit this URL (Ctrl+C cancels):"
        )
        try:
            await login(notify=self.view.note)
            # Only an Anthropic conversation adopts the new credential; a Codex
            # or Claude Code session keeps its own model and provider.
            if controller.model and controller.model.startswith("anthropic:"):
                controller.runtime.agent.model = await asyncio.to_thread(
                    AnthropicOAuthModel, controller.model
                )
            os.environ["PCODE_ANTHROPIC_AUTH"] = "oauth"
            controller.persist_defaults(anthropic_auth="oauth")
            self.view.note(
                f"Signed in to Anthropic. Credentials are stored in {credentials_path()} "
                "(owner-only) and refreshed automatically; /logout removes them."
            )
            self.view.note(
                "Future launches use this login automatically. "
                "Set PCODE_ANTHROPIC_AUTH=api-key to use ANTHROPIC_API_KEY instead."
            )
        except asyncio.CancelledError:
            self.view.note("Anthropic sign-in cancelled.")
            raise
        except LoginError as error:
            self.view.error(str(error))
        except Exception:
            self.view.error("Anthropic sign-in failed. No credential details were logged.")
