"""/reflect builds its prompt from where pcode looks on this machine."""

import subprocess
from pathlib import Path

from pcode.ext import BUNDLED_DIR, Extension, ExtensionUI, load_extension
from pcode.extensions import reflect
from pcode.preferences import save_preferences


def load(workspace, *, is_worker=False):
    return load_extension(
        Extension("reflect", BUNDLED_DIR / "reflect.py", "bundled"),
        workspace,
        ExtensionUI(),
        set(),
        set(),
        is_worker=is_worker,
    )


def repo(path: Path) -> Path:
    path.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    return path.resolve()


def test_ships_on_and_sends_the_prompt_as_a_turn(tmp_path):
    workspace = repo(Path.home() / "code" / "app")
    extension = load(workspace)
    assert extension.loaded, extension.error
    assert extension.capabilities == []  # No always-on prompt cost.
    [command] = extension.commands
    assert command.name == "/reflect"
    prompt = command.handler("the flaky test")
    assert "focus on: the flaky test" in prompt
    assert str(reflect.log_path()) in prompt
    assert reflect.log_path().is_relative_to(tmp_path / "state")
    assert f"## <YYYY-MM-DD> {workspace}" in prompt
    assert "{" not in prompt  # Every placeholder filled.


def test_workers_get_no_command(tmp_path):
    assert load(repo(Path.home() / "app"), is_worker=True).commands == []


def test_targets_name_each_layer_and_what_to_create():
    home = Path.home().resolve()
    home.mkdir(parents=True, exist_ok=True)
    (home / "AGENTS.md").write_text("user")
    group = home / "work"
    group.mkdir()
    (group / "AGENTS.md").write_text("group")
    project = repo(group / "app")
    (project / "CLAUDE.md").write_text("project")  # Read before AGENTS.md.
    (project / "AGENTS.md").write_text("other")

    text = reflect.targets(project / ".")
    assert f"User instructions (every project): `{home / 'AGENTS.md'}`" in text
    assert f"`{project / 'CLAUDE.md'}`" in text
    assert f"`{group / 'AGENTS.md'}`" in text
    assert f"User skills: `{home / '.agents' / 'skills'}`" in text
    assert f"Project skills: `{project / '.agents' / 'skills'}`" in text

    fresh = repo(home / "fresh")
    assert f"`{fresh / 'AGENTS.md'}` (does not exist yet; create it)" in reflect.targets(fresh)


def test_a_worktree_copy_of_the_project_file_is_not_a_group_layer():
    home = Path.home().resolve()
    main = repo(home / "app")
    (main / "AGENTS.md").write_text("same")
    worktree = repo(main / ".worktrees" / "branch")
    (worktree / "AGENTS.md").write_text("same")

    text = reflect.targets(worktree)
    assert f"Project instructions ({worktree}): `{worktree / 'AGENTS.md'}`" in text
    assert "between the two" not in text


def test_no_user_instructions_without_the_walk_up():
    save_preferences(repo_context_walk_up="off")
    text = reflect.targets(repo(Path.home() / "app"))
    assert "User instructions (every project): none" in text
