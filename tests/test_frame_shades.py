from pcode import theme
from pcode.ui import frame_shades


def test_background_report_is_kept_as_hex(monkeypatch):
    # Restored afterwards, so no other test sees this report.
    monkeypatch.setattr(theme, "_background", None)
    assert theme.background_theme(b"\x1b]11;rgb:2e2e/2a2a/2929\x1b\\") == "dark"
    assert theme.terminal_background() == "#2e2a29"


def test_shades_step_away_from_the_background():
    dark = frame_shades("#000000", "dark")
    assert dark.body == "#0f0f0f" and dark.header == "#1c1c1c"
    light = frame_shades("#ffffff", "light")
    assert light.body < "#ffffff" and light.header < light.body
    styles = dark.styles()
    # Border glyphs vanish into their own surface rather than drawing a line.
    assert styles["shaded frame.border"] == f"nodim fg:{dark.body}"
    assert styles["shaded frame.header frame.border"] == f"nodim fg:{dark.header}"
