# Native app plan: making Ghostty a leaf in Pcode.app

Status: steps 1, 2 and 3a done. Step 3b (native composer) is decided and
planned but not started (owner, 2026-11-27). Step 4 is undecided: do not start
it. This came out of a code survey, not a full design; check the current code
before extending any step.

## Goal

Pcode.app should be a native macOS client of pcode sessions, with the session
host as its spine and Ghostty reduced to a view that hosts a terminal where a
terminal is still wanted.

The native AppKit transcript has replaced the terminal render as the way a
session is shown, and it is nicer and richer than the terminal view. What is
left of Ghostty in the app is mostly plumbing: the app shell, a hidden terminal
behind the transcript, and terminal surfaces for the prompt editor, popups and
`/btw`. Steps 1 to 3a remove the plumbing that nobody sees. They are worth
doing whether or not Ghostty stays.

The native composer (step 3b) replaces the terminal editor in the app. Whether
the app then drops Ghostty altogether (its own container, step 4) is still an
open decision. Keeping Ghostty for the shell split is a valid end state.

The Ghostty fork (`aweis89/ghostty-smooth`) stays as the standalone terminal
for people who use pcode's terminal UI either way. pcode's terminal mode must
keep working unchanged through every step: these changes are for the Swift app
only.

## What Ghostty does in the app today

- App container. The fork owns the Xcode project, `AppDelegate`, the menu,
  config (theme, fonts, keybinds) and the debug socket that `scripts/gdbg.py`
  drives. Our Swift is copied in through the `PCodeAppHooks` seam.
- The hidden terminal behind the transcript. Each pane's surface runs the pcode
  TUI as scrollback only (`PCODE_DOCK=1`), with the native transcript mounted
  over it in `PCodeDock.swift`. It gives the pcode process a pty and carries
  the `pcode;...` APC messages.
- The composer. The whole prompt_toolkit editor, task list and status bar run
  on a second surface (the dock). Popups that are not native pick lists open as
  more terminal overlays, and `/btw` is another surface running
  `pcode.btw_tail`.
- The bottom shell split. This is the only real terminal use.

Size of the coupling when surveyed: 16 of 28 Swift files reference Ghostty
(`PCodeWorkspace.swift` alone 71 times). The fork is 137 commits and about
6,350 lines ahead of upstream, of which about 4,450 are the Zig glide renderer
that the app no longer shows anyone.

The terminal is the spine of a pane today, not a leaf:

```
pane's hidden Ghostty surface   runs the pcode TUI; owns the session's pty
   - draws a transcript nobody sees
   - carries pcode;... APCs (session id, toasts, popups, drawer, dock rows)
   - hands its editor to a second surface through the dock bridge (a spike)
native transcript               laid on top; reads the host socket or the journal
```

The session's life is tied to a surface the user never looks at, and the app
learns about its own session from escape codes in that surface's output.

## Steps, in order

### 1. Remove the terminal-render toggle (done)

The native transcript is the only session view. `PCodeNativeTranscriptMode`,
`PCodeUISurface`, `toggleLiveTranscript`, the Option-Cmd-T shortcut and button,
the debug `transcript` command and the old side-by-side
`PCodeLiveTranscriptPaneView` are gone, and panes always get
`PCODE_NATIVE_TRANSCRIPT=1`. Merged in `a1f4acef`, together with a fix for
copying text out of the native transcript.

The terminal surface behind the transcript is still there, because the pcode
process still needs a pty. The raw terminal can still be seen for a few seconds
at startup before the dock is ready, in a pane whose pcode has exited, and in
split panes. Step 3a removes that surface.

### 2. Move `/btw` to native (done)

`btw_tail` already reads the thread from the host socket, so a native drawer
can do the same and reuse the transcript block views. This removes one terminal
surface. The terminal-mode `/btw` path stays as it is. Merged in `b00c60ae` and
`ba8e2c96`.

### 3a. The app talks to the session host; the editor becomes a leaf (done)

Make the session host own every session the app shows, and have the app read
session state from the host socket. The terminal editor stays, as a client of
the host on one visible surface:

```
session host (headless)         owns the session
   - native transcript          host socket (works this way already)
   - native /btw                host socket (step 2)
   - editor                     one visible terminal surface, attached as a client
```

What this buys even with Ghostty still in the app:

- One surface per pane instead of two. The hidden pane surface and the dock's
  tty handoff go away.
- No wasted rendering. pcode stops drawing a full transcript into a terminal
  nobody sees.
- App state stops depending on the APC channel. Session id, activity and title
  come from the socket. APCs shrink to what the editor surface itself needs
  (popups, completions, toasts).
- Ghostty becomes swappable. Once it only hosts "a terminal that runs the
  editor" and "a terminal that runs a shell", replacing either is a local
  change, not a rewrite of `PCodeWorkspace.swift`.

What it does not buy: the app is still built on the fork's app shell, the Zig
toolchain and the unstable embedding API, and terminal popups (the `*_ui.py`
pickers) stay as overlays on the editor surface.

What has to be built:

- An editor-only pcode frontend: a mode where pcode attaches to a host and runs
  just the editor, task list and status bar on its own terminal, with no
  scrollback. `dock.py` is most of this already, minus the handoff. It is still
  marked as a spike and has no tests.
- A two-way Swift host client. `PCodeHostClient.swift` is read-only today
  (`observe_transcript` v1). For 3a it mostly needs state mirroring
  (`SESSION_FIELDS` in `pcode.controller`), since the terminal editor still
  does the submitting. `integrations/obsidian` is a working reference client.
- App-launched sessions always hosted. Today a new pane runs pcode in-process
  and only moves into a host when the app restarts; the transcript falls back
  to tailing the journal for those.

Main unknown: how much of the editor assumes it shares a process with the
transcript renderer. Survey that before sizing the work. The states listed
under step 1 where the raw terminal shows need a native answer here, since the
surface they fall back to is the one being removed.

#### 3a survey findings (2026-11-25)

The main unknown is mostly answered: the editor does not need the transcript
renderer in its process, but it builds the task list, tool rows, worker rows
and command/edit previews client-side from the transcript events it receives
(`present_stream_event` both prints and updates `Activity`). So an editor-only
client must keep processing view calls and send their output to a null sink.

Already there, no protocol change needed:

- Terminal host clients exist: `pcode --host` spawns a host and attaches,
  `pcode -c <id>` attaches when a host holds the session (`RemoteController`
  in `remote.py`). Both run the same `PreviewApp`, so `PCODE_DOCK` works there.
- `observe_transcript` already forwards `state` (`SESSION_FIELDS` deltas:
  busy, prompt state, turn outcome, queue) and `session_changed`; the Swift
  decoder ignores them. `hosts/<id>.json` is rewritten on every change and
  carries title, state, session id, unseen and outcome.
- Status bar, completions, drafts, history, image paste and the `/btw` thread
  already work over the host protocol. Popups, native pick, completions and
  toasts are APCs on the editor's own tty, so they keep working from whichever
  surface runs the editor.
- No Ghostty fork (Zig or seam) change is required. Several seam hooks become
  dead (`clickableHeight`, `clicksThroughOnFocus`, `allowsMouseFocus`,
  `didMouseUp`, `willKeyDown`); removing them from the fork is optional.

What has to change:

- Editor-only frontend (Python, gated on a new app-only env var such as
  `PCODE_EDITOR_ONLY=1`): a `Dock` variant on its own stdin/stdout with no
  `open_dock` handoff, pane forwarding, scroll region, CPR or glide, plus a
  null sink for `Transcript`/`TerminalOutput` writes, replay, `/redraw` and the
  full-screen popup fallback's redraw. Terminal mode never sets the variable.
- Popup, completion and toast layers are mounted per surface by the
  `surfaceOverlay` hook; on a card-sized editor surface they would be clipped.
  Mount them at pane level in `PCodeSessionPaneView` instead (Swift only).
- Native states for what the hidden terminal shows today: startup output
  (project-trust prompt, "needs a model", config errors), no session id yet,
  host stopped/reconnecting (replacing the journal-tail fallback), session
  ended. `startup_pending`/`startup_error` already exist host-side.
- Peek's scroll shield forwards to the hidden `pane.surface`
  (`PCodePeek.swift`); repoint it at the transcript.

Unverified: whether a clipped, fixed-grid editor surface sits well inside the
fork's split tree; host boot latency; what SIGUSR2 (Restart) does to a hosted
client.

Decisions for the "always hosted" piece:

- Closing a pane stops its host (owner, 2026-11-25), as closing ends the
  session today; it does not detach. The app picks the host's id up front
  (`PCODE_HOST_ID`), so a pane closed before its host reports itself still
  stops it.
- No model configured: `pcode --host` starts a host in preview mode with no
  model (`PreviewRuntime`) until `/model` activates an agent (owner, 2026-11-25),
  so every app pane runs hosted on a single editor-only surface and the app's
  two-surface dock handoff is removed. Only an explicit `--host` does this: in a terminal,
  `session_host=on` with no model still runs the canned preview in-process
  (owner).
- Startup output: until `pcode;ready` the pane's surface fills the pane
  unclipped, so the trust prompt and startup errors show at full height; a
  pcode that exits non-zero before its editor started waits for Enter
  (`dock.hold_failed_start`) instead of the pane vanishing.

#### 3a sequence

Each piece merges to `master` on its own and leaves the app working; the old
two-surface path stays the default until piece 6.

1. Swift: decode `state` and `session_changed` in `PCodeHostEventDecoder` and
   publish busy/outcome on the transcript controller. Unit tests; no consumer.
2. Swift: host-sourced pane state. Watch `hosts/<id>.json` for title, outcome
   and session id; pane activity and title prefer host data when hosted and
   fall back to OSC/APC otherwise.
3. Python: editor-only frontend behind `PCODE_EDITOR_ONLY`, with tests that
   nothing outside the editor layout reaches stdout and that tasks/tool rows
   still update.
4. Swift: move popup, completion and toast layers to pane level, still fed by
   the dock surface. No visible change.
5. Swift: app panes always hosted (`--host [-c id]`), with native
   startup/error and host-stopped cards replacing the journal fallback.
   Needs the two owner decisions above.
6. Swift: one surface. `makePane` runs the editor-only client as the pane
   surface laid out as the card; delete the dock handoff, `presizeHidden`'s
   dock branch and the spike hooks; repoint Peek; update the `ui-loop` skill.
   The Python handoff code stays for the standalone fork until that drops it.
7. Not part of 3a: removing the dead seam hooks in the fork (and
   `make app-lock`). Raise it with the owner once 3a is done; it depends on
   3b and step 4.

### 3b. Native composer (done, 2026-11-28; on by default)

Replace the prompt_toolkit editor in the app with a Swift one that talks to the
session host directly. The Python editor stays for terminal mode, untouched.

Why: the editor surface is the last thing a pane waits on. Today every pane
holds `PCodeStartupOverlay` until `pcode;ready` arrives from the editor-only
client. A native composer is focusable and editable on the first frame, for new
and resumed sessions, and only the model turn itself waits for the host. It
also removes the fixed-grid clipping and cursor-sync code in `PCodeDock.swift`
and `PCodeCompletion.swift`.

Scope decisions (owner, 2026-11-27):

- Text view: plain `NSTextView` on TextKit 2, which the app already uses
  (`PCodeTextKit2BlockView.swift`, `PCodePicker.swift`). STTextView is a code
  editor (gutters, folding, large files) and a new dependency for a box that
  holds 1 to 40 lines.
- No vi bindings at first. Native macOS and Emacs-style editing come with
  `NSTextView`.
- In scope: Enter to send, Shift+Enter and Ctrl+J for a newline, Ctrl+Enter
  (interrupt and send), Up/Down history on the first and last line, Ctrl+C
  (interrupt when busy, clear when idle), the send-mode toggle (steer/queue),
  drafts, image paste, `@` file references, `/` command completion, the status
  strip, and the task list.
- Out of scope: the worker (sub-agent) rows the terminal editor shows. The
  native transcript and the inspector's Agents tab already cover them.

#### What already exists

Check each of these against the code before building on it.

- Host intents. `_Client` in `host.py` already takes `submit(text, mode)`,
  `command(text, tag)`, `cancel`, `set_thinking`, `adjust_effort`, `stop_jobs`,
  `manage_worker`, `navigate_tree`, `cancel_asides`, `watch_job` and `query`.
  The Swift client only observes today (`observe_transcript`) and refuses
  requests, so the work is on the Swift side.
- The command catalog. `SessionController.session_state()` already serializes
  every command (name, description, aliases, fixed arguments and their
  descriptions, group), the dynamic argument lists, skill names and the model
  list, and `commands_changed` pushes it again. It is how an attached terminal
  builds its `/` menu (`_proxy` in `remote.py`). No new RPC is needed.
  Unverified: whether the observer welcome carries this state or only the full
  attach welcome does.
- The plan. The host reads it from `runtime.plan_store` for `peek()`. Nothing
  pushes it to a client yet; the terminal editor rebuilds the task list from
  transcript events.
- Native popups. Of the 14 `.popup()` call sites in `app.py`, eight already
  have a native view (Diffs, Tools, Agents, Conversation tree, Side questions,
  Sessions, Settings, Copy) and three are pick lists that `PCodePicker` can
  show (Model, Links, Hosts/Attach). Three need a small new sheet: the secret
  prompt, Session info, and Jobs.

#### Completion: local where it can be, cached where it cannot

The `/` and `@` menus are used in the first second of a session, so neither can
wait for the host to boot.

- `@` file references are fully native. The Swift side lists the workspace
  (`git ls-files --cached --others --exclude-standard`, with the directory-walk
  fallback `file_refs.py` uses), scores with `PCodePickMatch`, and inserts
  `./relative/path`. Typing `@` completes inline; the same index backs a file
  picker (`PCodePicker`) for browsing with a wider list. No host involved.
- `/` commands come from a snapshot. Sharing the code is not an option: the
  catalog is built from Python objects, and importing that stack is the boot
  cost being avoided. Reimplementing it in Swift only works for skills
  (`discover_skills` is a glob and a frontmatter read); built-ins are Python
  literals and extension commands come from user Python files. So:
  1. When the app receives session state, it writes the `commands`,
     `arguments` and `models` parts to a JSON file per workspace.
  2. The composer loads that file on the first frame, and the live state
     replaces it when the host connects.
  3. A workspace opened for the first time falls back to a global last-seen
     snapshot. Built-ins are the same everywhere, so only that workspace's
     skills are missing until the host answers.
- Known limits of the snapshot: a skill or extension command added since the
  last run appears about a second late; dynamic arguments (`/mcp`, `/jobs`) and
  `$MODEL` completion show the cached values until connect; a cached command
  that no longer exists gets the host's ordinary "unknown command" reply.
  A native skill scan would close the first gap at the price of mirroring the
  naming-style and shadowing rules in two languages. Not planned unless the
  gap is noticed.
- Completing is instant; running is not. A command submitted before the host
  is up goes through the same pre-connect queue as a prompt.

#### Tasks: where the plan goes

The terminal editor draws the model's plan above the input. In the app it
should not live inside the text view, and it does not need the full list on
screen all the time.

Proposed, pending the owner's call on placement:

- A one-line summary in the composer's status strip: progress and the step in
  flight ("3/7 · Adding the migration"), with the weighted progress bar the
  terminal shows. Hidden when there is no plan.
- Clicking it (or a shortcut) opens the full checklist in a popover anchored to
  the strip, so the list never pushes the transcript around.
- Optional later: a Tasks tab in the inspector for people who want it pinned.

Feeding it needs one small host addition: push the plan to observers when it
changes (a `plan_changed` call, or a `plan` field in the state delta), using
the same items `peek()` already serializes. Deriving it in Swift from
`write_plan` and `update_task_statuses` tool calls in the transcript would work
without a host change, but it means replaying tool arguments and result ids in
a second place.

#### Phases

Each phase merges to `master` behind a `native_composer` preference and leaves
the app working. The terminal editor stays the default until phase 5.

0. Two-way client. `PCodeHostClient.swift` attaches as a full client and can
   send `submit`, `command`, `cancel` and the rest. It decodes session state
   (commands, arguments, models) and writes the per-workspace snapshot. The
   composer status fields that arrive today as `pcode;composer;` APCs
   (`PCodeComposerStatus` in `PCodeDock.swift`: phase, send mode, queue,
   steering, jobs, `/btw` counts, cache note) move to the socket. The host is
   spawned headless with `Process`, not on a Ghostty surface, and startup
   states (project trust, no model, config errors) get native cards from
   `startup_pending` and `startup_error`.
1. Composer and keys. `PCodeComposerView` on `NSTextView`, growing with its
   content, mounted on the first frame. The keybindings listed under scope.
   A pre-connect queue: a prompt or command sent before the host is up shows in
   the transcript at once and is flushed when the socket connects.
2. History, drafts and image paste. Up/Down recall on the first and last
   visual line, keeping the text in progress. Drafts follow `drafts.py`
   (autosave, save on clear and pane switch, delete on send). A pasted or
   dropped image is saved and downscaled as `prompt_images.py` does, and its
   path inserted; the host already attaches any image path in a prompt, so
   there is no protocol change.
3. Completion. `@` inline completion and the file picker from the native
   index; `/` completion from the snapshot, then live. `PCodeCompletionLayer`
   is reused, anchored with `firstRect(forCharacterRange:)`. Tab, Ctrl+L,
   Enter, Up/Down, Esc and click to accept.
4. Tasks and status strip. The host pushes the plan; the strip shows the
   summary and the popover shows the list, as above. The status strip reads
   the socket only.
5. Remaining popups and cutover. Route the eight commands that have native
   views, drive the three pick lists through `PCodePicker`, add the three
   sheets. Turn `native_composer` on by default and stop running the
   editor-only client in session panes. Ghostty is then used only for the
   shell split.

Phases 0 and 1 are the first milestone: they deliver the instant startup and
are enough to judge the native text view before building the rest.

#### Phases 0 and 1 as built (2026-11-27, behind `native_composer`)

- Spawning: `pcode --host --spawn-only[=ask|trust|skip]` (`pcode.app_spawn`)
  spawns the host detached and prints one JSON line (`id`, `socket`, `log`,
  `pid`, `existing`). The app runs it through `PCodeWorkspaceMode.launchConfig`
  so the host gets the login-shell env and direnv. Project trust comes back as
  `{"trust": ...}` and is asked in a native card; the app reruns with `=trust`
  or `=skip`. No model means the host runs `PreviewRuntime`; `startup_error`
  shows as a card above the composer.
- Pane model: `PCodeSessionPane.surface` is optional, and a native pane has
  `composer: PCodeComposerSession` instead. Nothing runs a hidden terminal.
  Focus goes through `pane.focusInput()`, "is this the input" checks go through
  `pane.ownsInput(_:)`, and size, occlusion and reconcile skip surface-less panes.
  The bottom shell split is still a Ghostty surface.
- Client: `PCodeHostRPCClient` attaches as a full client (protocol 2) and
  shares socket framing with the observer (`PCodeHostLineSocket`,
  `PCodeLineBuffer`). The transcript keeps its own `observe_transcript`
  connection, which already handles history, catch-up and reconnects. Host
  requests all go to `PCodeHostRequests`: `run_command` handles `/quit`,
  `/exit` and `/redraw` and shows a toast for the rest, `choose_model` returns
  nil, and `read_secret` opens an `NSAlert` with an `NSSecureTextField`.
- Pre-connect queue: `PCodeComposerSession.outbox`. Prompts show at once as
  local rows (`PCodeLiveTranscriptController.showLocalPrompt`), and the host's
  echo (`turn_started` / `user`) replaces each one.
- Hooks for later phases: `PCodeComposerTextView.onPasteImage`, `onTextChange`
  and `caretScreenRect()` (completion anchor); `PCodeComposerSession.onHistory`
  for Up/Down history, `onSessionState` for the catalog snapshot, and `state.plan`
  for the task strip.
- Phases 2 and 3 (same flag): `PCodeComposerSession+Input.swift` wires
  `PCodePromptHistory` (Up/Down, drafts every `PCodeDraftStore.interval`, on
  Ctrl+C, pane switch and close), `PCodePastedImage` (Cmd+V without text, drop,
  `/paste-image`), and `PCodeComposerCompletion` (the `/` and `@` menu, drawn
  with the terminal menu's `PCodeCompletionList`). The catalog snapshot is saved
  on every `session_state` and loaded on the first frame. Cmd+Shift+A, or the
  strip's `@ file` button, opens the workspace in `PCodePicker.showLocal`.
- Known gap: attaching counts as a terminal, so opening a session another
  client holds marks it seen and routes its popups to the app.

#### Native command routes (phase 5)

`PCodeNativeCommands.route(for:)` decides what the app does with each terminal
command, and `run(_:context:)` does it through a `Context` (built for a real
pane by `Context.live(session:client:)`). Host popup requests have their own
entry points on the same type. Sheets go over the window as AppKit sheets
(`PCodeNativeSheetPresenter`), so the pane's views don't change.
`PCodeHostRequests` calls these for `run_command` and the popup requests, and
`PCodeCommandCatalog.terminalCommands` marks a command `.native` exactly when it
has a route here that isn't a toast (a test keeps them in step).

| Command / request | Native route | Entry point |
| --- | --- | --- |
| `/help`, `/commands` | Commands sheet: the catalog by group, filterable | `PCodeHelpSheet` |
| `/config` (bare) | Pcode.app Settings | `PCodeSettingsController.shared.show()` |
| `/config ARGS` | `pcode config ARGS` run for its output: a toast if it's one line, a text sheet otherwise | `runConfig`, `PCodeTextSheet` |
| `/bind`, `/unbind` | Not supported, toast: prompt_toolkit keybindings; the composer uses macOS text keys | |
| `/quit`, `/exit` | Close the pane (stops its host) | `session.onCloseRequested` |
| `/stop` | Same as `/quit`: closing a native pane ends its host | `session.onCloseRequested` |
| `/detach` | Not supported, toast: a native pane's host lives as long as the pane | |
| `/status` | Session sheet: mirrored state at once, then the host's `query("session_overview")` rows | `PCodeSessionInfoSheet` |
| `/tools`, `/tools failed` | Inspector Commands tab (shell calls and jobs; other tools are in the transcript) | `pane.inspectorState` |
| `/tools edits`, `/diffs` | Inspector Changes tab | `pane.inspectorState` |
| `/links` | Picker of URLs from the transcript's prompts and answers (newest first); Enter opens, `file://` links in the editor at their line | `PCodePicker.showLocal`, `PCodeExternalEditor.open(fileURL:)`, `NSWorkspace.open` |
| `/copy` | Picker of answers, their code blocks nested; Enter copies | `PCodePicker.showLocal`, `NSPasteboard` |
| `/paste-image` | Composer image paste | `session.pasteImage(from:)` |
| `/tree` | Inspector Tree tab | `pane.inspectorState` |
| `/agents` | Inspector Agents tab | `pane.inspectorState` |
| `/resume` | Picker of saved sessions; Enter opens one in a pane | `workspace.history`, `workspace.open(_:)` |
| `/attach`, `/switch` (bare) | Picker of background hosts | `workspace.projectGroups[].hostedSessions` |
| `/attach HOST` | Opens that host's session: exact id, else a prefix of exactly one, as `find_host` | `attachTarget`, `workspace.open(_:)` |
| `/attach -` | Not supported, toast: use the sidebar | |
| `/fork` | Not supported, toast: needs the terminal's fork-into-worktree flow | |
| `/new PROMPT` | New pane in this folder; the prompt waits in its outbox | `workspace.newSession(directory:)`, `composer.submit` |
| `/restart` | Restart this pane's host | `session.requestRelaunch(isManual: true)` |
| `/restart all` | Not supported, toast: Restart Pcode restarts every session | |
| `/show-thinking [mode]` | `set_thinking` intent (the host pushes `thinking_mode` to the transcript) and `show_thinking` saved to preferences.json; bare cycles off/scrollback | `context.notify`, `savePreference` |
| `/group-tools [on\|off]` | The transcript's own toggle | `PCodeTranscriptPreferences.shared.groupTools` |
| `/show-activity`, `/show-tasks`, `/autohide-tasks` | Not supported, toast: no activity panel (the task strip is phase 4) | |
| `/show-edits` | Not supported, toast: edits always show, folded, plus the Changes tab | |
| `/show-commands` | Not supported, toast: shell output lives in the Commands tab | |
| `/theme`, `/syntax`, `/theme-preview` | Not supported, toast: the app follows macOS appearance and its Ghostty theme | |
| `/redraw` | Nothing to do | |
| `choose_model(values, providers, current)` | Picker as `model_ui` builds it: `Provider · model`, current marked and selected, typed `provider:model-id` as a custom row; replies with the id or nil | `chooseModel(...)` |
| `browse_jobs()` | Jobs sheet from `peek().jobs`: Watch/Unwatch (`watch_job`), Stop and Stop All (`stop_jobs`). Replied at once, so the host's command queue isn't held | `browseJobs`, `PCodeJobsSheet` |
| `read_asides(tag)` | The `/btw` drawer, mounted at pane level for native panes (`PCodeDrawer(onFocusInput:)`); reply nil | `readAsides` → `Action.btw` → `session.openBtw()` |
| `read_secret(...)` | Unchanged: `PCodeHostRequests.readSecret` | |

#### Cutover and what is still not native (2026-11-28)

`native_composer` defaults to `on`: new session panes run no terminal for the
session. `native_composer off` (or `PCODE_NATIVE_COMPOSER=0`) brings back the
editor-only terminal client in new panes; both paths were smoke-tested. While
both exist a pane runs exactly one of them (`PCodeSessionPane.surface` xor
`composer`).

The `/btw` drawer in native panes: `read_asides` (bare `/btw`) and a fresh
answer (when the composer is empty) open the newest thread; Ctrl+\\ steps
threads; Escape closes. While it shows a thread, Enter follows up, and
`/merge`, `/summarize`, `/close` and `/stop` act on it, as `btw_submit` does.

Still not native, or not verified natively:

- Toasts only: `/bind`, `/unbind`, `/detach`, `/fork`, `/attach -`,
  `/restart all`, `/show-activity`, `/show-tasks`, `/autohide-tasks`,
  `/show-edits`, `/show-commands`, `/theme`, `/syntax`, `/theme-preview`.
- No vi bindings, and no user keybindings (`/bind`) in the composer.
- The status strip's phase is coarser than the terminal's: no tool or
  sub-agent counts, no cache-bust note.
- Driven only with the preview model, so never in the app: the Jobs sheet
  (preview has no job registry), the `/btw` drawer with real answers and its
  merge/summarize/follow-up paths, `read_secret`, a real host's
  `plan_changed` (the strip was driven with `PCODE_DEBUG_PLAN_FILE`), image
  attachment by a real model, and opening a session from the `/resume` picker.
- Synthetic ⌘V can't be driven in the harness, so image paste by ⌘V and
  drag-and-drop are unverified in the app (`/paste-image` is).
- Attaching counts as a terminal: opening a session another client holds marks
  it seen and routes its popups to the app.
- Restart Pcode doesn't signal native panes' hosts; it relies on reattaching
  them by session or saved host id on relaunch.
- The launcher's Enter can open two "New session" panes (seen with the flag on
  and off; not investigated).
- Peek's scroll shield and the terminal-only dock code (`PCodeDock`,
  `PCodeDockedSurface`, the APC composer status) stay for the flag-off path;
  removing them, and step 4, wait on deciding to drop that path.

### 4. Swap the container (only if 3b is done)

Once nothing draws through a terminal, the app gets its own Xcode project. The
APC protocol, `dock.py` and the popup bridge go away, and so do the Zig
toolchain, `app/ghostty.lock` and the copy-into-the-fork build.

Things the fork provides today that would need rebuilding:

- Theme and fonts. The transcript theme reads `ghostty.config` colors.
- The debug socket, about 1,400 lines of Swift that live in the fork and would
  move into `app/macos`.
- App basics: menu, keybindings and window restoration.

## The shell split: libghostty or something else

Only relevant if step 4 happens. "Keep only libghostty for the split" is less
clean than it sounds. From a quick search of third-party write-ups, not
upstream docs:

- `libghostty-vt` reached 1.0 in November 2026 and is stable, but it covers
  only VT parsing and terminal state. No renderer, fonts or pty.
- The full embedding API (`ghostty_surface_*`, which the app uses now) is what
  has the renderer. Upstream labels it internal and expects more breaking
  changes.

So "libghostty only" means keeping `GhosttyKit` and vendoring Ghostty's Swift
surface layer: the fork's app shell goes, but we still track unstable
internals. SwiftTerm (an ordinary Swift package with a pty-backed AppKit view)
is the lighter option for a plain shell split. Spike it before committing,
since rendering quality will be a step down from Ghostty.

## Open questions

- Where does the task list live in the native composer: the status-strip
  summary with a popover (proposed in 3b), an inspector tab, or both?
- Do vi bindings come back after the native composer ships?
- Is SwiftTerm-level rendering acceptable for the shell split, or does it need
  to look like Ghostty?

## Related

- [ghostty-app-plan.md](ghostty-app-plan.md): the original plan for building
  the app on the fork.
- [desktop-host-plan.md](desktop-host-plan.md): the earlier host-client design,
  useful background for step 3a.
