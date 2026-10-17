# The transcript

The conversation lives in your terminal's normal scrollback, so you can scroll,
select, and search it with the terminal or tmux. This page covers what gets
written there and the settings that control it.

Three kinds of text share the scrollback. Your prompts are quoted behind a `▌`
rail in the accent color. The model's replies are plain prose in the terminal's
own text color. pcode's own notes (a model switch, an MCP server coming up, the
key hints under `/help`, where a session was saved) start with an accent `·` and are
set in the muted italic shade, so a run of them between two replies reads as
status rather than as something the model said. A note that wraps or spans
several lines hangs under its text, with the mark on the first line only.

## Command previews

Shell tool calls show a compact two-row preview with their result and duration.
Long arguments and embedded scripts are abbreviated; short commands stay
readable. Previews are stripped of terminal control sequences.
Failure excerpts stay visible. To see a command's full output, use `/tools`, or
set [`show_commands`](#command-output-in-scrollback) to `scrollback` to mirror it into
scrollback.

## Edit diffs and streaming previews

Completed `edit_file` and `write_file` calls leave a compact unified diff in
scrollback by default. The diff compares the contents the operation used, not
Git's working tree, so it doesn't fold in earlier changes of yours. New files are
marked as created; unchanged files have no patch. Failed calls aren't shown as
successful edits.

With `live_edits` on (off by default), a preview shows the proposed
replacement or file content while the model is still generating a file-tool
call, labeled **not applied**. It shows only complete lines. It shares the live
panel's height budget (`command_preview_lines`) and disappears on execution,
cancellation, or failure; it is never saved as an applied change. Providers
that send arguments all at once may skip this phase. `/config set live_edits
on` turns it on at once; `/show-edits off` hides it along with completed diffs.

```text
/show-edits off   Hide edit blocks and previews, and redraw retained scrollback
/show-edits on    Show them again, including previously hidden completed diffs
/show-edits       Toggle visibility (also Ctrl+V)
```

The choice is saved for the next launch; `pcode config set show_edits on|off`
sets it from the shell. Like `/redraw`, toggling rebuilds terminal history. It
doesn't rerun tools or change files.

Completed diffs are saved with the session and come back on resume, even if the
files have since changed. Hiding diffs doesn't delete them. Sessions saved
before diffs were captured show their transcript without reconstructed diffs.

Limits:

- Terminal controls are stripped. Nothing else is hidden: a diff shows what the
  model wrote, credentials included.
- Files over 256 Ki characters or 4,000 lines aren't diffed.
- A saved diff is capped at 400 patch lines / 64 Ki characters, and each block
  on screen shows at most 60 wrapped rows, with an omission marker.
- Binary, unreadable, and too-large files get an "unavailable" notice instead of
  a misleading patch.
- These diffs aren't guaranteed to apply as patches. Changes made by shell
  commands, formatters, or other tools aren't captured.

### Diffs with delta

When [delta](https://dandavison.github.io/delta/) is on your `PATH`, edit
blocks, the live preview of an edit being written, `/tools` details, and `/diffs` use
it: syntax-highlighted code and word-level changes, with an optional
side-by-side layout. The Homebrew formula installs it for you. Without
it, or if delta exits with an error, pcode falls back to its built-in Rich
diffs; `diff_renderer rich` always uses them.

pcode ignores your git config's `[delta]` section and delta's environment
variables (`DELTA_FEATURES`, `BAT_THEME`), so these diffs look the same however
`git diff` is set up. `delta_args` is the one place to change them:

```text
/config set delta_args "--line-numbers --syntax-theme Dracula"
```

pcode itself only sets the width, dark or light (from your pcode theme), no
pager, no file or hunk headers (the block heading already names the file),
the layout, and syntax highlighting on removed lines as well as added lines.
The default background colors are unchanged. A flag in `delta_args` replaces
pcode's choice of that flag, so
`--side-by-side` gives the side-by-side layout at every width and
`--width=variable` stops backgrounds at the end of the text. Any
`--hunk-header-style` brings the hunk headers back. Diffs are unified (inline)
at every width by default. `diff_layout side-by-side` always shows them side by
side, and `diff_layout auto` does so only for a diff 180 columns or wider,
keeping narrower ones unified; the `/diffs` pane is four columns narrower than
the terminal. Settings apply on the next launch.

Each hunk drops the indentation all of its lines share, so a change deep in a
nested block starts at the left edge instead of several levels in. The lines
keep their indentation relative to each other, and line numbers stay correct.
This applies to edit blocks and `/diffs` with delta or Rich, but not to the
live preview, whose lines arrive one at a time. `diff_dedent off` shows the
file's own indentation.

A `--features NAME` that names a `[delta "NAME"]` section of your git config
finds nothing here; put that section's settings in `delta_args` as flags.

The live preview has no line numbers to show, since the edit hasn't been
applied yet, so it leaves out delta's hunk headers. New lines still appear as
they're written, in delta's layout and colored as added or removed, and
delta's syntax highlighting fills them in a moment later. Side by side, the
preview waits for delta instead.

## Thinking: activity panel or scrollback

`/show-thinking` picks where the thinking a provider exposes shows up. **Ctrl+T**
opens a chooser: `o` for off, `a` for the activity panel, or `b` for scrollback; Esc
cancels without changing the mode. A bare `/show-thinking` still cycles through
the modes:

| Mode | Shows |
| --- | --- |
| `activity-panel` (default; formerly `status-line`) | The newest thought, faded and marked with `∴`, on up to `thinking_max_lines` (default 10) rows of its own in the [activity panel](commands.md#activity-panel), under the status row |
| `scrollback` | The full thinking, streamed into scrollback in a dim style |
| `off` | Nothing, and pcode asks the provider for nothing extra |

The thinking rows stay through the tool calls that follow a thought, so a
running tool on the status row doesn't hide them, and the last thought usually
explains the call under it. They go as soon as any reply text streams into
scrollback, including a line written before a tool call. A long thought is cut at the end; a short terminal gets fewer rows (at most a quarter of its height).
Where a summary has section titles, as OpenAI's do, the rows show the newest
title rather than the prose under it.

Each mode asks the provider for the text that suits it:

- **Opus 5.5, Sonnet 5.5, Fable 5 and Mythos 5.1 over `anthropic:`**: `activity-panel`
  asks for progress updates, the short notes these models write between tool
  calls for whoever is watching. The row stays empty while the model reasons
  and fills in as it moves between tools. `scrollback` asks for full summaries.
- **Other `anthropic:` models**: summaries. Models older than Opus and Sonnet 5
  only think when asked, so `scrollback` turns their thinking on (more latency
  and tokens) and `activity-panel` leaves them alone.
- **`claude:`**: always summaries. Under its CLI the model writes progress notes
  as ordinary text, which already streams as part of the answer, so summaries
  are what give the row something to show.
- **`openai:` and `openai-responses:`**: each model's most detailed summary, in
  either mode. An API organisation that isn't verified is refused summaries;
  switch to `off` if that happens.
- **`openai-codex:`**: always detailed summaries.

```sh
pcode config set show_thinking scrollback   # Default is activity-panel
```

Switching into or out of `scrollback` saves the default and rebuilds the retained
transcript like `/redraw`, so it also reveals or hides earlier thinking, including text that
arrived while hidden. It works mid-turn, keeps your draft, and doesn't duplicate
answers or tool output. The usual [redraw limits](#regenerating-the-terminal-transcript)
apply, and output redirected to a file or pipe can't be redrawn.

This view shows the readable text the provider actually exposes, which may
itself be a summary. It is **not hidden internal reasoning**. In `scrollback`
mode there is no length or row limit. Sub-agents' thinking isn't shown in the
parent transcript (use `/agents`).

**Privacy:** readable thinking is saved in sessions even while hidden, and
resume restores it. Hiding it isn't redaction or deletion of the terminal, logs,
or session files. Opaque or redacted provider thinking is never printed, though
the model's message history may still carry it for continuation. `--no-save`
turns off session files, not the terminal scrollback. Older sessions saved
without thinking records can't show it retroactively.

Provider behavior:

- Direct Anthropic models: turning the view on also asks for visible thinking
  on the next turn (adaptive thinking where supported, otherwise a 2,048-token
  budget, summarized). This can add latency and token use. Turning it off drops
  that request and restores provider defaults; it doesn't disable reasoning.
  Requests already in flight and the selected effort are unchanged.
- Codex always requests thinking summaries, whatever this setting says.

## Error logs in scrollback

Errors and failed-tool diagnostics appear as code blocks in the active code
theme. Their text stays literal, even if it contains Markdown or backticks. Each
error shows at most **20 wrapped body lines** by default; a longer log keeps its
tail and shows a truncation marker.

```sh
pcode config set error_scrollback_lines 40  # Positive integer; default 20
```

This also works through `/config` and applies on the next launch.

By default a failed tool call doesn't write its diagnostic to scrollback. It
leaves the same compact summary line a successful call does, marked `✗` in the
error color instead of `✓`. Turn on `tool_error_scrollback` to get the full
diagnostic:

```sh
pcode config set tool_error_scrollback on  # Failed-tool diagnostics (default off)
```

Failed commands follow [command output](#command-output-in-scrollback) instead:
with `show_commands` `off` or `activity-panel` they keep only their summary
line; with `scrollback`, they
show that line plus captured output, but the output only appears when
`tool_error_scrollback` is also on. Application errors, warnings, and
cancellation notices are always shown. These display settings don't change what
is saved. Saved command diagnostics are capped at 200 lines / 32,000 characters.

## Delegated work in scrollback

A sub-agent's tool calls are written indented beneath the `delegate_task` row
they belong to, once that delegate finishes, so parallel delegates don't
interleave. Each step is one summary line, commands included: `show_commands`
and `tool_error_scrollback` apply only to the main agent's calls, and a child's
full output stays in `/tools`. If you cancel before a delegate finishes, the
steps it completed are written when the cancellation is reported.

```text
✓ Delegate task  worker · Fix the flaky test → Completed  41.2s
    ✓ Read file  tests/test_api.py → lines 1–80 · 80 lines
    ✓ Run shell · 2.3s · pytest -q tests/test_api.py
```

## Grouping tool calls

With `group_tools` on (the default), a run of consecutive tool calls leaves one line in
scrollback instead of one per call. While the run is going, the status row
counts it at the right (`✓ 7 ✗ 1 tools`). The full line is written once something else
reaches scrollback (the model's reply, a diff, mirrored command output) or the
turn ends. `/tools` still lists every call.

```sh
pcode config set group_tools off  # One line per call instead (default on)
```

```text
✓ 15 ✗ 1 tools · Edit file ✓ 10 · Run shell ✓ 5 ✗ 1
✓ Delegate task  worker · Fix the flaky test → Completed  41.2s
    ✓ 6 tools · Read file ✓ 4 · Run shell ✓ 2
✓ 2 ✗ 1 tools · Read file ✓ 2 · Search code ✗ 1
```

`✓` counts successes and `✗` failures, for the run as a whole at the start of
the line and per tool after it. Failed calls fold into the run too; `/tools
failed` lists just the failures. A delegate keeps its own line, with its
sub-agent's calls grouped the same way beneath it. A run of one call keeps its
usual line, and a background job's exit notice is never folded in.

`/group-tools on`, `/group-tools off`, or bare `/group-tools` (toggle, also
Ctrl+]) switch it for the session, save the default, and rebuild earlier
scrollback to match.

## Command output in scrollback

By default a finished command leaves a compact summary line like any other tool,
with the command inline after the elapsed time. Long lines are cut at the
terminal width rather than wrapped. The captured output is available in `/tools`.

A background job's exit uses the same line with its id after the label:
`✓ Run shell · j12 · exit 0 · 4.1s · make test`. It is written where the model
collected the result with `wait_for_job` or `job_output`, or once the session is
idle if nothing collected it.

`show_commands` picks where the model's shell commands show their output,
the way `/show-thinking` does for thinking:

| Mode | Shows |
| --- | --- |
| `off` (default) | A summary line in scrollback once the command finishes, nothing while it runs |
| `activity-panel` | Its output live in the [activity panel](commands.md#activity-panel) while it runs, then just the summary line |
| `scrollback` (formerly `on`) | The live output, then **every finished shell tool call and its captured output** mirrored into scrollback |

A failed call's output is mirrored only when `tool_error_scrollback` is also on.

```sh
pcode config set show_commands scrollback     # Live output, then mirror commands and output
pcode config set show_commands activity-panel # Live output only, nothing kept
pcode config set command_scrollback_lines 80  # Output rows per block; default 20
pcode config set command_preview_lines 10     # Live output height; default 10, or 0.25 of the screen
pcode config set show_commands off            # Summary lines only (default)
```

Each mirrored block has a heading with a ✓/✗ indicator, the tool label, the job
id, and elapsed time, then the command on a highlighted `$` line, then the
output, literally and with its indentation kept. A short dashed line separates
the command from its output, making multi-line commands (a heredoc, say) easier
to distinguish from what they printed. A plain line closes the block:

```text
✓ Run shell · j7 · 0.4s ───────────────────────────────────
  $ pytest -q
  ┄┄┄┄┄┄┄┄┄┄┄┄
  2 passed in 0.31s
────────────────────────────────────────────────────────────
```

A finished job's `[jN · exit C · elapsed]` line is dropped from the output
because the heading already says it. An unfinished job's marker, which names its
pid and how to get back to it, stays. Process-polling entries without a command
have no `$` line.

Details:

- It covers the `shell` tool, plus legacy `run_command`, `start_command`,
  `check_command`, and `stop_command` entries in older saved sessions. Other
  tools, and [delegated calls](#delegated-work-in-scrollback), keep their
  summary line.
- While a foreground `shell` call runs (in `activity-panel` or `scrollback`
  mode), a live preview above the prompt shows
  its combined stdout/stderr as complete lines arrive. It looks like the settled
  block, with `⟳` and a running elapsed time. It is capped at
  `command_preview_lines` wrapped rows (not counting the command and border
  lines) and uses whatever height is left after the editor, queued prompts, and
  the activity panel's other rows; in a cramped pane it keeps at least a one-line
  tail. With parallel calls, the most recently updated one is shown. It hides
  with the rest of the panel (Ctrl+O).
- The preview shows at most the first 16,000 bytes of output; a capped preview
  is marked, and the rest stays in the command log. Programs that buffer their
  own output must flush it to appear live (for example, `python -u`).
- When the command finishes, the preview disappears and one settled block is
  written, without repeating streamed lines. Previews aren't saved. Background
  `shell` calls return PID/log/status handles instead of streaming.
- When shown, a failed command prints one block with its captured output, or
  the saved diagnostic if there is none.
- Output is sanitized, then trimmed to the last
  `command_scrollback_lines` wrapped rows, with a marker counting what was
  omitted. The command, marker, and borders don't count against the budget, so
  even a budget of 1 keeps the final output row. Capture itself is capped at
  128 KiB.
- Verbose commands can push earlier conversation out of terminal history. Raise
  your terminal or tmux scrollback limit before turning this on.

Press **Ctrl+G** to cycle through the modes; it saves the default, so the next
launch starts the way you left it. `/show-commands off|activity-panel|scrollback`
picks one, and a bare `/show-commands` cycles (`on` still means `scrollback`).
Switching mirroring on or off rebuilds retained scrollback right away:
`scrollback` reveals earlier commands and their output, the other modes remove
every command block, failures included. Nothing is rerun.

These settings also work through `/config` and apply on the next launch.

## Paced scrollback

The model's reply reaches scrollback one finished Markdown block at a time, so
without pacing a whole paragraph would appear at once after a pause. By default
(`typed`), finished prose (paragraphs, lists, headings, quotes, and thinking) is
typed out a few characters per frame on a live row, and each row moves to
scrollback once complete. The text is already rendered, so nothing reflows while
it types: line breaks and styling are final from the first character, and
indentation appears at once. Code blocks, tables, rules, and tool output roll in
a row per frame instead, since half a code line or table border reads badly.

Typing runs at about 360 characters a second, close to a model's own pace. When
a burst arrives faster than that, typing speeds up to finish it in about two
seconds. Rows rolled in whole go one per frame, sped up to finish in about a
second. The two queue behind each other, so prose followed by a long code block
can take about three seconds to land. Opening a popup or ending the session
writes whatever is left at once, and a redraw or resize rebuild always lands
whole. Pacing changes only how text appears, not what is written or retained.

```sh
pcode config set paced_scrollback rows  # Roll every block in by row
pcode config set paced_scrollback off   # Write every block at once
```

The default is `typed`; changes apply on the next launch. An older saved `on`
falls back to the default.

## Regenerating the terminal transcript

`/redraw` rebuilds the retained transcript at the current terminal width with
the current display settings. Ctrl+G, Ctrl+V, Ctrl+], `/show-commands`,
`/show-edits`, `/show-thinking`, `/group-tools`, `/theme`, and `/syntax` rebuild it the same
way. Your draft, the live tool panel, and unfinished model text are kept; a
rebuild never calls tools or changes model history.

The transcript also rebuilds automatically once a terminal resize settles,
including height changes, so live preview fragments don't linger in scrollback
after the pane shrinks. It waits for a drag to finish rather than rebuilding at
every intermediate size. To turn it off:

```sh
pcode config set regenerate_on_resize off  # Default on; applies on next launch
```

With it off, the editor still resizes normally, completed output is left for
the terminal to reflow, and `/redraw` is still available.

Closing a full-screen popup such as `/tree`, `/links`, `/model`, `/resume`,
`/status`, `/tools`, or `/diffs` (including with Escape) also rebuilds the
transcript, which restores the conversation if the terminal lost its screen
contents. Your draft is kept. This happens even with `regenerate_on_resize` off.

**Terminal-history warning:** a rebuild clears the terminal's screen and
scrollback, including shell output from before pcode started, then redraws only
the transcript this pcode process retained. Terminals that ignore the
clear-scrollback escape sequence may keep older copies in history. Output
redirected to a file or pipe isn't cleared or redrawn; there, resume prints the
retained transcript once, as plain text.

Resume (`--continue` or `/resume`) loads the selected conversation from disk,
then redraws. Switching branches with `/tree` replaces the displayed history
rather than appending to it. Thinking, edits, and command results are retained
even while hidden, so visibility toggles work on resumed history too. Tools are
never re-executed.

One setting caps how much text is retained for both redraw and resume:

```sh
pcode config set transcript_max_chars 2000000  # Default; applies on next launch
```

The budget counts characters of retained text, not rendered lines, bytes, or
model tokens. The oldest entries are dropped first; the newest entry is always
kept, even if it alone exceeds the budget. The number of entries is also capped
at one per 100 budget characters (20,000 at the default), so many tiny writes
can't pile up. When older history has been dropped, a redraw says so. Saved
sessions, model context, and diagnostics are unaffected.
