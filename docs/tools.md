# Tools

## Tool permissions

**The agent edits files and runs shell commands for real. There is no approval
UI, and no sandbox unless you turn one on.** pcode does not rely on prompt text
as a safety control. If you want the agent sandboxed, run
`/extensions on sandbox` (see
[below](#write-policy-and-shell-sandbox-opt-in)), or run all of pcode inside a
container or VM. Otherwise use a trusted repository and a safe working
environment.

Without the sandbox, this is what the agent can do without asking:

- **Run any shell command.** Treat `shell` as arbitrary code execution as you.
  It can read or modify anything the OS allows, including files the file tools
  protect.
- **Read and write files anywhere the OS permits** with the file tools,
  including other worktrees, temporary directories and credential files such
  as SSH keys and pcode's own logins. The only exception: files matching `.git/*`, `.env`, `.env.*`,
  `*.pem`, `*.key` and `**/secrets*` are read-only through the file tools, at
  any depth. The shell is not bound by this.
- **Call any tool you have enabled**: [MCP servers](mcp.md), the
  [browser](#browser-per-conversation), web search and page fetching, and tools
  added by [extensions](guide/extending.md).
- **Delegate to sub-agents.** The built-in worker can do all of the above (see
  [below](#sub-agents)).

Files and command output returned by tools are sent to the selected model.
Relative paths in the file tools (including `..`) always resolve against the
workspace, even after a shell command changes directory. `list_files` and
`grep` default to the workspace and return workspace-relative paths (`../` for
results outside it).

Sessions started by [email remote control](email.md#the-remote-profile) always
run with the sandbox on.

### Write policy and shell sandbox (opt-in)

`/extensions on sandbox` limits where the agent can write, for the file tools
and for every `shell` command, using one policy:

- **Writable:** the workspace, its repository's main checkout (which covers
  every `.worktrees/` sibling), temp directories, `~/.cache`,
  `~/Library/Caches` and `~/.npm`, plus anything you grant.
- **Also writable, once they exist:** the stores Go and Cargo download
  dependencies into (`~/go/pkg/mod`, `~/go/pkg/sumdb`, and `registry/` and
  `git/` under `~/.cargo`, following `GOPATH`, `GOMODCACHE` and `CARGO_HOME`).
  Places programs get installed are left out on purpose, since something put
  there would later run outside the sandbox: `~/go/bin`, `~/.cargo/bin`,
  `~/.local/bin`, uv's tools and Pythons, and Homebrew. So `go install`,
  `cargo install`, `uv tool install`, `uv python install` and `brew install`
  need a grant.
- **Read-only even inside those:** pcode's config directory, any `.pcode/`
  directory and any `.git/hooks/`. Writing there would let the agent switch the
  policy off or run code outside the sandbox later.
- **Unreadable:** SSH private keys, `~/.aws`, `~/.gnupg`, `~/.netrc`, the GitHub
  CLI's, Docker's and uv's stored logins, and pcode's, Codex's and Claude
  Code's credential files.

Shell commands run under macOS's built-in `sandbox-exec`, or `bwrap` on Linux.
These are the same OS-level mechanisms Anthropic's
[sandbox runtime](https://github.com/anthropic-experimental/sandbox-runtime) uses
for Claude Code's sandboxing: a Seatbelt profile generated per command on macOS,
bubblewrap on Linux. pcode generates its own profile from the policy above.
Unlike that runtime, it doesn't filter network traffic.

macOS needs nothing extra. On Linux, install your distribution's `bubblewrap`
package. The Homebrew formula pulls in its own copy, but on Ubuntu 24.04 and
later only the distribution's `/usr/bin/bwrap` is allowed to run, so pcode
prefers it when both are present.

A write outside the policy fails with "Operation not permitted", and the
transcript still shows the command as typed. With no sandbox available, the
`shell` tool refuses to run rather than running unprotected. Your own `!`
commands are never sandboxed.

Grant more with `/allow-writes`:

```text
/allow-writes ../other-repo                     # this session
/allow-writes --global ~/.local/share/chezmoi   # every session
/allow-writes --global ~/work/AGENTS.md         # a single file
/allow-writes                                   # show the current policy
```

Press Tab while typing the path to complete it, including `~/` and paths
outside the repository.

Global grants are saved in `sandbox.json` beside `preferences.json`, which you
can also edit by hand:

```json
{
  "write": ["~/.local/share/chezmoi", "~/go"],
  "deny_read": ["~/.ssh/id_*", "~/.aws", "~/.kube"],
  "shell_sandbox": true
}
```

`deny_read`, when present, replaces the default list, including the entries
that hide pcode's own logins (`credentials.json`, `mcp-credentials.json` and
`codex-credentials.json` in the config directory), so copy those into your
list if you want them kept unreadable. `"shell_sandbox": false` keeps the file-tool checks but runs shell commands unsandboxed. A file that
isn't valid JSON blocks writes and shell commands until you fix it, rather than
silently dropping the policy.

What it does not cover:

- **Network access** is unrestricted.
- **Environment variables** reach shell commands as usual, so a token exported
  by direnv is still visible to them.
- **MCP servers** run as their own processes, outside the sandbox.
- **Files the repository runs later.** A `Makefile`, `.envrc` or `.git/config`
  the agent edits can still run code when you use it outside the sandbox.
- **Single-file grants are tighter for the file tools than for the shell.**
  Many shell tools (`sed -i`, editors) replace a file by writing a sibling and
  renaming it, which needs the directory. Grant the directory, or let the agent
  use `edit_file`.
- **Shared caches and stores.** They're used by every project, so code the
  agent changes in one (a crate's `build.rs`, a Go module) runs when you build
  another project outside the sandbox.
- **Other build tools' stores** (Gradle, Maven, pnpm) fail until you grant
  their directories.
- **Linux** protects `.pcode/` and `.git/hooks/` only where they already exist
  when a command starts.

## Sub-agents

The built-in `worker` sub-agent is general-purpose, not read-only: it can
inspect and edit files, run commands and tests, and use the same extension tools,
web tools, and enabled MCP servers as the main agent. It inherits the repository
instructions, file protections, and extension guardrails. Repository instruction
discovery stays scoped to the workspace and its configured ancestors, not every
path the tools can reach.

The model hands a worker a self-contained task through `delegate_task`. Each
worker starts with fresh context and its own plan; the parent's conversation is
not copied. The delegation is labeled with a short purpose in the task widget and
scrollback, its tool calls show under the parent's, and Ctrl+C cancels it with
the turn. Workers cannot delegate further, and have no request cap or timeout.

A steering message you send while the model waits on a sub-agent doesn't wait
for the sub-agent to finish. The delegation moves to the background, your message
reaches the model on its very next request, and the sub-agent keeps working. The
model can answer you or do other work in the meantime, and the sub-agent's result
arrives as a follow-up message once it's done. If the model runs out of things to
do first, the turn stays open (the status row reads `Waiting for a sub-agent`)
until the result comes in, and you can keep steering it while it waits. Ctrl+C
still cancels the sub-agent along with the turn.

There is **no limit on concurrent workers by default** (`worker_concurrency=0`).
Set a positive `worker_concurrency` and `/reload` to cap built-in workers per
session; extra delegations then wait for a slot. Provider limits and machine
resources still apply. Sub-agents defined by extensions keep their own tools
rather than gaining the worker's.

### Sub-agents on other models

By default every sub-agent runs on the session's model. A delegation can name
another `provider:model` instead, with the worker's tools and permissions
unchanged, so you can simply ask for a second opinion:

```text
❯ ask openai-codex:gpt-6-astra to review this diff
```

A name given this way is resolved the first time a delegation uses it; if it
cannot be (an unknown provider, or one you are not signed in to), the model is
told why and can pick another.

`/subagents` lists models up front, so the model knows what it can pick without
being told:

```text
❯ /subagents openai-codex:gpt-6-astra anthropic:claude-sonnet-5
```

The model can then pick one of the listed names per delegation, or leave it on
the session's model. Names complete from the `/model` catalog as you type, and
are resolved the way a [`/btw` side question](side-questions.md#choosing-the-model)
resolves another model: on pcode's own logins, with that model's defaults and
saved `/effort` as of launch or the last `/reload`. A name that cannot be resolved
(an unknown provider, or one you are not signed in to) is refused before anything
is saved. A name missing from the `/model` catalog is saved with a warning to
check its spelling, since a real typo only fails when a delegation uses it.

- `/subagents` alone lists the models, checking each again and flagging any
  that no longer resolves (after a `/logout`, say). The model only sees names
  that resolved at launch or the last `/reload`.
- `/subagents off` clears the list.
- Setting the list saves `subagent_models` and reloads the agent like `/reload`,
  so the next request rebuilds the prompt cache. A delegation on another model
  also starts without the parent's cache.

`pcode config set subagent_models A,B` does the same from the shell, taking
effect at the next launch or `/reload`. A repository can set the list in its
[`.pcode/preferences.json`](configuration.md#per-repository-overrides), which then
wins over yours: the bare listing says so, and `/subagents` refuses to save a
choice it would ignore. Such a list only picks among models you are already
signed in to, but it does send delegated work to them, so check it in a
repository you did not write.

### Worker worktrees

By default workers share their parent's live workspace, uncommitted files
included, even when `worktree` is on. Giving each worker its own checkout is
**opt-in** and needs both settings:

```sh
pcode config project set worktree on
pcode config project set worker_isolation on
```

With both on, built-in workers get their own worktree automatically; both are
checked at each delegation, so no reload is needed. A one-launch `--worktree`
does not count. Without them the model cannot request isolation, but it can
always ask for shared mode (for example, to investigate your uncommitted files).
Shared mode is not read-only, so concurrent edits and Git commands need
coordinating. Turning `worker_isolation` off stops new isolated workers but
leaves existing task worktrees and their management tools in place, so their
results can still be integrated or discarded.

An isolated worker gets a `task-<id>` branch and checkout under `.worktrees/`,
starting at the **parent's current commit**, not the main branch. The parent must
be on a branch with no tracked changes and no Git operation in progress, so
commit a checkpoint first; pcode never stashes or makes hidden commits.
Untracked files are not copied. Trusted worktree setup scripts run before the
worker starts, with a 15-minute limit; cancelling stops the setup process too.

Inside the child checkout, file tools, the shell, repository instructions, and
extensions all point at that checkout. Extensions are set up again for the
worker and can tell with `pcode.is_worker`; shared resources such as the browser
stay with the parent. Enabled MCP tools are shared services, not sandboxed per
worktree. An isolated worker has its own shell jobs: their completion notices go
to that worker, and any still running are stopped when it finishes. Worktrees do
not isolate ports, databases, credentials, or OS permissions.

Each task leaves a record with its branches and paths, base and result commits,
whether the checkout was dirty, its status, and the worker's summary. Commit and
cleanliness details come from Git; test claims in the summary are the worker's
own. Cancellation or failure keeps the checkout and record, and a task whose
owning pcode died shows as failed.

Only the parent can manage tasks:

- `list_task_worktrees()` lists this checkout's tasks, including after a restart.
  Task records are local files in the Git directory, never tracked files.
- `integrate_task(task_id)` merges a finished result into its **parent branch**,
  never into the main branch. Both checkouts must be clean (untracked files
  included), and the worker's branch must still match its result. Conflicts stay
  in the parent to resolve and commit before retrying. Review the diff first and
  run the combined checks afterward.
- `discard_task(task_id, confirm=False)` removes an inactive task's checkout and
  branch. Unintegrated or dirty work requires `confirm=True`, which the agent is
  told to use only after you approve. Running tasks cannot be discarded. This is
  an instruction to the model, not an approval prompt.

`/worktree list` labels task checkouts with their owner and status.
`/worktree clean` keeps active and unfinished tasks and their parents; a clean,
integrated task can be cleaned up before the parent merges. `/worktree merge`
and friends refuse task branches, so a task cannot land on the main branch by
accident. Moving a task checkout is unsupported; the worktree commands leave it
alone and explain how to restore its path.

Sub-agents defined by extensions always share the workspace; isolation is only
for the built-in worker.

## File and shell tools

The agent has `read_file`, `write_file`, `edit_file`, `list_files`, `grep`, and
`shell`, plus tools for shell jobs, planning, delegating to the worker, web
search, and [recalling earlier sessions](sessions.md#recalling-earlier-sessions).
`list_files` and `grep` use a bundled ripgrep and respect ignore files. An edit can be one replacement or a
list of them, all checked before the file is written once. When the model sends
a malformed list, pcode lets it correct itself: see
[retries](sessions.md#retries-and-resend).

### Shell jobs

A command the model runs keeps running until it finishes, whether or not the
model waits for it. A command still running is a **job** with an id (`j1`, `j2`,
…) that the model can come back to.

- A normal shell call waits and returns the output with a status line such as
  `[j1 · exit 0 · 1.4s]`. The exit code is the shell's, so `make test | tail`
  reports `exit 0` even when make fails; the model is told to read the output.
- The model can start a command in the background and get the job handle at
  once, when it has other work to do. Background and long-running commands carry
  a short purpose ("running the end-to-end suite") that labels them in the tool
  rows, `/tools`, and the command block; the command itself is always shown too.
- A wait that ends before the command does (it passed its timeout, at most 270
  seconds, or you typed a follow-up) hands back a job handle instead. **The
  command is not killed.**
- The model can wait on a job without re-running it, including until a line of
  output appears (a server's "listening on"), read its output so far, stop it
  and everything it started, and list jobs.

A job's exit is **delivered**, not polled for: the model hears about it before
its next request, and it is printed to the terminal while you are idle. A job the
model already collected is not reported twice. A failed job's notice includes
the last 2 KB of its output. The model is told never to `sleep` waiting for a
command.

If the model ends its turn with a background job still running, the job's exit
**wakes it**: the notice starts a new turn on its own, shown as a
`◈ Job finished` row instead of a prompt. Only jobs the model started do this,
never one you stopped or one adopted from an earlier pcode.
`pcode config set job_wake off` turns it off; the model then hears at your next
message.

The footer below the editor shows the active total as `1 job` or `N jobs`,
including jobs the model is waiting on, and hides the count when none remain.
Use `/jobs` for details. While the model waits on a job, the status row says `Wait for job` and the tool row in the
[activity panel](commands.md#activity-panel) names the job: `⧖ 45.2s · j3 · running the e2e suite · make e2e`.
The finished job goes to scrollback at the end of the turn (or at once while
idle) as a normal `Run shell` block labeled `background`, with its id and
elapsed time; `show_commands` and `tool_error_scrollback` apply as for other
commands.
`/jobs watch j3` pins a job's output tail into the command preview whatever
`show_commands` says; `/jobs unwatch` releases it, and it clears when the job
ends.

Waiting on or reading a job shows up in `/tools`, not scrollback, since it runs
nothing new. Errors such as an unknown job id still appear in scrollback.

A follow-up you type while the model waits on a command ends the wait, not the
command; the job keeps running. In `steering` send mode your message reaches the
model on its very next request instead of waiting out the timeout. In `interrupt`
mode the turn is cancelled and the wait abandoned the same way. Ctrl+C means
stop working, so it also stops the command the turn was waiting on. A job the
model explicitly backgrounded survives all of these. Nothing makes the model
finish a job before replying, so "write the release notes in the meantime" can
steer the same turn into other work while a build continues.

Jobs outlive the turn, the conversation, and pcode itself. Use
[`/jobs`](commands.md#slash-commands) to browse them and read each
one's log (the last 128 KiB), and **Ctrl+K** there,
`/jobs stop ID`, or `/jobs stop all` to stop one. A stop sends `SIGTERM` to the
job's whole process group, so a server can release its port, then `SIGKILL` to
whatever is left two seconds later. Logs of finished jobs are deleted on exit and
the oldest are dropped after 50; a running job keeps its log. `--no-save` does
not disable these logs.

Job logs live under `$XDG_STATE_HOME/pcode/jobs/` (default
`~/.local/state/pcode/jobs`). When pcode exits with jobs still running, the next
pcode to start **adopts** them: they appear in `/jobs` with fresh ids, marked
`adopted from an earlier pcode`, and can be read, watched, and stopped like any
other.

## Web search

The agent can search the web and read pages. Search and page fetching are
separate tools, since search results are only snippets. Each uses the best
backend available:

| | Search | Fetch a URL |
| --- | --- | --- |
| Model has a native tool (`anthropic:`, `claude:` and OpenAI models) | provider runs it server-side | Anthropic runs it server-side; `claude:` uses `get_page` below by default |
| `EXA_API_KEY` set | Exa `web_search` | Exa `get_page` |
| Otherwise | DuckDuckGo `web_search` | HTTP fetch `get_page`, converted to Markdown |

Native tools are billed by the provider per search. The Exa key is never passed
to the model. Anthropic ties each native result to the account that ran the
search, so a session resumed under a different login cannot replay them; see
[Retries](sessions.md#retries-and-resend) for how pcode handles that. Search
returns up to five results, and `get_page` up to 100,000 characters of a page
(10,000 through Exa). By default a long page is
[saved to disk](context.md#tool-output-limits) and read back in pieces rather
than filling the context. Queries, URLs, and returned
content go to whichever backend is in use, reach the model, and can be saved in
session history. The worker inherits the same web policy and tools.

On `claude:` models, Claude Code runs its own WebSearch, and pcode shows it
like any other native call. Pages are read with `get_page` by default, because
Claude Code's own WebFetch answers with a small model's summary of the page,
not the page text. The summary is shorter and makes instructions planted in a
page less likely to reach the model, but it can drop or blur the exact detail
you fetched the page for. Set `web_fetch` to `native` to use it anyway, or to `local` to
use `get_page` on every model.

```sh
pcode config set web_search local   # Never advertise native tools to the model
pcode config set web_search off     # No web tools at all
pcode config unset web_search       # Back to auto
pcode config set web_fetch local    # Native search, but pcode's own get_page
pcode config set web_fetch native   # Also Claude Code's summarizing fetch
```

`local` is the escape hatch for an endpoint that rejects server-side tools.
Changes apply on `/reload` or the next launch. This is all one bundled
extension, `web_research`; copy `src/pcode/extensions/web_research.py` to
`~/.config/pcode/extensions/web_research.py` to change backends, limits, or
instructions, or leave its `setup` empty to remove the tools.

## Browser (per conversation)

`/browser launch` gives the model your installed Chrome, through Harness's
[Playwright tools](https://pydantic.dev/docs/ai/harness/playwright/): navigate,
click, type, snapshot, screenshot, and the rest, plus `browser_open()`, which
brings the window to the front, and `browser_tabs()`. When a page needs you to
sign in, the model leaves it on screen and asks; log in there and tell it when
you are done. A `browser` sub-agent shares the same window, so a multi-step
task can run without every page landing in the main context. `/browser off`
quits that Chrome and removes the tools; a fresh pcode starts with them off.
All three work mid-turn: the running turn keeps the tools it started with, and
the change applies once it finishes.

pcode starts Chrome with its own profile under `~/.local/state/pcode/chrome`,
apart from your everyday one. Because it is your real Chrome rather than
Playwright's automation-flagged Chromium, Google and similar sign-in pages
accept it. The profile persists,
so a site you log in to once stays logged in for later pcode sessions; delete
the directory to forget everything. Set `PCODE_BROWSER_CHROME` to pick the
binary. With no Chrome installed it falls back to Playwright's Chromium,
downloaded on first use.

`/browser attach` joins Chrome, Chromium, or Microsoft Edge you already have
open instead, logins included. The model works in a tab of its own, and
`browser_tabs()` shows it what you have open, so "check my email" finds the mail
tab rather than guessing. pcode closes its tab on `/browser off` and never quits
your browser. **This is the higher-risk mode: the model can act as every account
that browser is signed in to.**

Attaching needs remote debugging on. The first `/browser attach` opens
`chrome://inspect/#remote-debugging` in Chrome for you to flip the switch; then
run it again. (Starting Chrome with `--remote-debugging-port` works too.) For
Edge, turn on remote debugging in Edge first, since the setup shortcut always
opens Chrome. pcode finds the browser through the standard Chrome, Chromium, and
Edge profile directories on macOS and Linux. To override that, set
`PCODE_BROWSER_CDP_URL` to a specific endpoint, or `PCODE_BROWSER_PORT_FILE` to a
custom profile's `DevToolsActivePort` file.

| `/browser …` | Does |
| --- | --- |
| `launch` | Open pcode's own Chrome window, with its own persistent logins |
| `attach` | Join Chrome, Chromium, or Edge you have open, your logins included |
| `off` | Close the browser (or pcode's tab in yours) and remove the tools |
| `status` | Show which browser is in use and where it is |

The window is visible and localhost is reachable, since a dev server is the
usual target. The trade-off of turning it on at all: any page the model reads
can tell it to act with your login, and nothing enforces otherwise beyond you
watching the window. This is the bundled `browser` extension; a user file of the
same name replaces it.

## Code mode (opt-in)

[Code mode](https://pydantic.dev/docs/ai/harness/code-mode/) replaces individual
tool calls with a single sandboxed Python snippet, so the model can fan out
lookups with `asyncio.gather`, filter results, and return only what matters
without a model turn per dependent batch.

```sh
pcode config set code_mode on   # Applies on next launch
pcode config unset code_mode    # Back to plain tool calling
```

Only read-only lookups are available inside a snippet: `read_file`,
`list_files`, `grep`, `read_tool_result`, `web_search`, and `get_page`. Edits,
plan updates, the shell, and delegation stay ordinary tool calls, so diffs,
command previews, and the plan panel still show what happened. A `run_code` call is displayed by the calls the
snippet makes and its size (`grep · read_file ×2 · 12 lines`); the snippet
itself is visible in the tool-call inspector.

With `live_edits` on (off by default), the snippet also streams into the pinned
preview box as the model writes it, titled `Preparing code · not yet run`, in
the same place edit diffs and command output appear. Only complete lines are
shown, the box clears once the snippet runs, and the text never enters the
transcript. `/show-edits off` hides it along with edit previews.

Snippets run in a sandbox with no access to the host filesystem or environment;
the lookup tools above are the only way out, and they follow the usual workspace
rules. Each snippet is limited to 30 seconds and 256 MiB of memory.
