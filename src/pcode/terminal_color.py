"""How many colors to draw: the terminal's own word, a terminal known for 24-bit, or the user's.

Detection only ever upgrades to true color. A terminal it can't vouch for keeps
prompt_toolkit's and Rich's own choice (256 colors for most), so a terminal
that worked before draws exactly as it did.

`NO_COLOR` (https://no-color.org) and `PROMPT_TOOLKIT_COLOR_DEPTH` are the
user's word and win over everything here: pcode then forces no depth, and
prompt_toolkit honors both, Rich `NO_COLOR` alone.
"""

import os
import subprocess
import sys
from collections.abc import Mapping
from functools import lru_cache

from prompt_toolkit.output.color_depth import ColorDepth

COLOR_DEPTHS = ("auto", "truecolor", "256")

# `TERM_PROGRAM` of terminals that draw 24-bit color, whether or not they also
# export `COLORTERM`. Apple's Terminal is left out: before macOS 26 it rounds
# 24-bit colors itself, worse than prompt_toolkit's 256.
_TRUE_COLOR_PROGRAMS = frozenset(
    {"iTerm.app", "WezTerm", "vscode", "ghostty", "Hyper", "rio", "Tabby", "WarpTerminal", "zed"}
)
# `TERM` survives ssh where `TERM_PROGRAM` does not.
_TRUE_COLOR_TERMS = frozenset(
    {"xterm-kitty", "xterm-ghostty", "wezterm", "alacritty", "foot", "contour", "rio"}
)

_setting = "auto"


def configure(setting: str | None) -> None:
    """Apply the `color_depth` preference: `auto`, `truecolor` or `256`."""
    global _setting
    _setting = setting if setting in COLOR_DEPTHS else "auto"


def _user_choice(env: Mapping[str, str]) -> bool:
    """Whether the environment already decides, leaving nothing to detect."""
    return bool(env.get("NO_COLOR") or env.get("PROMPT_TOOLKIT_COLOR_DEPTH"))


def true_color(env: Mapping[str, str] | None = None) -> bool:
    """Whether the editor renders 24-bit color, by choice or by the terminal's word."""
    env = os.environ if env is None else env
    if env.get("NO_COLOR"):
        return False
    explicit = env.get("PROMPT_TOOLKIT_COLOR_DEPTH")
    if explicit:
        return explicit == ColorDepth.TRUE_COLOR.value
    if _setting != "auto":
        return _setting == "truecolor"
    return advertised_true_color(env)


def advertised_true_color(env: Mapping[str, str]) -> bool:
    """Whether the terminal says, or is known, to draw 24-bit color."""
    if env.get("COLORTERM", "").lower() in ("truecolor", "24bit"):
        return True
    if env.get("TMUX"):
        # tmux replaces TERM and TERM_PROGRAM, and the server's environment can
        # name a terminal no longer attached, so ask tmux about its client.
        return _tmux_rgb(env.get("TMUX_PANE", ""))
    term = env.get("TERM", "")
    if term.endswith("-direct") or term in _TRUE_COLOR_TERMS:
        return True
    return env.get("TERM_PROGRAM", "") in _TRUE_COLOR_PROGRAMS


@lru_cache(maxsize=4)
def _tmux_rgb(pane: str) -> bool:
    """Whether tmux passes 24-bit color to the client showing `pane` (tmux 3.2+).

    tmux rounds 24-bit colors itself for a client without the `RGB` feature,
    so this asks whether the colors arrive whole, not whether tmux accepts them.
    """
    command = ["tmux", "display-message", "-p"]
    if pane:
        command += ["-t", pane]
    try:
        result = subprocess.run(
            [*command, "#{client_termfeatures}"],
            capture_output=True,
            text=True,
            timeout=1,
            check=False,
        )
    except OSError, subprocess.SubprocessError:
        return False
    return "RGB" in result.stdout.strip().split(",")


def color_depth() -> ColorDepth | None:
    """The editor's color depth, or None to leave it to prompt_toolkit.

    prompt_toolkit only reads `NO_COLOR` and `PROMPT_TOOLKIT_COLOR_DEPTH` and
    otherwise settles on 256 colors, rounding every palette hex to the nearest
    of them: a shade a few percent off the background becomes a visible blotch.
    """
    if _user_choice(os.environ):
        return None
    if _setting == "256":
        return ColorDepth.DEPTH_8_BIT
    return ColorDepth.TRUE_COLOR if true_color() else None


def rich_color_system() -> str:
    """The transcript console's `color_system`, matching the editor's depth.

    `auto` leaves Rich to its own detection, which honors `NO_COLOR` and reads
    `COLORTERM`, so nothing changes where pcode has no better answer. Rich
    colors even a pipe once told a system, so a redirected stdout keeps `auto`.
    """
    if _user_choice(os.environ) or not sys.stdout.isatty():
        return "auto"
    if _setting == "256":
        return "256"
    return "truecolor" if true_color() else "auto"
