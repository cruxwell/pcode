import asyncio
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from pydantic_ai import ModelRetry
from pydantic_ai.messages import ModelRequest, ToolReturnPart, UserPromptPart

import pcode.isolated_delegation as iso
from pcode import worker_channel
from pcode.commands import CommandRegistry
from pcode.ext import ExtensionAPI
from pcode.extensions import manager
from pcode.preferences import save_preferences
from pcode.worker_channel import WorkerHub, WorkerUpdate


class FakeAPI:
    def __init__(self, workspace):
        self.workspace = workspace
        self.id = "ext.manager"
        self.is_worker = False
        self.notes = []
        self.ui = SimpleNamespace(notify=lambda text, level="info": self.notes.append(text))
        self.commands = {}
        self.capabilities = []
        self.hooks = SimpleNamespace(
            on=SimpleNamespace(before_model_request=self._hook, before_tool_execute=self._guard)
        )

    def register_command(self, name, description, handler, **_):
        self.commands[name] = handler

    def add_capability(self, capability):
        self.capabilities.append(capability)

    @property
    def tools(self):
        return {tool.name: tool.function for cap in self.capabilities for tool in cap.tools}

    def _guard(self, fn):
        self.guard = fn
        return fn

    def _hook(self, fn):
        self.hook = fn
        return fn


@pytest.fixture(autouse=True)
def reset_isolation():
    yield
    iso.force_isolation = False


@pytest.fixture
def api(tmp_path):
    api = FakeAPI(tmp_path)
    manager.setup(api)
    return api


def context(hub=None, agent="pcode", messages=(), run_id=None):
    events = []

    async def emit(event):
        events.append(event)

    capabilities = {"subagents": SimpleNamespace(hub=hub)} if hub is not None else {}
    return SimpleNamespace(
        agent=SimpleNamespace(name=agent),
        capabilities=capabilities,
        messages=list(messages),
        emit=emit,
        events=events,
        run_id=run_id or uuid4().hex,
    )


def call(api, name, args, ctx=None):
    ctx = ctx or context()
    return asyncio.run(api.guard(ctx, call=None, tool_def=SimpleNamespace(name=name), args=args))


def remind(api, parts=None, agent="pcode", run_id=None):
    parts = parts or [UserPromptPart("hi")]
    request = ModelRequest(parts=list(parts))
    ctx = context(agent=agent, messages=[request], run_id=run_id)
    asyncio.run(api.hook(ctx, SimpleNamespace(messages=[request])))
    return [p.content for p in request.parts[len(parts) :]]


def finished_worker(hub, call_id, plan_step="", tokens=0):
    handle = hub.claim(call_id, {"task": "do it", "plan_step": plan_step})
    handle.status = "done"
    handle.tokens = tokens
    return handle


BRIEF = {"agent_name": "worker", "task": "Add export", "done_when": "tests pass"}


def test_command_turns_the_mode_on_and_off(api, tmp_path):
    assert remind(api) == []
    assert api.commands["/manager"]("on Build the export feature") == "Build the export feature"
    assert (tmp_path / ".pcode/manager/.active").exists()
    assert (tmp_path / ".pcode/manager/.gitignore").read_text() == "*\n"
    assert "Manager mode on" in api.notes[-1]
    assert iso.force_isolation

    assert api.commands["/manager"]("status") is None
    assert "Manager mode on: turned on by the user" in api.notes[-1]
    assert "Workers: none yet." in api.notes[-1]
    assert "worktree preference is off" in api.notes[-1]

    assert api.commands["/manager"]("off") is None
    assert not (tmp_path / ".pcode/manager/.active").exists()
    assert not iso.force_isolation
    assert remind(api) == []
    assert api.commands["/manager"]("off") is None
    assert "already off" in api.notes[-1]
    # Bare text is the request, as with /plan.
    assert api.commands["/manager"]("Refactor the parser") == "Refactor the parser"
    assert api.commands["/manager"]("") is None


def test_command_registers_with_free_text(tmp_path):
    notes = []
    ui = SimpleNamespace(notify=lambda text, level="info": notes.append(text))
    real = ExtensionAPI("manager", tmp_path, ui=ui, session_dir=tmp_path / "sessions")
    manager.setup(real)
    registry = CommandRegistry()
    for command in real.commands:
        registry.register(command)
    assert registry.dispatch("/manager status")
    assert notes[-1].startswith("Manager mode is off.")
    assert registry.dispatch("/manager on")
    assert "Manager mode on" in notes[-1]


def test_marker_survives_reload_and_is_independent_of_plan_mode(api, tmp_path):
    api.commands["/manager"]("on")
    iso.force_isolation = False  # as if this module had been re-imported
    (tmp_path / ".pcode/plans").mkdir(parents=True)
    (tmp_path / ".pcode/plans/.active").write_text("x\n")
    reloaded = FakeAPI(tmp_path)
    manager.setup(reloaded)
    assert iso.force_isolation
    assert remind(reloaded)
    (tmp_path / ".pcode/plans/.active").unlink()
    assert remind(reloaded)


def test_model_enters_and_exits(api, tmp_path):
    result = api.tools["enter_manager_mode"]("three independent  services to migrate")
    assert "Tell the user" in result and "delegate_task" in result
    assert api.notes[-1] == "Manager mode on: three independent services to migrate"
    assert "already on" in api.tools["enter_manager_mode"]("again")
    assert (tmp_path / ".pcode/manager/.active").read_text().startswith("three independent")

    result = api.tools["exit_manager_mode"]()
    assert ".pcode/manager/lessons.md" in result
    assert not iso.force_isolation
    assert "not on" in api.tools["exit_manager_mode"]()


def test_only_the_main_agent_switches_the_mode(api):
    with pytest.raises(ModelRetry, match="Only the main agent"):
        call(api, "enter_manager_mode", {"reason": "x"}, context(agent="worker"))
    assert call(api, "enter_manager_mode", {"reason": "x"}) == {"reason": "x"}


def test_edits_and_bare_briefs_are_left_to_the_models_judgement(api):
    api.commands["/manager"]("on")
    for name, args in (
        ("edit_file", {"path": "src/app.py"}),
        ("write_file", {"path": "README.md", "content": "x"}),
        ("delegate_task", {"agent_name": "worker", "task": "Add export"}),
    ):
        assert call(api, name, args) == args


def test_worker_model_is_injected_when_none_was_given(api):
    api.commands["/manager"]("on")
    assert "model" not in call(api, "delegate_task", BRIEF)
    save_preferences(manager_worker_model="anthropic:claude-haiku-5")
    assert call(api, "delegate_task", BRIEF)["model"] == "anthropic:claude-haiku-5"
    chosen = {**BRIEF, "model": "openai:gpt-6"}
    assert call(api, "delegate_task", chosen)["model"] == "openai:gpt-6"
    reviewer = {**BRIEF, "agent_name": "reviewer"}
    assert "model" not in call(api, "delegate_task", reviewer)


def test_budget_refuses_new_delegations(api, monkeypatch):
    hub = WorkerHub()
    finished_worker(hub, "c1", tokens=150_000)
    ctx = context(hub)
    api.commands["/manager"]("on")
    assert call(api, "delegate_task", BRIEF, ctx) == BRIEF  # 0 is unlimited
    save_preferences(manager_token_budget="200000")
    assert call(api, "delegate_task", BRIEF, ctx) == BRIEF
    finished_worker(hub, "c2", tokens=60_000)
    with pytest.raises(ModelRetry, match="budget"):
        call(api, "delegate_task", BRIEF, ctx)
    api.commands["/manager"]("status")
    assert "Worker tokens: 210,000 of 200,000" in api.notes[-1]
    assert "w1 done" in api.notes[-1] and "w2 done" in api.notes[-1]
    # A worker the hub has trimmed from its history still counts.
    monkeypatch.setattr(worker_channel, "HISTORY", 1)
    hub._trim()
    assert hub.get("w1") is None
    with pytest.raises(ModelRetry, match="210,000 used"):
        call(api, "delegate_task", BRIEF, ctx)
    # Status sees the workers with the mode off too.
    api.commands["/manager"]("off")
    api.commands["/manager"]("status")
    assert "w2 done" in api.notes[-1]


def test_review_worker_gates_linked_plan_steps(api):
    hub = WorkerHub()
    ctx = context(hub)
    running = hub.claim("c1", {"task": "export", "plan_step": "step-a"})
    review = api.tools["review_worker"]
    with pytest.raises(ModelRetry, match="still running"):
        asyncio.run(review(ctx, "w1", "accepted"))
    with pytest.raises(ModelRetry, match="Unknown worker"):
        asyncio.run(review(ctx, "w9", "accepted"))
    with pytest.raises(ModelRetry):
        asyncio.run(review(context(), "w1", "accepted"))

    api.commands["/manager"]("on")
    complete = {"task_id": "step-a", "status": "completed"}
    with pytest.raises(ModelRetry, match="review_worker"):
        call(api, "update_task_status", complete, ctx)
    unlinked = {"task_id": "step-b", "status": "completed"}
    assert call(api, "update_task_status", unlinked, ctx) == unlinked
    started = {"task_id": "step-a", "status": "in_progress"}
    assert call(api, "update_task_status", started, ctx) == started

    running.status = "done"
    assert "rejected" in asyncio.run(review(ctx, "w1", "rejected", "misses the CSV case"))
    assert running.review_note == "misses the CSV case"
    batch = {"updates": [SimpleNamespace(task_id="step-a", status="completed")]}
    with pytest.raises(ModelRetry):
        call(api, "update_task_statuses", batch, ctx)
    plan = {"items": [SimpleNamespace(id="step-a", status="completed", content="x")]}
    with pytest.raises(ModelRetry):
        call(api, "write_plan", plan, ctx)

    assert "step-a can now be completed" in asyncio.run(review(ctx, "1", "accepted"))
    [event] = [e for e in ctx.events if e.review == "accepted"]
    assert isinstance(event, WorkerUpdate) and event.worker_id == "w1"
    assert call(api, "update_task_status", complete, ctx) == complete
    assert call(api, "update_task_statuses", batch, ctx) == batch
    assert call(api, "write_plan", plan, ctx) == plan


def test_the_gate_follows_the_newest_attempt_and_any_running_one(api):
    hub = WorkerHub()
    ctx = context(hub)
    api.commands["/manager"]("on")
    complete = {"task_id": "step-a", "status": "completed"}
    first = finished_worker(hub, "c1", plan_step="step-a")
    first.review = "rejected"
    second = finished_worker(hub, "c2", plan_step="step-a")
    second.review = "accepted"
    # An accepted retry clears the earlier rejection.
    assert call(api, "update_task_status", complete, ctx) == complete
    # Continuing the rejected one for rework makes it the newest, and running.
    first.messages = ["history"]
    hub.claim("c3", {"task": "rework", "continue_worker": "w1", "plan_step": "step-a"})
    with pytest.raises(ModelRetry, match="w1"):
        call(api, "update_task_status", complete, ctx)
    first.status = "done"
    with pytest.raises(ModelRetry, match="review pending"):
        call(api, "update_task_status", complete, ctx)


def test_reminder_carries_decisions_and_lessons_once_per_turn(api, tmp_path):
    api.commands["/manager"]("on")
    [note] = remind(api)
    assert note.startswith("<manager-mode>") and "review_worker" in note
    assert "Decisions so far" not in note and "isolated worktrees" not in note

    directory = tmp_path / ".pcode/manager"
    (directory / "decisions.md").write_text("- Exports use CSV, not XLSX.\n")
    (directory / "lessons.md").write_text("- " + "x" * (manager.FILE_LIMIT + 10))
    save_preferences(worktree="on")
    [note] = remind(api)
    assert "Exports use CSV, not XLSX." in note
    assert "[truncated; read lessons.md" in note
    assert "isolated worktrees" in note
    # Mid-turn, a retried request that already has it, and workers get nothing.
    assert remind(api, [ToolReturnPart("t", "ok", "id")]) == []
    assert remind(api, [UserPromptPart("hi"), UserPromptPart(note)]) == []
    assert remind(api, agent="worker") == []
    # Later requests in the same run (background results, questions) get nothing.
    assert remind(api, run_id="turn-1") and remind(api, run_id="turn-1") == []
    api.commands["/manager"]("status")
    assert "Worker isolation: on." in api.notes[-1]


def test_workers_get_nothing(tmp_path):
    api = FakeAPI(tmp_path)
    api.is_worker = True
    iso.force_isolation = True
    manager.setup(api)
    assert not api.commands and not api.capabilities
    assert iso.force_isolation  # a worker's setup leaves the parent's flag alone


def test_tools_build_into_an_agent(tmp_path):
    from pydantic_ai import Agent
    from pydantic_ai.models.test import TestModel

    ui = SimpleNamespace(notify=lambda text, level="info": None)
    real = ExtensionAPI("manager", Path(tmp_path), ui=ui, session_dir=tmp_path / "sessions")
    manager.setup(real)
    agent = Agent(TestModel(call_tools=[]), capabilities=real.capabilities())
    assert agent.run_sync("hi").output
