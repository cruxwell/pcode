"""The bundled sandbox extension: write roots, credential reads, and the shell sandbox."""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
from pydantic_ai.models.function import DeltaToolCall, FunctionModel

from pcode import sandbox
from pcode.agent import create_agent
from pcode.ext import ExtensionAPI, ExtensionUI
from pcode.extensions import sandbox as sandbox_ext
from pcode.jobs import COMMAND_SANDBOX, JobRegistry


@pytest.fixture(autouse=True)
def no_session_grants(monkeypatch):
    monkeypatch.setattr(sandbox, "SESSION_GRANTS", [])


@pytest.fixture
def repo(tmp_path):
    path = tmp_path / "repo"
    (path / "src").mkdir(parents=True)
    return sandbox.real(path)


def policy(*write, protected=(), deny=()):
    return sandbox.Policy(
        write=[sandbox.real(path) for path in write],
        protected=[sandbox.real(path) for path in protected],
        deny_read=[sandbox._expand_pattern(str(pattern)) for pattern in deny],
    )


def test_writes_stay_inside_roots(repo, tmp_path):
    rules = policy(repo)
    assert rules.can_write(repo / "src" / "new.py")
    assert rules.can_write(repo / ".worktrees" / "other" / "x.py")
    assert not rules.can_write(tmp_path / "elsewhere.py")
    # A shared prefix is not containment.
    assert not rules.can_write(Path(str(repo) + "-evil") / "x.py")


def test_guarded_paths_reopen_only_through_a_grant_inside_them(repo, tmp_path):
    config = tmp_path / "config" / "pcode"
    rules = policy(repo, tmp_path, protected=[config])
    for path in (
        config / "extensions" / "x.py",
        repo / ".pcode" / "extensions" / "sandbox.py",
        repo / ".git" / "hooks" / "pre-commit",
    ):
        assert not rules.can_write(path), path
        assert rules.guards(path)
    assert rules.can_write(repo / ".git" / "config")
    assert rules.can_write(repo / "a.pcode" / "x")

    granted = policy(repo, tmp_path, config / "extensions", protected=[config])
    assert granted.can_write(config / "extensions" / "x.py")
    assert not granted.can_write(config / "preferences.json")


def _store_rules(repo, tmp_path):
    # Only the roots under tmp_path: the temp dir is a root and contains it.
    scratch = sandbox.real(tmp_path)
    roots = [root for root in sandbox.base_roots(repo) if root.is_relative_to(scratch)]
    return sandbox.Policy(write=roots, protected=[], deny_read=[])


def test_package_stores_are_writable_but_install_dirs_are_not(repo, tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("GOMODCACHE", raising=False)
    monkeypatch.setenv("GOPATH", "relative/go")  # Ignored, as Go does.
    monkeypatch.setenv("CARGO_HOME", str(tmp_path / "cargo"))
    rules = _store_rules(repo, tmp_path)
    home, cargo = sandbox.real(home), sandbox.real(tmp_path / "cargo")
    for path in (
        home / "go" / "pkg" / "mod" / "x",
        home / "go" / "pkg" / "sumdb" / "x",
        cargo / "registry" / "src" / "x",
        cargo / "git" / "x",
    ):
        assert rules.can_write(path), path
    for path in (
        home / "go" / "bin" / "x",
        cargo / "bin" / "x",
        home / ".local" / "bin" / "x",
        home / ".local" / "share" / "uv" / "tools" / "x",
        home / ".local" / "share" / "uv" / "python" / "x",
    ):
        assert not rules.can_write(path), path


def test_go_stores_follow_the_first_gopath_entry(repo, tmp_path, monkeypatch):
    first, second = tmp_path / "go1", tmp_path / "go2"
    monkeypatch.setenv("GOPATH", f"{first}{os.pathsep}{second}")
    monkeypatch.setenv("GOMODCACHE", str(tmp_path / "mods"))
    rules = _store_rules(repo, tmp_path)
    assert rules.can_write(sandbox.real(tmp_path / "mods") / "x")
    assert rules.can_write(sandbox.real(first) / "pkg" / "sumdb" / "x")
    assert not rules.can_write(sandbox.real(first) / "pkg" / "mod" / "x")
    assert not rules.can_write(sandbox.real(second) / "pkg" / "sumdb" / "x")


def test_a_file_grant_covers_only_that_file(repo, tmp_path):
    target = tmp_path / "home" / "AGENTS.md"
    target.parent.mkdir(parents=True)
    target.write_text("x")
    rules = policy(repo, target)
    assert rules.can_write(sandbox.real(target))
    assert not rules.can_write(sandbox.real(target.parent / "other.md"))


def test_reads_are_denied_only_for_credentials(tmp_path):
    home = tmp_path / "home"
    rules = policy(deny=[home / ".ssh" / "id_*", home / ".aws"])
    assert not rules.can_read(sandbox.real(home / ".ssh" / "id_ed25519"))
    assert not rules.can_read(sandbox.real(home / ".aws" / "credentials"))
    assert rules.can_read(sandbox.real(home / ".ssh" / "config"))
    assert rules.can_read(sandbox.real(home / ".ssh" / "keys" / "id_x"))
    assert rules.can_read(sandbox.real(home / ".awsome"))


def test_glob_regex_keeps_wildcards_inside_one_component():
    import re

    regex = sandbox.glob_regex("/h/.ssh/id_*")
    assert re.match(regex, "/h/.ssh/id_rsa")
    assert re.match(regex, "/h/.ssh/id_rsa/inner")
    assert not re.match(regex, "/h/.ssh/sub/id_rsa")
    assert sandbox.glob_regex("/a.b+c") == r"^/a\.b\+c(/|$)"


def test_config_adds_write_roots_and_replaces_deny_list(repo, tmp_path):
    extra = tmp_path / "chezmoi"
    extra.mkdir()
    rules = sandbox.Policy.build([repo], {"write": [str(extra)], "deny_read": ["~/.kube"]})
    assert rules.can_write(sandbox.real(extra) / "AGENTS.md")
    assert rules.deny_read == [str(sandbox.real(Path.home() / ".kube"))]
    assert rules.protected == [sandbox.real(sandbox.config_dir())]


def test_default_deny_list_hides_stored_credentials(repo, tmp_path, monkeypatch):
    home = sandbox.config_dir()
    rules = sandbox.Policy.build([repo], {})
    for name in ("credentials.json", "mcp-credentials.json", "codex-credentials.json"):
        assert not rules.can_read(sandbox.real(home / name)), name
    moved = tmp_path / "elsewhere" / "codex.json"
    monkeypatch.setenv("PCODE_CODEX_CREDENTIALS_FILE", str(moved))
    assert not sandbox.Policy.build([repo], {}).can_read(sandbox.real(moved))


def test_malformed_config_is_an_error_and_global_grants_persist(tmp_path):
    sandbox.config_path().parent.mkdir(parents=True)
    sandbox.config_path().write_text("{nope")
    with pytest.raises(ValueError, match="sandbox.json"):
        sandbox.load_config()
    sandbox.config_path().write_text(json.dumps({"shell_sandbox": False}))
    sandbox.add_global_grant(tmp_path / "a")
    sandbox.add_global_grant(tmp_path / "a")
    assert sandbox.load_config() == {"shell_sandbox": False, "write": [str(tmp_path / "a")]}


def test_seatbelt_profile_denies_after_allowing(repo, tmp_path):
    config = tmp_path / "config"
    profile = policy(repo, protected=[config], deny=[tmp_path / "secret"]).seatbelt_profile(
        [tmp_path / "job"]
    )
    lines = profile.splitlines()
    assert lines[:3] == ["(version 1)", "(allow default)", "(deny file-write*)"]
    assert f'(subpath "{repo}")' in lines[3] and "job" in lines[3]
    assert lines[4].startswith("(deny file-write*") and r"/\.pcode(/|$)" in lines[4]
    assert lines[5].startswith("(deny file-read*")


def test_launch_runs_the_supervisor_under_the_sandbox_prefix(tmp_path):
    jobs = JobRegistry(state=lambda: tmp_path / "jobs")
    seen = []

    def prefix(directory):
        seen.append(directory)
        return ["env", "PCODE_SANDBOXED=1"]

    token = COMMAND_SANDBOX.set(prefix)
    try:
        job = jobs.launch("echo $PCODE_SANDBOXED", cwd=tmp_path)
    finally:
        COMMAND_SANDBOX.reset(token)
    wait(jobs, job)
    assert seen == [job.directory]
    assert job.command == "echo $PCODE_SANDBOXED"
    assert (job.directory / "output.log").read_text() == "1\n"


def wait(jobs, job):
    for _ in range(100):
        jobs.refresh()
        if not job.running:
            return
        time.sleep(0.05)
    raise AssertionError("job did not finish")


def load(workspace, monkeypatch, notices=None):
    monkeypatch.setattr(sandbox, "base_roots", lambda workspace: [sandbox.real(workspace)])
    ui = ExtensionUI(lambda text, level: (notices if notices is not None else []).append(text))
    api = ExtensionAPI("sandbox", workspace, ui)
    sandbox_ext.setup(api)
    return api


def run(workspace, capabilities, calls):
    """Make each tool call in turn, then return every tool result the model saw."""
    seen = []

    async def respond(messages, info):
        if len(messages) > 1:
            seen.append(messages[-1].parts[0].content)
        if calls:
            name, args = calls.pop(0)
            yield {0: DeltaToolCall(name=name, json_args=json.dumps(args))}
            return
        yield "done"

    agent = create_agent("test", workspace, capabilities)
    agent.run_sync("go", model=FunctionModel(stream_function=respond))
    return seen


def test_file_tools_respect_the_policy(repo, tmp_path, monkeypatch):
    monkeypatch.delenv("EXA_API_KEY", raising=False)
    outside = tmp_path / "outside.txt"
    notices = []
    api = load(repo, monkeypatch, notices)
    results = run(
        repo,
        api.capabilities(),
        [
            ("write_file", {"path": "src/ok.py", "content": "x = 1\n"}),
            ("write_file", {"path": str(outside), "content": "no"}),
            ("write_file", {"path": ".pcode/extensions/sandbox.py", "content": "no"}),
        ],
    )
    assert (repo / "src" / "ok.py").read_text() == "x = 1\n"
    assert not outside.exists()
    assert "outside the writable paths" in results[1] and "/allow-writes" in results[1]
    assert "protected location" in results[2]
    assert not (repo / ".pcode").exists()
    assert len(notices) == 2


def test_allow_writes_grants_for_the_session_or_globally(repo, tmp_path, monkeypatch):
    monkeypatch.delenv("EXA_API_KEY", raising=False)
    other = tmp_path / "other"
    other.mkdir()
    notices = []
    api = load(repo, monkeypatch, notices)
    (command,) = api.commands
    assert command.name == "/allow-writes"

    command.handler(str(other))
    assert sandbox.SESSION_GRANTS == [sandbox.real(other)]
    run(repo, api.capabilities(), [("write_file", {"path": str(other / "f"), "content": "y"})])
    assert (other / "f").read_text() == "y"

    command.handler(f"--global {other}")
    assert sandbox.load_config()["write"] == [str(sandbox.real(other))]
    command.handler("")
    assert "Writable:" in notices[-1] and str(sandbox.real(other)) in notices[-1]
    with pytest.raises(ValueError, match="does not exist"):
        command.handler(str(tmp_path / "missing"))
    with pytest.raises(ValueError, match="Usage"):
        command.handler(f"--forever {other}")


def test_a_broken_config_blocks_writes_instead_of_dropping_the_policy(repo, monkeypatch):
    monkeypatch.delenv("EXA_API_KEY", raising=False)
    sandbox.config_path().parent.mkdir(parents=True)
    sandbox.config_path().write_text("[")
    api = load(repo, monkeypatch)
    results = run(repo, api.capabilities(), [("write_file", {"path": "a.txt", "content": "x"})])
    assert "sandbox policy is invalid" in results[0]
    assert not (repo / "a.txt").exists()


def test_code_mode_reads_pass_through_the_same_guard(repo, tmp_path, monkeypatch):
    from pcode.preferences import save_preferences

    monkeypatch.delenv("EXA_API_KEY", raising=False)
    save_preferences(code_mode="on")
    secret = tmp_path / "home" / ".aws" / "credentials"
    secret.parent.mkdir(parents=True)
    secret.write_text("TOKEN")
    monkeypatch.setattr(sandbox, "DEFAULT_DENY_READ", (str(secret.parent),))
    api = load(repo, monkeypatch)
    code = f"await read_file(path={str(secret)!r})"
    results = run(repo, api.capabilities(), [("run_code", {"code": code})])
    assert "TOKEN" not in json.dumps(results[0])
    assert "holds credentials" in json.dumps(results[0])


@pytest.mark.skipif(sandbox.backend() != "seatbelt", reason="needs macOS sandbox-exec")
def test_shell_commands_run_inside_the_sandbox(repo, tmp_path, monkeypatch):
    monkeypatch.delenv("EXA_API_KEY", raising=False)
    outside = tmp_path / "outside"
    outside.mkdir()
    api = load(repo, monkeypatch)
    command = f"echo in > inside.txt && echo ok; echo out > {outside}/x || echo refused"
    results = run(repo, api.capabilities(), [("shell", {"command": command})])
    assert (repo / "inside.txt").read_text() == "in\n"
    assert not (outside / "x").exists()
    assert "Operation not permitted" in results[0] and "refused" in results[0]


@pytest.mark.skipif(sandbox.backend() != "seatbelt", reason="needs macOS sandbox-exec")
def test_sandboxed_commands_can_open_a_pseudo_terminal(repo, tmp_path):
    prefix = sandbox.command_prefix(policy(repo), tmp_path)
    script = "import os, pty; main, _ = pty.openpty(); print(os.ttyname(_))"
    result = subprocess.run(
        [*prefix, sys.executable, "-c", script], capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith("/dev/tty")


def test_file_tools_are_checked_where_the_tool_resolves_them(repo, monkeypatch):
    """`link/..` collapses as text before the link is followed, as Harness does it.

    Following `link` first would check a path deep inside the workspace while the
    tool writes beside it, outside every writable root.
    """
    monkeypatch.delenv("EXA_API_KEY", raising=False)
    deep = repo / "a" / "b" / "c"
    deep.mkdir(parents=True)
    (repo / "link").symlink_to(deep, target_is_directory=True)
    escaped = repo.parent / "escaped.txt"
    api = load(repo, monkeypatch)
    results = run(
        repo,
        api.capabilities(),
        [("write_file", {"path": "link/../../escaped.txt", "content": "no"})],
    )
    assert not escaped.exists()
    assert "outside the writable paths" in results[0]


def test_browser_file_tools_are_checked_like_file_tools(repo, tmp_path, monkeypatch):
    """A page can ask for a download into a hook or an upload of a key file."""
    from pydantic_ai.capabilities import Capability

    monkeypatch.delenv("EXA_API_KEY", raising=False)
    done = []

    def save_file(path: str) -> str:
        done.append(("save", path))
        return "saved"

    def upload_file(selector: str, paths: list[str]) -> str:
        done.append(("upload", paths))
        return "uploaded"

    key = sandbox.real(Path.home()) / ".ssh" / "id_ed25519"
    api = load(repo, monkeypatch)
    results = run(
        repo,
        [*api.capabilities(), Capability(tools=[save_file, upload_file])],
        [
            ("save_file", {"path": "docs/lab.pdf"}),
            ("save_file", {"path": ".git/hooks/pre-commit"}),
            ("upload_file", {"selector": "#f", "paths": ["docs/lab.pdf", str(key)]}),
        ],
    )
    assert results[0] == "saved"
    assert "protected location" in results[1]
    assert "holds credentials" in results[2]
    assert done == [("save", "docs/lab.pdf")]
