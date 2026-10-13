# Sessions and recovery

Live conversations save automatically when the first model prompt is submitted.
Opening the app, using commands, or quitting without a prompt creates no session.

## Background sessions

A session starts inside its terminal: one process, quick to start, gone when
you quit. When you want it to outlive the terminal, `/detach` moves it into a
*session host*: a headless pcode process that owns the conversation while the
terminal only draws it. The terminal stays on it, and from then on it can
leave, switch to another session, or close, and the work carries on.

- `/detach` moves the conversation, its worktree and its running background
  commands (they keep their IDs). It refuses while a turn, command or side
  question is in progress, which can't move: let it finish or cancel it first.
  A session needs its first prompt (which saves it) before it can move.
  Anything you type while it moves goes to the host once it is there.
- Once it is in a host, `/detach` again (or closing the terminal) quits and
  leaves the host running for `pcode --attach`.
- `pcode --host` (or `session_host on`) starts every session in a host instead,
  so any turn can be walked away from. `--print` runs in-process either way
  unless it is given `--attach` (see
  [Scripting a running host](#scripting-a-running-host)).

```sh
pcode                            # start a session in this terminal
pcode --host                     # start it in a background host instead
pcode --hosts                    # list running hosts (--json for scripts)
pcode --attach                   # reattach to the newest host in this repository
pcode --attach 3f9c              # ...or to one by host or session ID prefix
pcode --kill-hosts idle          # stop hosts with nothing running (or: stale, all)
```

`--kill-hosts idle` is for the hosts a forgotten terminal tab keeps alive: an
attached terminal keeps a host from [stopping itself](#idle-hosts-stop). It
stops every host that has nothing running, terminal attached or not, the
same way the host would have stopped itself with no terminal: a worktree with
unmerged work is kept (an untouched one is removed) and a reply you haven't
read stays in `/switch`. A host
running a turn, a command, a side question or a background command is skipped
and listed with the reason. A terminal still showing a stopped host says so and
prints the `pcode --continue` command that resumes it. `stale` stops hosts
still running older pcode code, and `all` stops every host.

Inside a hosted session:

- `/switch` opens a picker over every running host, with what each one is doing,
  plus [stopped sessions](#idle-hosts-stop) with a reply you haven't read.
  Enter shows that session in this terminal (resuming a stopped one); Ctrl+N
  starts a new one; Ctrl+X (or Delete in the list), pressed twice, stops one
  or drops a stopped one from the list. A turn you switch away from keeps
  running. Both are [shortcuts](commands.md#shortcut-prefix).
- `/switch HOST` goes straight to one by host or session ID prefix, and
  `/switch -` (or Ctrl+^) back to the
  one this terminal showed before. Pressed again, it flips back.
- `/switch new` starts a new session and switches to it. `/switch new PROMPT`
  starts one working on PROMPT and leaves it in the background; this terminal
  stays where it is.
- `/resume` resumes a saved conversation in a host of its own, or shows it where
  it is already running.
- `/restart` restarts this session's host on the pcode installed now and picks the
  conversation up again in the same worktree. A host keeps the code it started
  with, so this is how a long-running session gets a fix you just merged; the
  picker and `--hosts` mark hosts that are on older code.
- Quitting (Ctrl+D, `/quit`, or `/stop`) ends this session's host, asking about
  the worktree the way a local exit does. `/detach` quits but leaves the host
  running for `pcode --attach`, as closing the terminal window does, until it
  [goes idle](#idle-hosts-stop).

From a session running in its terminal, `/switch` works too, but showing
another session ends this one (so it refuses while a turn is in progress);
`/resume` and `pcode --continue` bring it back.

### Scripting a running host

`--attach` with `--print` sends one message or command to a running host
without opening the editor, then detaches. The host keeps running until it
[goes idle](#idle-hosts-stop), and any terminal attached to it sees the turn as
usual. Once it has stopped, `--attach` with the session's ID (or a prefix)
carries the conversation on instead.

```sh
pcode --attach 3f9c -p "Run the tests and summarize failures"   # reply on stdout
pcode --attach 3f9c -p /compact                                  # a session command
pcode --attach 3f9c -p /stop                                     # end the host
```

`--attach` takes an optional HOST, so it swallows the word after it: write
`pcode --attach -p "..."` or `pcode --attach 3f9c "..." -p`, never
`pcode -p --attach "..."`, which reads the message as a host ID.

- A message waits behind whatever the host is already doing, then runs as its
  own turn, never mixed into one already running. That turn is printed: the
  reply on stdout, tools and notes on stderr, as for a local `--print` (notes
  another terminal causes meanwhile appear there too). The exit status says
  whether the turn succeeded. It fails at once if the message is dropped
  before running, because the host's queue was cleared (a Ctrl+C in another
  terminal, or a turn ahead of it failing).
- The message is always sent to the model: a leading `!` does not make it a
  shell command, as it would typed into an attached terminal.
- A session command (`/compact`, `/effort high`, `/model NAME`, ...) runs in the
  host, and pcode exits once it has, including compaction or MCP work it started.
  It exits non-zero if the command did not run (its queue was cleared) or
  reported an error or a warning, such as `/compact` refused while a turn runs.
  Anything else the host reports meanwhile counts too. A command that opens a
  picker (a bare `/model`) fails, since nobody is there to pick. Commands that
  queue a turn of their own (a skill, `/resend`) exit without waiting for it. A
  command that ends the session (`/worktree finish`) succeeds; a host stopped
  under a command by anything else is a failure.
- `/stop` ends the host, which tidies its own worktree the way `worktree_exit`
  says, without asking, since nobody is there to be asked. Its notes about that
  go to the host's log. Other terminal commands (`/tree`, `/switch`, ...) need
  the editor and are refused.
- Ctrl+C only detaches (exit status 130): the message's turn keeps running, or
  stays queued, in the host. Stop it from an attached terminal, where Ctrl+C
  also clears the host's queue.
- The session's own settings apply: `-m`, `--no-save`, `--worktree`, and
  `--continue` are ignored. (`pcode --continue ID -p ...` without `--attach`
  still runs a copy of the session locally, even while a host runs it.)
- The host must be running this version of pcode or later. An older one
  refuses with a message saying so, and `/restart` in an attached terminal
  updates it.

Other sessions stay out of this terminal: the footer says nothing about them,
and their turns never write into this transcript. The picker (`/switch`) lists
them, newest and unseen first. When one finishes a turn a desktop
notification goes out, whether or not a terminal is showing it: pcode asks the
terminal to raise it (OSC 9, which Ghostty shows by default), once per turn however many
terminals are open; `pcode config set desktop_notifications off` turns it off.
While a turn runs, the tab also shows a
[progress bar](terminal.md#progress-bar).

### Idle hosts stop

A host only runs while it has something to do: a terminal attached, a turn
running or queued, a slash command, compaction or MCP work, a running command,
or a side question. Fifteen seconds after the last of those ends it stops
itself, so a session you sent off in the background closes soon after its turn
finishes. The fifteen seconds cover switching away and straight back, and
`/switch -` to a session stopped since resumes it. A session stopped this way
keeps unmerged commits in its worktree, even under `worktree_exit merge`, ready
to be resumed.

Nothing is lost. If that last turn finished with no terminal watching, `/switch`
keeps listing the session, marked `stopped`, until you open it: Enter resumes it
in a new host on the pcode installed now. `/resume` or `pcode --continue` brings
any conversation back.

`session_host_idle_minutes` makes hosts wait longer: a number of idle minutes,
or `off` to never stop on their own.

### Host processes

Each session has its own host process, started by the terminal that asked for
it, so it inherits that terminal's environment (direnv credentials, `PATH`, tool
versions) exactly as a local session would. One session crashing or hanging
does not affect the others. Switching to a session mid-turn picks the turn up
where it is, streaming text and running commands included.

While the terminal waits on a host (starting up, connecting, finishing a slash
command, or loading the `/resume` list), a `◈` spinner row above the editor
names the wait and counts the seconds. Waits under a quarter of a second show
nothing.

With the `worktree` setting on, a new host makes its own worktree like a local
session, and `/switch new` starts from the main checkout so the new session
never shares yours. A host tidies its worktree when it stops, without asking:
unmerged work is kept with a note in the host's log.

Host sockets, status files, and logs live in `~/.local/state/pcode/hosts/`.
`PCODE_HOST_DIR` overrides it; keep that path short, since a Unix socket path is
limited to about 100 bytes.

### What works in a hosted session

Everything. The host runs the session's commands (`/model`, `/effort`, `/compact`,
`/resend`, `/new`, `/tree`, `/btw`, `/mcp`, `/jobs`, `/worktree`, `/login`,
`/reload`, skills, and extension commands), and opens their pickers in the
terminal that typed them. The terminal runs its own (`/switch`, `/resume`,
`/status`, `/tools`, `/diffs`, `/links`, `/agents`, `/help`, `/config`, and the
display commands).
MCP sign-ins that need a browser open it from the host, on the same machine.

A host started by an older pcode keeps running that code until it stops. A
terminal on a different protocol version is refused with a message saying so.

## Resuming

```sh
pcode --sessions
pcode --continue                             # this directory's newest session
pcode --continue SESSION_ID
pcode --continue SESSION_ID --fork           # branch off a copy, keep the original
pcode -m openai-codex:gpt-5.6-sol --no-save  # opt out for a sensitive session
```

`-c` / `--continue` restores the saved model, workspace, and message history. It
accepts an unambiguous ID prefix (at least 8 characters); without an ID it picks
the newest session whose workspace is the current directory (or `-C`), not the
newest overall. It redraws the saved transcript (up to `transcript_max_chars`,
default 2,000,000 characters), replacing the terminal's screen and scrollback
like `/redraw`, then waits for your next message. It never re-runs tools. See
[transcript regeneration](transcript.md#regenerating-the-terminal-transcript).

A different explicit `-m` is rejected on resume, as is a `-C` in another
repository; `-C` pointing at another worktree of the same repository is fine and
the session goes back to its own directory. Continuing a session that another
process already has open
[continues a copy](#continuing-a-session-that-is-open-elsewhere).

`/resume` opens a full-screen browser of saved conversations in the current
repository and its linked worktrees (or the exact workspace outside Git), newest
first and labeled by date, short session ID, name, and first prompt, with the
selected session's prompts, responses, and tool calls alongside.

- It opens in the search line: typing searches prompts, responses, and tool calls
  (file paths, shell commands) across sessions. A session is listed when every
  space-separated word appears somewhere in it, not necessarily in one turn; the
  pane shows the turns holding any of the words, marks each match, and scrolls to
  the first. The best matches come first: a session whose name, title or ID the
  query names, then one with every word in a single turn, then the one with more
  matching turns, and otherwise the newest.
- A session ID prefix of at least four characters, the full name of the `pcode-*`
  worktree it ran in, or a word of its name or [title](#session-titles) (or the
  start of one, from three letters) finds that session with every turn.
  `/rename NAME` names the current session (`/rename -` clears it).
- ↑/↓ move the selection while you type (Ctrl+U/Ctrl+D by half a page).
- Tab moves to the session list, and Tab again to the content pane, where arrows
  scroll by line, PageUp/PageDown by page, and Ctrl+U/Ctrl+D by half a page.
- From anywhere, Ctrl+F returns to the search, Ctrl+R narrows it to prompts only,
  and Ctrl+G includes every workspace (these are
  [shortcuts](commands.md#shortcut-prefix)).
- Enter resumes the selected session in place, restoring its model, history, and
  plan. Esc cancels.
- Ctrl+X (or Delete in the session list), pressed twice, permanently deletes the
  selected session. The active session and one open in another process are
  refused.

A session from another worktree of the same repository switches the workspace to
that worktree: file tools, the shell, extensions, and skill commands move there,
and the worktree being left is tidied as on exit (an untouched `pcode-` worktree
is removed; unmerged work is kept with a note). Sessions from another repository
are refused.

### Session titles

When a new session's first turn starts, pcode asks the session's own model for a
short title, in the background and at low effort, so it usually arrives while
the turn is still running. `/resume` lists it before the
first prompt and finds the session by its words, `/switch` lists it in place of
the first prompt, and the terminal [tab](terminal.md#tab-and-pane-titles) shows it.
Between turns it also heads the editor box (`┌─ Fix the flaky login test ──┐`,
or `┌─ Tasks 3/5 · Fix the flaky login test ──┐` above an attached task list), so coming back
to a pane tells you what it was about; while a turn runs, the
status row takes that border, and a session with no title or name yet keeps a
plain rule.
`/rename NAME` replaces it everywhere, and `/rename -` goes back to the title.

The request carries only your first message, not the conversation or the
reply, so it costs well under a thousand tokens. If the request fails, nothing
is shown and the session lists by its first prompt as before; pcode asks again
the next time the session is opened, not on every turn. Sessions from before
titles existed get one when their next turn starts.
`pcode config set session_naming off` turns titles off.

### Continuing a session that is open elsewhere

`--continue` or `/resume` on a session that another pcode process has open,
even one in the middle of a turn, continues a copy of it instead of refusing.
The copy is a new session with its own ID, and the transcript opens with a note
naming the original. The original is only read, never written, and keeps
running undisturbed.

A turn still running in the original is copied the way a crash would leave it:
the copy picks up from that turn's last settled step (or from the turn before,
if it has not settled one yet). `/resend` carries the turn on from there, a new
message starts from the same point, and `/tree` can go back to the last
finished turn instead.

The copy works in the same directory as the original, so if both edit files
their changes land in one checkout. Quitting either one does not remove or
merge a shared `pcode-` worktree while the other is still open in it, or has
turns it could be resumed with there.

A session still running in a [background host](#background-sessions) is not
copied: `--continue` and `/resume` show it where it runs instead.

### Forking a session on purpose

`pcode --continue SESSION --fork` always continues a copy, whether or not the
session is open anywhere, including one running in a background host. Use it to
try a different direction from the same history while keeping the original
conversation exactly as it was. The copy gets its own ID and works in the same
directory as the original, as above. To branch from an earlier point inside one
session instead, use [`/tree`](conversation-tree.md).

### When the workspace was deleted

A session worktree can be removed by something other than the session that owns
it: a sibling session merging and removing it, `/worktree clean`, or `git
worktree prune`. The conversation is still resumable. `--continue` then
continues in an explicit `-C DIR` of the same repository, or else in the
checkout the worktree was made from, and says on stderr which directory it
switched to; `/resume` continues in the workspace you are already in. Only a
session whose repository is also gone is refused, and the message names the
directory it was looking for.

A workspace that disappears *during* a session is not recoverable in place: the
shell and file tools resolve every path against it, so the next tool call stops
the turn with a message naming the deleted directory, rather than retrying a
command that cannot succeed. Quit and continue the session elsewhere.

## Recalling earlier sessions

The model can look things up in your saved sessions without resuming them, so
you can ask "What did we decide about editor flicker?" or "Did we already try
bumping the timeout?" It searches, then reads the matching turns, and cites the
session and turn it found them in. This is the bundled `session_history`
extension.

By default it searches the current repository, including its linked worktrees
(even ones since deleted). Ask about "this directory only" to narrow it, or
"across all my projects" to widen it; it only searches other projects when you
ask. It can also search the current conversation, which recovers details that
`/compact` or automatic compaction dropped from the model's context.

What it can find:

- Saved prompts, steering messages you sent mid-turn, the model's replies, and
  tool summaries and commands.
- Not full tool output, reasoning, or conversations run with `--no-save`.

Results from abandoned `/tree` branches and failed attempts are labeled as
such, and the model is told to treat past claims as evidence, not proof that a
change shipped. Very large histories are searched in stages, so the model may
need several searches to cover everything.

### Semantic search (opt-in)

Search is keyword-based by default, runs locally, and needs no credentials or
network. To add embedding-based matching, set a model before launching:

```sh
PCODE_HISTORY_EMBEDDING_MODEL=openai:text-embedding-3-small pcode
```

This sends redacted excerpts of your history and the search queries to that
provider, using its normal credentials. Redaction is best-effort, so do not
enable a hosted model for history you cannot send off-machine. Local embedding
models need their provider's optional dependencies. No model is chosen for you,
and if the provider fails, search falls back to keywords with a warning.

Vectors are cached in `.history-embeddings.sqlite3` in the session directory
(mode 0600). It holds content hashes and vectors, not transcript text, but
vectors are still sensitive. Old vectors stay after a session is deleted; delete
the file with pcode stopped to clear the cache (also needed if a provider changes
a model's vector size under the same name).

To turn recall off, create `~/.config/pcode/extensions/session_history.py` with
`def setup(pcode): pass`, then `/reload`. Recall uses the same session storage
location as the rest of pcode.

## Where sessions are stored

Default location: `$XDG_STATE_HOME/pcode/sessions`, or
`~/.local/state/pcode/sessions`. Override with `--session-dir PATH` or
`PCODE_SESSION_DIR`. Each session directory contains:

- `session.json`: model, workspace, timestamps, completed-turn usage, and package versions.
- `steps.sqlite3`: full message snapshots (including tool arguments and results
  and provider reasoning metadata) used for resume, and a record of tool effects.
- `transcript.jsonl`: submitted prompts, streamed text, tool summaries, bounded
  tool arguments and results, and failure details (HTTP status, provider
  code and message).
- `errors.log`: tracebacks from failed turns and side questions.

A failed turn also records the configured model's provider and base URL (without
userinfo, query, or fragment) in the transcript and `errors.log`. That is the
configured route, not proof of which endpoint failed inside a delegated run.
Quota exhaustion and rate limits get specific guidance rather than the generic
login/connectivity hint.

Session directories are mode 0700 and data files are 0600. **These files contain
conversation and repository content in plaintext.** They stay outside the repo by
default; do not commit or share them without inspection. HTTP headers, stored
provider credentials, and auth tokens are not deliberately captured, and error
details redact known token formats, credential assignments, and credential
values from the environment, but that is best-effort. Message snapshots are kept
verbatim for replay, so anything sensitive you paste or a tool returns can be
stored. Use `--no-save` when that is inappropriate. Delete a closed session's
directory to remove it; conversations are never deleted for you.

### Disk use

Each turn keeps its two newest checkpoints in `steps.sqlite3`, and every message
is stored once however many checkpoints include it, so a long session grows with
what was said rather than with how many times it was saved. Sessions saved by
older pcode versions kept every step, or a whole copy of the conversation in each
checkpoint, and can reach gigabytes. To shrink them:

```sh
pcode --sessions --compact   # Drop superseded checkpoints, store messages once, report space freed.
```

This rewrites each closed session's store in place, skipping any open in another
process, and reports what it reclaimed. It also clears messages no checkpoint
still uses. Sessions saved or compacted this way can't be resumed by older pcode
versions, so restart any pcode still running an older version first. No conversation is lost: `--continue`,
`/resume`, `/tree` navigation to earlier turns, and recall all work afterwards.

## Checkpoints

A checkpoint is saved after every completed tool call, not just when an answer
succeeds. If the request after a completed tool fails, its result is kept for the
next turn and for resume. Text that was streaming when a turn was interrupted is
kept in the transcript even when it cannot become a checkpoint.

Resume continues from the most recent checkpoint. Tool calls that were still
pending are not replayed, and their unknown outcomes are recorded. Interrupted
tools may already have changed the workspace; resuming does not undo that.
Checkpoints do not restore files, running processes, or in-memory state such as
the planner.

Rewinding to an earlier step *within* a turn is not offered; `/tree` moves
between turns.

## Retries and `/resend`

Dropped provider connections, transport timeouts, and recognized overload errors
inside a provider response stream get three automatic retries by default (four
attempts per submitted turn). The retry picks up from the failed
request's checkpoint, completed tool results included, without adding a
"continue" prompt. Partial output from the failed attempt may stay on screen but
is not sent again. Retries show the failure and attempt count.

```sh
pcode config set retry_attempts 5   # Five extra attempts per turn, next launch
pcode config set retry_attempts 0   # Disable automatic retries
```

Authentication failures, HTTP status errors, tool errors, and cancellation are
not retried. For Anthropic, rate-limit, billing, and server errors surface
immediately instead of waiting through hidden SDK backoff. Other providers' SDKs may retry internally.

One HTTP error is handled automatically. Anthropic ties each server-side
`web_search` result to the account that ran the search, so a session resumed
under a different API key or account fails with
`Invalid encrypted_content in search_result block`, and would keep failing since
the results are in the history. pcode drops those results, keeping each page's
title and URL and the model's own reading of them, says how many it removed, and
sends the turn again. This happens once per turn and does not use the retry
budget; if the request still fails, the error is reported.

A tool call whose arguments don't match the tool's schema has its own budget.
The model is told what was wrong and gets three corrections per turn by default;
past that the turn ends with a message naming the tool and the rejected field.
`edit_file`'s `replacements` array is the usual cause on Anthropic models.

```sh
pcode config set tool_retries 5   # More corrections before a turn is abandoned
pcode config set tool_retries 0   # Fail on the first rejected tool call
```

`strict_tools` (on by default) also sends `edit_file` with Anthropic's
[strict tool use][strict] flag. It reduces those malformed arguments only
slightly; the correction budget is what actually recovers them. OpenAI models
use strict mode anyway, other providers ignore the flag, and schemas it cannot
support are left alone.

```sh
pcode config set strict_tools off   # Leave edit_file arguments unconstrained
```

[strict]: https://platform.claude.com/docs/en/build-with-claude/structured-outputs

`/resend` retries manually while idle, without adding another message. The
original prompt appears above the task bar with the running spinner. Completed
tool results stay in context; if the last answer completed, only that answer is
regenerated. An empty conversation cannot be resent. If cancellation left tool
effects unsettled, `/resend` refuses to replay them; check `/tools` and send an
explicit next step instead.
