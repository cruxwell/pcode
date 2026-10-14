"""Play screenshot scenes in tmux and save each shot as an SVG.

    uv run --no-sync python scripts/screenshots/run.py            # every scene
    uv run --no-sync python scripts/screenshots/run.py tree jobs  # some scenes

Each scene runs in its own tmux server, with HOME and the XDG directories
pointed at /tmp/pcode-demo, inside a small throwaway git repo there. Nothing
touches your real sessions, preferences or logins. See scene.py for the scene
format.
"""

import argparse
import contextlib
import fcntl
import importlib.util
import io
import json
import math
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path

from iterm import Look, profile_look, progress_bar_svg
from rich.console import Console
from rich.text import Text

HERE = Path(__file__).resolve().parent
SCENES = HERE / "scenes"
OUT = HERE.parents[1] / "docs" / "assets" / "screenshots"
ITERM_OUT = HERE.parents[1] / "tmp" / "screenshots"
DEMO_ROOT = Path("/tmp/pcode-demo")
TIMEOUT = 30
UNTITLED = "pcode"
# What changes on a settled screen: spinner frames and running calls' clocks.
TICKING = re.compile(r"[⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏◜◠◝◞◡◟]|\d+(\.\d+)?s\b")

# status off: the pane gets the whole window, so SIZE is the screenshot size.
TMUX_CONF = """\
set -g default-terminal "tmux-256color"
set -ga terminal-overrides ",*:Tc"
set -g status off
set -g history-limit 5000
# --live: pcode's progress reports (OSC 9;4) reach your terminal's bar.
set -g allow-passthrough on
# pcode titles the pane with the session's name (OSC 0); --live hands that
# title on to your terminal's tab.
set -g set-titles on
set -g set-titles-string "#{pane_title}"
# --live: a shot waits for this before the scene moves on.
bind Space wait-for -S next-shot
"""

DEMO_FILES = {
    "README.md": "# acme-api\n\nA small orders service.\n",
    "pyproject.toml": '[project]\nname = "acme-api"\nversion = "0.3.0"\n',
    "acme/__init__.py": "",
    "acme/orders.py": (
        "from dataclasses import dataclass\n\n\n"
        "@dataclass\n"
        "class Order:\n"
        "    id: int\n"
        "    items: list[float]\n"
        "    discount: float = 0.0\n\n\n"
        "def total(order: Order) -> float:\n"
        "    subtotal = sum(order.items)\n"
        "    return subtotal - order.discount\n"
    ),
    "tests/test_orders.py": (
        "from acme.orders import Order, total\n\n\n"
        "def test_total_applies_discount():\n"
        "    assert total(Order(1, [10.0, 30.0], discount=25)) == 30.0\n"
    ),
}


def demo_repo(path: Path) -> None:
    for name, content in DEMO_FILES.items():
        file = path / name
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text(content)
    git = ["git", "-C", str(path), "-c", "user.name=Demo", "-c", "user.email=demo@example.com"]
    subprocess.run([*git, "init", "-q", "-b", "main"], check=True)
    subprocess.run([*git, "add", "."], check=True)
    subprocess.run([*git, "commit", "-q", "-m", "Initial commit"], check=True)


def load(name: str):
    spec = importlib.util.spec_from_file_location(f"scene_{name}", SCENES / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    if str(HERE) not in sys.path:
        sys.path.insert(0, str(HERE))
    spec.loader.exec_module(module)
    return module


def svg(ansi: str, width: int, title: str, look: Look, progress: tuple | None) -> str:
    console = Console(
        record=True, width=width, file=io.StringIO(), force_terminal=True, color_system="truecolor"
    )
    # Decoded whole, then split: capture-pane sets a style once and lets it run
    # on into the next line, as a wrapped paragraph does on a real terminal.
    for line in Text.from_ansi(ansi.rstrip("\n")).split("\n", allow_blank=True):
        console.print(line, no_wrap=True, overflow="crop")
    # Rich's default SVG theme is a dark terminal.
    image = console.export_svg(title=title, **({"theme": look.theme} if look.theme else {}))
    if progress:
        # Along the top of the session, under the title bar, as iTerm2 draws it.
        # Rich puts the terminal at (9, 41) inside a window 1px in from the edge.
        start = image.index('<g transform="translate(9, 41)"')
        frame = float(re.search(r'viewBox="0 0 ([\d.]+)', image)[1])
        bar = progress_bar_svg(*progress, look, 1, 41, frame - 2)
        image = image[:start] + bar + image[start:]
    return image


# OSC 9;4;state[;value], raw or inside tmux's passthrough wrapper.
PROGRESS = re.compile(rb"\x1b\]9;4;(\d+)(?:;(\d+))?")


def last_progress(log: Path) -> tuple[int, int | None] | None:
    """The bar pcode's reports leave up, from the pane's output log: a state and
    the percentage drawn, folded the way iTerm2 folds them.
    """
    shown = None
    for state, value in PROGRESS.findall(log.read_bytes()) if log.exists() else []:
        state, value = int(state), int(value) if value else None
        if value is not None and not 0 <= value <= 100:
            continue  # iTerm2 ignores these
        if state == 2:
            value = 100 if value is None else value
        elif state == 4 and value is None:
            # Paused keeps the percentage already showing, or shows a little.
            value = shown[1] if shown and shown[1] is not None else 10
        shown = (state, value)
    return shown


class Pane:
    def __init__(self, env: dict, root: Path, background: str | None = None):
        self.server = "pcode-shots-" + uuid.uuid4().hex[:8]
        conf = root / "tmux.conf"
        # A detached server has no terminal to ask, so it answers pcode's
        # background query (OSC 11) only from a pane style that names one.
        style = f"set -g window-style bg={background}\n" if background else ""
        conf.write_text(TMUX_CONF + style)
        self.base = ["tmux", "-L", self.server, "-f", str(conf)]
        self.env = env
        # Everything pcode writes, escape sequences tmux keeps to itself included.
        self.log = root / "pane.log"

    def __call__(self, *args: str) -> str:
        return subprocess.check_output([*self.base, *args], text=True, env=self.env)

    def screen(self, *, colors: bool = False) -> str:
        return self("capture-pane", "-p", *(["-e"] if colors else []), "-t", "shot:0.0")

    def title(self) -> str:
        return self("display-message", "-p", "-t", "shot:0.0", "#{pane_title}").strip()

    def wait_title(self, text: str) -> None:
        deadline = time.monotonic() + TIMEOUT
        while text not in (title := self.title()):
            if time.monotonic() > deadline:
                raise TimeoutError(f"title {text!r} never appeared; it is {title!r}")
            time.sleep(0.1)

    def wait(self, text: str) -> None:
        """Wait for `text`, which may wrap: any whitespace run matches any other."""
        deadline = time.monotonic() + TIMEOUT
        wanted = " ".join(text.split())
        while wanted not in " ".join((screen := self.screen()).split()):
            if time.monotonic() > deadline:
                raise TimeoutError(f"{text!r} never appeared:\n{screen}")
            time.sleep(0.1)

    def still(self, quiet: float = 0.6) -> None:
        """Wait until the screen stops changing, ignoring spinners and clocks."""
        deadline = time.monotonic() + TIMEOUT
        previous, since = None, time.monotonic()
        while time.monotonic() < deadline:
            screen = TICKING.sub("", self.screen())
            if screen != previous:
                previous, since = screen, time.monotonic()
            elif time.monotonic() - since >= quiet:
                return
            time.sleep(0.1)

    def kill(self) -> None:
        subprocess.run([*self.base, "kill-server"], capture_output=True, env=self.env)


CHROMES = (
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
)


def png(image: Path) -> Path:
    """Render an SVG shot to a PNG beside it, at 2x, in headless Chrome.

    A PNG looks the same everywhere: GitHub shows an SVG through <img>, which
    loads no fonts, so the README's shot would fall back to any monospace.
    Inlined in a page, the SVG's own Fira Code @font-face applies.
    """
    chrome = os.environ.get("CHROME") or next((c for c in CHROMES if Path(c).exists()), None)
    if chrome is None:
        raise SystemExit("--png needs Chrome or Chromium; set CHROME to its binary")
    source = image.read_text()
    view = re.search(r'viewBox="0 0 ([\d.]+) ([\d.]+)"', source)
    width, height = (math.ceil(float(n)) for n in view.groups())
    out = image.with_suffix(".png")
    with tempfile.TemporaryDirectory() as tmp:
        page = Path(tmp) / "page.html"
        page.write_text(
            f'<html><body style="margin:0;background:transparent">{source}</body></html>'
        )
        subprocess.run(
            [chrome, "--headless=new", "--disable-gpu", "--hide-scrollbars",
             "--force-device-scale-factor=2", "--default-background-color=00000000",
             f"--window-size={width},{height}", f"--screenshot={out}", page.as_uri()],
            check=True, capture_output=True,
        )  # fmt: skip
    return out


def lock_demo_root():
    """Hold DEMO_ROOT for this process, or exit: runs at once delete each other's
    demo repos and pane logs, and a shot then comes out wrong without an error.
    """
    lock = open(f"{DEMO_ROOT}.lock", "w")  # noqa: SIM115 - held until exit
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        sys.exit(f"another screenshot run is using {DEMO_ROOT}; try again when it ends")
    return lock


def stop_jobs(root: Path) -> None:
    """Stop background jobs a scene started; they outlive the terminal by design.

    Each job's supervisor leads its own process group and names its job
    directory, under `root`, on its command line. A stop is SIGTERM to the group.
    """
    # The slash keeps /tmp/pcode-demo/a from matching /tmp/pcode-demo/ab.
    found = subprocess.run(["pgrep", "-f", f"{root}/"], capture_output=True, text=True)
    for pid in map(int, found.stdout.split()):
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(pid, signal.SIGTERM)


def play_steps(pane: Pane, steps, out: Path, width: int, *, text: bool, look: Look, live: bool):
    """Play a scene's steps; return the SVGs written (none when `live`)."""
    saved = []
    shots = sum(step[0] == "shot" for step in steps)
    for step in steps:
        kind, *args = step
        if kind == "type":
            pane("send-keys", "-t", "shot:0.0", "-l", args[0])
        elif kind == "key":
            pane("send-keys", "-t", "shot:0.0", *args)
        elif kind == "wait":
            pane.wait(args[0])
        elif kind == "title":
            pane.wait_title(args[0])
        elif kind == "sleep":
            time.sleep(args[0])
        elif kind == "shot":
            pane.still()
            if live:
                # You are watching the pane: the shot is yours to take, and the
                # scene holds still until you say so (the last one never moves on).
                shots -= 1
                if shots:
                    pane("wait-for", "next-shot")
                continue
            path = out / f"{args[0]}.svg"
            # The window title a terminal would show: the session's name once
            # pcode has given one, else the shot's own.
            title = pane.title()
            if title == UNTITLED:
                title = args[1] if len(args) > 1 else UNTITLED
            # Reports change nothing on screen and are sampled once a second
            # (TabProgress), so the matching one may still be on its way.
            time.sleep(1.2)
            progress = last_progress(pane.log)
            path.write_text(svg(pane.screen(colors=True), width, title, look, progress))
            if text:
                print(f"--- {path.name}\n{pane.screen().rstrip()}")
            saved.append(path)
        else:
            raise ValueError(f"unknown step {step!r}")
    return saved


def setup(iterm: bool) -> tuple[Look, dict]:
    """How shots are drawn, and the preferences every scene's pcode starts with.

    `iterm` takes the current iTerm2 profile's colors, with pcode's palette to match.
    """
    look = Look()
    # Always sent, so the shot can draw the bar whatever terminal runs this.
    preferences = {
        "terminal_progress": "on",
        # A still of `arc` catches a broken circle; every braille frame reads whole.
        "spinner": "dots",
    }
    if iterm:
        look = profile_look()
        preferences["theme"] = look.palette
    return look, preferences


@contextlib.contextmanager
def session(
    command: list[str],
    root: Path,
    size: tuple[int, int],
    preferences: dict,
    background: str | None = None,
):
    """Run `command` (a scene) in a fresh tmux server, inside a new demo repo
    under `root`; yield its `Pane` once pcode's prompt is up.
    """
    width, height = size
    # A fixed, neutral path: pcode's banner prints the workspace in full, and a
    # default temp directory would put your username in the docs.
    # Resolved (macOS /tmp is a symlink), so pcode shows the repo as ~/acme-api.
    root = root.resolve()
    shutil.rmtree(root, ignore_errors=True)
    repo = root / "acme-api"
    repo.mkdir(parents=True)
    demo_repo(repo)
    try:
        env = {
            **os.environ,
            "HOME": str(root),
            "XDG_CONFIG_HOME": str(root / ".config"),
            "XDG_STATE_HOME": str(root / ".local/state"),
            "XDG_DATA_HOME": str(root / ".local/share"),
            "XDG_CACHE_HOME": str(root / ".cache"),
            "COLORTERM": "truecolor",
            "PYTHONPATH": os.pathsep.join(
                p for p in (str(HERE), os.environ.get("PYTHONPATH", "")) if p
            ),
            # Merged over the scene's PREFERENCES by `scene.launch()`.
            "SCREENSHOT_PREFERENCES": json.dumps(preferences),
        }
        env.pop("TMUX", None)
        env.pop("PROMPT_TOOLKIT_NO_CPR", None)
        pane = Pane(env, root, background)
        try:
            pane("new-session", "-d", "-s", "shot", "-x", str(width), "-y", str(height),
                 "-c", str(repo), shlex.join(command))  # fmt: skip
            # tmux's default title is the host name, which has no place in the docs.
            pane("select-pane", "-t", "shot:0.0", "-T", UNTITLED)
            pane("pipe-pane", "-O", "-t", "shot:0.0", f"cat >> {shlex.quote(str(pane.log))}")
            pane.wait("❯")
            yield pane
        finally:
            pane.kill()
            stop_jobs(root)
    finally:
        shutil.rmtree(root, ignore_errors=True)


def play(
    name: str,
    out: Path,
    *,
    text: bool = False,
    iterm: bool = False,
    live: bool = False,
    tab: str | None = None,
    ready: Path | None = None,
) -> list[Path]:
    """Play scene `name`. `iterm` draws SVGs in the current iTerm2 profile's colors
    and sets pcode's palette to match; `live` attaches this terminal to the scene's
    pane instead of saving SVGs, for a screenshot of your real terminal.

    A scene with `TABS` plays only live, one `tab` at a time: iterm_window.py
    opens each in a real iTerm2 tab. `ready` is written, with the pane's tmux server,
    once a live scene's steps are done (see iterm_window.py).
    """
    scene = load(name)
    width, height = getattr(scene, "SIZE", (100, 30))
    if live:
        # The pane takes this window's size, so pcode never redraws for a resize.
        width, height = shutil.get_terminal_size()
    # Live in iTerm2, pcode's palette follows the profile you are looking at.
    look, preferences = setup(iterm or bool(live and os.environ.get("ITERM_PROFILE")))
    command = [sys.executable, str(SCENES / f"{name}.py")]
    tabs = getattr(scene, "TABS", None)
    if tabs is not None and not live:
        raise SystemExit(f"{name} is a real iTerm2 window: run iterm_window.py {name}")
    root, steps = DEMO_ROOT, getattr(scene, "STEPS", [])
    if tabs is not None:
        if tab not in tabs:
            raise SystemExit(f"--live {name} plays one tab: --tab {' or '.join(tabs)}")
        root, steps, command = DEMO_ROOT / tab, tabs[tab].steps, [*command, tab]
    background = look.theme.background_color.hex if look.theme else None
    with session(command, root, (width, height), preferences, background) as pane:
        if not live:
            return play_steps(pane, steps, out, width, text=text, look=look, live=False)
        failure = []

        def play_live():
            try:
                play_steps(pane, steps, out, width, text=False, look=look, live=True)
                if ready:
                    # Whole or absent: iterm_window.py reads it as soon as it exists.
                    partial = ready.with_name(ready.name + ".tmp")
                    partial.write_text(pane.server)
                    os.replace(partial, ready)
            except Exception as error:  # in the pane now, in full once you detach
                failure.append(error)
                pane("display-message", "-d", "0", f"scene failed: {error!s:.80}  (Ctrl-b d)")

        # Closing the window hangs up: exit through `session()`'s cleanup.
        signal.signal(signal.SIGHUP, lambda *_: sys.exit(1))
        threading.Thread(target=play_live, daemon=True).start()
        print(
            "Attaching. Take each screenshot once the scene settles; Ctrl-b Space moves "
            "on to the next shot, Ctrl-b d leaves (Ctrl-b Ctrl-b d inside your own tmux)."
        )
        # Named `pcode`: iTerm2's tab title appends the foreground job's argv[0],
        # which in real use is pcode (see pcode.proctitle), not tmux.
        tmux = shutil.which("tmux", path=pane.env.get("PATH")) or sys.exit("tmux not found")
        attach = ["pcode", *pane.base[1:], "attach", "-t", "shot"]
        subprocess.run(attach, executable=tmux, env=pane.env)
        if failure:
            raise failure[0]
        return []


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("scenes", nargs="*", help="scene names (default: all)")
    parser.add_argument(
        "--out", type=Path, help=f"output directory ({OUT}, or {ITERM_OUT} with --iterm)"
    )
    parser.add_argument(
        "--text", action="store_true", help="also print each shot as plain text, for review"
    )
    parser.add_argument(
        "--iterm",
        action="store_true",
        help="draw SVGs in the current iTerm2 profile's colors, with pcode's palette to match",
    )
    parser.add_argument(
        "--png", action="store_true", help="also render each shot as a 2x PNG (headless Chrome)"
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="play one scene in this terminal for a real screenshot instead of saving SVGs",
    )
    parser.add_argument("--tab", help="--live: the tab of a scene with TABS to play")
    parser.add_argument(
        "--ready", type=Path, help="--live: write the tmux server's name here once played"
    )
    args = parser.parse_args()
    # Scenes with TABS are whole iTerm2 windows, shot by iterm_window.py.
    every = sorted(p.stem for p in SCENES.glob("*.py"))
    names = args.scenes or [name for name in every if not hasattr(load(name), "TABS")]
    if args.live:
        if len(names) != 1:
            parser.error("--live plays exactly one scene")
        if args.text:
            parser.error("--live saves no shots for --text to print")
        if not sys.stdout.isatty():
            parser.error("--live needs a terminal to attach")
    elif args.tab or args.ready:
        parser.error("--tab and --ready go with --live")
    # iterm_window.py holds the lock for the tabs it starts, which pass --ready.
    lock = None if args.ready else lock_demo_root()  # noqa: F841 - held until exit
    # Your terminal's colors are for you, not for the docs.
    args.out = args.out or (ITERM_OUT if args.iterm else OUT)
    args.out.mkdir(parents=True, exist_ok=True)
    for name in names:
        options = dict(text=args.text, iterm=args.iterm, live=args.live)
        shots = play(name, args.out, **options, tab=args.tab, ready=args.ready)
        for path in shots + ([png(shot) for shot in shots] if args.png else []):
            print(path.relative_to(Path.cwd()) if path.is_relative_to(Path.cwd()) else path)


if __name__ == "__main__":
    main()
