---
name: ui-loop
description: See and drive the Pcode macOS app (app/macos on the Ghostty fork, including the Swift workspace) while changing its UI. Use when working on the app's Swift code, popups, drawer, native composer, docked editor or any pcode rendering inside the app, to snapshot windows, send keys and clicks, check selection and focus, and catch one-frame flicker yourself instead of asking the user to look.
---

# UI loop

The Pcode app (pcode's Swift in `app/macos`, built onto the Ghostty fork
[aweis89/ghostty-smooth](https://github.com/aweis89/ghostty-smooth) at
`app/ghostty.lock` by `app/scripts/build.sh`) has a debug socket. Start
the app with `GHOSTTY_DEBUG_SOCKET=<path>` and `scripts/gdbg.py` can then read
and drive it:
- **What it gives you:** every terminal's frame, grid size, focus and text, plus
  view and layer trees, hit-testing, synthetic keys, clicks and drags,
  window snapshots and frame recordings.
- **No macOS permissions needed:** everything runs inside the app, so it works
  without Screen Recording or Accessibility. `screencapture` and `osascript`
  keystrokes fail in agent shells.
- **When it's off:** with the variable unset, the socket costs nothing.

Before reporting a UI change as done, check the result yourself: snapshot it and
look at it with `read_file`, then drive the interaction that broke.

## Build, launch, look

```sh
# From the pcode repo (any worktree). A separate bundle ID keeps the user's
# Pcode.app untouched. Add GHOSTTY_SRC=<fork checkout> to try unmerged Zig work.
APP_NAME=PcodeMytest BUNDLE_ID=com.pcode.terminal.mytest app/scripts/build.sh --out /tmp/mytest/PcodeMytest.app

# Replaces whatever app was on the socket, waits for a terminal.
G="python3 -I scripts/gdbg.py"
$G launch --app /tmp/mytest/PcodeMytest.app --command /tmp/run.sh
$G dump                      # windows, surfaces (id, overlay or pane, grid, frame, focus)
$G snap /tmp/s.png           # then read_file /tmp/s.png
```

Don't launch `builds/current`: that is the user's app, which `make app`
replaces.

`/tmp/run.sh` is whatever the first terminal should run, for example
`cd /tmp/demo && exec pcode --no-worktree`, or
`PCODE_EDITOR_ONLY=1 uv run --no-sync pcode --host` in a pcode worktree for the
old single-surface docked editor card. Workspace session panes don't run a
terminal for the session any more: they use the native composer
(`PCodeComposer*.swift`), which spawns the host headless with
`pcode --host --spawn-only` and talks to its socket. `PCODE_NATIVE_COMPOSER=0`
(or `native_composer off`) brings back the terminal editor card in new panes.
Launching brings the window to the front once. After that, nothing in the
harness takes focus from the user unless you run `activate`.

## Commands

| Goal | Command |
| --- | --- |
| Terminal text (what the user sees) | `text -s ID` (`--scope screen` for scrollback, `selection`) |
| Wait for a state instead of sleeping | `wait 'regex' -s ID --timeout 10` (`--absent` to wait for it to go) |
| Keys | `key ctrl+a backslash enter`, `key cmd+shift+]`, `type 'some text'` |
| Click / select | `click --cell COL ROW -s ID`, `drag --from-cell 2 3 --to-cell 30 3 -s ID` (replies with the selection) |
| Which view takes a click | `hit X Y` (window points from the top-left) |
| Clips, corner radii, shadows, colors | `layers` (SwiftUI clips via layers, so `views` won't show them) |
| Picture | `snap [path]`, `--crop -s ID`, `--rect X Y W H`, `--retina`, `--raw -s ID` (the terminal's whole grid, unclipped) |
| Flicker | `record --seconds 2` then read the flagged `flash` frames from its directory |

Surface IDs are the 8-character prefixes `dump` prints, and they change on every
launch. `--json` prints the raw reply.

## Traps

- **A covered window stops drawing.** Ghostty skips rendering a fully covered
  window, so only `snap` and `record` show it as it is now: they wake its
  terminals first. `dump` marks such a window `covered(not drawing)`. `text`
  reads terminal state, so it is always current.
- **Background windows lose the first click.** AppKit spends the first click on
  a background window activating it, so a real click there never reaches the
  terminal. The harness delivers clicks straight to the view under the pointer
  instead, which keeps focus with the user. Use `activate` when a test needs
  the real first-click path.
- **Snapshots are composited inside the app.** Window-server effects are
  missing: blur, vibrancy, backdrop layers and the window's own shadow. For
  those, the user has to look, or grant Screen Recording to the app you run in
  and use `screencapture -l <window number from dump>`.
- **`record` samples; it can miss frames.** It captures when a terminal
  presents a frame and on a timer (for SwiftUI animation and terminals that
  appear mid-recording), but each capture renders the whole window on the main
  thread, so presents that land during a capture are merged or lost, and the
  capturing slows the app it measures. A flagged flash is real; no flag is not
  proof of no flash. It stops at 900 frames (`hit_frame_cap` in the reply).
- **Synthetic mouse events skip AppKit tracking loops.** Terminals don't run
  one, so clicks and drags on them are faithful. A control that does (buttons,
  split dividers, scrollers, a right-click menu) may stay pressed or open; check
  with `snap`, and press `escape` or click elsewhere to clear it.
- **Keys assume a US layout.** Key codes are US ANSI; characters without a key
  are inserted as text.
- **The Ghostty build has its own traps.** A release tag on `HEAD` fails the
  build ("tagged releases must be in vX.Y.Z format"), so commit first. A
  one-off `xcodebuild -create-xcframework` failure (exit 70) passes on rerun.
  Compile errors are hidden behind zig's "exit 65". To see them, run
  `xcodebuild -target Ghostty -configuration ReleaseLocal -arch arm64 build SYMROOT=/tmp/x | grep error:`
  in `macos/`.
- **The workspace window ignores `--command`.** With workspace mode on (the
  default), the window starts each pane's host by running `$PCODE_COMMAND`,
  and `launch` passes your environment on. Run from inside a pcode session,
  that is the parent's own pcode, not your worktree's: check with
  `pgrep -fl bin/pcode`. Export `PCODE_COMMAND=<worktree>/.venv/bin/pcode`
  before `launch`.
- **A test app restores and attaches to the user's real sessions.** It reopens
  the sessions its bundle ID last had open and the hosts in the real hosts
  dir, and a native pane attaching marks them seen. Isolate every launch:
  export `XDG_STATE_HOME`, `XDG_CONFIG_HOME`, `PCODE_CONFIG_DIR`,
  `PCODE_HOST_DIR` and `PCODE_SESSION_DIR` pointing at a throwaway dir, run
  `defaults delete <your bundle id>`, and check `dump` shows only your window
  before typing.
- **Native sheets are their own windows.** `/help`, `/status`, pickers and the
  model chooser are AppKit sheets: `dump` lists them as window 0 and
  `snap -w 0` takes them; a plain `snap` shows only the main window.
- **Synthetic ⌘V doesn't paste** (not even text): test image paste with
  `/paste-image`, which takes the same save-and-insert path.
- **`make app-test` runs the last build's copy of the tests.** After editing
  `app/macos/Tests`, rebuild (or rsync them into `tmp/ghostty`) first.
- **The preview model never plans, runs jobs or answers `/btw`.** Drive the
  task strip with `PCODE_DEBUG_PLAN_FILE=<json file>`; jobs and `/btw` need a
  real model.
- **Shared worktrees collide.** Another session may be building and
  relaunching in the same Ghostty worktree. Commit only your own paths
  (`git commit <paths>`, never `-a`), and use your own `APP_NAME`, bundle ID
  and socket path (`--socket`) so your relaunches don't kill its test runs.
