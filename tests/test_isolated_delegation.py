"""Real Git artifacts and offline models exercise the delegation boundary."""

import asyncio
import fcntl
import json
import os
import re
import signal
import subprocess
import threading
from dataclasses import asdict
from pathlib import Path

import pytest
from pydantic_ai import Agent
from pydantic_ai.messages import RetryPromptPart, ToolReturnPart, UserPromptPart
from pydantic_ai.models.function import DeltaToolCall, FunctionModel
from pydantic_ai_harness.subagents import SubAgent

from pcode.agent import create_agent
from pcode.ext import ExtensionAPI, ExtensionUI, load_extensions
from pcode.live import AgentRuntime
from pcode.preferences import save_preferences, set_project_root, update_preferences
from pcode.task_worktrees import TaskWorktrees
from pcode.task_worktrees import records as task_records

PARENT_ONLY = {"delegate_task", "task_worktree"}


def git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-b", "main")
    git(root, "config", "user.name", "Test User")
    git(root, "config", "user.email", "test@example.invalid")
    git(root, "config", "commit.gpgsign", "false")
    (root / "file.txt").write_text("original\n")
    (root / "AGENTS.md").write_text("ISOLATED_REPOSITORY_GUIDANCE\n")
    (root / ".gitignore").write_text(".worktrees/\n.pcode/preferences.json\n__pycache__/\n")
    git(root, "add", ".")
    git(root, "commit", "-m", "Initial fixture")
    save_preferences(worktree="on", worker_isolation="on", web_search="off")
    set_project_root(root)
    return root


def call(name, args, call_id="call"):
    return {0: DeltaToolCall(name=name, json_args=json.dumps(args), tool_call_id=call_id)}


def results(messages):
    return [
        part
        for message in messages
        for part in message.parts
        if isinstance(part, (ToolReturnPart, RetryPromptPart))
    ]


def content(part):
    """A delegate result without its worker label (see test_worker_channel)."""
    value = part.content
    if isinstance(value, str):
        value = re.sub(r"^\[worker w\d+\]\n", "", value)
        try:
            value = json.loads(value)
        except ValueError:
            return value
    if isinstance(value, dict):
        value = {k: v for k, v in value.items() if k != "worker_id"}
    return value


def assert_artifact(repo, value, status="completed"):
    assert isinstance(value, dict), value
    assert value["workspace_mode"] == "isolated"
    assert value["status"] == status, value
    record = next(r for r in TaskWorktrees(repo).list() if r.task_id == value["task_id"])
    assert {key: value[key] for key in asdict(record)} == asdict(record)
    assert Path(value["parent"]) == repo.resolve()
    assert Path(value["worktree"]).is_dir()
    assert git(Path(value["worktree"]), "branch", "--show-current") == value["branch"]
    assert value["verification"] == "Worker-reported; see summary."
    return record


def delegate_once(agent, child, **arguments):
    returned = []

    async def model(messages, info):
        if "delegate_task" not in {t.name for t in info.function_tools}:
            async for delta in child(messages, info):
                yield delta
        elif not results(messages):
            yield call("delegate_task", {"agent_name": "worker", "task": "Implement", **arguments})
        else:
            returned.append(results(messages)[-1])
            yield "Parent complete"

    agent.run_sync("Implement", model=FunctionModel(stream_function=model))
    assert len(returned) == 1
    return returned[0]


async def finished_child(messages, info):
    assert not PARENT_ONLY & {t.name for t in info.function_tools}
    yield "Worker complete"


def test_isolated_edit_commit_extension_rebind_close_then_parent_integrates(repo):
    extension = repo / ".pcode/extensions/workspace_probe.py"
    extension.parent.mkdir(parents=True)
    extension.write_text('''def setup(pcode):
    @pcode.tool
    def extension_workspace() -> str:
        """Return this extension's bound workspace."""
        return str(pcode.workspace)

    @pcode.on_close
    async def close():
        (pcode.session_dir / (pcode.workspace.name + ".closed")).write_text(str(pcode.workspace))
''')
    git(repo, "add", ".pcode/extensions")
    git(repo, "commit", "-m", "Add fixture extension")
    save_preferences(project_extensions="on")
    session = repo.parent / "sessions"
    session.mkdir()
    loaded = load_extensions(repo, session_dir=session)
    assert all(e.error is None for e in loaded.extensions)
    agent = create_agent("test", repo, extensions=loaded.capabilities)
    base = git(repo, "rev-parse", "HEAD")
    artifact = None
    child_path = None
    operations = [
        ("extension_workspace", {}),
        ("shell", {"command": "pwd"}),
        ("read_file", {"path": "file.txt"}),
        ("write_file", {"path": "file.txt", "content": "worker\n"}),
        ("shell", {"command": "git add file.txt && git commit -m 'Worker change'"}),
    ]

    async def model(messages, info):
        nonlocal artifact, child_path
        names = {t.name for t in info.function_tools}
        previous = results(messages)
        if "delegate_task" in names:
            assert PARENT_ONLY <= names
            if not previous:
                yield call("delegate_task", {"agent_name": "worker", "task": "Implement"})
            elif len(previous) == 1:
                artifact = content(previous[-1])
                assert_artifact(repo, artifact)
                assert artifact["base_commit"] == base
                assert artifact["head_commit"] != base
                assert artifact["dirty"] is False
                assert (repo / "file.txt").read_text() == "original\n"
                assert git(repo, "rev-parse", "HEAD") == base
                assert (session / (child_path.name + ".closed")).read_text() == str(child_path)
                assert not (session / (repo.name + ".closed")).exists()
                yield call(
                    "task_worktree",
                    {"action": "integrate", "task_id": artifact["task_id"]},
                    "integrate",
                )
            else:
                assert content(previous[-1])["status"] == "integrated"
                yield "Integrated"
            return
        assert not PARENT_ONLY & names
        assert "ISOLATED_REPOSITORY_GUIDANCE" in info.instructions
        if len(previous) == 1:
            child_path = Path(content(previous[-1]))
            assert child_path != repo
        elif len(previous) == 2:
            assert str(child_path) in str(previous[-1].content)
        elif len(previous) == 3:
            assert "original" in str(previous[-1].content)
        if len(previous) < len(operations):
            name, args = operations[len(previous)]
            yield call(name, args, f"child-{len(previous)}")
        else:
            assert (child_path / "file.txt").read_text() == "worker\n"
            yield "Committed and verified worker change"

    try:
        agent.run_sync("Implement", model=FunctionModel(stream_function=model))
    finally:
        asyncio.run(loaded.close())
    assert (repo / "file.txt").read_text() == "worker\n"
    assert TaskWorktrees(repo).list()[0].status == "integrated"


def test_concurrent_children_same_filename_and_serial_integration_conflict(repo):
    paths = {}
    both_written = asyncio.Event()
    artifacts = []
    parent_step = 0

    async def model(messages, info):
        nonlocal parent_step
        previous = results(messages)
        if "delegate_task" in {t.name for t in info.function_tools}:
            parent_step += 1
            if parent_step == 1:
                yield {
                    i: DeltaToolCall(
                        name="delegate_task",
                        json_args=json.dumps({"agent_name": "worker", "task": f"CHANGE_{i}"}),
                        tool_call_id=f"delegate-{i}",
                    )
                    for i in range(2)
                }
            elif parent_step == 2:
                artifacts.extend(content(p) for p in previous)
                for artifact in artifacts:
                    assert_artifact(repo, artifact)
                assert len({a["worktree"] for a in artifacts}) == 2
                assert len({a["base_commit"] for a in artifacts}) == 1
                assert (repo / "file.txt").read_text() == "original\n"
                yield call(
                    "task_worktree",
                    {"action": "integrate", "task_id": artifacts[0]["task_id"]},
                    "merge-0",
                )
            elif parent_step == 3:
                assert content(previous[-1])["status"] == "integrated"
                yield call(
                    "task_worktree",
                    {"action": "integrate", "task_id": artifacts[1]["task_id"]},
                    "merge-1",
                )
            else:
                assert isinstance(previous[-1], RetryPromptPart)
                assert "conflict" in str(previous[-1].content).lower()
                yield "Conflict preserved"
            return
        identity = "0" if "CHANGE_0" in str(messages[0]) else "1"
        if not previous:
            yield call("shell", {"command": "pwd"}, "cwd")
        elif len(previous) == 1:
            # Query Git metadata instead of parsing the human shell output envelope.
            paths[identity] = next(
                Path(r.worktree)
                for r in task_records(repo)
                if r.worktree in str(previous[-1].content)
            )
            yield call(
                "write_file", {"path": "file.txt", "content": f"child-{identity}\n"}, "write"
            )
        elif len(previous) == 2:
            if len(paths) == 2 and all(
                (p / "file.txt").read_text() != "original\n" for p in paths.values()
            ):
                both_written.set()
            await asyncio.wait_for(both_written.wait(), 5)
            assert (paths[identity] / "file.txt").read_text() == f"child-{identity}\n"
            yield call(
                "shell", {"command": "git add file.txt && git commit -m 'Child change'"}, "commit"
            )
        else:
            yield f"Child {identity} complete"

    create_agent("test", repo).run_sync("Parallel", model=FunctionModel(stream_function=model))
    records = TaskWorktrees(repo).list()
    assert {r.status for r in records} == {"integrated", "conflicted"}
    assert all(Path(r.worktree).is_dir() for r in records)
    assert git(repo, "status", "--porcelain", "--untracked-files=no") == "UU file.txt"
    assert git(repo, "rev-parse", "MERGE_HEAD") == artifacts[1]["head_commit"]
    for identity, path in paths.items():
        assert (path / "file.txt").read_text() == f"child-{identity}\n"
        assert not git(path, "status", "--porcelain")


@pytest.mark.parametrize("key", ["worktree", "worker_isolation"])
@pytest.mark.parametrize("user_setting,overlay", [("on", "off"), ("off", "on")])
def test_active_overlay_and_live_toggle_control_default(repo, key, user_setting, overlay):
    save_preferences(**{key: user_setting})
    preferences = repo / ".pcode/preferences.json"
    preferences.parent.mkdir(exist_ok=True)
    agent = create_agent("test", repo)
    for setting in (overlay, user_setting, overlay):
        preferences.write_text(json.dumps({key: setting}))
        result = delegate_once(agent, finished_child)
        if setting == "on":
            assert_artifact(repo, content(result))
        else:
            assert content(result) == "Worker complete"


@pytest.mark.parametrize("worktree", [None, "off", "on"])
@pytest.mark.parametrize("worker_isolation", [None, "off", "on"])
@pytest.mark.parametrize("mode", ["auto", "isolated"])
def test_isolation_requires_both_settings(repo, worktree, worker_isolation, mode):
    settings = {"worktree": worktree, "worker_isolation": worker_isolation}
    update_preferences(
        {key: value for key, value in settings.items() if value is not None},
        remove=tuple(key for key, value in settings.items() if value is None),
    )
    returned = delegate_once(create_agent("test", repo), finished_child, workspace_mode=mode)
    if worktree == worker_isolation == "on":
        assert_artifact(repo, content(returned))
    else:
        if mode == "isolated":
            assert isinstance(returned, RetryPromptPart)
            assert "worktree=on and worker_isolation=on" in str(returned.content)
        else:
            assert content(returned) == "Worker complete"
        assert not (repo / ".worktrees").exists()
        assert not (repo / ".git/pcode-tasks").exists()


@pytest.mark.parametrize("setting", [None, "off"])
def test_worktree_alone_keeps_shared_edits_and_jobs_without_setup(repo, monkeypatch, setting):
    from pcode.jobs import registry

    if setting is None:
        update_preferences({}, remove=("worker_isolation",))
    else:
        save_preferences(worker_isolation=setting)
    (repo / "file.txt").write_text("parent uncommitted\n")
    parent_jobs = registry()

    async def unexpected_setup(tree):
        pytest.fail("Disabled worker isolation must not run worktree setup")

    monkeypatch.setattr("pcode.isolated_delegation._setup", unexpected_setup)

    async def child(messages, info):
        assert registry() is parent_jobs
        if not results(messages):
            yield call("read_file", {"path": "file.txt"})
        elif len(results(messages)) == 1:
            assert "parent uncommitted" in str(results(messages)[-1].content)
            yield call("write_file", {"path": "file.txt", "content": "shared edit\n"})
        else:
            yield "Shared complete"

    returned = delegate_once(create_agent("test", repo), child)
    assert content(returned) == "Shared complete"
    assert (repo / "file.txt").read_text() == "shared edit\n"
    assert not (repo / ".worktrees").exists()
    assert not (repo / ".git/pcode-tasks").exists()


def test_disabling_isolation_keeps_existing_task_recovery_available(repo):
    store = TaskWorktrees(repo)
    record = store.create()
    store.finish(record.task_id, "completed")
    save_preferences(worker_isolation="off")
    returned = []

    async def model(messages, info):
        previous = results(messages)
        if not previous:
            yield call("task_worktree", {"action": "list"})
        elif len(previous) == 1:
            assert content(previous[-1])[0]["task_id"] == record.task_id
            yield call("task_worktree", {"action": "integrate", "task_id": record.task_id})
        elif len(previous) == 2:
            assert content(previous[-1])["status"] == "integrated"
            yield call("task_worktree", {"action": "discard", "task_id": record.task_id})
        else:
            returned.append(content(previous[-1]))
            yield "Existing task recovered and cleaned"

    create_agent("test", repo).run_sync("Recover task", model=FunctionModel(stream_function=model))
    assert returned[0]["status"] == "discarded"
    assert not Path(record.worktree).exists()


@pytest.mark.parametrize("settings", [{"worktree": "off"}, {"worker_isolation": "off"}])
def test_existing_linked_checkout_with_config_off_uses_shared(repo, settings):
    linked = repo.parent / "linked"
    git(repo, "worktree", "add", "-b", "session", str(linked))
    save_preferences(**settings)
    set_project_root(linked)
    returned = delegate_once(create_agent("test", linked), finished_child)
    assert content(returned) == "Worker complete"
    assert TaskWorktrees(linked).list() == []


@pytest.mark.parametrize("setting", ["on", "off"])
@pytest.mark.parametrize("worker_isolation", ["on", "off"])
def test_explicit_shared_allows_dirty_tracked_files(repo, setting, worker_isolation):
    save_preferences(worktree=setting, worker_isolation=worker_isolation)
    (repo / "file.txt").write_text("parent uncommitted\n")

    async def child(messages, info):
        if not results(messages):
            yield call("write_file", {"path": "file.txt", "content": "shared edit\n"})
        else:
            yield "Shared complete"

    returned = delegate_once(create_agent("test", repo), child, workspace_mode="shared")
    assert content(returned) == "Shared complete"
    assert (repo / "file.txt").read_text() == "shared edit\n"
    assert TaskWorktrees(repo).list() == []


@pytest.mark.parametrize("failure", ["config_off", "worker_isolation_off", "dirty", "no_git"])
def test_rejected_isolation_is_actionable_and_never_starts_child(repo, tmp_path, failure):
    workspace = repo
    expected = {
        "config_off": "worktree=on",
        "worker_isolation_off": "worker_isolation=on",
        "dirty": "commit",
        "no_git": "git",
    }[failure]
    if failure == "config_off":
        save_preferences(worktree="off")
    elif failure == "worker_isolation_off":
        save_preferences(worker_isolation="off")
    elif failure == "dirty":
        (repo / "file.txt").write_text("dirty\n")
    else:
        workspace = tmp_path / "not-git"
        workspace.mkdir()
    seen = False

    async def child(messages, info):
        nonlocal seen
        seen = True
        yield "Unexpected child"

    returned = delegate_once(create_agent("test", workspace), child, workspace_mode="isolated")
    assert isinstance(returned, RetryPromptPart)
    assert expected in str(returned.content).lower()
    assert not seen
    assert TaskWorktrees(repo).list() == []


@pytest.mark.parametrize("mode", ["auto", "shared", "isolated"])
def test_specialized_delegate_is_shared_or_rejects_explicit_isolation(repo, mode):
    seen = False
    specialist = Agent(name="specialist", description="Inspect the live checkout")

    @specialist.tool_plain
    def specialist_workspace() -> str:
        """Return the specialized agent's workspace."""
        return str(repo)

    async def child(messages, info):
        nonlocal seen
        seen = True
        names = {t.name for t in info.function_tools}
        assert "specialist_workspace" in names
        assert not PARENT_ONLY & names
        if not results(messages):
            yield call("specialist_workspace", {})
        else:
            assert content(results(messages)[-1]) == str(repo)
            yield "Specialist complete"

    agent = create_agent("test", repo, subagents=[SubAgent(specialist)])
    result = delegate_once(agent, child, agent_name="specialist", workspace_mode=mode)
    if mode == "isolated":
        assert isinstance(result, RetryPromptPart)
        assert "shared" in str(result.content)
        assert not seen
    else:
        assert content(result) == "Specialist complete"
        assert seen
    assert TaskWorktrees(repo).list() == []


def test_aside_keeps_task_management_tools_declared_but_refuses_them(repo):
    """Declared for a cache-identical prefix; refused so no task is touched."""
    agent = create_agent("test", repo)
    runtime = AgentRuntime(agent)
    seen = {}

    async def model(messages, info):
        returned = results(messages)
        if not returned:
            assert PARENT_ONLY <= {t.name for t in info.function_tools}
            yield {
                0: DeltaToolCall(
                    name="task_worktree",
                    json_args='{"action":"discard","task_id":"missing","confirm":true}',
                    tool_call_id="discard",
                ),
                1: DeltaToolCall(
                    name="task_worktree", json_args='{"action":"list"}', tool_call_id="list"
                ),
            }
            return
        seen.update({part.tool_call_id: part.model_response_str() for part in returned})
        yield "Answered"

    with agent.override(model=FunctionModel(stream_function=model)):
        assert asyncio.run(runtime.aside("Which tasks exist?")).answer == "Answered"
    assert "unavailable in a side question" in seen["discard"]
    # Listing only reads, so it runs.
    assert "unavailable" not in seen["list"]


@pytest.mark.parametrize("mode", ["shared", "isolated"])
def test_direct_api_capabilities_only_rejected_for_isolation(repo, mode):
    api = ExtensionAPI("direct", repo, ExtensionUI())
    api.instructions("DIRECT_CAPABILITY")
    returned = delegate_once(
        create_agent("test", repo, extensions=api.capabilities()),
        finished_child,
        workspace_mode=mode,
    )
    if mode == "shared":
        assert content(returned) == "Worker complete"
        assert TaskWorktrees(repo).list() == []
    else:
        artifact = content(returned)
        assert_artifact(repo, artifact, "failed")
        assert "load_extensions" in artifact["summary"]
        assert "shared" in artifact["summary"]


def test_setup_failure_preserves_record_and_partial_files(repo):
    script = repo / ".pcode/worktree-setup"
    script.parent.mkdir(exist_ok=True)
    script.write_text("printf 'partial setup' > partial.txt\nexit 17\n")
    git(repo, "add", ".pcode/worktree-setup")
    git(repo, "commit", "-m", "Failing setup fixture")
    save_preferences(project_extensions="on")
    seen = False

    async def child(messages, info):
        nonlocal seen
        seen = True
        yield "Unexpected child"

    artifact = content(delegate_once(create_agent("test", repo), child))
    assert_artifact(repo, artifact, "failed")
    assert "17" in artifact["summary"]
    assert artifact["dirty"]
    assert (Path(artifact["worktree"]) / "partial.txt").read_text() == "partial setup"
    assert not (repo / "partial.txt").exists()
    assert not seen


def test_cancelled_worker_preserves_partial_artifact(repo):
    entered = asyncio.Event()
    stopped = asyncio.Event()

    async def model(messages, info):
        if "delegate_task" in {t.name for t in info.function_tools}:
            assert not results(messages), "a cancelled worker must not return"
            yield call("delegate_task", {"agent_name": "worker", "task": "Partial change"})
        elif not results(messages):
            yield call("write_file", {"path": "file.txt", "content": "partial edit\n"})
        else:
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()
            yield "Unreachable"

    async def run():
        agent = create_agent("test", repo)
        task = asyncio.create_task(
            agent.run("Implement", model=FunctionModel(stream_function=model))
        )
        try:
            await asyncio.wait_for(entered.wait(), 5)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        finally:
            if not task.done():
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task

    asyncio.run(run())
    assert stopped.is_set()
    (record,) = TaskWorktrees(repo).list()
    assert record.status == "cancelled"
    assert record.dirty
    assert (Path(record.worktree) / "file.txt").read_text() == "partial edit\n"
    assert (repo / "file.txt").read_text() == "original\n"


def test_hung_setup_times_out_without_leaking_and_preserves_record(repo, monkeypatch):
    script = repo / ".pcode/worktree-setup"
    script.parent.mkdir(exist_ok=True)
    script.write_text('printf %s "$$" > "$PCODE_MAIN/../setup-pid"\nexec sleep 60\n')
    git(repo, "add", ".pcode/worktree-setup")
    git(repo, "commit", "-m", "Add hanging setup fixture")
    save_preferences(project_extensions="on")
    monkeypatch.setattr("pcode.isolated_delegation.SETUP_TIMEOUT_SECONDS", 1)
    seen = False

    async def child(messages, info):
        nonlocal seen
        seen = True
        yield "Unexpected child"

    artifact = content(delegate_once(create_agent("test", repo), child))
    assert_artifact(repo, artifact, "failed")
    assert "setup exceeded 1s" in artifact["summary"]
    assert not seen
    pid = int((repo.parent / "setup-pid").read_text())
    state = subprocess.run(
        ["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True
    ).stdout.strip()
    assert not state or state.startswith("Z"), state


def test_isolated_shell_jobs_are_child_owned_and_cleaned_before_return(repo):
    from pcode.jobs import registry

    parent_registry = registry()
    parent_job = parent_registry.launch("sleep 60", cwd=repo, background=True)
    child_registry = None
    child_job = None

    async def child(messages, info):
        nonlocal child_registry, child_job
        child_registry = registry()
        assert child_registry is not parent_registry
        assert parent_job not in child_registry.jobs.values()
        if not results(messages):
            yield call("shell", {"command": "sleep 60", "background": True})
        else:
            (child_job,) = child_registry.jobs.values()
            assert child_job.running
            yield "Background work left for cleanup"

    try:
        artifact = content(delegate_once(create_agent("test", repo), child))
        assert_artifact(repo, artifact)
        assert child_job is not None
        assert child_registry.jobs == {}
        assert not child_job.directory.exists()
        assert registry() is parent_registry
        parent_registry.refresh()
        assert parent_job.running
        assert parent_job.id in parent_registry.jobs
    finally:
        parent_registry.reset()


@pytest.mark.parametrize("exit_code", [0, 17])
def test_isolated_job_notices_reach_only_the_worker_once(repo, exit_code):
    from pcode.job_notices import JobNotices
    from pcode.jobs import registry

    parent_jobs = registry()
    parent_job = parent_jobs.launch("printf 'PARENT_OUTPUT'; exit 23", cwd=repo, background=True)
    returned = []
    child_notices = []
    parent_notices = []
    child_jobs = None

    def notices(messages):
        return [
            str(part.content)
            for message in messages
            for part in message.parts
            if isinstance(part, UserPromptPart) and str(part.content).startswith("[j1]")
        ]

    async def model(messages, info):
        nonlocal child_jobs
        if "delegate_task" in {tool.name for tool in info.function_tools}:
            if not results(messages):
                yield call("delegate_task", {"agent_name": "worker", "task": "Run a check"})
            else:
                returned.append(content(results(messages)[-1]))
                parent_notices.extend(notices(messages))
                yield "Parent complete"
        elif not results(messages):
            child_jobs = registry()
            assert child_jobs is not parent_jobs
            yield call(
                "shell",
                {"command": f"printf 'CHILD_OUTPUT'; exit {exit_code}", "background": True},
            )
        elif len(results(messages)) == 1:
            # Let real processes finish during independent work, not via a model poll.
            def finished():
                parent_jobs.refresh()
                child_jobs.refresh()
                return not parent_job.running and not child_jobs.get("j1").running

            await wait_until(finished)
            yield call("read_file", {"path": "file.txt"})
        elif len(results(messages)) == 2:
            child_notices.extend(notices(messages))
            yield call("read_file", {"path": "AGENTS.md"})
        else:
            # A further request must not duplicate a notice already delivered.
            assert notices(messages) == child_notices
            yield "Worker complete"

    create_agent("test", repo).run_sync(
        "Delegate",
        model=FunctionModel(stream_function=model),
        capabilities=[JobNotices(parent_jobs)],
    )
    assert len(returned) == 1
    assert_artifact(repo, returned[0])
    assert len(child_notices) == 1
    assert f"exit {exit_code}" in child_notices[0]
    assert "CHILD_OUTPUT" in child_notices[0]
    assert "PARENT_OUTPUT" not in child_notices[0]
    assert len(parent_notices) == 1
    assert "exit 23" in parent_notices[0] and "PARENT_OUTPUT" in parent_notices[0]
    assert "CHILD_OUTPUT" not in parent_notices[0]
    assert "model" in parent_job.announced
    assert child_jobs.jobs == {}


def test_extension_setup_body_and_close_share_child_job_registry(repo):
    from pcode.jobs import registry

    extension = repo / ".pcode/extensions/registry_probe.py"
    extension.parent.mkdir(parents=True)
    extension.write_text("""from pcode.jobs import registry

def setup(pcode):
    if not pcode.is_worker:
        return
    (pcode.session_dir / "setup-registry").write_text(str(id(registry())))
    registry().launch("sleep 60", cwd=pcode.workspace, background=True)

    @pcode.on_close
    async def close():
        (pcode.session_dir / "close-registry").write_text(str(id(registry())))
        registry().launch("sleep 60", cwd=pcode.workspace, background=True)
""")
    git(repo, "add", ".pcode/extensions")
    git(repo, "commit", "-m", "Add registry lifecycle fixture")
    save_preferences(project_extensions="on")
    session = repo.parent / "sessions"
    session.mkdir()
    loaded = load_extensions(repo, session_dir=session)
    assert all(e.error is None for e in loaded.extensions)
    parent = registry()
    parent_job = parent.launch("sleep 60", cwd=repo, background=True)
    children = []

    async def child(messages, info):
        current = registry()
        children.append(current)
        assert current is not parent
        assert (session / "setup-registry").read_text() == str(id(current))
        assert len(current.jobs) == 1
        assert parent_job not in current.jobs.values()
        yield "Worker complete"

    try:
        agent = create_agent("test", repo, extensions=loaded.capabilities)
        artifact = content(delegate_once(agent, child))
        assert_artifact(repo, artifact)
        assert len(children) == 1
        assert (session / "close-registry").read_text() == str(id(children[0]))
        assert children[0].jobs == {}
        assert registry() is parent
        parent.refresh()
        assert parent_job.running
        assert parent.jobs[parent_job.id] is parent_job
    finally:
        parent.reset()
        asyncio.run(loaded.close())


async def wait_until(predicate):
    async with asyncio.timeout(10):
        while not predicate():
            await asyncio.sleep(0.01)


def test_cancelled_setup_kills_process_group_and_persists_after_repeated_cancel(repo, monkeypatch):
    import pcode.isolated_delegation as delegation

    script = repo / ".pcode/worktree-setup"
    script.parent.mkdir(exist_ok=True)
    # The child ignores TERM so cleanup has to kill the entire process group.
    script.write_text("""trap '' TERM
sh -c 'trap "" TERM; while :; do sleep 1; done' &
printf '%s %s' "$$" "$!" > "$PCODE_MAIN/../setup-pids"
wait
""")
    git(repo, "add", ".pcode/worktree-setup")
    git(repo, "commit", "-m", "Add hanging setup fixture")
    save_preferences(project_extensions="on")
    marker = repo.parent / "setup-pids"
    cleanup_entered = asyncio.Event()
    release_cleanup = asyncio.Event()
    processes = []
    original_stop = delegation._stop_setup

    async def stop(process):
        processes.append(process)
        cleanup_entered.set()
        await release_cleanup.wait()
        await original_stop(process)

    monkeypatch.setattr(delegation, "_stop_setup", stop)

    async def model(messages, info):
        assert "delegate_task" in {t.name for t in info.function_tools}, "setup must not finish"
        yield call("delegate_task", {"agent_name": "worker", "task": "Provision"})

    async def scenario():
        task = asyncio.create_task(
            create_agent("test", repo).run("Provision", model=FunctionModel(stream_function=model))
        )
        try:
            await wait_until(marker.exists)
            task.cancel()
            await asyncio.wait_for(cleanup_entered.wait(), 5)
            assert not task.done()
            task.cancel()
            await asyncio.sleep(0)
            assert not task.done()
            release_cleanup.set()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 10)
            assert len(processes) == 1
            assert processes[0].returncode is not None
            leader, child = map(int, marker.read_text().split())
            assert leader == processes[0].pid
            # Check before emergency cleanup, so that cleanup cannot hide a leak.
            # An orphan zombie is dead even if the system has not reaped it yet.
            for pid in (leader, child):
                state = subprocess.run(
                    ["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True
                ).stdout.strip()
                assert not state or state.startswith("Z"), (pid, state)
            (record,) = TaskWorktrees(repo).list()
            assert record.status == "cancelled"
        finally:
            release_cleanup.set()
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            if marker.exists():
                leader, _ = map(int, marker.read_text().split())
                try:
                    os.killpg(leader, signal.SIGKILL)
                except ProcessLookupError:
                    pass

    asyncio.run(scenario())


@pytest.mark.parametrize("cancel", [False, True])
def test_finalization_waits_for_parent_lock_without_blocking_event_loop(repo, monkeypatch, cancel):
    finish_entered = threading.Event()
    original_finish = TaskWorktrees.finish

    def finish(self, *args, **kwargs):
        finish_entered.set()
        return original_finish(self, *args, **kwargs)

    monkeypatch.setattr(TaskWorktrees, "finish", finish)
    child_done = asyncio.Event()
    lock = None
    emergency_release = None
    emergency_fired = threading.Event()

    async def model(messages, info):
        nonlocal lock, emergency_release
        if "delegate_task" in {t.name for t in info.function_tools}:
            if not results(messages):
                yield call("delegate_task", {"agent_name": "worker", "task": "Finish"})
            else:
                yield "Parent complete"
        else:
            (lock_path,) = TaskWorktrees(repo).directory.glob("parent-*.lock")
            lock = lock_path.open("a")
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)

            def unblock_broken_implementation():
                emergency_fired.set()
                fcntl.flock(lock, fcntl.LOCK_UN)

            # Prevent a synchronous flock regression from hanging the test runner.
            emergency_release = threading.Timer(10, unblock_broken_implementation)
            emergency_release.start()
            child_done.set()
            yield "Worker complete"

    async def scenario():
        task = asyncio.create_task(
            create_agent("test", repo).run("Finish", model=FunctionModel(stream_function=model))
        )
        try:
            await asyncio.wait_for(child_done.wait(), 5)
            await wait_until(finish_entered.is_set)
            # This coroutine must run while finalization is blocked on the real flock.
            await asyncio.sleep(0.05)
            assert not emergency_fired.is_set()
            assert not task.done()
            if cancel:
                task.cancel()
                await asyncio.sleep(0)
                task.cancel()
                await asyncio.sleep(0)
                assert not task.done()
            fcntl.flock(lock, fcntl.LOCK_UN)
            if cancel:
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.wait_for(task, 10)
            else:
                await asyncio.wait_for(task, 10)
        finally:
            if emergency_release is not None:
                emergency_release.cancel()
                emergency_release.join()
            if lock is not None:
                fcntl.flock(lock, fcntl.LOCK_UN)
                lock.close()
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    asyncio.run(scenario())
    assert not emergency_fired.is_set()
    (record,) = TaskWorktrees(repo).list()
    assert record.status == ("cancelled" if cancel else "completed")


@pytest.mark.parametrize("mode", ["isolated", "shared"])
@pytest.mark.parametrize(
    "configured_limit",
    [None, 0, 1],
    ids=["default-unlimited", "explicit-unlimited", "configured-one"],
)
def test_worker_concurrency_limits_calls_in_one_response(repo, mode, configured_limit):
    if configured_limit is not None:
        save_preferences(worker_concurrency=str(configured_limit))
    call_count = configured_limit + 1 if configured_limit else 6
    expected_limit = configured_limit or call_count
    agent = create_agent("test", repo)
    from pcode.isolated_delegation import WorkspaceSubAgents

    delegation = next(
        c for c in agent.root_capability.capabilities if isinstance(c, WorkspaceSubAgents)
    )
    assert (delegation.worker_slots is None) == (not configured_limit)
    active = peak = executions = 0
    returned = []
    slots_filled = asyncio.Event()

    async def model(messages, info):
        nonlocal active, peak, executions
        if "delegate_task" in {t.name for t in info.function_tools}:
            if not results(messages):
                yield {
                    index: DeltaToolCall(
                        name="delegate_task",
                        json_args=json.dumps(
                            {
                                "agent_name": "worker",
                                "task": f"Task {index}",
                                "workspace_mode": mode,
                            }
                        ),
                        tool_call_id=f"worker-{index}",
                    )
                    for index in range(call_count)
                }
            else:
                returned.extend(content(part) for part in results(messages))
                yield "Parent complete"
        else:
            active += 1
            executions += 1
            peak = max(peak, active)
            if active >= expected_limit:
                slots_filled.set()
            try:
                # Isolated workers each check out a worktree first, which takes
                # seconds on a busy machine; the timeout only stops a hang.
                await asyncio.wait_for(slots_filled.wait(), 60)
                await asyncio.sleep(0.1)
                yield "Worker complete"
            finally:
                active -= 1

    agent.run_sync("Concurrent tasks", model=FunctionModel(stream_function=model))
    assert executions == call_count, returned
    assert peak == expected_limit
    assert active == 0
    assert len(returned) == call_count
    records = TaskWorktrees(repo).list()
    if mode == "isolated":
        assert len(records) == call_count
        assert {record.status for record in records} == {"completed"}
        for artifact in returned:
            assert_artifact(repo, artifact)
    else:
        assert records == []
        assert returned == ["Worker complete"] * call_count


def test_task_worktree_outside_a_checkout_asks_the_model_to_adjust(tmp_path):
    from pydantic_ai import ModelRetry

    from pcode.isolated_delegation import WorkspaceSubAgentToolset

    toolset = WorkspaceSubAgentToolset.__new__(WorkspaceSubAgentToolset)
    toolset.workspace = tmp_path
    for action, task_id in (("list", ""), ("integrate", "t1"), ("discard", "t1")):
        with pytest.raises(ModelRetry):
            asyncio.run(toolset.task_worktree(action, task_id))
    with pytest.raises(ModelRetry, match="needs a task_id"):
        asyncio.run(toolset.task_worktree("integrate"))
