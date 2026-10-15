"""One contextual help indicator replaces hints beside individual controls."""

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
from prompt_toolkit.formatted_text import fragment_list_to_text
from test_status_line import make_app

from pcode import preferences
from pcode.app import PreviewApp
from pcode.preferences import SETTINGS, load_preferences, save_preferences
from pcode.prefix_keys import PrefixKeys
from pcode.ui import Activity, PromptLayout

PLAN = [{"content": "Task", "status": "pending"}]


@pytest.fixture
def shipped_default(monkeypatch):
    """conftest turns hints off for every other test; restore the real default."""
    original = preferences.SETTINGS["show_hints"]
    monkeypatch.setitem(preferences.SETTINGS, "show_hints", replace(original, default="on"))


def heading(activity: Activity, prefix: str = "ctrl", columns: int = 80) -> str:
    layout = SimpleNamespace(
        activity=activity,
        shortcuts=PrefixKeys(prefix),
        size=lambda: SimpleNamespace(columns=columns),
        plan_attached=lambda: True,
        session_label=lambda: [],
        heading_lead=lambda: "┌─ ",
    )
    return fragment_list_to_text(PromptLayout.plan_heading(layout))


def test_hints_default_on(shipped_default):
    assert SETTINGS["show_hints"].default == "on"
    assert PreviewApp().activity.show_hints


@pytest.mark.parametrize("prefix, indicator", [("ctrl", "Ctrl+/ Keys"), ("ctrl+p", "Ctrl+P Keys")])
def test_footer_shows_one_help_indicator(tmp_path, monkeypatch, shipped_default, prefix, indicator):
    app, _ = make_app(tmp_path, monkeypatch)
    app.prompt_session = SimpleNamespace(shortcuts=PrefixKeys(prefix))
    text = fragment_list_to_text(app.toolbar())
    assert text.endswith(f" · steering · preview · {indicator}")
    assert text.count("Keys") == 1
    assert "(^S)" not in text
    save_preferences(show_hints="off")
    app.handle("/config get show_hints")  # Any /config re-reads the layout settings.
    text = fragment_list_to_text(app.toolbar())
    assert " · steering · preview" in text
    assert "Keys" not in text


def test_narrow_footer_cuts_help_before_model_metadata(tmp_path, monkeypatch, shipped_default):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    app, _ = make_app(tmp_path, monkeypatch, width=len(" ~ · steering · preview · Ctrl+/"))
    assert fragment_list_to_text(app.toolbar()) == " ~ · steering · preview · Ctrl+…"


def test_narrow_footer_keeps_live_queue_status_before_help(tmp_path, monkeypatch, shipped_default):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    app, _ = make_app(tmp_path, monkeypatch, width=30)
    app.prompt_session = SimpleNamespace(shortcuts=PrefixKeys("ctrl+b"))
    app.activity.busy = True
    app.activity.queued = 2
    app.activity.queued_modes = ["queue", "queue"]
    text = fragment_list_to_text(app.toolbar())
    assert text.startswith(" ~ · steering · 2 queued · ")
    assert "Keys" not in text


def test_config_applies_hints_at_once(tmp_path, monkeypatch, shipped_default):
    app, stream = make_app(tmp_path, monkeypatch)
    app.handle("/config set show_hints off")
    assert not app.activity.show_hints
    assert load_preferences()["show_hints"] == "off"
    assert "Layout settings apply immediately." in stream.getvalue()
    app.handle("/config unset show_hints")
    assert app.activity.show_hints


def test_task_heading_has_no_inline_hide_hint():
    assert heading(Activity(plan=PLAN)) == "Tasks 0/1"
    assert heading(Activity()) == "Tools"
    assert heading(Activity(plan=PLAN), prefix="ctrl+p") == "Tasks 0/1"
    assert heading(Activity(plan=PLAN, show_hints=False)) == "Tasks 0/1"


def test_narrow_pane_preserves_the_task_heading():
    # 8 columns of border chrome, then exactly "Tasks 0/1" fits.
    assert heading(Activity(plan=PLAN), columns=8 + len("Tasks 0/1")) == "Tasks 0/1"
