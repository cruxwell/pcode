"""`/login claude`: a fake `claude` CLI, no browser or network."""

import asyncio
import json
import os
import stat
from pathlib import Path

import pytest

from pcode.auth import LoginError
from pcode.claude_sdk import login as cl


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    for name in cl.REMOTE_VARIABLES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)


def script(path: Path, body: str) -> Path:
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return path


CLI_OUTPUT = (
    "Opening browser to sign in\u2026\n"
    "If the browser didn't open, visit: \x1b]8;;https://claude.com/x\x1b\\"
    "https://claude.com/x\x1b]8;;\x1b\\\n"
    "Paste code here if prompted > "
    "Login successful.\n"
)


def fake_claude(tmp_path, login_body=None, status=None) -> str:
    if login_body is None:
        (tmp_path / "login-output").write_text(CLI_OUTPUT)
        login_body = 'cat "$(dirname "$0")/login-output"\n'
    status = status or {"loggedIn": True, "authMethod": "claude.ai", "subscriptionType": "max"}
    status = {**status, "email": "someone@example.com", "orgName": "Private Org"}
    return str(
        script(
            tmp_path / "claude",
            'echo "$CLAUDE_CONFIG_DIR|$*" >> "$(dirname "$0")/calls"\n'
            'if [ "$2" = status ]; then\n'
            f"  echo '{json.dumps(status)}'\n"
            "  exit 0\n"
            "fi\n" + login_body,
        )
    )


def test_login_uses_the_scrubbed_environment(monkeypatch, tmp_path):
    from pcode.claude_sdk import LOGIN_ENV

    monkeypatch.setenv("ANTHROPIC_API_KEY", "synthetic-key")
    monkeypatch.setenv("CLAUDE_CODE_USE_BEDROCK", "1")
    claude = script(
        tmp_path / "bundled-claude",
        'echo "[$ANTHROPIC_API_KEY][$CLAUDE_CODE_USE_BEDROCK]|$*" >> "$(dirname "$0")/calls"\n'
        'if [ "$2" = status ]; then echo \'{"loggedIn": true}\'; fi\n',
    )
    status = asyncio.run(cl.claude_login(lambda _: None, str(claude), extra_env=LOGIN_ENV))
    assert status == {"loggedIn": True}
    # An API key must not pass for the subscription login, at login or status.
    assert (tmp_path / "calls").read_text().splitlines() == [
        "[][]|auth login --claudeai",
        "[][]|auth status --json",
    ]


def test_login_runs_claude_and_reports_only_non_identifying_status(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "config"))
    lines = []
    status = asyncio.run(cl.claude_login(lines.append, fake_claude(tmp_path)))
    assert status == {"loggedIn": True, "authMethod": "claude.ai", "subscriptionType": "max"}
    assert (tmp_path / "calls").read_text().splitlines() == [
        f"{tmp_path / 'config'}|auth login --claudeai",
        f"{tmp_path / 'config'}|auth status --json",
    ]
    assert lines[0] == "Opening browser to sign in…"
    assert "claude.com" not in "\n".join(lines)  # The paste-a-code URL is replaced...
    assert "CLAUDE_CONFIG_DIR=" in lines[1]  # ...with how to finish in a terminal.
    assert lines[-1] == "Login successful."
    assert not any("Paste code" in line or "\x1b" in line for line in lines)


@pytest.mark.parametrize(
    "body,status,message",
    [
        ("exit 3\n", None, "exit 3"),
        ("exit 0\n", {"loggedIn": False}, "still reports no login"),
    ],
)
def test_login_failures(tmp_path, body, status, message):
    with pytest.raises(LoginError, match=message):
        asyncio.run(cl.claude_login(lambda _: None, fake_claude(tmp_path, body, status)))


def test_login_times_out_and_stops_the_cli(tmp_path):
    claude = fake_claude(tmp_path, "exec sleep 30\n")
    with pytest.raises(LoginError, match="timed out"):
        asyncio.run(cl.claude_login(lambda _: None, claude, timeout=0.5))


def test_cancelled_login_stops_the_cli(tmp_path):
    pid_file = tmp_path / "pid"
    claude = fake_claude(tmp_path, f'echo $$ > "{pid_file}"\nexec sleep 30\n')

    async def run():
        task = asyncio.create_task(cl.claude_login(lambda _: None, claude))
        # The redirect creates the file before `echo` writes the pid into it.
        while not (pid_file.exists() and pid_file.read_text().strip()):
            await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run())
    pid = int(pid_file.read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_login_refuses_what_it_cannot_finish(monkeypatch, tmp_path):
    with pytest.raises(LoginError, match=r"not found\. Install it, then retry /login claude"):
        asyncio.run(cl.claude_login(lambda _: None, None))
    monkeypatch.setenv("SSH_CONNECTION", "10.0.0.1 1 10.0.0.2 22")
    with pytest.raises(LoginError, match="remote session.*claude auth login"):
        asyncio.run(cl.claude_login(lambda _: None, fake_claude(tmp_path)))
    assert not (tmp_path / "calls").exists()
