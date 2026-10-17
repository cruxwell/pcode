# Context, limits and caching

## Where the fixed prompt goes

`/status` breaks down the **prompt overhead**: the instructions and tool
definitions sent with every request, before any conversation. Use it to see why
the footer's token count is as large as it is.

```
Prompt overhead           ~7.4k tokens · 3% of 200k · estimated
  Instructions            ~4.3k
    ~/AGENTS.md           ~2.8k · instructions
    AGENTS.md             ~675 · instructions
    Harness base prompts  ~228
    Planning tool         ~162
    Assistant config      ~105 · paths only · 1 skill
    File tools            ~102
    Sub-agents            ~90
    Web research          ~74
    Tool output limits    ~60
    Terminal instructions ~15
  Tool schemas            ~3.2k · 16 tools
    Largest               write_plan 625 · grep 314 · edit_file 310 · shell 259
```

Each instruction file gets its own row, so a global `AGENTS.md` that costs more
than everything else combined stands out. `Assistant config` lists discovered
skills: **a skill costs its path, not its body**, because the model reads
`SKILL.md` only when the skill runs. See
[Skills as slash commands](workspace.md#skills-as-slash-commands).

The rows describe what the last request actually sent, so before the first
request there is nothing to show. Counts are estimates (about 4 characters per
token), the same ones compaction uses, not exact provider counts.

## Context compaction

Compaction replaces older conversation history with a summary so a long session
fits in the model's context window. `/compact` asks the current model for the
summary, with no tools; optional instructions add focus to the standard summary:

```text
/compact
/compact Preserve auth debugging findings, exact file paths, and failing tests
/autocompact on
/autocompact off
/autocompact 200k
/autocompact auto
```

The summary keeps goals and constraints, decisions, current state, exact
artifacts, verification results, and next steps or blockers. Recent messages
stay verbatim (up to about 20k tokens, less for smaller windows or caps), and compacting
again updates the previous summary. Summaries are lossy, but earlier tool results
stay reachable through the session history and spill handles, so the model can
re-read output or source files when exact details matter. Old tool results are
never cleared before compaction runs.

Manual compaction needs an idle live session. Ctrl+C cancels it; queued prompts
wait for it and are cleared if it is cancelled or fails. A short history is left
alone, and a summary that is empty, invalid, or no smaller is rejected without
changing anything. The result shows estimated before and after tokens, and the
context indicator shows `~` until the next response gives a measured count.
Summary requests count toward session usage totals, not the turn count.

Each manual compaction adds a `/tree` checkpoint on the current branch, saved
immediately (in memory for unsaved sessions). Earlier checkpoints, branches, plan
state, transcript, and tool records are kept, and returning to a compaction
checkpoint never runs a model or replays tools. Compaction does not delete or
redact the saved conversation.

Automatic compaction is **on by default**; `/autocompact off` saves that choice
in `~/.config/pcode/preferences.json` (or `$XDG_CONFIG_HOME/pcode/preferences.json`).
pcode checks before every model request, including within a turn, and compacts
at about 90% of the context window, leaving more room on small windows or with a
large output limit. Automatic summaries are saved as checkpoints before the next
request. If compaction cannot free enough room, the run stops with an error
rather than dropping history or replaying tools. A provider's context-overflow
error is not retried automatically.

To compact sooner, for example to keep a 1m-token model's requests cheaper and
faster, set a cap with `/autocompact 200k` (also `200000` or `1.5m`; minimum 50k,
since the fixed prompt plus the kept history and summary need room). Compaction
then fires at whichever comes first, the cap or the usual threshold, and when the
cap is the lower of the two the footer shows usage against it (`85k/200k`).
`/autocompact auto` removes the cap. Like on and off, the cap is saved and works
mid-turn: it applies from the running turn's next model request.

The window comes from the model catalog. For a model with no known window,
automatic compaction is skipped, and turning it on interactively requires a known
window or an override. For a custom proxy, a gated window, or a wrong catalog
entry, set the real limit:

```sh
PCODE_CONTEXT_WINDOW=128000 pcode --model your-provider:your-model
```

The override applies to the status line and compaction for the whole process,
including after model switches, so update it if you change deployments. It
cannot exceed the provider's known limit. Codex uses a larger window only when
its own metadata advertises one. If the history is too large even for the
summary request, pcode retries with less detail; if that also fails, the history
is left unchanged.

With automatic compaction off, run `/compact` before the window fills, or `/new`
for unrelated work.

## Images

An image reaches the model when it opens an image file with
[`read_file`](tools.md#images), or when your prompt names one
([images in a prompt](commands.md#images-in-a-prompt)). Whether it can see one
comes from the model
catalog, the same source as the context window, with Codex's and Anthropic's own
model metadata taking precedence. Claude Code (`claude:`) models always count as
seeing, including aliases such as `claude:opus`.

A model that cannot see an image is never sent one. It gets a note in its place,
naming the image and saying why it was left out, so reading a screenshot never
ends a turn. The conversation keeps the picture: switch to a model that can see
(`/model`) and the next request carries it again.

For a model the catalog does not list (a custom endpoint, a model newer than the
catalog), the note says its image support is unknown. Set it yourself:

```sh
PCODE_IMAGE_INPUT=on pcode --model your-provider:your-vision-model
```

`off` does the opposite: no images, even to a model that takes them.

Every request sends the whole conversation again, so images add up. A request
carries at most 20 images and 12 MB of them, which fits the strictest provider
limit (Gemini's 20 MB of inline data per request). Past that the oldest are left
out ten at a time, as notes that tell the model to read the file again if it
needs one. Leaving them out in batches keeps the provider's prompt cache: the
images a request carries change once every ten new ones, not with each. Without
the cap a long session of screenshots would grow past what the provider accepts,
and so would the request `/compact` sends to shrink it.

## Model output limits

Anthropic models need a maximum response length, which covers thinking and
tool-call arguments too. pcode sets it from the model's advertised maximum on
each request, including for delegated agents and after model switches. When no
limit is known it uses 16,384 tokens. An explicit model setting wins, and other
providers keep their own defaults.

This is a ceiling, not a target length or a reasoning budget, and thinking and
effort settings do not change it. Compaction summaries use their own smaller
limit. Automatic compaction leaves room for the ceiling, but never more than
half the window, so small `PCODE_CONTEXT_WINDOW` overrides stay usable. A
response cut off at the limit is not retried.

## Tool output limits

Large tool results are cut down **once, before they enter the model's history**.
By default a result of 10,000 characters or more is saved to disk, and the model
gets a handle plus a 1,000-character preview of its start and end. Smaller
results pass through unchanged. This applies to every tool, including web, MCP,
and delegation results, for the main agent and workers. It makes no extra model
calls and works with automatic compaction off.

```sh
pcode config set tool_output_mode spill          # Default: store, preview, read back
pcode config set tool_output_threshold 8000      # Trigger at 8,000 characters
pcode config set tool_output_preview_chars 800   # Content preview, excluding headers
pcode config set tool_output_max_chars 3000      # Fallback if storing fails
pcode config set tool_output_strategy head_tail  # Truncation keeps both ends
pcode config set tool_output_retention_hours 168 # Optional: prune spills older than a week

pcode config set tool_output_mode truncate       # Lossy, no new spill files
pcode config set tool_output_mode off            # No new result reduction
pcode config unset tool_output_mode              # Restore default spill mode
```

- These settings also work through `/config`, with tab completion and validation.
  Restart pcode to apply them to an existing conversation. They do not change
  results already in history.
- All budgets are characters, not tokens. Keep the preview and truncation budgets
  below the threshold to save context.
- Spill previews always show both ends; `tool_output_strategy` applies only to
  `truncate` mode and to the fallback when a spill cannot be saved.

The model reads spilled output back with `read_tool_result`, by line range or
matching text, up to 1,000 lines or 50,000 characters per call. For a single
line longer than that, it is told how to read a character range with the shell.
Readback keeps working in `off` and `truncate` modes, so handles in a resumed
session still work.

Spills live in `$XDG_STATE_HOME/pcode/tool-results` (default
`~/.local/state/pcode/tool-results`), an owner-only directory shared by all
workspaces. They hold **raw tool output**, not the sanitized version the terminal
shows, and are written even with `--no-save`. This is plain local storage, not
encrypted or isolated. `truncate` or `off` stops new spill files but does not
delete existing spills, sessions, or shell logs.

Spills are kept forever by default. A nonzero `tool_output_retention_hours`
prunes older spills in the background as new ones are written, by modification
time. A pruned or deleted spill breaks its handle, and the model is told to rerun
the original tool. Set retention back to `0` to stop pruning.

Spilling only keeps what the tool returned. File-read paging and the shell's
16 KB output tail still apply, even in `off` mode; full command output stays in
the shell log. The shell's process ID, log path, and status always survive
reduction. There is no per-tool configuration.

## Prompt cache notices

When a request reuses much less of the prompt cache than an earlier one had
built up, pcode adds a muted note near the end of the footer under the editor,
just before the keybindings hint, kept until your next prompt:

```text
~/p/pcode@main · steering · claude:claude-opus-5-5 (high) · 92k/1m · cache miss 0/166k · Ctrl+/ Keys
```

That reads "reused 0 of about 166k cached tokens"; `cache drop 41k/166k` means
some was reused. A sub-agent's drop is labeled `sub-agent cache …`. A narrow
pane cuts the footer from the right, so only the keybindings hint is cut before
this note. It never goes into
the scrollback.

The full notice is saved with the session (in `transcript.jsonl` in its session
directory), where later debugging can read it:

```text
Prompt cache: request 1 reused 0 of ~48,210 tokens cached in an earlier turn (anthropic/claude-sonnet-4-5).
```

This is information, not an error, and it does not interrupt the run. Expect one
after `/compact` (the summary replaces the cached history), after enabling an MCP
server or extension (the tool list changes), or when you come back after the
provider's cache has expired. The notice reports what was measured, not a guessed
cause.

A notice appears when cache reads fall below half of an earlier cached prefix of
at least 1,024 tokens. The first request of a turn is compared with the previous
turn only if the conversation was idle less than the provider's cache lifetime
(about 5 minutes); after that the cache is gone anyway. A sustained drop is
reported once until reads recover. Notices never include prompt text.

Notices are **on by default**, for the main agent and sub-agents. Turn them off
with `cache_notices`, then restart pcode or use `/reload`:

```sh
pcode config set cache_notices off   # Or /config set cache_notices off inside pcode
pcode config set cache_notices on    # Default
```

Turning them off does not remove notices already in a saved session.

When a notice follows another request in the same turn, the saved notice adds a
line comparing the two, such as `Tool definitions changed (added …)`,
`Request fingerprints unchanged; 2 messages appended (~10s gap), cause unknown.`
or `Message N of M changed`. With `debug` on
(`pcode config set debug on`), pcode also writes the recent requests' fingerprints
to `~/.local/state/pcode/cache-diagnostics/` (`XDG_STATE_HOME` is honored) and
the notice names the file:

```text
Prompt cache: request 14 reused 3,712 of ~10,752 previously cached tokens (provider/model).
Request fingerprints unchanged; 2 messages appended (~10s gap), cause unknown.
Request fingerprints: ~/.local/state/pcode/cache-diagnostics/20260919T035812-4821-step14.json
```

The files contain sizes, digests, and token counts, never prompt text. Set
`PCODE_CACHE_DIAGNOSTICS=off` to skip them even with `debug` on, or to a
directory to write them there; the notice itself is unaffected. From a source
checkout, `make cache-report` summarizes cache performance across saved sessions.
[Prompt caching](https://github.com/cruxwell/pcode/blob/master/dev/prompt-caching.md#reading-a-cache-notice)
explains how to read the comparison and the files.
