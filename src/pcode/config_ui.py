"""Browse and edit saved defaults without leaving the configuration popup."""

import json
from collections.abc import Callable

from prompt_toolkit.application import Application, get_app
from prompt_toolkit.data_structures import Point
from prompt_toolkit.filters import Condition, has_focus
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.key_binding.bindings.focus import focus_next, focus_previous
from prompt_toolkit.layout import DynamicContainer, HSplit, Layout, VSplit, Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.widgets import TextArea
from rich.text import Text

from pcode import config
from pcode.frame import Frame
from pcode.popup_ui import RichPane, focus_overlay, fuzzy_match, popup_container, popup_style
from pcode.preferences import (
    SETTINGS,
    USER_ONLY,
    project_preferences_path,
    read_preferences,
    valid_preferences,
)
from pcode.prefix_keys import PrefixKeys, compact_label

# Side by side from this width; stacked below it.
WIDE_COLUMNS = 100
# Values longer than this are cut in the list; the details show them whole.
VALUE_CHARS = 48


def _display(value) -> str:
    return "unset" if value is None else json.dumps(value, ensure_ascii=False)


def _clip(text: str, limit: int = VALUE_CHARS) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def name_width() -> int:
    """The list's name column, measured over every setting so filtering never shifts it."""
    return min(32, max(len(key) for key in config.listed_settings()))


def _field(label: str, value: str) -> str:
    return f"{label + ':':<18}{value}"


class ConfigBrowser:
    def __init__(self, *, save: Callable[[list[str]], str], input=None, output=None, style=None):
        self.save = save
        self.scope = "user"
        self.changed_only = False
        self.selected = 0
        self.editing = False
        self.choices: tuple[str, ...] = ()
        self.choice = 0
        self.message = ""
        self.search = TextArea(height=1, prompt="Filter: ", multiline=False)
        self.value = TextArea(height=1, prompt="Value: ", multiline=False)
        self.search.buffer.on_text_changed += self.filter
        self.name_width = name_width()
        self.reload()
        self.filter()
        browsing = Condition(lambda: not self.editing)
        editing_text = Condition(lambda: self.editing and not self.choices)
        rows = FormattedTextControl(
            self.fragments, get_cursor_position=lambda: Point(x=0, y=self.selected)
        )
        self.rows = Window(
            rows,
            wrap_lines=False,
            always_hide_cursor=True,
        )
        self.choice_rows = Window(
            FormattedTextControl(
                self.choice_fragments,
                focusable=True,
                get_cursor_position=lambda: Point(x=0, y=self.choice),
            ),
            height=lambda: Dimension(
                min=1, preferred=len(self.choices), max=max(1, len(self.choices))
            ),
            always_hide_cursor=True,
        )
        self.detail = RichPane(notify=lambda text: setattr(self, "message", text))
        self._detail_text = None
        keys = KeyBindings()
        self.detail.bind_scrolling(keys)
        keys.add("tab")(focus_next)
        keys.add("s-tab")(focus_previous)
        selecting = ~editing_text & ~has_focus(self.detail.window)
        shortcuts = self.shortcuts = PrefixKeys()
        shortcuts.set_help(
            lambda: [
                ("Type", "Filter settings, or edit a value"),
                ("↑/↓ / Ctrl+P/N", "Select setting or enum value"),
                ("PgUp/PgDn / Ctrl+U/D", "Page / half page settings or details"),
                ("Tab/Shift+Tab", "Switch between settings/editor and details"),
                ("Enter", "Edit setting / save value"),
                ("Esc / Ctrl+C / Ctrl+L", "Cancel edit / close browser"),
            ]
        )

        @keys.add("up", eager=True, filter=selecting)
        @keys.add("c-p", eager=True, filter=selecting)
        def previous(event):
            self.move(-1)

        @keys.add("down", eager=True, filter=selecting)
        @keys.add("c-n", eager=True, filter=selecting)
        def next_row(event):
            self.move(1)

        for key, direction, half in (
            ("pageup", -1, False),
            ("pagedown", 1, False),
            ("c-u", -1, True),
            ("c-d", 1, True),
        ):

            def page(event, direction=direction, half=half):
                info = self.rows.render_info
                size = max(1, info.window_height - 1) if info else 10
                self.move(direction * (max(1, (size + 1) // 2) if half else size))

            keys.add(key, eager=True, filter=browsing & selecting)(page)

        @keys.add("enter", eager=True)
        def accept(event):
            if self.editing:
                value = self.choices[self.choice] if self.choices else self.value.text
                self.apply("set", value)
            elif self.matches:
                self.begin_edit()

        @keys.add("escape", eager=True)
        @keys.add("c-c")
        @keys.add("c-l")
        def cancel(event):
            if self.editing:
                self.end_edit()
                self.message = "Edit cancelled; nothing saved."
            else:
                event.app.exit()

        @shortcuts.add(
            "t",
            "Toggle user/project scope",
            filter=browsing & Condition(lambda: self.project_path is not None),
        )
        def toggle_scope(event):
            self.scope = "project" if self.scope == "user" else "user"
            self.message = ""
            self.filter()

        @shortcuts.add("o", "Toggle overridden settings only", filter=browsing)
        def toggle_changed(event):
            self.changed_only = not self.changed_only
            self.message = ""
            self.filter()

        @shortcuts.add(
            "r",
            "Remove selected scope override",
            filter=browsing & Condition(lambda: bool(self.matches)),
        )
        def reset(event):
            self.apply("unset")

        def line(text, style=""):
            return Window(FormattedTextControl(text), height=1, wrap_lines=False, style=style)

        def header():
            show = "Overrides only" if self.changed_only else "All settings"
            parts = [
                ("bold", "Saved configuration"),
                ("", "  ·  Scope: "),
                ("bold", self.scope),
                ("", "  ·  Show: "),
                ("bold", show),
            ]
            # The note is the first thing to go in a narrow terminal, never Show.
            note = "  ·  no project workspace"
            room = get_app().output.get_size().columns
            if self.project_path is None and sum(len(t) for _, t in parts) + len(note) <= room:
                parts.append(("dim", note))
            return parts

        def keys_hint():
            def key(letter):
                return compact_label(shortcuts.label(letter))

            tab = "Tab Back" if get_app().layout.has_focus(self.detail.window) else "Tab Details"
            if self.editing:
                parts = ["Enter Save", "Esc Cancel", tab]
            else:
                parts = ["Enter Edit", "Esc Close"]
                if self.project_path is not None:
                    parts.append(f"{key('t')} Scope")
                parts += [f"{key('o')} Overrides", f"{key('r')} Reset", tab]
            # Drop the least important hints rather than cutting off the help key.
            room = get_app().output.get_size().columns
            while parts and len(" · ".join([*parts, shortcuts.summary()])) > room:
                parts.pop()
            return " · ".join([*parts, shortcuts.summary()])

        def list_title():
            total = sum(
                self.scope == "user" or key not in USER_ONLY for key in config.listed_settings()
            )
            return f"Settings {len(self.matches)}/{total}"

        def detail_title():
            if not self.editing:
                return "Details"
            return f"Edit {self.matches[self.selected]} · {self.scope}"

        # Edit controls open at the top of the details, so the list beside or
        # above keeps its place and the outer layout never moves.
        self.detail.window.height = Dimension(min=1)
        text_editor = HSplit([self.value, Window(height=1), self.detail])
        choice_editor = HSplit([self.choice_rows, Window(height=1), self.detail])

        def detail_body():
            if not self.editing:
                return self.detail
            return choice_editor if self.choices else text_editor

        def stacked_detail_height() -> int:
            # Header, filter, two message rows, and keys take five. The details
            # get the larger share: the list row already shows name and value.
            body = get_app().output.get_size().rows - 5
            # Always leave the list at least one row between its borders.
            return max(3, min(16, body * 3 // 5, body - 3))

        wide = VSplit(
            [
                Frame(self.rows, title=list_title, width=Dimension(weight=3)),
                Frame(DynamicContainer(detail_body), title=detail_title, width=Dimension(weight=2)),
            ]
        )
        narrow = HSplit(
            [
                Frame(self.rows, title=list_title),
                Frame(
                    DynamicContainer(detail_body),
                    title=detail_title,
                    height=lambda: Dimension.exact(stacked_detail_height()),
                ),
            ]
        )
        filter_label = line(lambda: f"Filter: {self.search.text}", "dim")
        body = HSplit(
            [
                line(header),
                DynamicContainer(lambda: filter_label if self.editing else self.search),
                DynamicContainer(
                    lambda: wide if get_app().output.get_size().columns >= WIDE_COLUMNS else narrow
                ),
                # Two rows, wrapped: the end of a long error is often what matters.
                Window(
                    FormattedTextControl(lambda: self.message),
                    height=2,
                    wrap_lines=True,
                    style="class:hint.message",
                ),
                line(keys_hint, "dim"),
            ]
        )
        self.app = Application(
            layout=Layout(popup_container(body, shortcuts), focused_element=self.search),
            key_bindings=shortcuts.key_bindings(keys),
            full_screen=True,
            input=input,
            output=output,
            style=popup_style(style),
            before_render=self.refresh_details,
        )

    def refresh_details(self, app) -> None:
        text = self.details()
        if text != self._detail_text:
            self._detail_text = text
            rendered = Text(text)
            if self.matches:
                rendered.stylize("bold", 0, len(text.partition("\n")[0]))
            self.detail.set([rendered])

    def reload(self) -> None:
        self.project_path = project_preferences_path()
        self.user = read_preferences()
        self.project = read_preferences(self.project_path) if self.project_path is not None else {}
        self.valid_project = {
            k: v for k, v in valid_preferences(self.project).items() if k not in USER_ONLY
        }
        self.data = valid_preferences(self.user) | self.valid_project

    def filter(self, buffer=None) -> None:
        terms = self.search.text.casefold().split()

        def rank(key: str) -> int:
            # Names starting with the search first, then names containing it,
            # then description hits, then fuzzy matches.
            text = f"{key} {SETTINGS[key].description}".casefold()
            if terms and key.startswith(terms[0]) and all(t in key for t in terms):
                return 0
            if all(t in key for t in terms):
                return 1
            return 2 if all(t in text for t in terms) else 3

        matches = [
            key
            for key in config.listed_settings()
            if (self.scope == "user" or key not in USER_ONLY)
            and (
                not self.changed_only
                or key in (self.project if self.scope == "project" else self.user)
            )
            and all(
                fuzzy_match(term, f"{key} {SETTINGS[key].description}".casefold()) for term in terms
            )
        ]
        self.matches = sorted(matches, key=rank)
        self.selected = 0

    def effective(self, key: str) -> tuple[str | None, str]:
        value = config._effective(self.data, key)
        source = (
            "project" if key in self.valid_project else "user" if key in self.data else "default"
        )
        return value, source

    def fragments(self):
        if not self.matches:
            return [("class:plan", "No matching settings.")]
        result = []
        for index, key in enumerate(self.matches):
            value, source = self.effective(key)
            chosen = index == self.selected
            style = "class:selected" if chosen else ""
            # The source sits in its own column before the value, so a long
            # value can never push it out of a narrow pane. Defaults go
            # unmarked, so the eye lands on what was changed.
            marker = "" if source == "default" else source
            result += [
                (style, f"{'›' if chosen else ' '} {key:<{self.name_width}}  "),
                (f"{style} bold", f"{marker:<7}  "),
                (style, _clip(_display(value))),
            ]
            if index < len(self.matches) - 1:
                result.append(("", "\n"))
        return result

    def details(self) -> str:
        if not self.matches:
            return (
                "No setting selected.\n\nSaved effective values include user and project overrides."
            )
        key = self.matches[self.selected]
        setting = SETTINGS[key]
        scope = self.project if self.scope == "project" else self.user
        value, source = self.effective(key)
        override = _display(scope[key]) if key in scope else "none (inherited)"
        timing = (
            "immediately (layout setting)"
            if key in config.IMMEDIATE_SETTINGS
            else "saved default; may require next launch"
        )
        lines = [
            key,
            setting.description,
            "",
            _field("Saved effective", f"{_display(value)} (from {source})"),
            _field("Default", _display(setting.default)),
            _field(f"{self.scope.capitalize()} override", override),
            _field("Applies", timing),
        ]
        if setting.choices and (setting.positive_integer or setting.whole_number):
            lines.append(_field("Suggestions", ", ".join(setting.choices)))
        elif setting.choices and not self.editing:
            lines.append(_field("Choices", ", ".join(setting.choices)))
        if self.scope == "user" and source == "project":
            lines += ["", "Project override takes precedence over edits to the user value."]
        return "\n".join(lines)

    def choice_fragments(self):
        result = []
        for index, value in enumerate(self.choices):
            if index:
                result.append(("", "\n"))
            result.append(
                (
                    "class:selected" if index == self.choice else "",
                    f"{'›' if index == self.choice else ' '} {value}",
                )
            )
        return result

    def move(self, amount: int) -> None:
        if self.editing:
            self.choice = max(0, min(len(self.choices) - 1, self.choice + amount))
        else:
            self.selected = max(0, min(len(self.matches) - 1, self.selected + amount))

    def begin_edit(self) -> None:
        key = self.matches[self.selected]
        setting = SETTINGS[key]
        scope = self.project if self.scope == "project" else self.user
        current = scope.get(key, self.effective(key)[0])
        self.editing = True
        self.message = "Enter saves; Escape cancels."
        self.choices = (
            setting.choices if not (setting.positive_integer or setting.whole_number) else ()
        )
        self.choice = self.choices.index(current) if current in self.choices else 0
        self.value.text = current if isinstance(current, str) else ""
        self.value.buffer.cursor_position = len(self.value.text)
        focus_overlay(self.app, self.choice_rows if self.choices else self.value)

    def end_edit(self) -> None:
        self.editing = False
        self.choices = ()
        focus_overlay(self.app, self.search)

    def apply(self, action: str, value: str | None = None) -> None:
        key = self.matches[self.selected]
        args = ["project"] if self.scope == "project" else []
        args.extend([action, key])
        try:
            if action == "set":
                assert value is not None
                SETTINGS[key].validate(key, value)
                args.append(value)
            self.message = self.save(args)
            self.reload()
        except (ValueError, OSError) as error:
            self.message = str(error)
            return
        if self.editing:
            self.end_edit()
        # Removing an override can remove this row from the filtered list.
        # Keep the same setting selected when it still belongs in the results.
        self.filter()
        if key in self.matches:
            self.selected = self.matches.index(key)

    async def run(self):
        return await self.app.run_async()
