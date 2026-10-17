import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic_ai import ModelRetry
from pydantic_ai.messages import ModelRequest, ToolReturnPart, UserPromptPart

from pcode import obsidian_plans as op
from pcode.commands import CommandRegistry
from pcode.ext import ExtensionAPI
from pcode.extensions import plan_mode


class FakeAPI:
    def __init__(self, workspace):
        self.workspace = workspace
        self.is_worker = False
        self.notes = []
        self.ui = SimpleNamespace(notify=lambda text, level="info": self.notes.append(text))
        self.commands = {}
        self.tools = {}
        self.hooks = SimpleNamespace(
            on=SimpleNamespace(
                before_model_request=self._hook,
                before_tool_execute=self._guard,
                before_run=self._remember,
                after_tool_execute=self._seen,
            )
        )

    def register_command(self, name, description, handler, **_):
        self.commands[name] = handler

    def tool(self, fn):
        self.tools[fn.__name__] = fn
        return fn

    def _remember(self, fn):
        self.before_run = fn
        return fn

    def _seen(self, fn):
        self.after_tool = fn
        return fn

    def _guard(self, fn):
        self.guard = fn
        return fn

    def _hook(self, fn):
        self.hook = fn
        return fn


async def _remind(api, parts=None, agent="pcode"):
    request = ModelRequest(parts=parts or [UserPromptPart("hi")])
    ctx = SimpleNamespace(agent=SimpleNamespace(name=agent), messages=[request])
    await api.hook(ctx, SimpleNamespace(messages=[request]))
    return request.parts[len(parts or [None]) :]


def remind(api, parts=None, agent="pcode"):
    return [p.content for p in asyncio.run(_remind(api, parts, agent))]


def test_plan_mode_reminds_until_the_model_exits(tmp_path):
    api = FakeAPI(tmp_path)
    plan_mode.setup(api)
    assert remind(api) == []

    assert api.commands["/plan"]("Add Export Button!") == "Add Export Button!"
    assert (tmp_path / ".pcode/plans/.gitignore").exists()
    [note] = remind(api)
    assert ".pcode/plans/add-export-button.md" in note
    # Mid-turn (tool results), a retry resending it, and shared workers: nothing.
    assert remind(api, [ToolReturnPart("t", "ok", "id")]) == []
    assert remind(api, [UserPromptPart("hi"), UserPromptPart(note)]) == []
    assert remind(api, agent="worker") == []
    tool_def = SimpleNamespace(name="exit_plan_mode")
    worker = SimpleNamespace(agent=SimpleNamespace(name="worker"))
    with pytest.raises(ModelRetry):
        asyncio.run(api.guard(worker, call=None, tool_def=tool_def, args={}))

    # Survives a reload: a fresh setup reads the marker.
    reloaded = FakeAPI(tmp_path)
    plan_mode.setup(reloaded)
    assert "add-export-button.md" in reloaded.tools["exit_plan_mode"]()
    assert remind(reloaded) == []
    assert "not on" in reloaded.tools["exit_plan_mode"]()


def test_bare_plan_resumes_latest_and_off_stops(tmp_path):
    api = FakeAPI(tmp_path)
    plan_mode.setup(api)
    (tmp_path / ".pcode/plans").mkdir(parents=True)
    (tmp_path / ".pcode/plans/old-idea.md").write_text("# plan\n")
    assert api.commands["/plan"]("") is None
    assert "Resuming .pcode/plans/old-idea.md" in api.notes[-1]
    assert api.commands["/plan"]("off") is None
    assert remind(api) == []


def test_plan_accepts_a_topic_beside_its_fixed_arguments(tmp_path):
    notes = []
    ui = SimpleNamespace(notify=lambda text, level="info": notes.append(text))
    api = ExtensionAPI("plan_mode", tmp_path, ui=ui, session_dir=tmp_path / "sessions")
    plan_mode.setup(api)
    registry = CommandRegistry()
    for command in api.commands:
        registry.register(command)
    assert registry.dispatch("/plan Add export button")
    assert "add-export-button.md" in notes[-1]
    assert registry.dispatch("/plan status")
    assert "add-export-button.md" in notes[-1]


def test_plan_sends_a_multiline_request_and_names_the_plan_after_it(tmp_path):
    api = FakeAPI(tmp_path)
    plan_mode.setup(api)
    request = (
        "let's write a doc about this project, what it currently has, and how it differs\n\n"
        "a brief section on the stack"
    )
    assert api.commands["/plan"](request) == request
    assert ".pcode/plans/let-s-write-a-doc-about-this-project-what.md" in api.notes[-1]
    long = "internationalization " * 9
    assert plan_mode.topic_slug(long) == "internationalization-internationalization"
    assert plan_mode.slugify("x" * 70) == "x" * 60
    assert "Describe the change" not in api.notes[-1]


def test_workers_get_nothing(tmp_path):
    api = FakeAPI(tmp_path)
    api.is_worker = True
    plan_mode.setup(api)
    assert not api.commands and not api.tools


@pytest.fixture
def configured_vault(tmp_path, monkeypatch):
    config = tmp_path / "obsidian.toml"
    vault = tmp_path / "vault"
    workspace = tmp_path / "work" / "app"
    workspace.mkdir(parents=True)
    config.write_text(
        f'[[org]]\nname = "personal"\nroot = "{workspace.parent}"\nvault = "{vault}"\n'
    )
    monkeypatch.setattr(op, "config_path", lambda: config)
    monkeypatch.setattr(op, "project_checkout", lambda _: None)
    return workspace, vault, config


def marker(workspace):
    return workspace / plan_mode.PLANS / plan_mode.ACTIVE


def test_explicit_vault_creates_only_the_plans_folder(configured_vault):
    workspace, vault, _ = configured_vault
    api = FakeAPI(workspace)
    plan_mode.setup(api)
    request = "Add export\n\n  Keep indentation and --local as request text."
    assert api.commands["/plan"]("--obsidian " + request) == request
    path = Path(marker(workspace).read_text().strip())
    assert path == vault / "plans" / "Plan — app — Add export.md"
    assert [p.name for p in vault.iterdir()] == ["plans"]
    assert not path.exists()  # The model writes the note, not the command.
    [note] = remind(api)
    assert "Plan — app — Add export.md" in note and "brief" not in note
    result = api.tools["exit_plan_mode"]()
    assert "Plan — app — Add export.md" in result and "brief" not in result


def test_destination_overrides_and_destination_only_resume(configured_vault):
    workspace, vault, _ = configured_vault
    api = FakeAPI(workspace)
    plan_mode.setup(api)
    command = api.commands["/plan"]
    command("Default topic")  # The default is local.
    assert not Path(marker(workspace).read_text().strip()).is_absolute()
    assert not vault.exists()  # Nothing touches the vault until --obsidian asks.
    command("--obsidian Vault topic")
    old = marker(workspace).read_text()
    path = Path(old.strip())
    path.write_text("vault contents")
    assert command("--obsidian") is None
    assert marker(workspace).read_text() == old
    command("--local")
    assert not Path(marker(workspace).read_text().strip()).is_absolute()
    assert path.read_text() == "vault contents"
    command("--local A local topic")
    local = marker(workspace).read_text()
    assert command("--local") is None
    assert marker(workspace).read_text() == local
    assert command("") is None  # Bare /plan always resumes an active plan.
    assert marker(workspace).read_text() == local
    command("--obsidian")
    assert Path(marker(workspace).read_text().strip()).parent == vault / "plans"
    assert path.read_text() == "vault contents"


@pytest.mark.parametrize(
    "flags", ["--obsidian --local", "--local --obsidian", "--other", "--obsidian --bad"]
)
def test_bad_flags_leave_marker_and_vault_untouched(configured_vault, flags):
    workspace, vault, _ = configured_vault
    api = FakeAPI(workspace)
    plan_mode.setup(api)
    api.commands["/plan"]("Original")
    before = marker(workspace).read_text()
    with pytest.raises(ValueError):
        api.commands["/plan"](flags + "\nRequest")
    assert marker(workspace).read_text() == before
    assert not vault.exists()


@pytest.mark.parametrize(
    "config_text",
    [
        "broken = [",
        "org = 3",
        '[[org]]\nname = "p"',
        "vault_root = 3",
        '[[org]]\nname = "p"\nroot = 4',
    ],
)
def test_explicit_config_errors_do_not_fall_back(configured_vault, config_text):
    workspace, vault, config = configured_vault
    config.write_text(config_text)
    api = FakeAPI(workspace)
    plan_mode.setup(api)
    api.commands["/plan"]("Original")
    before = marker(workspace).read_text()
    with pytest.raises(ValueError, match="obsidian.toml") as error:
        api.commands["/plan"]("--obsidian New topic")
    assert "/plan --local" in str(error.value)
    assert marker(workspace).read_text() == before
    assert not vault.exists()


def test_leftover_memory_config_is_not_silently_ignored(configured_vault, tmp_path):
    # Falling back to the default vault would put an org's plans in a shared one.
    workspace, vault, config = configured_vault
    config.rename(tmp_path / "memory.toml")
    api = FakeAPI(workspace)
    plan_mode.setup(api)
    with pytest.raises(ValueError, match="rename .*memory.toml to obsidian.toml") as error:
        api.commands["/plan"]("--obsidian Topic")
    assert "writable" not in str(error.value)
    assert not vault.exists()


def test_config_lives_in_the_config_dir():
    from pcode.preferences import config_dir

    assert op.config_path() == config_dir() / "obsidian.toml"


def test_plan_at_the_org_root_is_named_after_the_org(configured_vault):
    workspace, vault, _ = configured_vault
    api = FakeAPI(workspace.parent)
    plan_mode.setup(api)
    api.commands["/plan"]("--obsidian Topic")
    path = Path(marker(api.workspace).read_text().strip())
    assert path == vault / "plans" / "Plan — personal — Topic.md"


def test_explicit_reports_invalid_vault(configured_vault):
    workspace, vault, _ = configured_vault
    api = FakeAPI(workspace)
    plan_mode.setup(api)
    api.commands["/plan"]("--obsidian Topic")
    assert list((vault / "plans").iterdir()) == []  # The write probe was removed.
    before = marker(workspace).read_text()
    (vault / "plans").rmdir()
    (vault / "plans").write_text("not a directory")
    with pytest.raises(ValueError, match="vault writable"):
        api.commands["/plan"]("--obsidian Another")
    assert marker(workspace).read_text() == before


@pytest.mark.parametrize("arguments", ["--obsidian", "--obsidian New topic"])
def test_explicit_unwritable_vault_preserves_active_plan(configured_vault, monkeypatch, arguments):
    workspace, vault, config = configured_vault
    folder = vault / "plans"
    folder.mkdir(parents=True)
    api = FakeAPI(workspace)
    plan_mode.setup(api)
    api.commands["/plan"]("Original")
    before = marker(workspace).read_text()
    guidance = remind(api)

    def deny_probe(*, dir, prefix):
        assert dir == folder
        raise PermissionError(f"Permission denied: {dir}")

    monkeypatch.setattr(op.tempfile, "NamedTemporaryFile", deny_probe)
    with pytest.raises(ValueError, match="vault writable") as error:
        api.commands["/plan"](arguments)
    assert str(folder) in str(error.value)
    assert str(config) in str(error.value)
    assert "/plan --local" in str(error.value)
    assert marker(workspace).read_text() == before
    assert remind(api) == guidance
    assert list(folder.iterdir()) == []


def test_reload_absolute_marker_resumes_without_config(configured_vault):
    workspace, vault, config = configured_vault
    api = FakeAPI(workspace)
    plan_mode.setup(api)
    api.commands["/plan"]("--obsidian Topic")
    config.unlink()
    reloaded = FakeAPI(workspace)
    plan_mode.setup(reloaded)
    assert reloaded.commands["/plan"]("") is None
    [note] = remind(reloaded)
    assert str(vault / "plans" / "Plan — app — Topic.md") in note


def observe(api, path, *, agent="pcode", capabilities=None):
    ctx = SimpleNamespace(agent=SimpleNamespace(name=agent), capabilities=capabilities)
    return asyncio.run(
        api.after_tool(
            ctx,
            call=None,
            tool_def=SimpleNamespace(name="read_file"),
            args={"path": str(path)},
            result="ok",
        )
    )


def test_external_feedback_is_active_only_and_deduplicated(configured_vault):
    workspace, vault, _ = configured_vault
    api = FakeAPI(workspace)
    plan_mode.setup(api)
    api.commands["/plan"]("--obsidian Topic")
    path = Path(marker(workspace).read_text().strip())
    path.write_text("Before")
    observe(api, path)
    other = vault / "plans" / "Other.md"
    other.write_text("Before")
    observe(api, other)
    path.write_text("After: user answer")
    other.write_text("Unrelated edit")
    assert remind(api, [UserPromptPart("hi"), ToolReturnPart("t", "ok", "id")]) == []
    assert remind(api, agent="worker") == []
    [note] = remind(api)
    assert "-Before" in note and "+After: user answer" in note
    assert "Unrelated edit" not in note
    path.write_text("Another user answer")
    assert remind(api, [UserPromptPart("hi"), UserPromptPart(note)]) == []
    [next_note] = remind(api)
    assert "+Another user answer" in next_note
    assert "```diff" not in remind(api)[0]
    # The model's own file operations reset the baseline instead of echoing them back.
    path.write_text("Model edit")
    observe(api, path)
    assert "```diff" not in remind(api)[0]
    api.commands["/plan"]("off")
    path.write_text("No longer active")
    assert remind(api) == []


def test_side_questions_do_not_consume_edits_or_exit(configured_vault):
    from pcode.aside_guard import AsideGuard

    workspace, _, _ = configured_vault
    api = FakeAPI(workspace)
    plan_mode.setup(api)
    api.commands["/plan"]("--obsidian Topic")
    path = Path(marker(workspace).read_text().strip())
    path.write_text("Before")
    observe(api, path)
    path.write_text("Feedback")
    capabilities = {"aside": AsideGuard()}
    observe(api, path, capabilities=capabilities)
    observe(api, path, agent="worker")
    request = ModelRequest(parts=[UserPromptPart("side question")])
    ctx = SimpleNamespace(
        agent=SimpleNamespace(name="pcode"), capabilities=capabilities, messages=[request]
    )
    asyncio.run(api.hook(ctx, SimpleNamespace(messages=[request])))
    assert len(request.parts) == 1
    with pytest.raises(ModelRetry):
        asyncio.run(
            api.guard(ctx, call=None, tool_def=SimpleNamespace(name="exit_plan_mode"), args={})
        )
    assert "+Feedback" in remind(api)[0]


def test_feedback_resets_when_conversation_changes(configured_vault):
    workspace, _, _ = configured_vault
    api = FakeAPI(workspace)
    plan_mode.setup(api)
    api.commands["/plan"]("--obsidian Topic")
    path = Path(marker(workspace).read_text().strip())
    path.write_text("Before")
    observe(api, path)
    ctx = SimpleNamespace(agent=SimpleNamespace(name="pcode"), conversation_id="first")
    asyncio.run(api.before_run(ctx))
    path.write_text("First conversation feedback")
    assert "+First conversation feedback" in remind(api)[0]
    path.write_text("Unrelated conversation")
    ctx.conversation_id = "second"
    asyncio.run(api.before_run(ctx))
    assert "```diff" not in remind(api)[0]


def test_vault_plans_name_their_session_for_the_obsidian_bridge(configured_vault):
    workspace, _, _ = configured_vault
    api = FakeAPI(workspace)
    plan_mode.setup(api)

    def reminder(conversation_id):
        request = ModelRequest(parts=[UserPromptPart("hi")])
        ctx = SimpleNamespace(
            agent=SimpleNamespace(name="pcode"), messages=[request], conversation_id=conversation_id
        )
        asyncio.run(api.hook(ctx, SimpleNamespace(messages=[request])))
        return request.parts[-1].content

    api.commands["/plan"]("Local topic")
    assert "session:" not in reminder("abc123")  # Only vault plans are opened in Obsidian.
    api.commands["/plan"]("--obsidian Vault topic")
    # A side model's run id is folded back to the session it belongs to.
    assert "`session: abc123`" in reminder("abc123.btw-deadbeef")
    assert "session:" not in reminder(None)


def test_destination_only_does_not_resume_unrelated_local_plan(configured_vault):
    workspace, _, _ = configured_vault
    api = FakeAPI(workspace)
    plan_mode.setup(api)
    api.commands["/plan"]("--local Old topic")
    (workspace / plan_mode.PLANS / "old-topic.md").write_text("Old plan")
    api.commands["/plan"]("--obsidian New topic")
    api.commands["/plan"]("--local")
    assert marker(workspace).read_text().strip() == "plan"


def test_resume_and_reload_feedback_baselines(configured_vault):
    workspace, _, _ = configured_vault
    api = FakeAPI(workspace)
    plan_mode.setup(api)
    api.commands["/plan"]("--obsidian Topic")
    path = Path(marker(workspace).read_text().strip())
    path.write_text("Before")
    reloaded = FakeAPI(workspace)
    plan_mode.setup(reloaded)
    path.write_text("Edited after reload")
    reloaded.commands["/plan"]("--obsidian")
    assert "+Edited after reload" in remind(reloaded)[0]


def test_explicit_vault_resolves_worktrees_to_main_checkout(
    configured_vault, tmp_path, monkeypatch
):
    workspace, vault, _ = configured_vault
    worktree = tmp_path / "outside-org" / "branch"
    worktree.mkdir(parents=True)
    monkeypatch.setattr(op, "project_checkout", lambda _: workspace)
    api = FakeAPI(worktree)
    plan_mode.setup(api)
    api.commands["/plan"]("--obsidian Topic")
    assert Path(marker(worktree).read_text().strip()) == vault / "plans" / "Plan — app — Topic.md"


def test_explicit_vault_outside_every_org_uses_the_default_vault(
    configured_vault, tmp_path, monkeypatch
):
    _, vault, _ = configured_vault
    monkeypatch.setattr(op, "DEFAULT_VAULT_ROOT", str(tmp_path / "vaults"))
    api = FakeAPI(tmp_path / "outside")
    plan_mode.setup(api)
    api.commands["/plan"]("--obsidian Topic")
    default = (tmp_path / "vaults" / "default").resolve()
    assert Path(marker(api.workspace).read_text().strip()) == (
        default / "plans" / "Plan — outside — Topic.md"
    )
    assert not vault.exists()


def test_default_vault_plans_report_edits(configured_vault, tmp_path, monkeypatch):
    monkeypatch.setattr(op, "DEFAULT_VAULT_ROOT", str(tmp_path / "vaults"))
    api = FakeAPI(tmp_path / "outside")
    plan_mode.setup(api)
    api.commands["/plan"]("--obsidian Topic")
    path = Path(marker(api.workspace).read_text().strip())
    path.write_text("Before")
    observe(api, path)
    path.write_text("After: user answer")
    assert "+After: user answer" in remind(api)[0]
