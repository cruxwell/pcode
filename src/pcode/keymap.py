"""User-owned prompt bindings, distinct from editable project preferences."""

import json
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path

from filelock import FileLock
from prompt_toolkit.completion import CompleteEvent, Completion
from prompt_toolkit.document import Document
from prompt_toolkit.filters import Always, Condition

from pcode.commands import CommandRegistry, SlashCompleter
from pcode.preferences import config_dir, write_json
from pcode.prefix_keys import PrefixKeys, Shortcut, validate_action_key

# These names keep native actions addressable even after their keys are replaced.
DEFAULT_ACTIONS = {
    "s": "@send-mode",
    "l": "@model",
    "n": "@effort-up",
    "p": "@effort-down",
    "o": "@tasks",
    "t": "@thinking",
    "g": "@commands",
    "^": "@previous-session",
    "y": "@copy",
    "q": "@side-threads",
    "\\": "@next-side-thread",
}
# Default keys that run a slash command, much as a `/bind KEY /command` would.
# Only chords the Emacs editor barely uses. Ctrl+V pastes an image, the key
# Claude Code and Codex use (vi loses quoted insert and visual block to it);
# Ctrl+Z otherwise inserts a literal ^Z, since pcode never suspends; Ctrl+] is
# character search.
DEFAULT_COMMANDS = {
    "v": "/paste-image",
    "z": "/show-edits",
    "]": "/group-tools",
}
# How the help overlay names these targets, on whichever key they sit.
COMMAND_LABELS = {
    "/paste-image": "Paste an image from the clipboard",
    "/show-edits": "Show / hide edit diffs",
    "/group-tools": "Group / ungroup tool calls",
}
USAGE = "/bind [list | actions | KEY [TARGET] | reset [KEY]]; /unbind KEY disables a key"


def default_target(key: str) -> str | None:
    return DEFAULT_ACTIONS.get(key) or DEFAULT_COMMANDS.get(key)


def bindings_path() -> Path:
    return config_dir() / "bindings.json"


def validate_target(target: str) -> None:
    if not target or any(not char.isprintable() for char in target):
        raise ValueError("A binding target must be a single-line slash command or named @action.")
    if target.startswith("@"):
        if target not in DEFAULT_ACTIONS.values():
            raise ValueError(f"Unknown built-in action: {target}. Use /bind actions.")
    elif not target.startswith("/") or target.split(maxsplit=1)[0] == "/":
        raise ValueError("A binding target must start with / (command) or @ (built-in action).")


def read_bindings() -> dict[str, str | None]:
    path = bindings_path()
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError:
        return {}
    except ValueError:
        raise ValueError(
            f"Invalid bindings JSON: {path}. Repair it before editing bindings."
        ) from None
    if not isinstance(data, dict):
        raise ValueError(f"Bindings must be a JSON object: {path}")
    for key, target in data.items():
        validate_action_key(key)
        if target is not None:
            if not isinstance(target, str):
                raise ValueError(f"Binding {key!r} must be a command, @action, or null.")
            validate_target(target)
    return data


def save_binding(key: str | None, target: str | None = None, *, reset: bool = False) -> dict:
    path = bindings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with FileLock(str(path) + ".lock", timeout=5):
        data = read_bindings()
        if reset:
            if key is None:
                data.clear()
            else:
                data.pop(key, None)
        else:
            validate_action_key(key)
            if target is not None:
                validate_target(target)
            data[key] = target
        write_json(path, data)
    return data


class PromptKeymap:
    def __init__(
        self,
        shortcuts: PrefixKeys,
        registry: CommandRegistry,
        execute: Callable[[str], None],
        report: Callable[[str], None],
    ) -> None:
        self.shortcuts = shortcuts
        self.registry = registry
        self.execute = execute
        self.report = report
        self.defaults = {shortcut.key: shortcut for shortcut in shortcuts.shortcuts}
        self.actions = {DEFAULT_ACTIONS[key]: value for key, value in self.defaults.items()}
        self.overrides: dict[str, str | None] = {}
        try:
            self.apply(read_bindings())
        except (OSError, ValueError) as error:
            report(f"Could not load keybindings: {error}")

    def check_command(self, target: str) -> None:
        validate_target(target)
        if self.registry.resolve(target) is None:
            raise ValueError(f"Command unavailable: {target.split(maxsplit=1)[0]}")

    def invoke(self, target: str) -> None:
        try:
            # Re-resolve at keypress time: extensions and skills can disappear.
            self.check_command(target)
            self.execute(target)
        except ValueError as error:
            self.report(str(error))

    def keys(self) -> list[str]:
        """Every key with a default or a saved binding, defaults first."""
        return list(dict.fromkeys([*self.defaults, *DEFAULT_COMMANDS, *self.overrides]))

    def apply(self, overrides: dict[str, str | None]) -> None:
        for key in set(self.keys()) | set(overrides):
            self.shortcuts.remove(key)
        self.overrides = overrides
        for key in self.keys():
            target = overrides.get(key, default_target(key))
            if target is None:
                continue
            if target.startswith("@"):
                self.shortcuts.set_shortcut(replace(self.actions[target], key=key))
                continue
            # A default command stays out of the way where this prompt lacks
            # it; a saved one still reports why its key did nothing.
            name = target.split(maxsplit=1)[0]
            available = (
                Always()
                if key in overrides
                else Condition(lambda name=name: self.registry.find(name) is not None)
            )
            self.shortcuts.set_shortcut(
                Shortcut(
                    key,
                    COMMAND_LABELS.get(target, target),
                    lambda event, text=target: self.invoke(text),
                    available,
                )
            )

    def describe(self, key: str) -> str:
        target = self.overrides.get(key, default_target(key))
        source = "custom" if key in self.overrides else "default"
        if target is None:
            return f"{key}: disabled" if key in self.overrides else f"{key}: unbound"
        description = self.actions[target].label if target.startswith("@") else target
        if target.startswith("/") and self.registry.find(target.split(maxsplit=1)[0]) is None:
            description += " (unavailable)"
        return f"{self.shortcuts.label(key)}: {target} — {description} [{source}]"

    def manage(self, argument: str, *, unbind: bool = False) -> str:
        parts = argument.strip().split(maxsplit=1)
        if unbind:
            if len(parts) != 1:
                raise ValueError("Usage: /unbind KEY")
            key = parts[0]
            validate_action_key(key)
            self.apply(save_binding(key, None))
            return f"Disabled binding {key}. /bind reset {key} restores its default."
        if not parts or parts == ["list"]:
            return "Prompt keybindings:\n" + "\n".join(self.describe(key) for key in self.keys())
        if parts == ["actions"]:
            return "Built-in actions:\n" + "\n".join(
                f"{name}: {action.label}" for name, action in self.actions.items()
            )
        if parts[0] == "reset":
            key = parts[1] if len(parts) == 2 else None
            if key is not None:
                validate_action_key(key)
            self.apply(save_binding(key, reset=True))
            return (
                f"Restored default binding for {key}." if key else "Restored all default bindings."
            )
        key = parts[0]
        validate_action_key(key)
        if len(parts) == 1:
            return self.describe(key)
        target = parts[1].strip()
        validate_target(target)
        if target.startswith("/"):
            self.check_command(target)
        self.apply(save_binding(key, target))
        return "Saved " + self.describe(key)

    def action_label(self, default_key: str) -> str:
        """Tell help/notifications where a native action moved, or that it is unbound."""
        target = default_target(default_key)
        for key in self.keys():
            if self.overrides.get(key, default_target(key)) == target:
                return self.shortcuts.label(key)
        return "unbound"

    def complete(self, argument: str):
        parts = argument.split(maxsplit=1)
        keys = self.keys()
        if not any(char.isspace() for char in argument):
            for word in ["list", "actions", "reset", *keys]:
                if word.startswith(argument):
                    yield Completion(word, start_position=-len(argument))
            return
        head = parts[0] if parts else ""
        tail = parts[1] if len(parts) > 1 else ""
        if head == "reset":
            for key in keys:
                if key.startswith(tail):
                    yield Completion(key, start_position=-len(tail))
        elif len(head) == 1:
            for target, action in self.actions.items():
                if target.startswith(tail):
                    yield Completion(target, start_position=-len(tail), display_meta=action.label)
            yield from (
                SlashCompleter(self.registry).get_completions(
                    Document(tail or "/"), CompleteEvent()
                )
                if tail
                else (
                    Completion(command.name, display_meta=command.description)
                    for command in self.registry.commands
                )
            )
