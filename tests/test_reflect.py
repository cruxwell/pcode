"""/reflect builds its prompt from where pcode looks on this machine."""

import subprocess
from pathlib import Path

import pytest

from pcode.ext import BUNDLED_DIR, Extension, ExtensionUI, load_extension
from pcode.extensions import reflect
from pcode.preferences import save_preferences
from pcode.repo_context import create_repo_context


def load(workspace, *, is_worker=False):
    return load_extension(
        Extension("reflect", BUNDLED_DIR / "reflect.py", "bundled"),
        workspace,
        ExtensionUI(),
        set(),
        set(),
        is_worker=is_worker,
    )


def git(*args, cwd):
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    git("init", "-q", cwd=path)
    return path.resolve()


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path.resolve()


@pytest.fixture
def home():
    path = Path.home()
    path.mkdir(parents=True, exist_ok=True)
    return path.resolve()


def loaded(workspace: Path) -> list[Path]:
    """What pcode itself would load for `workspace`."""
    context = create_repo_context(workspace).preload()
    return [Path(file.path).resolve() for file in context._context_files or []]


def line(text: str, label: str) -> str:
    [match] = [row for row in text.splitlines() if row.startswith(f"- {label}")]
    return match


def test_ships_on_and_sends_the_prompt_as_a_turn(tmp_path, home):
    workspace = repo(home / "code" / "app")
    extension = load(workspace)
    assert extension.loaded, extension.error
    assert extension.capabilities == []  # No always-on prompt cost.
    [command] = extension.commands
    assert command.name == "/reflect"
    prompt = command.handler("the flaky test")
    assert "focus on: the flaky test" in prompt
    assert reflect.log_path().is_relative_to(tmp_path / "state")
    assert f"`{reflect.log_path()}`" in prompt
    assert f"## <YYYY-MM-DD> {workspace}" in prompt
    assert "{" not in prompt  # Every placeholder filled.
    assert "focus on" not in command.handler("  ")


def test_session_scope_by_default_and_saved_sessions_on_request(home):
    workspace = repo(home / "app")
    [command] = load(workspace).commands
    assert command.arguments == ("--project", "--all")

    plain = command.handler("")
    assert plain.startswith("Reflect on this session ")
    assert "search_sessions` (scope=" not in plain
    assert "sessions after its date" not in plain

    project = command.handler("--project  the flaky test")
    assert "focus on: the flaky test\n" in project
    assert "search_sessions` (scope='project')" in project
    assert "sessions after its date" in project
    assert "span projects" not in project

    everywhere = command.handler("--all")
    assert "search_sessions` (scope='all')" in everywhere
    assert "never copy another project's names" in everywhere
    assert "focus on" not in everywhere
    for prompt in (plain, project, everywhere):
        assert "{" not in prompt
        assert "lessons come only from `harness` rows" in prompt
        assert "A removal is a lesson" in prompt


@pytest.mark.parametrize(
    ("argument", "expected"),
    [
        ("", (None, "")),
        ("  why the tmux test --all hung ", (None, "why the tmux test --all hung")),
        ("--all", ("--all", "")),
        ("--project tmux", ("--project", "tmux")),
    ],
)
def test_parse_arguments(argument, expected):
    assert reflect.parse_arguments(argument) == expected


@pytest.mark.parametrize("argument", ["--everything", "--all --project", "--project --project x"])
def test_bad_options_are_reported_not_sent(home, argument):
    notices = []
    extension = load_extension(
        Extension("reflect", BUNDLED_DIR / "reflect.py", "bundled"),
        repo(home / "app"),
        ExtensionUI(lambda text, level: notices.append((level, text))),
        set(),
        set(),
    )
    [command] = extension.commands
    assert command.handler(argument) is None
    [(level, _)] = notices
    assert level == "error"


def test_workers_get_no_command(home):
    assert load(repo(home / "app"), is_worker=True).commands == []


def test_every_layer_pcode_loads_is_offered(home):
    write(home / "AGENTS.md", "user")
    group = write(home / "work" / "AGENTS.md", "group")
    project = repo(home / "work" / "app")
    both = [write(project / "CLAUDE.md", "claude"), write(project / "AGENTS.md", "agents")]

    text = reflect.targets(project / ".")
    assert line(text, "User instructions") == (
        f"- User instructions (every project): `{home / 'AGENTS.md'}`"
    )
    assert f"`{group}`" in line(text, "Instruction files above")
    assert line(text, "Project instructions") == (
        f"- Project instructions ({project}): `{both[0]}`, `{both[1]}`"
    )
    assert line(text, "User skills") == f"- User skills: `{home / '.agents' / 'skills'}`"
    assert line(text, "Project skills") == f"- Project skills: `{project / '.agents' / 'skills'}`"
    assert set(loaded(project)) == {home / "AGENTS.md", group, *both}

    fresh = repo(home / "fresh")
    assert "(does not exist yet; create it)" in line(reflect.targets(fresh), "Project instructions")


def test_a_subdirectory_workspace_is_the_project(home):
    root = repo(home / "mono")
    top = write(root / "AGENTS.md", "root")
    package = write(root / "pkg" / "a" / "AGENTS.md", "package").parent

    text = reflect.targets(package)
    assert f"Project instructions ({package}): `{package / 'AGENTS.md'}`" in text
    assert f"`{top}`" in line(text, "Instruction files above")
    assert f"`{package / '.agents' / 'skills'}`" in line(text, "Project skills")
    assert f"`{package / '.pcode' / 'extensions'}`" in text

    save_preferences(repo_context_walk_up="off")
    text = reflect.targets(package)
    assert loaded(package) == [package / "AGENTS.md"]
    assert f"`{top}`" not in text
    assert "none, because `repo_context_walk_up` is off" in line(text, "User instructions")


def test_a_worktree_copy_of_the_project_file_is_not_a_layer_above(home):
    main = repo(home / "app")
    write(main / "AGENTS.md", "main")
    git("add", ".", cwd=main)
    git("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init", cwd=main)
    git("worktree", "add", "-q", str(main / ".worktrees" / "b"), "-b", "b", cwd=main)
    worktree = (main / ".worktrees" / "b").resolve()
    write(worktree / "AGENTS.md", "edited on the branch")

    text = reflect.targets(worktree)
    assert main / "AGENTS.md" in loaded(worktree)  # pcode does load it,
    assert "Instruction files above" not in text  # but it is the project's own file.
    assert f"Project instructions ({worktree}): `{worktree / 'AGENTS.md'}`" in text
    assert f"## <YYYY-MM-DD> {main}\n" in reflect.build_prompt(worktree)


def test_home_as_the_workspace_or_a_git_repo(home):
    git("init", "-q", cwd=home)
    write(home / "AGENTS.md", "user")
    plain = (home / "notes").resolve()
    plain.mkdir()
    text = reflect.targets(plain)
    assert line(text, "User instructions").endswith(f"`{home / 'AGENTS.md'}`")
    assert f"Project instructions ({plain}): `{plain / 'AGENTS.md'}` (does not" in text

    text = reflect.targets(home)
    assert "the workspace is your home directory" in line(text, "User instructions")
    assert f"Project instructions ({home}): `{home / 'AGENTS.md'}`" in text


def test_outside_home_offers_the_files_pcode_loads_above(tmp_path, home):
    outside = tmp_path / "srv"
    above = write(outside / "AGENTS.md", "org")
    workspace = repo(outside / "app")

    text = reflect.targets(workspace)
    assert above in loaded(workspace)
    assert f"`{above}`" in line(text, "Instruction files above")
    assert "the workspace is outside your home" in line(text, "User instructions")


def test_skill_dirs_split_by_where_they_resolve(tmp_path, home):
    workspace = repo(home / "app")
    shared = tmp_path / "shared-skills"
    save_preferences(skill_dirs=f" ~/s2 :tools/skills:{shared}")

    text = reflect.targets(workspace)
    assert line(text, "User skills") == f"- User skills: `{home / 's2'}`, `{shared.resolve()}`"
    assert f"`{workspace / 'tools' / 'skills'}`" in line(text, "Project skills")


def test_a_project_already_using_another_skill_root_keeps_it(home):
    workspace = repo(home / "app")
    (workspace / ".claude" / "skills").mkdir(parents=True)
    assert line(reflect.targets(workspace), "Project skills").startswith(
        f"- Project skills: `{workspace / '.claude' / 'skills'}`, "
    )


def test_a_symlinked_instruction_file_is_listed_once(home):
    workspace = repo(home / "app")
    write(workspace / "AGENTS.md", "rules")
    (workspace / "CLAUDE.md").symlink_to("AGENTS.md")
    assert line(reflect.targets(workspace), "Project instructions") == (
        f"- Project instructions ({workspace}): `{workspace / 'AGENTS.md'}`"
    )
