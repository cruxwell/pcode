"""One registry drives dispatch, help, and slash completion."""

import asyncio
from collections.abc import AsyncGenerator, Callable, Iterable, Sequence
from dataclasses import dataclass

from prompt_toolkit.completion import CompleteEvent, Completer, Completion
from prompt_toolkit.document import Document


@dataclass(frozen=True)
class Command:
    name: str
    description: str
    handler: Callable[[str], None]
    arguments: tuple[str, ...] = ()
    aliases: tuple[str, ...] = ()
    free_arguments: bool = False
    argument_provider: Callable[[], tuple[str, ...]] | None = None
    # /help groups commands under this heading, in first-seen order.
    group: str = "Other"
    # Shown beside each argument in the completion menu, like the command's own
    # description; arguments without an entry complete bare.
    argument_descriptions: dict[str, str] | None = None
    # Completes free-form arguments the fixed list cannot describe. Given the
    # argument text before the cursor, it yields completions for its end.
    argument_completer: Callable[[str], Iterable[Completion]] | None = None


class CommandRegistry:
    def __init__(self) -> None:
        self.commands: list[Command] = []
        self._lookup: dict[str, Command] = {}

    def register(self, command: Command) -> None:
        names = (command.name, *command.aliases)
        if len(set(names)) != len(names) or any(name in self._lookup for name in names):
            raise ValueError(f"Duplicate command: {command.name}")
        self.commands.append(command)
        self._lookup.update((name, command) for name in names)

    def replace(self, command: Command) -> None:
        """Swap in `command` for the one registered under its name, keeping its place."""
        current = self._lookup.get(command.name)
        if current is None:
            self.register(command)
            return
        self.commands[self.commands.index(current)] = command
        for alias in (current.name, *current.aliases):
            self._lookup.pop(alias, None)
        self._lookup.update((name, command) for name in (command.name, *command.aliases))

    def unregister(self, name: str) -> None:
        """Remove a command and its aliases; unknown names are ignored."""
        command = self._lookup.get(name)
        if command is None:
            return
        self.commands.remove(command)
        for alias in (command.name, *command.aliases):
            self._lookup.pop(alias, None)

    def find(self, name: str) -> Command | None:
        return self._lookup.get(name)

    def grouped(self) -> list[tuple[str, list[Command]]]:
        """Commands by group, groups and members both in registration order."""
        groups: dict[str, list[Command]] = {}
        for command in self.commands:
            groups.setdefault(command.group, []).append(command)
        return list(groups.items())

    def resolve(self, text: str) -> tuple[Command, str] | None:
        """Validate a command's declared arguments without executing its handler."""
        parts = text.strip().split(maxsplit=1)
        command = self.find(parts[0]) if parts else None
        if command is None:
            return None
        argument = parts[1].strip() if len(parts) > 1 else ""
        if argument and not command.free_arguments and argument not in command.arguments:
            usage = "|".join(command.arguments)
            raise ValueError(f"Usage: {command.name}" + (f" [{usage}]" if usage else ""))
        return command, argument

    def dispatch(self, text: str) -> bool:
        resolved = self.resolve(text)
        if resolved is None:
            return False
        command, argument = resolved
        command.handler(argument)
        return True


class SlashCompleter(Completer):
    """Complete slash commands and their arguments.

    `extra` lists (name, description) pairs that complete ahead of the
    registry's and hide its commands of the same name, e.g. the /btw thread's
    own commands while one is shown.
    """

    def __init__(
        self,
        registry: CommandRegistry,
        extra: Callable[[], Sequence[tuple[str, str]]] = tuple,
    ) -> None:
        self.registry = registry
        self.extra = extra

    async def get_completions_async(
        self, document: Document, complete_event: CompleteEvent
    ) -> AsyncGenerator[Completion, None]:
        # Argument completers can touch the filesystem (a path on a slow mount);
        # a thread keeps a stalled read from freezing the prompt on every key.
        completions = await asyncio.to_thread(
            lambda: list(self.get_completions(document, complete_event))
        )
        for completion in completions:
            yield completion

    def get_completions(self, document: Document, complete_event: CompleteEvent):
        text = document.text_before_cursor
        if "\n" in document.text or document.text_after_cursor:
            return
        if text.startswith(("$", "+")) and not any(char.isspace() for char in text):
            # A prompt's leading `$MODEL` or `+EFFORT` picks its model the way
            # /btw's does, so it completes from the same catalog.
            btw = self.registry.find("/btw")
            if btw is not None and btw.argument_completer is not None:
                yield from btw.argument_completer(text)
            return
        if not text.startswith("/"):
            return
        if not any(char.isspace() for char in text):
            extra = self.extra()
            for name, description in extra:
                if name.startswith(text):
                    yield Completion(name, start_position=-len(text), display_meta=description)
            shadowed = {name for name, _ in extra}
            for command in self.registry.commands:
                if command.name in shadowed:
                    continue
                if any(name.startswith(text) for name in (command.name, *command.aliases)):
                    yield Completion(
                        command.name,
                        start_position=-len(text),
                        display_meta=command.description,
                    )
            return
        name, prefix = text.split(maxsplit=1) if len(text.split()) > 1 else (text.strip(), "")
        command = self.registry.find(name)
        if command and command.argument_completer:
            yield from command.argument_completer(prefix)
        elif command:
            arguments = (
                command.argument_provider() if command.argument_provider else command.arguments
            )
            described = command.argument_descriptions or {}
            for argument in arguments:
                if argument.startswith(prefix):
                    yield Completion(
                        argument,
                        start_position=-len(prefix),
                        display_meta=described.get(argument, ""),
                    )
