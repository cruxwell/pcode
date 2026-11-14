import asyncio
import json
from pathlib import Path
from unittest.mock import patch

import pytest
from pydantic_ai import Agent
from pydantic_ai.messages import ModelRequest, SystemPromptPart, TextContent, UserPromptPart
from pydantic_ai.models.function import DeltaToolCall, FunctionModel

from pcode.agent import create_agent, create_coder
from pcode.preferences import save_preferences
from pcode.repo_context import AutomaticRepoContext, create_repo_context


def _notes(messages) -> list[str]:
    """Each request's injected text: user parts as text or `TextContent`, and system notes."""
    notes = []
    for message in messages:
        if not isinstance(message, ModelRequest):
            continue
        texts = []
        for part in message.parts:
            if isinstance(part, SystemPromptPart):
                texts.append(part.content)
            elif isinstance(part, UserPromptPart):
                items = [part.content] if isinstance(part.content, str) else part.content
                texts += [i.content if isinstance(i, TextContent) else i for i in items]
        text = "\n".join(t for t in texts if isinstance(t, str))
        if text:
            notes.append(text)
    return notes


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)


def test_inventory_is_metadata_only_and_preserves_instructions(tmp_path):
    (tmp_path / "AGENTS.md").write_text("Repository development guidance")
    skill = tmp_path / ".claude/skills/example/SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("Asset body should not be loaded")
    capability = AutomaticRepoContext(root=tmp_path)
    instructions = capability.preload().instructions_text()
    assert "Repository development guidance" in instructions
    assert ".claude/skills/example/SKILL.md" in instructions
    assert "Asset body should not be loaded" not in instructions
    assert "Call `inventory_agent_context`" not in instructions
    assert capability.get_toolset() is None


def test_inventory_cached_within_run_and_refreshed_between_runs(tmp_path):
    async def run():
        capability = AutomaticRepoContext(root=tmp_path)
        first = (await capability.for_run(None)).preload()
        assert "No assistant configuration directories" in first.instructions_text()
        (tmp_path / ".agents").mkdir()
        assert "No assistant configuration directories" in first.instructions_text()
        second = (await first.for_run(None)).preload()
        assert '"root":".agents"' in second.instructions_text()

    asyncio.run(run())


def test_main_agent_receives_inventory_without_tool_call(tmp_path, monkeypatch):
    monkeypatch.delenv("EXA_API_KEY", raising=False)
    (tmp_path / ".claude").mkdir()
    workspace = tmp_path / "project"
    workspace.mkdir()
    (tmp_path / "AGENTS.md").write_text("Inherited main guidance")
    (workspace / ".claude").mkdir()
    seen = []

    async def respond(messages, info):
        seen.append(info)
        assert "inventory_agent_context" not in {tool.name for tool in info.function_tools}
        assert "Inherited main guidance" in info.instructions
        assert '"root":".claude"' in info.instructions
        assert "Call `inventory_agent_context`" not in info.instructions
        yield "Done"

    agent = create_agent("test", workspace)
    agent.run_sync("Hello", model=FunctionModel(stream_function=respond))
    assert len(seen) == 1


def test_worker_uses_automatic_context(tmp_path, monkeypatch):
    monkeypatch.delenv("EXA_API_KEY", raising=False)
    # Capture construction to check the actual worker configuration.
    from pydantic_ai import Agent

    workspace = tmp_path / "project"
    workspace.mkdir()
    (tmp_path / "AGENTS.md").write_text("Inherited worker guidance")
    with patch("pcode.agent.Agent", wraps=Agent) as constructor:
        create_coder(workspace)
    capabilities = constructor.call_args.kwargs["capabilities"]
    context = next(cap for cap in capabilities if isinstance(cap, AutomaticRepoContext))
    assert context.home_dir == tmp_path.resolve()
    assert "Inherited worker guidance" in context.preload().instructions_text()
    assert context.get_toolset() is None
    assert "inventory_agent_context" not in context.preload().instructions_text()


def test_startup_summary_reports_only_selected_instructions(tmp_path):
    (tmp_path / "CLAUDE.md").write_text("selected instruction body")
    (tmp_path / "AGENTS.md").write_text("selected instruction body")
    root = tmp_path / ".claude"
    (root / "agents").mkdir(parents=True)
    (root / "agents/helper.md").write_text("agent body")
    (root / "settings.json").write_text('{"hooks": {}}')
    capability = AutomaticRepoContext(root=tmp_path)
    # Instruction reads are allowed; asset bodies must never be read.
    from pathlib import Path

    original = Path.read_text

    def read(path, *args, **kwargs):
        assert path.name in {"CLAUDE.md", "AGENTS.md"}
        return original(path, *args, **kwargs)

    with patch.object(Path, "read_text", read):
        text = "\n".join(capability.startup_summary())
    assert text == "Loaded repository instructions: CLAUDE.md"
    # The asset inventory reaches the model, not the startup notes.
    assert ".claude" in capability.preload().instructions_text()


def test_startup_summary_empty_repository(tmp_path):
    summary = AutomaticRepoContext(root=tmp_path).startup_summary()
    assert summary == []


def test_startup_summary_instructions_only(tmp_path):
    (tmp_path / "AGENTS.md").write_text("Repository guidance")
    summary = AutomaticRepoContext(root=tmp_path).startup_summary()
    assert summary == ["Loaded repository instructions: AGENTS.md"]


def test_startup_summary_omits_discovered_configuration(tmp_path):
    (tmp_path / ".claude").mkdir()
    assert AutomaticRepoContext(root=tmp_path).startup_summary() == []


def test_app_displays_actual_agent_context_and_preview_stays_local(tmp_path, monkeypatch):
    from io import StringIO

    from rich.console import Console

    from pcode.app import PreviewApp
    from pcode.live import AgentRuntime

    monkeypatch.delenv("EXA_API_KEY", raising=False)
    (tmp_path / "AGENTS.md").write_text("do not display this body")
    output = StringIO()
    app = PreviewApp(
        model="test",
        workspace=tmp_path,
        console=Console(file=output, width=160, color_system=None),
        runtime=AgentRuntime(create_agent("test", tmp_path)),
    )
    app.controller.show_startup_context()
    assert "Loaded repository instructions: AGENTS.md" in output.getvalue()
    assert "do not display this body" not in output.getvalue()
    output.seek(0)
    output.truncate()
    preview = PreviewApp(console=Console(file=output), workspace=tmp_path)
    preview.controller.show_startup_context()
    assert output.getvalue() == ""


def test_startup_context_is_not_repeated_after_a_model_switch(tmp_path, monkeypatch):
    from io import StringIO

    from rich.console import Console

    from pcode.app import PreviewApp
    from pcode.live import AgentRuntime

    monkeypatch.delenv("EXA_API_KEY", raising=False)
    (tmp_path / "AGENTS.md").write_text("guidance")
    output = StringIO()
    app = PreviewApp(
        model="test",
        workspace=tmp_path,
        console=Console(file=output, width=160, color_system=None),
        runtime=AgentRuntime(create_agent("test", tmp_path)),
    )
    app.skill_command_names = ["/skill:review"]
    app.controller.show_startup_context()
    first = output.getvalue()
    assert "Loaded repository instructions: AGENTS.md" in first
    assert "Skill commands: /skill:review" in first
    output.seek(0)
    output.truncate()
    app.controller.show_startup_context()
    assert output.getvalue() == ""


def test_walk_up_loads_both_filenames_in_ancestor_first_order(tmp_path, monkeypatch):
    home = tmp_path / "home"
    repo = home / "project"
    workspace = repo / "src"
    workspace.mkdir(parents=True)
    monkeypatch.setattr(Path, "home", lambda: home)
    (tmp_path / "AGENTS.md").write_text("Above home must not load")
    (home / "AGENTS.md").write_text("Home guidance")
    (repo / ".git").mkdir()  # Repository boundaries do not stop inheritance.
    (repo / "CLAUDE.md").write_text("Project Claude guidance")
    (repo / "AGENTS.md").write_text("Project agents guidance")
    (workspace / "AGENTS.md").write_text("Workspace guidance")
    (workspace / "nested").mkdir()
    (workspace / "nested/AGENTS.md").write_text("Descendant must not load")
    (repo / "sibling").mkdir()
    (repo / "sibling/AGENTS.md").write_text("Sibling must not load")

    context = create_repo_context(workspace)
    instructions = context.preload().instructions_text()
    bodies = [
        "Home guidance",
        "Project Claude guidance",
        "Project agents guidance",
        "Workspace guidance",
    ]
    positions = [instructions.index(body) for body in bodies]
    assert positions == sorted(positions)
    assert "must not load" not in instructions
    assert context.startup_summary() == [
        # Ancestors relative to the workspace, so no host path is shown.
        "Loaded repository instructions: ../../AGENTS.md, ../CLAUDE.md, ../AGENTS.md, AGENTS.md"
    ]


def test_local_instruction_files_load_after_shared_ones(tmp_path, monkeypatch):
    home = tmp_path / "home"
    repo = home / "project"
    workspace = repo / ".worktrees/session"  # A session worktree inherits them.
    workspace.mkdir(parents=True)
    monkeypatch.setattr(Path, "home", lambda: home)
    (repo / "AGENTS.local.md").write_text("Agents local guidance")
    (repo / "AGENTS.md").write_text("Shared guidance")
    (repo / "CLAUDE.local.md").write_text("Claude local guidance")
    (workspace / "AGENTS.md").write_text("Shared guidance")

    context = create_repo_context(workspace)
    instructions = context.preload().instructions_text()
    bodies = ["Shared guidance", "Claude local guidance", "Agents local guidance"]
    positions = [instructions.index(body) for body in bodies]
    assert positions == sorted(positions)
    assert context.startup_summary() == [
        "Loaded repository instructions: "
        "../../AGENTS.md, ../../CLAUDE.local.md, ../../AGENTS.local.md"
    ]


def test_local_instruction_file_loads_without_shared_one(tmp_path):
    (tmp_path / "AGENTS.local.md").write_text("Only local guidance")
    context = create_repo_context(tmp_path)
    assert "Only local guidance" in context.preload().instructions_text()


def test_workspace_at_home_loads_home_only(tmp_path):
    (tmp_path / "AGENTS.md").write_text("Home workspace guidance")
    context = create_repo_context(tmp_path)
    assert context.home_dir == tmp_path.resolve()
    assert "Home workspace guidance" in context.preload().instructions_text()
    assert context.startup_summary() == ["Loaded repository instructions: AGENTS.md"]


def test_workspace_outside_home_still_loads_ancestors(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    (home / "AGENTS.md").write_text("Unrelated home guidance")
    monkeypatch.setattr(Path, "home", lambda: home)
    workspace = tmp_path / "elsewhere/project"
    workspace.mkdir(parents=True)
    (workspace.parent / "AGENTS.md").write_text("Outside home ancestor guidance")
    (workspace / "AGENTS.md").write_text("Outside home workspace guidance")
    context = create_repo_context(workspace)
    assert context.home_dir == Path(workspace.resolve().anchor)
    instructions = context.preload().instructions_text()
    assert "Unrelated home guidance" not in instructions
    assert instructions.index("Outside home ancestor guidance") < instructions.index(
        "Outside home workspace guidance"
    )


def test_symlinked_workspace_uses_resolved_ancestry(tmp_path):
    real = tmp_path / "real/project"
    real.mkdir(parents=True)
    (real.parent / "AGENTS.md").write_text("Real ancestor guidance")
    alias_parent = tmp_path / "aliases"
    alias_parent.mkdir()
    (alias_parent / "AGENTS.md").write_text("Alias ancestor must not load")
    alias = alias_parent / "project"
    alias.symlink_to(real, target_is_directory=True)
    context = create_repo_context(alias)
    assert context.root == real.resolve()
    instructions = context.preload().instructions_text()
    assert "Real ancestor guidance" in instructions
    assert "Alias ancestor must not load" not in instructions


def test_walk_up_deduplicates_contents_and_symlink_targets(tmp_path):
    workspace = tmp_path / "project"
    workspace.mkdir()
    (tmp_path / "CLAUDE.md").write_text("Shared guidance")
    (tmp_path / "AGENTS.md").symlink_to("CLAUDE.md")
    (workspace / "AGENTS.md").write_text("Shared guidance")
    context = create_repo_context(workspace)
    assert context.preload().instructions_text().count("Shared guidance") == 1
    assert context.startup_summary() == ["Loaded repository instructions: ../CLAUDE.md"]


def test_ancestor_instructions_refresh_between_runs_not_within_run(tmp_path):
    workspace = tmp_path / "project"
    workspace.mkdir()
    instruction_file = tmp_path / "AGENTS.md"
    instruction_file.write_text("Original ancestor guidance")

    async def run():
        context = create_repo_context(workspace)
        first = (await context.for_run(None)).preload()
        assert "Original ancestor guidance" in first.instructions_text()
        instruction_file.write_text("Updated ancestor guidance")
        assert "Original ancestor guidance" in first.instructions_text()
        second = (await first.for_run(None)).preload()
        assert second.home_dir == context.home_dir
        assert "Updated ancestor guidance" in second.instructions_text()
        assert "Original ancestor guidance" not in second.instructions_text()

    asyncio.run(run())


@pytest.mark.parametrize("walk_up", ["on", "off"])
@pytest.mark.parametrize("nested", ["off", "pointer", "contents"])
@pytest.mark.parametrize("worker", [False, True], ids=["main", "worker"])
@pytest.mark.parametrize("tool", ["read_file", "list_files"])
def test_discovery_settings_work_together_in_real_agents(
    tmp_path, monkeypatch, walk_up, nested, worker, tool
):
    monkeypatch.delenv("EXA_API_KEY", raising=False)
    workspace = tmp_path / "project"
    child = workspace / "backend"
    child.mkdir(parents=True)
    (tmp_path / "AGENTS.md").write_text("ANCESTOR_GUIDANCE")
    (workspace / "AGENTS.md").write_text("WORKSPACE_GUIDANCE")
    (child / "AGENTS.md").write_text("NESTED_GUIDANCE")
    (child / "api.py").write_text("pass")
    save_preferences(repo_context_walk_up=walk_up, repo_context_nested=nested)

    if worker:
        with patch("pcode.agent.Agent", wraps=Agent) as constructor:
            create_coder(workspace)
        agent = Agent("test", capabilities=constructor.call_args.kwargs["capabilities"])
    else:
        agent = create_agent("test", workspace)

    calls = 0
    initial_instructions = None

    async def respond(messages, info):
        nonlocal calls, initial_instructions
        instructions = info.instructions
        assert ("ANCESTOR_GUIDANCE" in instructions) == (walk_up == "on")
        assert "WORKSPACE_GUIDANCE" in instructions
        assert "NESTED_GUIDANCE" not in instructions
        if initial_instructions is None:
            initial_instructions = instructions
        assert instructions == initial_instructions  # Traversal leaves the prefix stable.
        notes = [note for note in _notes(messages) if "backend/AGENTS.md" in note]
        if calls == 0 or nested == "off":
            assert notes == []
        else:
            # Traversing the same directory twice only surfaces it once per run.
            assert len(notes) == 1
            assert ("NESTED_GUIDANCE" in notes[0]) == (nested == "contents")
            if nested == "pointer":
                assert "Read the file if it is relevant" in notes[0]
        calls += 1
        if calls <= 2:
            path = "backend/api.py" if tool == "read_file" else "backend"
            yield {
                0: DeltaToolCall(
                    name=tool, json_args=json.dumps({"path": path}), tool_call_id=str(calls)
                )
            }
        else:
            yield "Done"

    model = FunctionModel(stream_function=respond)
    assert agent.run_sync("Explore", model=model).output == "Done"
    assert calls == 3
    # A fresh run can surface the nested file again; settings survive for_run().
    calls = 0
    assert agent.run_sync("Explore again", model=model).output == "Done"
    assert calls == 3


def test_discovery_settings_are_snapshotted_until_agent_recreation(tmp_path):
    save_preferences(repo_context_walk_up="off", repo_context_nested="pointer")
    context = create_repo_context(tmp_path)
    save_preferences(repo_context_walk_up="on", repo_context_nested="contents")

    async def run():
        snapshot = await context.for_run(None)
        assert snapshot.home_dir is None
        assert snapshot.nested_traversal
        assert snapshot.nested_inject == "pointer"

    asyncio.run(run())
    replacement = create_repo_context(tmp_path)
    assert replacement.home_dir == tmp_path.resolve()
    assert replacement.nested_inject == "contents"


@pytest.mark.parametrize("value", ["invalid", True, [], None])
def test_invalid_discovery_preferences_fall_back_to_defaults(tmp_path, value):
    save_preferences(repo_context_walk_up=value, repo_context_nested=value)
    context = create_repo_context(tmp_path)
    assert context.home_dir == tmp_path.resolve()
    assert not context.nested_traversal


@pytest.mark.parametrize("worker", [False, True])
@pytest.mark.parametrize("tool", ["read_file", "list_files"])
def test_external_traversal_does_not_load_external_instructions(
    tmp_path, monkeypatch, worker, tool
):
    monkeypatch.delenv("EXA_API_KEY", raising=False)
    workspace = tmp_path / "workspace"
    internal = workspace / "child"
    external = tmp_path / "external"
    internal.mkdir(parents=True)
    external.mkdir()
    (internal / "AGENTS.md").write_text("INTERNAL_GUIDANCE")
    (external / "AGENTS.md").write_text("EXTERNAL_GUIDANCE")
    (internal / "sample.txt").write_text("inside")
    (external / "sample.txt").write_text("outside")
    save_preferences(repo_context_nested="contents")
    if worker:
        with patch("pcode.agent.Agent", wraps=Agent) as constructor:
            create_coder(workspace)
        agent = Agent("test", **constructor.call_args.kwargs)
    else:
        agent = create_agent("test", workspace)
    calls = 0

    async def respond(messages, info):
        nonlocal calls
        notes = _notes(messages)
        assert "EXTERNAL_GUIDANCE" not in info.instructions
        assert not any("EXTERNAL_GUIDANCE" in note for note in notes)
        assert any("INTERNAL_GUIDANCE" in note for note in notes) == (calls == 2)
        if calls < 2:
            selected = external if calls == 0 else internal
            path = selected / "sample.txt" if tool == "read_file" else selected
            calls += 1
            yield {0: DeltaToolCall(name=tool, json_args=json.dumps({"path": str(path)}))}
        else:
            yield "Done"

    assert agent.run_sync("Explore", model=FunctionModel(stream_function=respond)).output == "Done"
