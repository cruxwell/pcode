"""Temporary conversation tree chooser; never executes a model request."""

from prompt_toolkit.application import Application, get_app
from prompt_toolkit.document import Document
from prompt_toolkit.filters import Always, Condition, has_focus
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.key_binding.bindings.focus import focus_next, focus_previous
from prompt_toolkit.layout import (
    ConditionalContainer,
    DynamicContainer,
    Float,
    FloatContainer,
    HSplit,
    Layout,
    VSplit,
)
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.widgets import Label, TextArea
from rich.markdown import Markdown
from rich.padding import Padding
from rich.text import Text
from rich.theme import Theme

from pcode.clipboard import copy as copy_to_clipboard
from pcode.conversation_tree import ConversationTree, TurnNode
from pcode.copy_ui import Snippet, SnippetPicker, snippets
from pcode.frame import Frame
from pcode.popup_ui import (
    RichPane,
    bind_list_paging,
    focus_overlay,
    list_pane_height,
    popup_container,
    popup_mouse,
    popup_style,
)
from pcode.prefix_keys import PrefixKeys
from pcode.session_ui import literal
from pcode.task_prompt import TaskPrompt
from pcode.text_selection import copy_drags

Selection = tuple[str | None, bool]


class TreeBrowser:
    """Full-screen chooser over the conversation tree, laid out like ``/resume``.

    The tree lists every user and assistant row; the Conversation pane shows
    the branch through the selected row, root to leaf, and scrolls so the
    selected prompt or response is at the top, with what led there above and
    what followed below.
    """

    def __init__(
        self,
        tree: ConversationTree,
        *,
        navigable: bool = True,
        rich_theme: Theme | None = None,
        code_theme: str = "ansi_dark",
        color_system: str | None = "truecolor",
        key_prefix: str | None = None,
        **app_options,
    ) -> None:
        self.tree = tree
        self.navigable = navigable
        self.code_theme = code_theme
        self.rows = tree.rows()
        self.selected: Selection = (tree.active, False)
        self.notice = ""
        self._branch: tuple[str, ...] | None = None
        self._anchors: dict[Selection, int] = {}
        self._refreshing = False
        # The quote/code picker over the tree, while one is open.
        self.picker: SnippetPicker | None = None
        self.list = TextArea(read_only=True, wrap_lines=False, scrollbar=True)
        copy_drags(self.list, lambda text: setattr(self, "notice", text))
        self.list.window.cursorline = Always()
        self.detail = RichPane(
            theme=rich_theme,
            color_system=color_system,
            notify=lambda text: setattr(self, "notice", text),
        )
        # The pane always reports its top row as the cursor position, so this
        # marks whichever line the Tree selection scrolled to (or wherever the
        # Conversation pane was scrolled to by hand), the same "cursor-line"
        # highlight the Tree list uses for its own selection.
        self.detail.window.cursorline = Always()
        self.list.buffer.on_cursor_position_changed += lambda _: self.select()
        keys = KeyBindings()
        self.detail.bind_scrolling(keys)

        @keys.add("escape", eager=True)
        @keys.add("c-c")
        def cancel(event):
            event.app.exit(result=None)

        bind_list_paging(keys, self.list, has_focus(self.list))

        @keys.add("enter")
        def accept(event):
            # A running turn will overwrite the history a checkout installs, so
            # the picker reads instead of navigating until the turn ends.
            if self.navigable:
                event.app.exit(result=self.selected)

        keys.add("tab")(focus_next)
        keys.add("s-tab")(focus_previous)

        self.prefix_keys = shortcuts = PrefixKeys(key_prefix)
        shortcuts.set_help(
            lambda: (
                [
                    ("↑/↓", "Select snippet"),
                    ("PgUp/PgDn", "Page"),
                    ("Ctrl+U/D", "Half page"),
                    ("Enter", "Copy selected snippet"),
                    ("Esc/Ctrl+C", "Cancel"),
                ]
                if self.picker is not None
                else [
                    ("↑/↓", "Select turn / scroll conversation"),
                    ("PgUp/PgDn", "Page"),
                    ("Ctrl+U/D", "Half page"),
                    *([("Enter", "Navigate to selected turn")] if navigable else []),
                    ("Tab/Shift+Tab", "Change focus"),
                    ("Esc/Ctrl+C", "Cancel" if navigable else "Close"),
                ]
            )
        )

        @shortcuts.add("y", "Copy selection", filter=Condition(lambda: self.picker is None))
        def copy_selection(event):
            self.copy(event.app.output)

        header = Label(
            lambda: (
                f"Conversation tree · {len(tree.nodes)} turns · "
                + (
                    "User: edit & fork · Assistant: continue · Start: empty context"
                    if navigable
                    else "read-only while working"
                )
                + (f" · {self.notice}" if self.notice else "")
            )
        )
        wide = VSplit(
            [
                Frame(self.list, title="Tree", width=Dimension(weight=2)),
                Frame(self.detail, title="Conversation", width=Dimension(weight=3)),
            ]
        )
        narrow = HSplit(
            [
                Frame(self.list, title="Tree", height=lambda: list_pane_height(len(self.rows))),
                Frame(self.detail, title="Conversation"),
            ]
        )
        body = DynamicContainer(
            lambda: wide if get_app().output.get_size().columns >= 100 else narrow
        )
        root_container = HSplit(
            [
                header,
                body,
                Label(shortcuts.summary),
                Label(
                    "Switching context does not undo file changes or tool effects."
                    if navigable
                    else "Forking waits for the running turn; /btw asks a question meanwhile."
                ),
            ]
        )
        picker = Frame(
            DynamicContainer(lambda: self.picker.container if self.picker else HSplit([])),
            title="Copy",
        )
        overlaid = FloatContainer(
            root_container,
            floats=[Float(ConditionalContainer(picker, Condition(lambda: bool(self.picker))))],
        )
        self.app = Application(
            layout=Layout(popup_container(overlaid, shortcuts), focused_element=self.list),
            key_bindings=shortcuts.key_bindings(keys),
            full_screen=True,
            mouse_support=popup_mouse(shortcuts),
            style=popup_style(app_options.pop("style", None)),
            **app_options,
        )
        self.refresh()

    def refresh(self) -> None:
        lines = [label for _, label in self.rows]
        index = next((i for i, (value, _) in enumerate(self.rows) if value == self.selected), 0)
        position = sum(len(line) + 1 for line in lines[:index])
        self._refreshing = True
        self.list.buffer.set_document(Document("\n".join(lines), position), bypass_readonly=True)
        self._refreshing = False
        self.select(force=True)

    def select(self, force: bool = False) -> None:
        if self._refreshing:
            return
        row = self.list.document.cursor_position_row
        value = self.rows[row][0] if row < len(self.rows) else (None, False)
        if value == self.selected and not force:
            return
        self.selected = value
        self.notice = ""
        branch = self.branch(value[0])
        if branch != self._branch:
            self._branch = branch
            self.detail.set(self.details(branch), anchor=self._anchors.get(value))
        else:
            self.detail.scroll_to(self._anchors.get(value, 0))

    def copy(self, output=None) -> None:
        """Copy the selected row's prompt or response, as the pane shows it.

        The text is the redacted `literal` form rendered above, not the raw
        record: what is on screen is what leaves the popup. A response holding
        quotes or code blocks opens a picker to copy just one of them.
        """
        identity, prompt = self.selected
        node = self.tree.nodes.get(identity) if identity is not None else None
        name = "prompt" if prompt else "response"
        text = literal(node.prompt if prompt else node.response) if node is not None else ""
        if not text:
            self.notice = f"No {name} to copy"
            return
        choices = [] if prompt or node.kind == "compaction" else snippets(text)
        if len(choices) > 1:
            self.open_picker(choices, output)
            return
        self.copy_text(name, text, output)

    def copy_text(self, name: str, text: str, output=None) -> None:
        copied, truncated = copy_to_clipboard(text, output)
        limit = " (truncated)" if truncated else ""
        self.notice = f"Copied {name}{limit}" if copied else f"Could not copy {name}"

    def open_picker(self, choices: list[Snippet], output=None) -> None:
        def close() -> None:
            self.picker = None
            self.app.layout.focus(self.list)

        def pick(snippet: Snippet) -> None:
            close()
            self.copy_text(snippet.kind, snippet.text, output)

        self.picker = SnippetPicker(choices, on_pick=pick, on_cancel=close)
        focus_overlay(self.app, self.picker.list)

    def branch(self, identity: str | None) -> tuple[str, ...]:
        """Root-to-leaf path through ``identity``, following the active branch below it."""
        path = self.tree.path(identity)
        active = set(self.tree.path(self.tree.active))
        children: dict[str | None, list[TurnNode]] = {}
        for node in self.tree.nodes.values():
            children.setdefault(node.parent, []).append(node)
        while descendants := children.get(path[-1] if path else None):
            chosen = next((n for n in descendants if n.id in active), descendants[-1])
            path.append(chosen.id)
        return tuple(path)

    def details(self, branch: tuple[str, ...]) -> list:
        """Rich renderables for the branch, recording where each row's block starts."""
        self._anchors = {(None, False): 0}
        blocks: list = [Text("Conversation start", style="dim")]
        for identity in branch:
            node = self.tree.nodes[identity]
            if node.kind != "compaction":
                blocks.append(Text(""))
                self._anchors[(identity, True)] = len(blocks)
                blocks.append(TaskPrompt(literal(node.prompt)))
                if node.kind == "aside":
                    blocks.append(Text("  from a /btw side thread", style="dim"))
            blocks.append(Text(""))
            self._anchors[(identity, False)] = len(blocks)
            if node.kind == "compaction":
                blocks.append(Text(f"  ({node.response})", style="dim"))
            elif text := literal(node.response):
                blocks.append(Padding(Markdown(text, code_theme=self.code_theme), (0, 0, 0, 2)))
                if node.status != "completed":
                    blocks.append(Text(f"  ({node.status})", style="dim"))
            else:
                blocks.append(Text(f"  ({node.status}; last safe checkpoint)", style="dim"))
        return blocks

    async def run(self) -> Selection | None:
        return await self.app.run_async()


def tree_dialog(tree, **options) -> Application:
    return TreeBrowser(tree, **options).app
