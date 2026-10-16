"""The main prompt's key bindings: its shortcuts and its editing keys.

Registration order is behavior here. prompt_toolkit runs the last of several
bindings that match the same keys, so a later ``c-d`` outranks an earlier one
under the same filter, and the shortcuts list in the order they are added.
"""

from collections.abc import Callable
from dataclasses import dataclass

from prompt_toolkit.application import get_app
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.filters import Condition, vi_insert_mode, vi_mode, vi_navigation_mode
from prompt_toolkit.key_binding import KeyBindings, KeyPressEvent
from prompt_toolkit.key_binding.vi_state import InputMode
from prompt_toolkit.keys import Keys
from prompt_toolkit.layout.processors import DynamicProcessor

from pcode.clipboard import copy as copy_to_clipboard
from pcode.preferences import SETTINGS, load_preferences
from pcode.prefix_keys import Choice, PrefixKeys
from pcode.word_wrap import WordWrapProcessor, display_row_step


@dataclass(frozen=True)
class PromptCallbacks:
    """What the prompt asks of the app. Any may be absent; its key then does less."""

    on_cancel: Callable | None = None
    on_effort: Callable | None = None
    on_model: Callable | None = None
    on_tasks: Callable | None = None
    on_thinking: Callable | None = None
    on_commands: Callable | None = None
    on_send_mode: Callable | None = None
    on_interrupt_submit: Callable[[str], None] | None = None
    on_previous_session: Callable | None = None
    on_copy_response: Callable | None = None


def prompt_key_bindings(
    activity,
    transcript,
    callbacks: PromptCallbacks,
    key_prefix: str | None,
    default_buffer: Callable[[], Buffer],
) -> tuple[KeyBindings, PrefixKeys]:
    """The editor's own keys and its shortcuts.

    ``default_buffer`` is asked for at keypress time, since the session that
    owns the buffer is built from these bindings.
    """
    # The prompt and popups share one contextual keybinding overlay.
    shortcuts = PrefixKeys(
        key_prefix,
        vi_prefix=load_preferences().get("vi_key_prefix", SETTINGS["vi_key_prefix"].default),
    )
    _add_shortcuts(shortcuts, activity, transcript, callbacks)
    shortcuts.set_help(
        lambda: [
            ("Enter", "Send"),
            ("Ctrl+Enter", "Interrupt and send"),
            ("Shift+Enter", "Newline"),
            ("Ctrl+J", "Newline"),
            ("Tab", "Complete"),
            *([] if shortcuts.leader else [("Ctrl+L", "Accept completion / suggestion")]),
            ("↑ / ↓", "Move / history"),
            ("Ctrl+C", "Interrupt" if activity.busy else "Clear input"),
            *([] if activity.busy else [("Ctrl+D", "Exit (empty prompt)")]),
        ],
        title="Keybindings · Prompt",
    )
    keys = _editing_keys(activity, transcript, callbacks, default_buffer)
    return keys, shortcuts


def _add_shortcuts(shortcuts: PrefixKeys, activity, transcript, callbacks: PromptCallbacks) -> None:
    on_send_mode = callbacks.on_send_mode
    on_model = callbacks.on_model
    on_effort = callbacks.on_effort
    on_tasks = callbacks.on_tasks
    on_thinking = callbacks.on_thinking
    on_commands = callbacks.on_commands
    on_previous_session = callbacks.on_previous_session
    on_copy_response = callbacks.on_copy_response

    @shortcuts.add("s", "Cycle send mode", filter=on_send_mode is not None)
    def cycle_send_mode(event: KeyPressEvent) -> None:
        on_send_mode()
        event.app.invalidate()

    @shortcuts.add("l", "Select model", filter=on_model is not None)
    def choose_model(event: KeyPressEvent) -> None:
        # The Ctrl+L chord (not a leader's plain `l`) first takes whatever the
        # editor is offering, as Tab and → do; it picks a model otherwise.
        if event.key_sequence[-1].key == Keys.ControlL and _accept_offer(event.current_buffer):
            return
        on_model()

    effort = "Thinking effort up / down"

    @shortcuts.add("n", "Increase thinking effort", filter=on_effort is not None, group=effort)
    def increase_effort(event: KeyPressEvent) -> None:
        on_effort(1)
        event.app.invalidate()

    @shortcuts.add("p", "Decrease thinking effort", filter=on_effort is not None, group=effort)
    def decrease_effort(event: KeyPressEvent) -> None:
        on_effort(-1)
        event.app.invalidate()

    @shortcuts.add("o", lambda: "Hide task panel" if activity.tasks_shown else "Show task panel")
    def toggle_tasks(event: KeyPressEvent) -> None:
        shown = activity.toggle_tasks()
        if on_tasks is not None:
            on_tasks(shown)
        event.app.invalidate()

    @shortcuts.add("t", "Select thinking visibility")
    def choose_thinking(event: KeyPressEvent) -> None:
        def select(mode: str):
            def apply(event: KeyPressEvent) -> None:
                if on_thinking is not None:
                    on_thinking(mode)
                else:
                    activity.thinking_mode = mode

            return apply

        shortcuts.choose(
            "Thinking visibility",
            [
                Choice(
                    key,
                    label + (" (current)" if activity.thinking_mode == mode else ""),
                    select(mode),
                )
                for key, mode, label in (
                    ("o", "off", "Off"),
                    ("s", "status-line", "Status line"),
                    ("b", "scrollback", "Scrollback"),
                )
            ],
        )

    def command_output_label() -> str:
        shown = getattr(transcript, "command_scrollback", None)
        if shown is None:
            return "Toggle command output"
        return "Hide command output" if shown else "Show command output"

    @shortcuts.add("g", command_output_label, filter=on_commands is not None)
    def toggle_command_scrollback(event: KeyPressEvent) -> None:
        on_commands()
        event.app.invalidate()

    # Vim's alternate-buffer key; terminals send Ctrl+^ for Ctrl+6 as well.
    @shortcuts.add("^", "Previous session", filter=on_previous_session is not None)
    def previous_session(event: KeyPressEvent) -> None:
        on_previous_session()

    @shortcuts.add("y", "Copy draft / choose response")
    def copy_draft(event: KeyPressEvent) -> None:
        text = event.current_buffer.text
        # With nothing typed, there is no draft to copy: choose a response.
        if not text and on_copy_response is not None:
            on_copy_response()
            return
        if transcript is None:
            return
        if not text:
            transcript.flash("Nothing to copy")
            return
        copied, truncated = copy_to_clipboard(text, event.app.output)
        limit = " (truncated)" if truncated else ""
        transcript.flash(f"Copied prompt{limit}" if copied else "Could not copy prompt")


def _accept_offer(buffer: Buffer) -> bool:
    """Take the completion menu's selection (or first entry), else the ghost text.

    False when the editor offers neither.
    """
    state = buffer.complete_state
    if state is not None and state.completions:
        if state.current_completion is None:
            buffer.apply_completion(state.completions[0])
        else:
            # Moving through the menu already inserted it; just close the menu.
            buffer.complete_state = None
        return True
    suggestion = buffer.suggestion
    if suggestion is not None and suggestion.text and buffer.document.is_cursor_at_the_end:
        buffer.insert_text(suggestion.text)
        return True
    return False


def _down_would_idle() -> bool:
    # prompt_toolkit's Down moves within the text, walks the completion
    # menu, or steps forward through history; only when none of those
    # apply does it do nothing. Claim just that case for a newline, so ↓
    # never loses its existing meanings. Vi normal mode keeps `j`.
    app = get_app()
    buffer = app.current_buffer
    document = buffer.document
    if buffer.complete_state or document.cursor_position_row < document.line_count - 1:
        return False
    if buffer.working_index < len(buffer._working_lines) - 1:
        return False
    return not vi_mode() or app.vi_state.input_mode == InputMode.INSERT


def _wrap_width(app) -> int | None:
    """Columns the focused editor wraps its text at, or None if it does not word-wrap."""
    window = app.layout.current_window
    info = window.render_info
    # PromptSession nests its own input_processors in a DynamicProcessor,
    # which may hand back several merged into one.
    pending = list(getattr(window.content, "input_processors", None) or ())
    wrap = None
    while pending and wrap is None:
        processor = pending.pop()
        if isinstance(processor, WordWrapProcessor):
            wrap = processor
        elif isinstance(processor, DynamicProcessor):
            pending.append(processor.get_processor())
        else:
            pending.extend(getattr(processor, "processors", ()))
    if info is None or wrap is None:
        return None
    return info.window_width - wrap.prefix_width


def _add_wrapped_row_keys(keys: KeyBindings) -> None:
    """Vi normal-mode j/k by display row, as vim's gj/gk.

    On the first or last row they fall back to prompt_toolkit's own j/k,
    which step through history; a count that runs out of rows just stops. Operators (`dj`)
    are untouched: ``vi_navigation_mode`` is off while one is pending.
    """
    # The goal column survives a chain of moves through shorter rows, as long
    # as nothing else moved the cursor or changed the text in between.
    sticky: dict = {}

    def move(event: KeyPressEvent, delta: int) -> None:
        buffer = event.current_buffer
        width = None if buffer.complete_state else _wrap_width(event.app)
        count = event.arg
        unmoved = sticky.get("at") == (buffer.text, buffer.cursor_position)
        column = sticky.get("column") if unmoved else None
        while count and width and width > 0:
            step = display_row_step(buffer.document, width, delta, column)
            if step is None:
                break
            buffer.cursor_position, column = step
            count -= 1
        if count == event.arg:
            scroll = buffer.auto_down if delta > 0 else buffer.auto_up
            scroll(count=count, go_to_start_of_line_if_history_changes=True)
            column = None
        sticky.update(at=(buffer.text, buffer.cursor_position), column=column)

    keys.add("j", filter=vi_navigation_mode)(lambda event: move(event, 1))
    keys.add("k", filter=vi_navigation_mode)(lambda event: move(event, -1))


def _editing_keys(
    activity,
    transcript,
    callbacks: PromptCallbacks,
    default_buffer: Callable[[], Buffer],
) -> KeyBindings:
    keys = KeyBindings()

    @keys.add("enter")
    @keys.add(Keys.ControlF24)
    def submit(event: KeyPressEvent) -> None:
        buffer = event.current_buffer
        interrupt = event.key_sequence[-1].key == Keys.ControlF24
        if interrupt and not buffer.text.strip():
            return
        if not interrupt and buffer.complete_state and buffer.complete_state.current_completion:
            # First Enter accepts the selected completion; next Enter sends it.
            buffer.complete_state = None
        else:
            accept_handler = buffer.accept_handler
            if interrupt and callbacks.on_interrupt_submit is not None:
                # Keep validation, history, and buffer reset identical to Enter.
                def accept_interrupt(buffer: Buffer) -> None:
                    callbacks.on_interrupt_submit(buffer.text)

                buffer.accept_handler = accept_interrupt
            try:
                buffer.validate_and_handle()
            finally:
                buffer.accept_handler = accept_handler

    @keys.add("escape", filter=vi_mode, eager=True)
    def normal_mode(event: KeyPressEvent) -> None:
        # Match native vi Escape semantics, without waiting for Alt bindings.
        buffer = event.current_buffer
        state = event.app.vi_state
        if state.input_mode in (InputMode.INSERT, InputMode.REPLACE):
            buffer.cursor_position += buffer.document.get_cursor_left_position()
        state.input_mode = InputMode.NAVIGATION
        if buffer.selection_state:
            buffer.exit_selection()

    sequence = load_preferences().get("vi_escape_sequence", SETTINGS["vi_escape_sequence"].default)
    if sequence != "escape":
        # Native sequence buffering preserves unmatched/expired characters. Its
        # timeout is separate from terminal Escape decoding; paste bypasses it.
        keys.add(
            *sequence,
            filter=vi_insert_mode & Condition(lambda: not get_app().quoted_insert),
            eager=True,
        )(normal_mode)

    if load_preferences().get("vi_wrapped_rows", SETTINGS["vi_wrapped_rows"].default) == "on":
        _add_wrapped_row_keys(keys)

    @keys.add("c-j")
    @keys.add("escape", "enter", filter=~vi_mode)
    def newline(event: KeyPressEvent) -> None:
        event.current_buffer.insert_text("\n")

    @keys.add("down", filter=Condition(_down_would_idle))
    def newline_on_down(event: KeyPressEvent) -> None:
        event.current_buffer.insert_text("\n")

    @keys.add("c-d", filter=Condition(lambda: activity.busy))
    def cancel(event: KeyPressEvent) -> None:
        event.app.exit(exception=KeyboardInterrupt)

    if transcript is None:
        return keys

    on_cancel = callbacks.on_cancel

    @keys.add("c-d", filter=Condition(lambda: activity.busy))
    def interrupt_turn(event):
        on_cancel()

    @keys.add("c-c")
    def interrupt(event):
        # Never discard a draft and interrupt the turn in one keypress: clear
        # the editor first, so interrupting a busy turn needs an empty prompt.
        buffer = default_buffer()
        if activity.busy and not buffer.text:
            on_cancel()
            return
        buffer.reset()
        transcript.note(
            "Input discarded. Ctrl+C again interrupts."
            if activity.busy
            else "Input discarded. Ctrl+D on an empty prompt exits."
        )

    @keys.add("c-d", filter=Condition(lambda: not activity.busy))
    def exit_or_delete(event):
        if not event.current_buffer.text:
            event.app.exit()
        else:
            event.current_buffer.delete()

    return keys
