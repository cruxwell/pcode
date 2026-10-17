"""Defaults commands, shared by the CLI and interactive terminal.

Editing defaults deliberately does not mutate a running conversation. In-session
shortcuts change both the active setting and its saved default; the terminal also
applies layout-only settings (`attach_tasks`, `tasks_max_height`, `task_style`, `frame_style`,
`tool_glyphs`, `tool_max_lines`, `tool_linger_seconds` and `show_hints`) immediately.

`config project ...` edits the workspace's `.pcode/preferences.json`, which is
layered over the user file at launch (except for `USER_ONLY` keys).
"""

import json
from collections.abc import Sequence
from pathlib import Path

from pcode.preferences import (
    MODEL_EFFORTS_KEY,
    SETTINGS,
    USER_ONLY,
    Setting,
    preferences_path,
    project_preferences_path,
    read_preferences,
    update_preferences,
    valid_preferences,
)

USAGE = "config [list | diff | path | get KEY | set KEY VALUE | unset KEY | reset | project ...]"
PROJECT_USAGE = "config project [list | path | set KEY VALUE | unset KEY | reset]"

# The terminal reapplies these after a config edit. Shared with the browser so
# its timing hints cannot drift from the command's immediate-update behavior.
IMMEDIATE_SETTINGS = frozenset(
    {
        "attach_tasks",
        "tasks_max_height",
        "tasks_min_rows",
        "tasks_min_columns",
        "task_style",
        "frame_style",
        "tool_glyphs",
        "tool_max_lines",
        "tool_linger_seconds",
        "show_hints",
        "live_edits",
    }
)

# Resetting clears every per-repository extension grant, so say so rather than
# leaving the next launch to re-prompt unexplained.
_RESET_NOTICE = {
    "trusted_projects": "repositories must be trusted again",
}


def listed_settings() -> list[str]:
    """The settings `list` and completion show."""
    return list(SETTINGS)


def _setting(key: str) -> Setting:
    if key not in SETTINGS:
        raise ValueError(f"Unknown setting '{key}'. Available: {', '.join(listed_settings())}")
    return SETTINGS[key]


def _effective(data: dict, key: str) -> str | None:
    setting = SETTINGS[key]
    value = data.get(key)
    if isinstance(value, str):
        try:
            setting.validate(key, value)
        except ValueError:
            pass
        else:
            return value
    return setting.default


def _effective_data() -> dict:
    """Both files read strictly, so a broken one is reported instead of hidden."""
    data = valid_preferences(read_preferences())
    project = project_preferences_path()
    if project is not None:
        data.update(
            {
                k: v
                for k, v in valid_preferences(read_preferences(project)).items()
                if k not in USER_ONLY
            }
        )
    return data


def _reset(path: Path | None, label: str) -> str:
    """Drop every known setting, leaving unrecognized keys the file may carry."""
    # Per-model efforts are a setting in every sense a user cares about, so a
    # reset must clear them too even though they live outside SETTINGS.
    known = set(SETTINGS) | {MODEL_EFFORTS_KEY}
    removed = sorted(set(read_preferences(path)) & known)
    if not removed:
        return f"No {label} defaults to reset."
    update_preferences({}, remove=tuple(removed), path=path)
    notice = "".join(f" {_RESET_NOTICE[key]}." for key in removed if key in _RESET_NOTICE)
    return f"Reset {label} defaults: {', '.join(removed)}. Applies on next launch.{notice}"


def _diff() -> str:
    """Only the settings actually changed, and which file changed them.

    A stored value equal to the default is not a difference, and neither is an
    invalid one: both leave the effective value where `list` shows it.
    """
    user = read_preferences()
    project_path = project_preferences_path()
    project = valid_preferences(read_preferences(project_path)) if project_path is not None else {}
    data = _effective_data()
    lines = []
    for key, setting in SETTINGS.items():
        value = _effective(data, key)
        if value == setting.default:
            continue
        source = "project" if key in project and key not in USER_ONLY else "user"
        default = "unset" if setting.default is None else setting.default
        lines.append(f"{key} = {value} (default {default}, from {source})")
    efforts = user.get(MODEL_EFFORTS_KEY)
    if isinstance(efforts, dict) and efforts:
        chosen = ", ".join(f"{model}={effort}" for model, effort in sorted(efforts.items()))
        lines.append(f"{MODEL_EFFORTS_KEY} = {chosen} (default none, from user)")
    if not lines:
        return "Every setting is at its default."
    return "\n".join(lines)


def _project_path() -> Path:
    path = project_preferences_path()
    if path is None:
        raise ValueError("No workspace is selected, so there is no project config to edit.")
    return path


def _configure_project(args: list[str]) -> str:
    action = args[0] if args else "list"
    if action == "path" and len(args) == 1:
        return str(_project_path())
    if action == "list" and len(args) == 1:
        return json.dumps(read_preferences(_project_path()), indent=2)
    if action == "set" and len(args) == 3:
        key, value = args[1:]
        if key in USER_ONLY:
            raise ValueError(f"{key} is user-only; a repository cannot set it. Use config set.")
        _setting(key).validate(key, value)
        update_preferences({key: value}, path=_project_path())
        return f"Saved project default: {key} = {value}. Applies on next launch."
    if action == "unset" and len(args) == 2:
        key = args[1]
        _setting(key)
        update_preferences({}, remove=(key,), path=_project_path())
        return f"Removed project default: {key}. Applies on next launch."
    if action == "reset" and len(args) == 1:
        return _reset(_project_path(), "project")
    raise ValueError(f"Usage: {PROJECT_USAGE}")


def configure(arguments: Sequence[str]) -> str:
    """Inspect or edit saved defaults without starting a model or session.

    `list`, `diff`, and `get` report the effective value, with the project
    overlay applied; `set`, `unset`, and `reset` edit the user file.
    """
    args = list(arguments) or ["list"]
    action = args[0]
    if action == "project":
        return _configure_project(args[1:])
    if action == "path" and len(args) == 1:
        return str(preferences_path())
    if action == "list" and len(args) == 1:
        data = _effective_data()
        return json.dumps({key: _effective(data, key) for key in listed_settings()}, indent=2)
    if action == "diff" and len(args) == 1:
        return _diff()
    if action == "get" and len(args) == 2:
        key = args[1]
        _setting(key)
        value = _effective(_effective_data(), key)
        return "null" if value is None else value
    if action == "set" and len(args) == 3:
        key, value = args[1:]
        _setting(key).validate(key, value)
        update_preferences({key: value})
        return f"Saved global default: {key} = {value}. Applies on next launch."
    if action == "unset" and len(args) == 2:
        key = args[1]
        setting = _setting(key)
        update_preferences({}, remove=(key,))
        value = setting.default if setting.default is not None else "null (offline preview)"
        return f"Reset global default: {key} = {value}. Applies on next launch."
    if action == "reset" and len(args) == 1:
        return _reset(None, "global")
    raise ValueError(f"Usage: {USAGE}")


def config_argument_descriptions() -> dict[str, str]:
    """Completion-menu text: the setting's purpose beside each key, its default beside values."""
    described: dict[str, str] = {
        "list": "Show every effective setting",
        "diff": "Show only settings that differ from their defaults",
        "path": "Print the user preferences file",
        "get": "Show one effective setting",
        "set": "Save a user default (applies on next launch)",
        "unset": "Remove a user default",
        "reset": "Remove every user default",
        "project": "Edit the workspace's .pcode/preferences.json",
        "project list": "Show the project file",
        "project path": "Print the project preferences file",
        "project reset": "Remove every project default",
    }
    for key, setting in SETTINGS.items():
        default = "unset" if setting.default is None else setting.default
        text = f"{setting.description} (default {default})" if setting.description else ""
        for action in ("get", "unset", "set", "project set", "project unset"):
            described[f"{action} {key}"] = text
        for value in setting.choices:
            marker = " (default)" if value == setting.default else ""
            described[f"set {key} {value}"] = f"{setting.description}{marker}"
            described[f"project set {key} {value}"] = described[f"set {key} {value}"]
    return described


def config_arguments() -> tuple[str, ...]:
    """Complete subcommands, keys, and enum values with the existing completer."""
    keys = listed_settings()
    project_keys = [key for key in keys if key not in USER_ONLY]
    return (
        "list",
        "diff",
        "path",
        "get",
        "set",
        "unset",
        "reset",
        "project",
        *(f"get {key}" for key in keys),
        *(f"unset {key}" for key in keys),
        *(f"set {key}" for key in keys),
        *(f"set {key} {value}" for key in keys for value in SETTINGS[key].choices),
        "project list",
        "project path",
        "project reset",
        *(f"project set {key}" for key in project_keys),
        *(f"project unset {key}" for key in project_keys),
        *(f"project set {key} {value}" for key in project_keys for value in SETTINGS[key].choices),
    )
