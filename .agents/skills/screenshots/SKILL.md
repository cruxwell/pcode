---
name: screenshots
description: Create or regenerate the docs screenshots in docs/assets/screenshots from scripted pcode sessions. Use when adding a screenshot to the docs, when a UI change makes the existing ones stale, or when asked to show a feature in a specific state without driving a real model.
---

# Screenshots

Docs screenshots come from scenes in `scripts/screenshots/scenes/`, never from a
real model session. A scene runs the real pcode terminal (real tools, real
status line, real popups) on a scripted model, in a private tmux server, inside
a throwaway `acme-api` git repo at `/tmp/pcode-demo` with HOME and the XDG
directories pointed there. Your own sessions, preferences and logins are never
touched, and nothing costs anything.

```sh
make screenshots                       # every scene
make screenshots SCENES="tree jobs"    # some scenes
uv run --no-sync python scripts/screenshots/run.py --text tools   # also print each shot as text
make screenshots SCENES=review ARGS=--iterm   # tmp/screenshots/, in your iTerm2 profile's colors
make screenshot-live SCENE=review      # play it in this terminal; screenshot it yourself
```

The docs SVGs use Rich's dark terminal theme. `--iterm` takes the colors of
the iTerm2 session you run it from, asked of iTerm2 itself (AppleScript), so
Dynamic Profiles and built-in presets come out as you see them; the progress
bar's style comes from that session's profile. It sets pcode's `theme` to
match the background, and writes to `tmp/screenshots/` unless given `--out`. `--png` also
renders each shot as a 2x PNG in headless Chrome (`CHROME` overrides the
binary). `--live`
attaches your terminal to the scene's tmux pane, sized to your window. Each
shot but the last waits for `Ctrl-b Space`; `Ctrl-b d` leaves (`Ctrl-b Ctrl-b d`
inside your own tmux). Closing the window instead skips cleanup, so check
`pgrep -fl pcode-demo` afterwards.

Shots taken while a turn runs show iTerm2's tab progress bar (OSC 9;4) across
the top: the runner logs the pane's raw output (`pipe-pane`) and draws the last
report pcode sent, as `iterm.py` copies iTerm2's drawing. iTerm2's default style
otherwise, the profile's `Progress Bar Color Scheme` and `Height` with
`--iterm`. `--live` passes the reports through to your terminal's real bar,
except from inside your own tmux, which drops them.

A scene with `TABS` (`scenes/tabs.py`) is several sessions at once, one per
tab, each a short named turn and then the turn left where the shot wants it.
It is shot as a real iTerm2 window, not an SVG, so `make screenshots` skips it:
`iterm_window.py` opens each tab with `run.py --live --tab`, in the profile of
the tab you run it from (`--profile` picks another), waits until every tab has
played and shows its title, then captures the window with `screencapture`.
That needs Screen Recording permission for your terminal; without it the script
waits for you to take the shot (Cmd-Shift-4, Space, click the window). The docs
keep a light and a dark capture (`tabs-light.png`, `tabs-dark.png`), shown per
the reader's theme with `#only-light`/`#only-dark`; the README shows the same
two files, so replacing them updates both. iTerm2 starts a tab's
command with a bare PATH, so its launcher exports yours.

Each `("shot", name)` step writes `docs/assets/screenshots/<name>.svg`. You
can't look at an SVG, so check a shot one of two ways:

- `--text` prints the plain-text dump, which works on any model.
- `--png --out tmp/shots` also renders each shot as a PNG (headless Chrome)
  outside the docs tree, and `read_file` on a PNG shows you the picture when your
  model takes images. That is the only way to catch what text can't: colors,
  alignment, clipping, a glyph that rendered as a box. A 2x shot is a few hundred
  KB, well under the 5 MB `read_file` shows.

## Writing a scene

Copy the closest existing scene. A scene file has:

- `TURNS`: prompt substring → the list of model responses for that turn, one
  per model request. A response is a list of parts: a `str` (reply text,
  Markdown), `Think("...")` (readable reasoning), or `Call(tool, **args)` with
  the tool's real arguments (`shell` takes `command`, `background`,
  `purpose`; `edit_file` takes `path`, `old_text`, `new_text`). pcode sends a
  new request after each round of tool results, so a response with calls is
  followed by the next response in the list.
- `STEPS`: `("type", text)`, `("key", tmux-key)` (`Enter`, `C-g`, `Up`,
  `Escape`), `("wait", text)`, `("sleep", seconds)`, `("shot", name, title)`.
- Optionally `SIZE` (columns, rows; default 100×30), `MODEL` (shown on the
  status line; the scripted model stands in for whatever it names) and
  `PREFERENCES` (saved before launch, e.g. `{"show_thinking": "on"}`).
- The `if __name__ == "__main__": launch(TURNS, model=MODEL, ...)` footer.

The demo repo's files are `DEMO_FILES` in `run.py`. `acme/orders.py` has a
deliberate bug (a percentage discount subtracted as a flat amount) that
scenes fix.

## Traps

- Always `("wait", ...)` for text the step produces before the next step or a
  shot. A shot also waits for the screen to stop changing, but that can't
  tell a finished turn from one that hasn't started.
- Wait on rendered text, not Markdown source: backticks and `**` are gone on
  screen. Whitespace doesn't matter, since wait text may wrap.
- Tool calls really run in the demo repo, so a failing command must really
  fail (the `tools` scene's `python3 -c ... assert` does). Use commands that
  exist on a bare machine; `pytest` isn't installed in the demo repo.
- Background jobs outlive pcode by design. The runner stops any job whose
  supervisor runs under `/tmp/pcode-demo` when a scene ends; if a scene is
  interrupted, `pgrep -fl pcode-demo` finds leftovers.
- `launch()` patches `pcode.agent.resolve_model`, so the terminal must run
  in-process (`--no-host`, which `launch()` passes). A model resolved some
  other way isn't scripted. `/btw` and delegated workers (on the session's
  model) are scripted too: a worker's prompt is its `task`, so key its turn on
  a phrase from the task that the parent's prompt doesn't contain (see
  `scenes/review.py`), or the parent's key will answer the worker as well.
