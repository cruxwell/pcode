"""`/usage`: plan limits and spend for the Claude Code and Codex logins, on demand.

Both come from the logins pcode already runs on, so no admin key is involved:

- Claude: Claude Code's own `/usage`, run headless (`claude -p /usage`). The
  CLI answers from its own login; pcode never reads Claude Code's token.
- Codex: `GET chatgpt.com/backend-api/wham/usage`, an undocumented endpoint the
  Codex CLI calls, through the Codex provider's auth, which injects the token,
  refreshes it on a 401, and never writes the Codex CLI's `auth.json`.
"""

import asyncio
import json
import os
import subprocess
import tempfile
import time
from collections.abc import Callable

import httpx2

CODEX_USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"
TIMEOUT = httpx2.Timeout(15, connect=5)
CLAUDE_USAGE_ARGS = ("-p", "/usage", "--output-format", "json", "--no-session-persistence")
CLAUDE_USAGE_TIMEOUT = 30
# The CLI's report continues with a local breakdown of what drove usage.
CLAUDE_BREAKDOWN_HEADING = "What's contributing"
CODEX_WINDOW_LABELS = {5 * 3600: "Session (5h)", 7 * 86400: "Weekly"}


class UsageUnavailable(Exception):
    """A provider's usage could not be read; the message says why."""


def usage_report() -> list[str]:
    """Transcript lines for every provider; one failing never hides the other."""
    lines: list[str] = []
    sources: tuple[tuple[str, Callable[[], list[str]]], ...] = (
        ("Claude Code", claude_usage),
        ("Codex", codex_usage),
    )
    for title, fetch in sources:
        try:
            body = fetch()
        except UsageUnavailable as error:
            body = [str(error)]
        lines.append(title)
        lines.extend(f"  {line}" for line in body)
    return lines


# Claude Code


def claude_usage() -> list[str]:
    """The plan limits Claude Code's own `/usage` reports, run headless.

    `claude -p /usage` answers locally from the CLI's login without a model
    turn, so pcode never reads Claude Code's token.
    """
    from pcode.models import claude_sdk_installed

    if not claude_sdk_installed():
        from pcode.claude_sdk import MISSING_SDK

        raise UsageUnavailable(MISSING_SDK)
    from pcode.claude_sdk import LOGIN_ENV, cli_path

    executable = cli_path()
    if executable is None:
        raise UsageUnavailable("Claude Code (`claude`) was not found.")
    try:
        result = subprocess.run(
            # No setting sources: a user or project hook has no business here.
            [executable, *CLAUDE_USAGE_ARGS, "--setting-sources", ""],
            capture_output=True,
            text=True,
            timeout=CLAUDE_USAGE_TIMEOUT,
            stdin=subprocess.DEVNULL,
            cwd=tempfile.gettempdir(),
            # Scrubbed as `claude:` requests are, so an API key cannot answer for the plan.
            env={**os.environ, **LOGIN_ENV},
        )
    except subprocess.TimeoutExpired:
        raise UsageUnavailable("Claude Code did not answer in time.") from None
    except OSError as error:
        raise UsageUnavailable(f"Could not run Claude Code ({type(error).__name__}).") from None
    try:
        data = json.loads(result.stdout)
    except ValueError:
        data = None
    if not isinstance(data, dict) or not isinstance(data.get("result"), str):
        raise UsageUnavailable(f"Claude Code's /usage failed (exit {result.returncode}).")
    if data.get("is_error"):
        first = data["result"].strip().splitlines()[:1]
        raise UsageUnavailable(first[0] if first else "Claude Code's /usage failed.")
    return claude_lines(data["result"])


def claude_lines(report: str) -> list[str]:
    """The limits part of the CLI's report, without its local usage breakdown."""
    # Without a subscription login the CLI reports this session's cost instead.
    if report.lstrip().startswith("Total cost:"):
        return ["No Claude subscription login; run /login claude."]
    report = report.split(CLAUDE_BREAKDOWN_HEADING, 1)[0]
    lines = [line.strip() for line in report.splitlines() if line.strip()]
    return lines or ["No limits reported for this login."]


# Codex


def codex_usage() -> list[str]:
    try:
        data = asyncio.run(_fetch_codex())
    except UsageUnavailable:
        raise
    except httpx2.HTTPError as error:
        raise UsageUnavailable(f"Request failed: {type(error).__name__}") from None
    except Exception as error:  # noqa: BLE001 - credential errors are the provider's own types.
        raise UsageUnavailable(str(error).split("\n")[0]) from None
    return codex_lines(data)


async def _fetch_codex() -> dict:
    from pydantic_ai.providers.openai_codex import OpenAICodexProvider

    from pcode.codex_login import credential_source

    # The same route Codex model requests take: PCODE_LLM_PROXY when set.
    proxy = os.environ.get("PCODE_LLM_PROXY", "").strip()
    options = {"proxy": proxy, "trust_env": False} if proxy else {}
    async with httpx2.AsyncClient(timeout=TIMEOUT, **options) as client:
        source = credential_source()
        # Attaches the provider's auth to `client`; without a pcode login it
        # reads the Codex CLI's auth.json read-only.
        if source is not None:
            OpenAICodexProvider(credential_source=source, http_client=client)
        else:
            OpenAICodexProvider(http_client=client)
        try:
            response = await client.get(CODEX_USAGE_URL, headers={"Accept": "application/json"})
        except (httpx2.ConnectError, httpx2.ConnectTimeout, httpx2.ProxyError) as error:
            route = " through PCODE_LLM_PROXY" if proxy else ""
            raise UsageUnavailable(
                f"Could not reach chatgpt.com{route} ({type(error).__name__})."
            ) from None
    if response.status_code == 403 and "json" not in response.headers.get("content-type", ""):
        hint = "" if proxy else "; set PCODE_LLM_PROXY if chatgpt.com needs a proxy here"
        raise UsageUnavailable(f"chatgpt.com blocked the request (HTTP 403){hint}.")
    if response.status_code != 200:
        raise UsageUnavailable(f"The usage endpoint answered HTTP {response.status_code}.")
    return response.json()


def codex_lines(data: dict, now: float | None = None) -> list[str]:
    now = time.time() if now is None else now
    lines = []
    if data.get("plan_type"):
        lines.append(f"Plan: {data['plan_type']}")
    lines.extend(codex_window_lines(data.get("rate_limit"), "", now))
    for extra in data.get("additional_rate_limits") or ():
        name = extra.get("limit_name") or extra.get("metered_feature") or "Other"
        lines.extend(codex_window_lines(extra.get("rate_limit"), f"{name} ", now))
    credits = data.get("credits") or {}
    if credits.get("unlimited"):
        lines.append("Credits: unlimited")
    elif credits.get("has_credits") and credits.get("balance") is not None:
        lines.append(f"Credits: {credits['balance']}")
    spend = data.get("spend_control") or {}
    cap = spend.get("individual_limit")
    if cap:
        line = f"Spend: {cap.get('used')} of {cap.get('limit')} ({cap.get('used_percent', 0)}%)"
        lines.append(line + (", limit reached" if spend.get("reached") else ""))
    return lines or ["No limits reported for this login."]


def codex_window_lines(details: dict | None, prefix: str, now: float) -> list[str]:
    lines = []
    for key in ("primary_window", "secondary_window"):
        window = (details or {}).get(key)
        if not window:
            continue
        seconds = window.get("limit_window_seconds") or 0
        label = CODEX_WINDOW_LABELS.get(seconds) or f"{duration(seconds)} window"
        lines.append(
            percent_line(prefix + label, window.get("used_percent"), window.get("reset_at"), now)
        )
    return lines


# Formatting


def percent_line(label: str, percent: float | None, resets: float | None, now: float) -> str:
    line = f"{label}: {percent:.0f}% used" if percent is not None else f"{label}: unknown"
    if resets is not None and resets > now:
        line += f", resets in {duration(resets - now)}"
    return line


def duration(seconds: float) -> str:
    minutes = int(seconds // 60)
    days, minutes = divmod(minutes, 1440)
    hours, minutes = divmod(minutes, 60)
    if days:
        return f"{days}d {hours}h" if hours else f"{days}d"
    if hours:
        return f"{hours}h {minutes}m" if minutes else f"{hours}h"
    return f"{max(minutes, 1)}m"
