# Configuration

## Global configuration

Global defaults are shared across workspaces in `~/.config/pcode/preferences.json`
(or `$XDG_CONFIG_HOME/pcode/preferences.json`). Inspect and edit them without
opening a terminal UI or connecting a model:

```sh
pcode config                     # List effective startup defaults as JSON
pcode config diff                # List only settings that differ from defaults
pcode config path                # Print the resolved config path
pcode config get theme
pcode config set theme light
pcode config set autocompact on
pcode config set effort high
pcode config set model openai-codex:gpt-5.6-luna
pcode config unset model          # Remove saved model; return to offline preview
pcode config unset theme          # Restore automatic theme detection
pcode config reset                # Remove every saved default at once
```

Inside pcode, **`/config` opens a full-screen, searchable settings editor**.
The settings list fills the screen, with the selected setting's details beside it
(or below it in a narrow terminal), and the layout stays fixed as you browse,
filter, and edit. Type to search setting names and descriptions; settings whose
name contains your search come first. Use ↑/↓ to select a setting and press Enter
to edit it in the details pane. Choose from the available options or enter a value; Enter saves and
keeps the browser open. Escape cancels the current edit, or closes the browser
when you are not editing. Invalid values stay in the editor with an explanation.
Tab switches focus to the details so you can scroll long descriptions and values;
Shift+Tab returns to the search or value editor.

Each setting shows its built-in default, saved effective value, and whether that
value comes from the user or project configuration. These are saved defaults,
not a snapshot of the running conversation. The detail area identifies layout
settings that apply immediately and warns when a project override takes
precedence over a user edit.

The scope control shows the shortcut for switching between user and project
settings. Project scope is available when a workspace is selected and omits
user-only settings. Toggle **Overrides only** to see just the values saved in the
selected scope, including explicit overrides equal to the built-in default.
Removing an override clears that scope's saved value, letting the underlying
user value or built-in default take over. The scope, filter, and reset shortcuts
are shown in the browser and follow your configured key prefix; **Keys** opens
the complete keyboard help.

Explicit commands remain available with tab completion: `/config list` prints
JSON, and `/config set theme light`, `/config get autocompact`,
`/config unset effort`, and `/config diff` work without opening the browser.
**Most config edits affect the next launch, not the running conversation.** To change
an active setting and save its default immediately, use `/theme light|dark|auto`, `/effort`,
`/model`, or `/autocompact` instead. The layout settings `attach_tasks`, `attach_activity`,
`tasks_max_height`, `tasks_min_rows`, `tasks_min_columns`, `task_style`, `frame_style`, `tool_glyphs`, `tool_max_lines` and `tool_linger_seconds` apply immediately through `/config`. CLI overrides such as
`--theme` and `--model` do not rewrite global defaults, and resumed sessions retain
their own model.

## Model provider filter

Limit the `/model` selector to specific providers:

```sh
pcode config set model_providers openai-codex,anthropic
pcode config unset model_providers # Restore automatic detection of all active providers
```

Or run `/config set model_providers openai-codex,anthropic` inside pcode.
This setting is read each time the selector opens, without a restart. In
`preferences.json` it is a string: `"model_providers": "openai-codex,anthropic"`.
Names match exactly: `openai-codex` does not include `openai`, `openai-chat`, or
`openai-responses`. Unknown provider names are rejected.

An unset or empty value shows all automatically detected active providers.
A nonempty list filters those providers; it does not configure credentials or
force inactive providers to appear. The current provider is also hidden if it
is excluded. This only filters the selector, not explicit `/model PROVIDER:MODEL`
commands, CLI model overrides, or resumed sessions.

## Keybindings

Use [`/bind` and `/unbind`](keybindings.md) to customize main-prompt action keys.
Mappings are user-only and stored in `bindings.json`, separate from
`preferences.json`: `$PCODE_CONFIG_DIR/bindings.json` when set, otherwise
`$XDG_CONFIG_HOME/pcode/bindings.json`, defaulting to
`~/.config/pcode/bindings.json`. There is no project binding file or project
configuration override. Writes are locked and atomic.

Binding edits apply immediately in the current terminal. Other running terminals
pick them up on their next start or a binding management edit, not automatically.
The editor and prefix preferences (`editing_mode`, `vi_escape_sequence`,
`vi_key_prefix`, and `key_prefix`) still require restarting the prompt; see
[Keybindings](keybindings.md) for their setup and how global shortcuts and the
optional vi leader share mappings.

## Independent instances

`PCODE_CONFIG_DIR` points pcode at a different config directory without moving
the rest of your `XDG_CONFIG_HOME`. Everything pcode keeps there follows it:
`preferences.json`, `codex-credentials.json` and `mcp-credentials.json` (so
each instance has its own `/login openai-codex` and MCP sign-ins), `mcp.json`,
`bindings.json`, `extensions/`, and `worktree-setup`.
Sessions and other state still live under `XDG_STATE_HOME`; set
`PCODE_SESSION_DIR` too if those should be separate.

```sh
PCODE_CONFIG_DIR=~/.config/pcode-work pcode      # separate login and settings
```

`PCODE_CODEX_CREDENTIALS_FILE` and `PCODE_MCP_CONFIG`
still win over the directory for their single file.

## Per-repository overrides

A workspace's `.pcode/preferences.json` is layered over the user file at launch,
so a setting can hold for one repository and be committed for everyone who
clones it. `config list` and `config get` show the merged result;
`config project` edits the repository file (from `-C DIR` or the current
directory):

```sh
pcode config project set worktree on    # this repo only; writes .pcode/preferences.json
pcode config project list               # the raw overlay
pcode config project unset worktree
pcode config project reset              # Drop the whole overlay
```

A cloned repository must not be able to run code or pick credentials on your
behalf, so `project_extensions`, `trusted_projects`, `extension_dirs`,
`extensions_off`, and `extensions_on` are user-only: the project file cannot
set them, and pcode says so at launch if it tries. `model` and `subagent_models`
can be set, but only choose among the providers you are signed in to. When the
repository sets `subagent_models`, `/subagents` shows that list, says it comes
from the repository, and refuses to change it; edit it with
`pcode config project set` or `unset` instead. The overlay is read from the
launch workspace before any worktree is created, so `worktree on` in a
repository's file is what starts each of its sessions in a worktree.

## Trusting a repository's own code

A repository can ship code that runs at launch with your permissions:
`.pcode/extensions/*.py` (see `/extensions`, which lists every extension and its
state, and turns one on or off) and `.pcode/worktree-setup`. Neither
runs until you trust that repository. The first interactive launch inside one
that ships either asks:

```
pcode: /path/to/repo ships code that runs at launch with your permissions: .pcode/worktree-setup
Trust this repository? [y/N]
```

`y` records the repository's primary checkout in `trusted_projects` (so all of
its worktrees are covered) and never asks again; `n` skips the code for this
launch and asks next time. `--print` has nobody to ask, so it skips and says so.
Revoke with `pcode config unset trusted_projects` (or edit the `:`-separated
list). `pcode config set project_extensions on` trusts every repository, which
is only sensible on a machine where you wrote all of them.

## Settings reference

Every key works with `pcode config set KEY VALUE` and `/config set KEY VALUE`.

### Models and providers

| Key | Built-in default | Values |
| --- | --- | --- |
| `model` | `null` (offline preview) | A model name, normally `provider:model` |
| `effort` | `default` | `low`, `medium`, `high`, `xhigh`, `default` (OpenAI/Codex, Anthropic, Claude Code); fallback for models `/effort` has not set |
| `model_providers` | empty | `,`-separated providers `/model` lists; empty shows every provider you're signed in to or have a key for |
| `claude_idle_processes` | `1` | Whole number of finished [`claude:`](providers.md#claude-code-provider) CLI processes each session keeps warm (about 120 MB each beyond the first); `0` stops each when its turn ends. None are kept under memory pressure |
| `claude_idle_minutes` | `10` | Positive integer, minutes a finished `claude:` CLI process is kept warm |
| `retry_attempts` | `3` | Whole number, automatic retries after a dropped connection or streamed overload; `0` disables. See [retries](sessions.md#retries-and-resend) |

### Editor and keys

| Key | Built-in default | Values |
| --- | --- | --- |
| `send_mode` | `steering` | `steering`, `queue`, `interrupt` (what Enter does while a turn runs; see [sending while the agent is working](commands.md#sending-while-the-agent-is-working)) |
| `editing_mode` | `emacs` | `emacs`, `vi` (prompt editor key bindings; see [vi editing](commands.md#optional-vi-editing)) |
| `vi_key_prefix` | `off` | Additional leader for the main prompt's vi normal mode: `<space>` or a single printable non-whitespace character, such as `,` or `\`. Keeps the global prefix. Requires restart; see [Use Space as the vi leader](keybindings.md#use-space-as-the-vi-leader). |
| `vi_escape_sequence` | `escape` | Escape-only by default; set `jj` (or another printable sequence without spaces) to also leave vi insert mode with that sequence. Escape remains available. Requires restart; see [vi editing](commands.md#optional-vi-editing). |
| `vi_wrapped_rows` | `on` | `on`, `off` (vi normal-mode `j` / `k` move by wrapped screen row, like vim's `gj` / `gk`; `off` moves by whole line. Requires restart) |
| `key_prefix` | `ctrl` | Direct Ctrl+letter shortcuts, such as Ctrl+L for models and Ctrl+Y to copy. No global action-menu leader by default. Set `ctrl+b`, `ctrl+p`, `ctrl+space`, `f2` or `"ctrl+x ctrl+p"` to use a leader before the action letter. Ctrl+/ browses contextual help. See [shortcut prefix](commands.md#shortcut-prefix) |
| `popup_mouse` | `on` | `on`, `off` (popups capture clicks and the wheel, and a drag selects and copies a pane's text; `off` leaves selection to the terminal, Ctrl+Q flips it inside one popup (Ctrl+G in the `/btw` viewer), see [selecting text](commands.md#selecting-and-copying-text-in-popups)) |
| `btw_auto_open` | `on` | `on`, `off` (open the viewer when a [side answer](side-questions.md) is ready) |
| `btw_view` | `popup` | `popup`, `swap` (show side threads in a full-screen popup, or [swap them in](side-questions.md#swapping-a-thread-in) for the conversation, scrollback and all, where the main editor replies to them) |

### Scrollback and display

| Key | Built-in default | Values |
| --- | --- | --- |
| `theme` | `auto` | `dark`, `light`, `auto` |
| `syntax_dark` | `terminal` | `terminal` or a Pygments style, for the dark palette |
| `syntax_light` | `terminal` | `terminal` or a Pygments style, for the light palette |
| `spinner` | `dots` | The status row's animation while the model works. A Rich spinner name, e.g. `dots`, `line`, `point` (`python -m rich.spinner` previews them; `/config set spinner` lists the ones that fit; applies on next launch) |
| `tool_spinner` | `arc` | The same, for a running tool call's row and the terminal's own waits, so they look different from the model's |
| `show_thinking` | `activity-panel` | `off`, `activity-panel`, `scrollback` (where the model's readable reasoning shows; `/show-thinking`. `status-line` is still read as `activity-panel`) |
| `thinking_max_lines` | `10` | Max rows of thinking in the activity panel in `activity-panel` mode, or a share of the screen (`0.2`). A row count is also capped at a quarter of the pane, so a short pane gets fewer. Applies on next launch |
| `show_edits` | `on` | `on`, `off` (show a diff of each file edit; Ctrl+Z or `/show-edits`) |
| `live_edits` | `off` | `on`, `off` (preview an edit or `run_code` snippet at the bottom while the model writes it) |
| `diff_renderer` | `delta` | `delta`, `rich` (draw diffs in scrollback and `/diffs` with [delta](https://dandavison.github.io/delta/) when it's installed, falling back to Rich; see [diffs with delta](transcript.md#diffs-with-delta)) |
| `delta_args` | empty | delta's arguments, quoted as in a shell, such as `--line-numbers`; the only delta configuration pcode reads (git config is ignored), and they override pcode's own choices |
| `diff_dedent` | `on` | `on`, `off` (strip the indentation every line of a diff hunk shares, so an edit deep in a nested block starts at the left edge; with delta or Rich; see [diffs with delta](transcript.md#diffs-with-delta)) |
| `diff_layout` | `unified` | `unified`, `side-by-side`, `auto` (delta's layout; `auto` goes side by side at 180 columns or wider) |
| `show_commands` | `off` | `off`, `activity-panel` (a running command's output live in the activity panel), `scrollback` (that, then each command and its output mirrored into scrollback; `on` is still read as this). Ctrl+G or `/show-commands`; see [command output](transcript.md#command-output-in-scrollback) |
| `group_tools` | `on` | `on`, `off` (fold each run of tool calls into one line; Ctrl+] or `/group-tools`, see [grouping tool calls](transcript.md#grouping-tool-calls)) |
| `command_scrollback_lines` | `20` | Positive integer, lines of each command's output mirrored into scrollback |
| `command_preview_lines` | `10` | Rows (`10`) or a share of the screen (`0.25`) for the live preview of a running command |
| `tool_error_scrollback` | `off` | `on`, `off` (keep a failed tool call's full diagnostic in scrollback instead of one line) |
| `error_scrollback_lines` | `20` | Positive integer, lines of an error notice kept in scrollback before it is clipped |
| `show_activity` | `on` | `on`, `off` (show the [activity panel](commands.md#activity-panel): thinking, tool calls, tasks and running commands together; Ctrl+O or `/show-activity`) |
| `show_tasks` | `on` | `on`, `off` (show the task list in the activity panel; `/show-tasks`) |
| `autohide_tasks` | `off` | `on`, `off` (hide the task list when a turn ends; `/autohide-tasks`) |
| `tasks_min_rows` | `30` | Hide the activity panel (thinking and tool rows included; the status row says what runs) in a pane shorter than this, such as a stacked split; `0` never hides. Ctrl+O overrides it until the pane crosses the threshold again |
| `tasks_min_columns` | `100` | Hide the activity panel in a pane narrower than this, such as a side-by-side split; `0` never hides. Ctrl+O overrides it the same way |
| `show_hints` | `on` | `on`, `off` (show one compact help indicator at the prompt instead of shortcut hints beside individual controls) |
| `attach_tasks` | `on` | `on`, `off` (draw tasks inside the editor box; `/config` applies immediately) |
| `attach_activity` | `on` | `on`, `off` (draw the thinking and tool rows inside the editor box, under the status row; `off` lists them above the box, where they still show and hide with the panel. `/config` applies immediately) |
| `tool_glyphs` | `auto` | `on` leads each tool row in the activity panel with a symbol for the call (`$`, `⌕`, `✎`, `⎘`, `⧖`), `off` drops them and spells out the verb once the call settles (shell calls keep `$`), for fonts that draw those symbols badly. `auto` is on except on the Linux console or with ASCII `glyphs` (`/config` applies immediately) |
| `glyphs` | `auto` | `unicode`, `ascii` (draw marks, borders and spinners as plain ASCII stand-ins of the same width, for fonts or terminals that draw the symbols badly). `auto` picks ASCII on the Linux console, under a locale naming a charset other than UTF-8, or in a terminal that draws box-drawing lines two cells wide (a CJK locale, iTerm2's ambiguous-width option); see [terminal setup](terminal.md#symbols-and-colors). Applies on next launch |
| `color_depth` | `auto` | `truecolor`, `256`. `auto` uses 24-bit color where the terminal says so (`COLORTERM`) or is one known to support it (iTerm2, Ghostty, kitty, WezTerm, VS Code and others, or tmux whose client has the `RGB` feature), and the terminal's own 256 colors otherwise. `NO_COLOR` and `PROMPT_TOOLKIT_COLOR_DEPTH` win over it. Applies on next launch |
| `tool_max_lines` | `3` | Max tool calls listed in the activity panel, newest last; `1` shows only the call the status row is on. A short pane gets fewer, sharing a quarter of its height with the thinking rows (`/config` applies immediately) |
| `tool_linger_seconds` | `10` | Seconds a finished tool call stays listed in the activity panel, so a long wait on the model doesn't keep showing stale calls; `0` keeps it until newer calls push it out (`/config` applies immediately) |
| `task_style` | `status` | `status` (shade task text by status), `icons` (one text weight, coloured icons only; `/config` applies immediately) |
| `frame_style` | `shaded` | `borders` (line boxes around the editor and tasks), `shaded` (no lines: the boxes are background shades lifted off the terminal's own background; falls back to borders on a terminal without 24-bit color (see `color_depth`), or under `/syntax terminal` when the terminal never reports its background; `/config` applies immediately). The box drawings elsewhere in these docs show `borders` |
| `tasks_max_height` | unset | Rows (`20`) or a share of the screen (`0.5`) for the editor box (with the thinking, tool and task rows in it) together with any detached task box; unset keeps the task list to 10 rows or half the screen |
| `paced_scrollback` | `typed` | `typed`, `rows`, `off` (type settled prose out, or roll blocks in a row per frame; see [the transcript](transcript.md#paced-scrollback)) |
| `regenerate_on_resize` | `on` | `on`, `off` (rebuild scrollback at the new size after a resize; applies on next launch) |
| `transcript_max_chars` | `2000000` | Positive integer, retained text budget shared by resume and redraw; applies on next launch |
| `cache_notices` | `on` | `on`, `off` (footer note, saved with the session, when a request reuses less of the prompt cache; see [prompt cache notices](context.md#prompt-cache-notices)) |
| `terminal_progress` | `auto` | `auto`, `on`, `off` (the terminal's [tab progress bar](terminal.md#progress-bar) while a turn runs; OSC 9;4) |
| `desktop_notifications` | `on` | `on`, `off` (desktop notification when a background session finishes; OSC 9) |
| `terminal_title` | `on` | `on`, `off` (set the terminal [tab title](terminal.md#tab-and-pane-titles) to the session's name; OSC 0) |
| `session_naming` | `on` | `on`, `off` (ask the session's own model for a [title](sessions.md#session-titles) beside the first turn) |

### Tools and sub-agents

| Key | Built-in default | Values |
| --- | --- | --- |
| `web_search` | `auto` | `auto`, `local`, `off` (provider-native web search when available, pcode's own, or none; see [web search](tools.md#web-search)) |
| `web_fetch` | `auto` | `auto`, `native`, `local` (provider-native page fetching where it returns the page text, so not on `claude:`; any provider-native fetch, including Claude Code's summaries; or always pcode's own `get_page`, which returns the page text verbatim) |
| `code_mode` | `off` | `on`, `off` (batch read-only tools through a sandboxed `run_code`) |
| `job_wake` | `on` | `on`, `off` (start a turn when a job the model backgrounded finishes while idle; see [shell jobs](tools.md#shell-jobs)) |
| `tool_retries` | `3` | Whole number, corrections the model gets per turn when a tool call has invalid arguments |
| `strict_tools` | `on` | `on`, `off` (constrain `edit_file` arguments with Anthropic strict tool use) |
| `subagent_models` | empty | `,`-separated models `delegate_task` lists for running a sub-agent; others can still be named per delegation ([`/subagents`](tools.md#sub-agents-on-other-models)); empty runs every sub-agent on the session's model; `/reload` to apply |
| `worker_concurrency` | `0` | `0` means unlimited; a positive integer caps concurrent built-in workers per session; `/reload` to apply |
| `tool_output_mode` | `spill` | `spill`, `truncate`, `off` |
| `tool_output_threshold` | `10000` | Positive integer, characters that trigger reduction |
| `tool_output_preview_chars` | `1000` | Positive integer, spill preview characters |
| `tool_output_max_chars` | `4000` | Positive integer, truncation budget (also spill-failure fallback) |
| `tool_output_strategy` | `head_tail` | `head`, `tail`, `head_tail` (truncation only) |
| `tool_output_retention_hours` | `0` | Whole number, spill retention; `0` keeps indefinitely |

### Context

| Key | Built-in default | Values |
| --- | --- | --- |
| `autocompact` | `on` | `on`, `off` |
| `autocompact_tokens` | unset | Tokens such as `200000` or `200k` (minimum 50k) at which to compact automatically; unset uses about 90% of the window |

### Sessions and worktrees

| Key | Built-in default | Values |
| --- | --- | --- |
| `session_host` | `off` | `on`, `off` (run sessions in a [background host](sessions.md#background-sessions) that outlives the terminal from the start; off runs them in the terminal until [`/detach`](sessions.md#background-sessions) moves one into a host; `--host`/`--no-host` override it once) |
| `session_host_idle_minutes` | `0` | whole minutes a [background session](sessions.md#idle-hosts-stop) may sit idle with no terminal before its host stops; `0` stops it 15 seconds after it goes idle, `off` never |
| `worktree` | `off` | `on`, `off` (start new sessions in `.worktrees/` git worktrees; does not enable worker isolation on its own) |
| `worktree_exit` | `ask` | `ask`, `merge`, `keep` (what to do with unmerged commits when a session worktree is left) |
| `worker_isolation` | `off` | `on`, `off` (opt in to isolated built-in worker tasks; also requires effective `worktree=on`, not just the CLI launch override; checked at each delegation) |

### Email remote control

Settings for [`pcode --email-listen`](email.md). All of them are user-only: a repository's `.pcode/preferences.json` cannot set them.

| Key | Built-in default | Values |
| --- | --- | --- |
| `email_owner` | unset | the Gmail address allowed to control pcode by email; set by `pcode --email-setup` |
| `email_mcp` | `off` | `on`, `off` (start default MCP servers in email-started sessions; they run outside the sandbox) |
| `email_turn_minutes` | `30` | wall-clock minutes per email-started turn; `0` is no limit |
| `email_turn_requests` | `100` | model requests per email-started turn, sub-agents included; `0` is no limit |
| `email_turn_tool_calls` | `100` | tool calls per email-started turn, sub-agents included; `0` is no limit |
| `email_concurrent_sessions` | `2` | email sessions that may be working at once |
| `email_max_sessions` | `20` | sessions one `--email-listen` may start |
| `email_session_inputs` | `20` | emails that may wait in one session |
| `email_max_inputs` | `100` | emails that may wait across every session |

### Repository instructions, skills and extensions

| Key | Built-in default | Values |
| --- | --- | --- |
| `repo_context_walk_up` | `on` | `on`, `off` (inherit ancestor instruction files) |
| `repo_context_nested` | `off` | `off`, `pointer`, `contents` (discover instructions on file-tool traversal) |
| `skill_commands` | `prefix` | `prefix`, `bare`, `both`, `off` (how discovered skills appear as slash commands) |
| `skill_dirs` | `~/.agents/skills:.agents/skills` | `:`-separated directories searched for skills; relative entries resolve against the workspace |
| `project_extensions` | `off` | `on`, `off` (`on` trusts every repository's `.pcode/extensions` and `worktree-setup`) |
| `trusted_projects` | empty | `:`-separated repository paths whose shipped code may run; the launch prompt appends here |
| `extension_dirs` | empty | `:`-separated extra directories searched for extensions, after the user one |
| `extensions_off` | empty | `,`-separated extension names that never load (`/extensions off NAME`) |
| `extensions_on` | empty | `,`-separated opt-in extension names to load (`/extensions on NAME`) |

### Diagnostics

| Key | Built-in default | Values |
| --- | --- | --- |
| `debug` | `off` | `on`, `off` (also write request fingerprints to disk with each cache notice) |
| `profile` | `off` | `off`, `resources`, `cpu`, `memory` (capture each session's resource use; see [profiling](https://github.com/cruxwell/pcode/blob/master/dev/profiling.md)) |
| `stall_log` | `on` | `on`, `off` (when the editor stops responding for roughly 150 ms or more, append what was running to `~/.local/state/pcode/stalls.jsonl`) |

## Code highlighting styles

Each palette gets its own setting: `syntax_dark` applies whenever the resolved
theme is dark, `syntax_light` whenever it is light. `/syntax NAME` changes the
setting for the palette in use and saves it as that palette's default; `/syntax`
alone reports the current one. Tab completion lists the choices, and an unknown
name is rejected with the full list.

Both default to `terminal`, which is not a Pygments style: it hands every color
to the terminal's own ANSI palette. Scrollback uses named colors, fenced code
uses `ansi_dark` / `ansi_light` with no painted background, and the prompt, task
rows and completion popup use ANSI names too (the selected popup row is
reversed). pcode then looks right in whatever scheme the terminal runs.

`/theme-preview` draws a sample of every style on one line, marks the one in
use, and repeats the commands below, so a style can be chosen by eye rather than
by name. The `terminal` row is drawn with the palette's ANSI style.

Any other value is a Pygments style. The completion menu and the prompt chrome
(the chevron, plan rows, the frame, `@file` references) are painted from it, so the screen matches the
code on it. A Pygments style only colors code, though, so any color it leaves
out or that would be unreadable falls back to the palette's own. The two are
judged against different backgrounds: the menu brings the style's own surface
with it, while chrome lands on the terminal's background, so a light style
chosen while the dark palette is active keeps its popup but leaves the chrome
on the palette. Picking any Pygments style also switches scrollback headings,
links, quotes and tables from ANSI names to the palette's own colors.

These are the styles that come with Pygments; installing a Pygments style plugin
package adds to the list automatically.

Darker backgrounds: `coffee`, `dracula`, `fruity`, `github-dark`, `gruvbox-dark`,
`inkpot`, `lightbulb`, `material`, `monokai`, `native`, `night-owl`, `nord`,
`nord-darker`, `one-dark`, `paraiso-dark`, `rrt`, `solarized-dark`, `stata-dark`,
`vim`, `zenburn`.

Lighter backgrounds: `abap`, `algol`, `algol_nu`, `arduino`, `autumn`, `borland`,
`bw`, `colorful`, `default`, `emacs`, `friendly`, `friendly_grayscale`,
`gruvbox-light`, `igor`,
`lilypond`, `lovelace`, `manni`, `murphy`, `paraiso-light`, `pastie`, `perldoc`,
`rainbow_dash`, `sas`, `solarized-light`, `staroffice`, `stata-light`, `tango`,
`trac`, `vs`, `xcode`.

Styles differ in how much they color: some leave plain identifiers and
punctuation at the default foreground, so a snippet that is mostly names can look
unhighlighted even though the lexer ran. Compare a few against your own terminal
background before settling on one.

Outside the editor, `pcode config set syntax_dark NAME` and
`pcode config set syntax_light NAME` save the same two settings.

## Notes

Automatic compaction still requires a known context window; setting its global
preference does not validate a particular model or trigger a compaction. For custom
deployments, use `PCODE_CONTEXT_WINDOW` as described in
[context compaction](context.md#context-compaction). MCP configuration and
credentials are separate from these non-secret defaults.

Writes are atomic and serialized across terminals. Unknown JSON keys are preserved;
invalid setting values fall back to built-in defaults. Normal startup tolerates a
malformed file, but config commands report it and refuse to overwrite it: use
`pcode config path` to find and repair it first. Invalid commands exit nonzero.

At startup pcode looks up the latest release on PyPI, at most once a day and
without delaying the session, and shows a warning with the upgrade command for
your install (Homebrew, uv, pipx or pip) when a newer version is out. Development
and editable installs skip it; set `PCODE_NO_UPDATE_CHECK=1` to turn it off.

`PCODE_CONFIG_DIR` overrides the user config directory for preferences, extensions,
MCP configuration, keybindings, worktree setup, and stored logins. Otherwise pcode uses
`$XDG_CONFIG_HOME/pcode`, defaulting to `~/.config/pcode`. Per-file overrides
(`PCODE_CODEX_CREDENTIALS_FILE`, `PCODE_MCP_CONFIG`)
take precedence.
