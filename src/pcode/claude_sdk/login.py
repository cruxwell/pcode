"""`/login claude`: run Claude Code's own sign-in, so pcode never sees the credential.

Sign-in always runs the real `claude auth login`, which completes through
Anthropic's own flow and writes to Claude Code's config directory
(`CLAUDE_CONFIG_DIR`, else its default).
"""

import asyncio
import json
import os
import re
import subprocess

from pcode.auth import LoginError

LOGIN_TIMEOUT_SECONDS = 300.0
RETRY = "/login claude"
# OSC (hyperlinks, titles) and CSI (colour, cursor) escapes in CLI output.
ESCAPES = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b\[[0-9;?]*[ -/]*[@-~]")
REMOTE_VARIABLES = ("SSH_CONNECTION", "SSH_CLIENT", "SSH_TTY", "NO_BROWSER")
# Printed without a newline, so whatever the CLI says next lands on the same line.
PASTE_PROMPT = re.compile(r"Paste code here if prompted >\s*")


def remote_session() -> bool:
    return any(os.environ.get(name) for name in REMOTE_VARIABLES)


def auth_status(executable: str, env: dict[str, str]) -> dict:
    """Only the non-identifying parts of `claude auth status`: no email or org."""
    try:
        result = subprocess.run(
            [executable, "auth", "status", "--json"],
            capture_output=True,
            text=True,
            timeout=15,
            env=env,
        )
        data = json.loads(result.stdout)
    except OSError, subprocess.SubprocessError, ValueError:
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: data[k] for k in ("loggedIn", "authMethod", "subscriptionType") if k in data}


def manual_login_command() -> str:
    config_dir = os.environ.get("CLAUDE_CONFIG_DIR")
    if config_dir:
        return f"CLAUDE_CONFIG_DIR={config_dir} claude auth login"
    return "claude auth login"


async def claude_login(
    notify,
    executable: str | None,
    *,
    extra_env: dict[str, str] | None = None,
    timeout: float = LOGIN_TIMEOUT_SECONDS,
) -> dict:
    """Run `claude auth login --claudeai` and return its auth status.

    `extra_env` overrides the inherited environment for both the login and the
    status check.
    """
    if executable is None:
        raise LoginError(f"Claude Code (`claude`) was not found. Install it, then retry {RETRY}.")
    if remote_session():
        raise LoginError(
            "This looks like a remote session, so the browser cannot finish signing in here. "
            f"Run `{manual_login_command()}` in a terminal on this machine, then retry."
        )
    env = {**os.environ, **(extra_env or {})}
    process = await asyncio.create_subprocess_exec(
        executable,
        "auth",
        "login",
        "--claudeai",
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        env=env,
    )
    try:
        async with asyncio.timeout(timeout):
            async for raw in process.stdout:
                line = ESCAPES.sub("", raw.decode("utf-8", "replace"))
                line = PASTE_PROMPT.sub("", line).strip()
                if not line:
                    continue
                if line.startswith("If the browser didn't open"):
                    # That URL leads to a code to paste, which pcode cannot pass on.
                    notify(
                        "If no browser opened, press Ctrl+C and run "
                        f"`{manual_login_command()}` in a terminal instead."
                    )
                    continue
                notify(line)
            code = await process.wait()
    except TimeoutError:
        raise LoginError(f"Claude sign-in timed out. Run {RETRY} to try again.") from None
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
    if code != 0:
        raise LoginError(
            f"Claude sign-in did not complete (exit {code}). "
            f"Run `{manual_login_command()}` in a terminal to see why."
        )
    status = await asyncio.to_thread(auth_status, executable, env)
    if status.get("loggedIn") is False:
        raise LoginError(f"Claude Code still reports no login. Run {RETRY} to try again.")
    return status
