"""Project briefs: orgs, the vault, /brief prompts, sync reminders, edit diffs, plans."""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic_ai.messages import ModelRequest, ToolReturnPart, UserPromptPart

from pcode.ext import BUNDLED_DIR, Extension, ExtensionUI, load_extension, set_enabled
from pcode.extensions import basic_memory as bm
from pcode.extensions import plan_mode
from pcode.sessions import SessionInfo

SESSION = "0123456789abcdef0123456789abcdef"


class FakeAPI:
    def __init__(self, workspace: Path, session_dir: Path, *, is_worker=False):
        self.workspace = workspace
        self.session_dir = session_dir
        self.is_worker = is_worker
        self.notes: list[str] = []
        self.ui = SimpleNamespace(notify=lambda text, level="info": self.notes.append(text))
        self.commands = {}
        self.tools = {}
        self.instruction_text: list[str] = []
        self.hooked = {}
        on = SimpleNamespace(
            before_run=self._hook("before_run"),
            after_tool_execute=self._hook("after_tool_execute"),
            before_model_request=self._hook("before_model_request"),
            before_tool_execute=self._hook("before_tool_execute"),
        )
        self.hooks = SimpleNamespace(on=on)

    def _hook(self, name):
        def register(fn):
            self.hooked[name] = fn
            return fn

        return register

    def register_command(self, name, description, handler, **_):
        self.commands[name] = handler

    def instructions(self, text):
        self.instruction_text.append(text)

    def tool(self, fn):
        self.tools[fn.__name__] = fn
        return fn


def ctx(session=SESSION, agent="pcode", messages=()):
    return SimpleNamespace(
        conversation_id=session, agent=SimpleNamespace(name=agent), messages=list(messages)
    )


def run(coroutine):
    return asyncio.run(coroutine)


@pytest.fixture
def org(tmp_path, monkeypatch):
    """An org rooted at tmp/p with its vault at tmp/vaults/personal."""
    monkeypatch.setattr(bm, "project_checkout", lambda path: None)
    root = tmp_path / "p"
    (root / "app").mkdir(parents=True)
    bm.config_path().parent.mkdir(parents=True, exist_ok=True)
    bm.config_path().write_text(
        f'vault_root = "{tmp_path / "vaults"}"\n[[org]]\nname = "personal"\nroot = "{root}"\n'
    )
    return root


@pytest.fixture
def api(org, tmp_path):
    api = FakeAPI(org / "app", tmp_path / "sessions")
    (tmp_path / "sessions" / SESSION).mkdir(parents=True)
    bm.setup(api)
    return api


def vault(tmp_path) -> Path:
    return (tmp_path / "vaults" / "personal").resolve()


def turn(api, text="hi", *, context=None):
    """Run the per-message hook on a fresh user message; return what it appended."""
    request = ModelRequest(parts=[UserPromptPart(text)])
    context = context or ctx(messages=[request])
    context.messages = [request]
    run(api.hooked["before_model_request"](context, SimpleNamespace(messages=[request])))
    return [p.content for p in request.parts[1:]]


def tool(api, name, path, context=None):
    call = SimpleNamespace(tool_name=name)
    run(
        api.hooked["after_tool_execute"](
            context or ctx(),
            call=call,
            tool_def=SimpleNamespace(name=name),
            args={"path": str(path)},
            result="ok",
        )
    )


def test_ships_off(tmp_path):
    extension = load_extension(
        Extension("basic_memory", BUNDLED_DIR / "basic_memory", "bundled"),
        tmp_path,
        ExtensionUI(),
        set(),
        set(),
    )
    assert extension.state() == "off by default (/extensions on basic_memory)"


def test_loads_through_the_real_extension_api(org, tmp_path):
    extension = load_extension(
        Extension("basic_memory", BUNDLED_DIR / "basic_memory", "bundled"),
        org / "app",
        ExtensionUI(),
        set(),
        {"basic_memory"},
    )
    assert extension.loaded, extension.state()
    assert [command.name for command in extension.commands] == ["/brief"]


def test_orgs_label_repos_worktrees_and_plain_dirs(tmp_path):
    c, p = (tmp_path / "c").resolve(), (tmp_path / "p").resolve()
    orgs = [bm.Org("work", c, tmp_path / "v1"), bm.Org("personal", p, tmp_path / "v2")]
    worktree = c / "team" / "svc" / ".worktrees" / "svc-1"
    place = bm.locate(orgs, worktree, c / "team" / "svc")
    assert (place.org.name, place.repo, place.dir) == ("work", "svc", "team/svc")
    assert bm.locate(orgs, c / "team", None) == bm.Place(orgs[0], None, "team")
    assert bm.locate(orgs, p, None).dir == "."
    assert bm.locate(orgs, tmp_path / "elsewhere", None) is None
    inner = bm.Org("inner", c / "team", tmp_path / "v3")
    assert bm.find_org([*orgs, inner], c / "team" / "x").name == "inner"


def test_config_errors_are_reported_not_raised(tmp_path):
    bm.config_path().parent.mkdir(parents=True, exist_ok=True)
    bm.config_path().write_text('[[org]]\nname = "a/b"\nroot = "/tmp"\n')
    api = FakeAPI(tmp_path, tmp_path / "sessions")
    bm.setup(api)
    assert "plain file name" in api.notes[0] and not api.commands and not api.hooked


def test_outside_any_org_offers_only_guidance(tmp_path, monkeypatch):
    monkeypatch.setattr(bm, "project_checkout", lambda path: None)
    api = FakeAPI(tmp_path, tmp_path / "sessions")
    bm.setup(api)
    assert list(api.commands) == ["/brief"] and not api.instruction_text and not api.hooked
    api.commands["/brief"]("")
    assert "memory.toml" in api.notes[0]


def test_instructions_say_where_briefs_live_and_add_no_tools(api, tmp_path):
    [text] = api.instruction_text
    assert str(vault(tmp_path) / "briefs") in text and "Brief format.md" in text
    assert "`app`" in text and not api.tools


def test_workers_get_the_instructions_only(org, tmp_path):
    api = FakeAPI(org / "app", tmp_path / "sessions", is_worker=True)
    bm.setup(api)
    assert api.instruction_text and not api.commands and not api.hooked


def test_setup_writes_the_vault_once_and_enables_the_styling(api, tmp_path):
    api.commands["/brief"]("setup")
    root = vault(tmp_path)
    for relative in ("Brief format.md", "Home.md", "views/Briefs.base"):
        assert (root / relative).exists()
    css = root / ".obsidian" / "snippets" / "memory-states.css"
    assert ".proposed" in css.read_text()
    appearance = json.loads((root / ".obsidian" / "appearance.json").read_text())
    assert appearance["enabledCssSnippets"] == ["memory-states"]
    (root / "Home.md").write_text("mine")
    api.commands["/brief"]("setup")
    assert (root / "Home.md").read_text() == "mine" and "kept" in api.notes[-1]


def test_new_asks_for_a_brief_from_evidence_with_an_optional_focus(api, tmp_path):
    prompt = api.commands["/brief"]('new "Repo onboarding" cover the dispatcher in depth')
    assert str(vault(tmp_path) / "briefs" / "Repo onboarding.md") in prompt
    assert "Brief format.md" in prompt and "search_sessions" in prompt
    assert prompt.endswith("Focus: cover the dispatcher in depth")
    assert (vault(tmp_path) / "Brief format.md").exists()  # set up on first use
    (vault(tmp_path) / "briefs" / "Repo onboarding.md").write_text("x")
    with pytest.raises(ValueError, match="exists"):
        api.commands["/brief"]("new 'Repo onboarding'")
    with pytest.raises(ValueError, match="Usage"):
        api.commands["/brief"]("new")
    assert "Focus: just the API" in api.commands["/brief"]("update just the API")
    with pytest.raises(ValueError, match="Usage"):
        api.commands["/brief"]("frobnicate")


def test_listing_shows_briefs_by_priority_and_open_plans(api, tmp_path):
    briefs = vault(tmp_path) / "briefs"
    briefs.mkdir(parents=True)
    (briefs / "B.md").write_text("---\ntype: brief\npriority: 2\ntldr: second\n---\n")
    (briefs / "A.md").write_text('---\ntype: brief\npriority: 1\ntldr: "first"\n---\n')
    (briefs / "Plan — X.md").write_text("---\ntype: plan\nstate: proposed\ntldr: idea\n---\n")
    (briefs / "Plan — Old.md").write_text("---\ntype: plan\nstate: implemented\n---\n")
    (briefs / "Notes.md").write_text("no frontmatter")
    api.commands["/brief"]("")
    lines = api.notes[-1].splitlines()
    assert "P1  A" in lines[1] and "first" in lines[1] and "P2  B" in lines[2]
    assert "plan [proposed]  Plan — X" in lines[3] and len(lines) == 4


def test_sync_reminds_once_per_message_and_waits_for_the_session(api):
    assert turn(api) == []  # off by default
    api.commands["/brief"]("sync on")
    assert "first message" in api.notes[-1]
    run(api.hooked["before_run"](ctx()))
    [reminder] = turn(api)
    assert reminder.startswith(bm.TAG) and "newcomer" in reminder
    # A retried request already has it; a mid-turn request holds tool results.
    request = ModelRequest(parts=[UserPromptPart("hi"), UserPromptPart(reminder)])
    context = ctx(messages=[request])
    run(api.hooked["before_model_request"](context, SimpleNamespace(messages=[request])))
    assert len(request.parts) == 2
    mid = ModelRequest(parts=[ToolReturnPart("t", "ok", "id"), UserPromptPart("steer")])
    context = ctx(messages=[mid])
    run(api.hooked["before_model_request"](context, SimpleNamespace(messages=[mid])))
    assert len(mid.parts) == 2
    # Persisted beside the session, so a reload keeps it.
    assert json.loads((api.session_dir / SESSION / "memory.json").read_text()) == {"sync": True}
    api.commands["/brief"]("sync off")
    assert turn(api) == []


def test_sync_default_comes_from_config(org, tmp_path):
    config = bm.config_path()
    config.write_text(
        config.read_text().replace('name = "personal"', 'name = "personal"\nauto_sync = true')
    )
    api = FakeAPI(org / "app", tmp_path / "sessions")
    bm.setup(api)
    assert any("Brief sync is on" in part for part in turn(api))


def test_user_edits_come_back_as_a_diff_once(api, tmp_path):
    note = vault(tmp_path) / "briefs" / "A.md"
    note.parent.mkdir(parents=True)
    note.write_text("## Open questions\n**Answer:**\n")
    tool(api, "write_file", note)
    assert turn(api) == []  # nothing changed since the model wrote it
    note.write_text("## Open questions\n**Answer:** yes\n> [!comment] tighten this\n")
    [block] = turn(api)
    assert (
        "changed since you last read" in block
        and "+**Answer:** yes" in block
        and "[!comment]" in block
    )
    assert turn(api) == []  # reported once
    # Notes outside the vault, and side questions, are not tracked.
    outside = tmp_path / "elsewhere.md"
    outside.write_text("a")
    tool(api, "read_file", outside)
    outside.write_text("b")
    assert turn(api) == []
    note.write_text("changed again")
    assert turn(api, context=ctx(agent="worker")) == []


def save_session(root: Path, session_id: str, workspace: Path, updated: str):
    directory = root / session_id
    directory.mkdir(parents=True)
    info = SessionInfo(
        id=session_id,
        model="m",
        workspace=str(workspace),
        created=updated,
        updated=updated,
        turns=1,
        title="Old work",
        packages={},
    )
    (directory / "session.json").write_text(info.model_dump_json())
    records = [
        {"kind": "turn_started", "prompt": "fix it", "run_id": "run-1"},
        {"kind": "ToolSummary", "name": "shell", "command": "make test"},
        {"kind": "Message", "markdown": "Did the thing."},
        {"kind": "turn_completed"},
    ]
    (directory / "transcript.jsonl").write_text("\n".join(json.dumps(r) for r in records) + "\n")


def test_import_lists_sessions_here_and_sends_digests(api, org, tmp_path):
    old = "aaaaaaaa" + "0" * 24
    save_session(tmp_path / "sessions", old, org / "app", "2026-01-02T10:00:00+00:00")
    save_session(tmp_path / "sessions", "b" * 32, org / "other", "2026-01-03T10:00:00+00:00")
    api.commands["/brief"]("import")
    assert "aaaaaaaa" in api.notes[-1] and "bbbbbbbb" not in api.notes[-1]
    prompt = api.commands["/brief"]("import aaaaaaaa -- only the API")
    assert "### Turn run-1" in prompt and "[shell] make test" in prompt
    assert "Did the thing." in prompt and "Focus: only the API" in prompt
    assert "read_session" in prompt
    with pytest.raises(ValueError, match="8 characters"):
        api.commands["/brief"]("import aaa")


@pytest.mark.parametrize("memory_on", [False, True])
def test_plan_location_does_not_depend_on_the_extension(org, tmp_path, memory_on):
    set_enabled("basic_memory", memory_on)
    location = bm.plan_location(org / "app")
    assert location.directory == vault(tmp_path) / "briefs"
    assert location.path_for("add export button to the orders page").name == (
        "Plan — app — Add export button to the orders page.md"
    )
    with pytest.raises(ValueError, match="No configured org"):
        bm.plan_location(tmp_path / "elsewhere")


def test_plan_mode_writes_vault_plans_and_approves_them(org, tmp_path):
    set_enabled("basic_memory", True)
    api = FakeAPI(org / "app", tmp_path / "sessions")
    plan_mode.setup(api)
    api.commands["/plan"]("Local by default")
    assert ".pcode/plans/local-by-default.md" in api.notes[-1]
    assert api.commands["/plan"]("--obsidian Add export button") == "Add export button"
    path = vault(tmp_path) / "briefs" / "Plan — app — Add export button.md"
    assert "Plan — app — Add export button.md" in api.notes[-1]
    request = ModelRequest(parts=[UserPromptPart("hi")])
    context = SimpleNamespace(agent=SimpleNamespace(name="pcode"), messages=[request])
    run(api.hooked["before_model_request"](context, SimpleNamespace(messages=[request])))
    reminder = request.parts[-1].content
    assert str(path).replace(str(Path.home()), "~") in reminder or str(path) in reminder
    assert "proposed brief" in reminder and "Brief format.md" in reminder
    result = api.tools["exit_plan_mode"]()
    # The model makes the approval edit itself, so it's never mistaken for the user's.
    assert "state: approved" in result and "Open plans" in result
    # With no active plan, bare /plan resumes the newest local plan, never a
    # vault plan (a vault holds every project's plans), even with memory on.
    path.parent.mkdir(parents=True, exist_ok=True)
    (path.parent / "Plan — other — Something.md").write_text("x")
    (org / "app" / ".pcode/plans/local-by-default.md").write_text("x")
    api.commands["/plan"]("")
    assert "Resuming .pcode/plans/local-by-default.md" in api.notes[-1]


@pytest.mark.parametrize("plan_first", [True, False])
def test_plan_and_memory_hooks_emit_one_diff(org, tmp_path, plan_first):
    set_enabled("basic_memory", True)
    memory = FakeAPI(org / "app", tmp_path / "sessions")
    planner = FakeAPI(org / "app", tmp_path / "sessions")
    bm.setup(memory)
    plan_mode.setup(planner)
    planner.commands["/plan"]("--obsidian Topic")
    path = vault(tmp_path) / "briefs" / "Plan — app — Topic.md"
    path.write_text("Before")
    tool(memory, "read_file", path)
    path.write_text("External answer")
    request = ModelRequest(parts=[UserPromptPart("Continue")])
    context = ctx(messages=[request])
    hooks = (planner, memory) if plan_first else (memory, planner)
    for api in hooks:
        run(api.hooked["before_model_request"](context, SimpleNamespace(messages=[request])))
    assert sum("```diff" in str(part.content) for part in request.parts) == 1
    for api in hooks:  # Retry neither repeats nor consumes additional feedback.
        run(api.hooked["before_model_request"](context, SimpleNamespace(messages=[request])))
    assert sum("```diff" in str(part.content) for part in request.parts) == 1


def test_sync_works_in_unsaved_sessions_and_edits_reset_per_conversation(org, tmp_path):
    api = FakeAPI(org / "app", tmp_path / "sessions")  # no session directory: --no-save
    bm.setup(api)
    run(api.hooked["before_run"](ctx()))
    api.commands["/brief"]("sync on")
    assert any("Brief sync is on" in part for part in turn(api))
    note = vault(tmp_path) / "briefs" / "A.md"
    note.parent.mkdir(parents=True)
    note.write_text("one")
    tool(api, "read_file", note)
    note.write_text("two")
    other = ctx(session="f" * 32)
    run(api.hooked["before_run"](other))  # /new: a different conversation
    assert not any("changed since" in part for part in turn(api, context=other))
