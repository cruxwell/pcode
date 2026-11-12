"""The private prompt_toolkit API pcode relies on still exists.

pcode reaches into prompt_toolkit internals for its scrollback handoff
(``ui.suspended_editor``), the tmux reflow renderer, layout division
(``layout_speed``) and input aliases (``input_keys``). A rename in a
prompt_toolkit release would not fail loudly there: most uses read state, so
the editor would just misrender. Upgrading the pinned version should start by
running this file, and a failure names what to port.
"""

import inspect
from asyncio import Future

import pytest
from prompt_toolkit.application import Application
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.data_structures import Point
from prompt_toolkit.input import create_pipe_input, vt100_parser
from prompt_toolkit.layout import HSplit, Layout, VSplit, Window
from prompt_toolkit.layout.screen import Char, Screen
from prompt_toolkit.output import DummyOutput
from prompt_toolkit.renderer import Renderer


@pytest.fixture
def app():
    with create_pipe_input() as pipe:
        yield Application(layout=Layout(Window()), input=pipe, output=DummyOutput())


def parameters(function) -> list[str]:
    return list(inspect.signature(function).parameters)


def test_application_state_used_by_the_scrollback_handoff(app):
    assert app._is_running is False
    assert app._running_in_terminal is False
    assert app._running_in_terminal_f is None or isinstance(app._running_in_terminal_f, Future)
    # suspended_editor shadows the method with an instance attribute and then
    # deletes it; that only restores resizing if the method lives on the class.
    assert "_on_resize" in vars(Application) and "_on_resize" not in vars(app)
    assert parameters(app._on_resize) == []
    assert parameters(app._request_absolute_cursor_position) == []
    assert "render_as_done" in parameters(app._redraw)
    assert app._merged_style is not None


def test_renderer_state_and_overridden_methods(app):
    renderer = app.renderer
    names = (
        "_min_available_height",
        "_in_alternate_screen",
        "_last_size",
        "_last_screen",
        "_style_string_has_style",
    )
    for name in names:
        assert hasattr(renderer, name), name
    assert isinstance(renderer._cursor_pos, Point)
    assert isinstance(renderer._min_available_height, int)
    # ReflowAwareRenderer overrides these and forwards the same arguments.
    assert parameters(Renderer.reset) == ["self", "_scroll", "leave_alternate_screen"]
    assert parameters(Renderer.erase) == ["self", "leave_alternate_screen"]
    assert parameters(Renderer.render)[:4] == ["self", "app", "layout", "is_done"]
    # install_reflow_renderer constructs it with these keywords.
    assert {"style", "output", "full_screen", "mouse_support", "cpr_not_supported_callback"} <= set(
        parameters(Renderer.__init__)
    )
    assert hasattr(app, "cpr_not_supported_callback")


def test_screen_cells_read_by_the_reflow_renderer():
    screen = Screen()
    screen.data_buffer[0][0] = Char("x", "class:test")
    cell = screen.data_buffer[0][0]
    assert (cell.char, cell.style, cell.width) == ("x", "class:test", 1)


def test_buffer_working_lines():
    buffer = Buffer()
    assert len(buffer._working_lines) == 1
    assert buffer.working_index == 0


def test_split_division_patched_by_layout_speed():
    assert parameters(VSplit._divide_widths) == ["self", "width"]
    assert parameters(HSplit._divide_heights) == ["self", "write_position"]
    split = HSplit([Window()])
    assert isinstance(split._all_children, list)


def test_vt100_prefix_cache_cleared_by_input_keys():
    cache = vt100_parser._IS_PREFIX_OF_LONGER_MATCH_CACHE
    cache.clear()
    assert isinstance(cache["\x1b["], bool)


def test_buffer_state_used_to_find_unsent_text(app):
    # `drafts.edited` compares the working lines against the history they
    # were copied from, once the background copy is done.
    buffer = Buffer()
    assert list(buffer._working_lines) == [""]
    assert buffer._load_history_task is None
