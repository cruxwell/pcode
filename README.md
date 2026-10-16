<img width="1392" height="1522" alt="pcode in a dark terminal" src="docs/assets/screenshots/tabs-dark.png" />

<img width="1392" height="1522" alt="pcode in a light terminal" src="docs/assets/screenshots/tabs-light.png" />

# pcode

pcode is a terminal-native coding agent that lets you change what's visible in
your scrollback, even after the work is done. Expand commands, output, and diffs
when you need the details; hide them when you want to read the conversation.
The same history, with the detail you choose.

Long-running commands continue in the background and wake the agent when they
finish. Run sessions in separate git worktrees, or rewind and branch a
conversation to try another approach.

- **Slow work doesn't block you.** Long commands become background jobs, and a
  finished job wakes the agent, so it can watch tests or CI and fix what fails.
- **Agents in parallel.** A git worktree per session lets several agents work
  on one repo at once.
- **Rewind and fork.** `/tree` returns to any point in a conversation and
  branches from there. Every session is kept, so you can resume or search it.
- **Change your view, not your history.** Show or hide commands, output, and
  diffs throughout the conversation. Hidden output stays available, and `/tools`
  lets you inspect every tool call and its full output.

It runs on any model [Pydantic AI](https://ai.pydantic.dev/) supports, or on
your Claude or ChatGPT subscription.

**Documentation: [cruxwell.github.io/pcode](https://cruxwell.github.io/pcode/)**

**Tried it? [Share feedback](https://github.com/cruxwell/pcode/discussions/4)**: what worked, what didn't, and where you got stuck.

## Install

With [Homebrew](https://brew.sh/):

```sh
brew tap cruxwell/pcode https://github.com/cruxwell/pcode.git
brew install cruxwell/pcode/pcode
```

Or from [PyPI](https://pypi.org/project/pcode/) with [uv](https://docs.astral.sh/uv/):

```sh
uv tool install --python 3.15 'pcode[claude]'
```

`brew install --HEAD` tracks `master` instead of the latest release.

## Quick start

```sh
pcode -m claude:claude-sonnet-5              # Claude subscription; run /login claude if not signed in
pcode -m openai-codex:gpt-5.6-luna           # ChatGPT subscription (/login openai-codex)
pcode -m anthropic:claude-sonnet-5           # or any provider, with its API key set
pcode                                        # reuses the model /model (Ctrl+L) saved; with none, an offline preview
pcode -C /path/to/repo "Summarize the open TODOs"
git diff | pcode -p --no-save                # non-interactive: reply to stdout
pcode --continue                             # resume this directory's newest session
pcode --theme-preview                        # offline sample output and the style gallery
```

**The agent edits files and runs shell commands with your permissions and no
approval prompt.** Read [tool permissions](https://cruxwell.github.io/pcode/tools/#tool-permissions)
before pointing it at anything you care about.

## More

- **Your subscription:** Claude runs through Anthropic's own Agent SDK and
  Claude Code login (the route Anthropic supports), and ChatGPT through a Codex
  login. See [providers](https://cruxwell.github.io/pcode/providers/).
- [Email remote control](https://cruxwell.github.io/pcode/email/): send a task
  from Gmail on your phone, reply to keep going, and take the session over at a
  terminal with `pcode --attach`.
- [Custom keybindings and Vim editing](https://cruxwell.github.io/pcode/keybindings/):
  map keys to commands with arguments, add a Space leader in normal mode, or use
  `jj` to leave insert mode.
- `/btw` side questions, background sessions, recall of past sessions, a
  browser the agent can drive, and Python extensions.
- Built on Pydantic AI's Harness coder, with MCP (OAuth and tool search),
  searchable sessions, jobs and worktrees on top. Extensions are plain
  Pydantic AI capabilities. The conversation tree follows
  [pi](https://github.com/badlogic/pi-mono/blob/main/packages/coding-agent/docs/tree.md)'s.

See [PLAN.md](https://github.com/cruxwell/pcode/blob/master/PLAN.md) for the longer-term direction.

## Documentation

| Page | What it covers |
| --- | --- |
| [Getting started](https://cruxwell.github.io/pcode/getting-started/) | Homebrew, uv and source installs, signing in, `--print`, shell completion |
| [Scrollback and transparency](https://cruxwell.github.io/pcode/guide/scrollback/) | Guide: what goes into scrollback, `/tools`, `/diffs` |
| [A shell for long-running work](https://cruxwell.github.io/pcode/guide/shell/) | Guide: background jobs, watching CI |
| [Parallel agents](https://cruxwell.github.io/pcode/guide/parallel/) | Guide: worktrees, parallel sub-agents, `/agents` |
| [Extending pcode](https://cruxwell.github.io/pcode/guide/extending/) | Guide: extensions, skills, settings |
| [Providers and models](https://cruxwell.github.io/pcode/providers/) | Authentication, supported providers, the model picker, reasoning effort, Claude Code, proxies |
| [Configuration](https://cruxwell.github.io/pcode/configuration/) | `pcode config`, per-repository overrides, trusting repository code, the settings table, syntax styles |
| [Commands and keys](https://cruxwell.github.io/pcode/commands/) | Slash commands, key bindings, vi mode, tmux newlines, status line, `!command`, the diff and tool inspectors |
| [Keybindings](https://cruxwell.github.io/pcode/keybindings/) | Custom command mappings with `/bind`, Ctrl shortcuts, vi editing, a normal-mode leader, and custom escape sequences such as `jj` |
| [Terminal setup](https://cruxwell.github.io/pcode/terminal/) | Tab titles, the progress bar, iTerm2 and tmux settings |
| [Tools](https://cruxwell.github.io/pcode/tools/) | Tool permissions, web search, the browser, code mode |
| [MCP servers](https://cruxwell.github.io/pcode/mcp/) | Opt-in MCP configuration, OAuth sign-in, deferred tool search |
| [Working in a repository](https://cruxwell.github.io/pcode/workspace/) | `AGENTS.md`/`CLAUDE.md`, skills as slash commands, one worktree per session |
| [Sessions and recovery](https://cruxwell.github.io/pcode/sessions/) | Saving, resuming, recalling earlier sessions, checkpoints, retries |
| [Context, limits and caching](https://cruxwell.github.io/pcode/context/) | Prompt overhead, compaction, output limits, prompt cache notices |
| [The transcript](https://cruxwell.github.io/pcode/transcript/) | What lands in scrollback: diffs, thinking, errors, command output, `/redraw` |
| [Email remote control](https://cruxwell.github.io/pcode/email/) | Driving a session from Gmail, the remote profile |
| [Conversation tree](https://cruxwell.github.io/pcode/conversation-tree/) | `/tree`: rewinding and forking a conversation |
| [Side questions](https://cruxwell.github.io/pcode/side-questions/) | `/btw`: asking about the running turn without interrupting it |

The same pages build into a browsable site with `make docs-serve`; `make docs`
checks every page and anchor link.

## Contributing

Bugs: [open an issue](https://github.com/cruxwell/pcode/issues/new/choose).
Feedback and ideas: [the feedback discussion](https://github.com/cruxwell/pcode/discussions/4).
Security problems: see [SECURITY.md](https://github.com/cruxwell/pcode/blob/master/SECURITY.md). Setup and checks are in
[CONTRIBUTING.md](https://github.com/cruxwell/pcode/blob/master/CONTRIBUTING.md).

```sh
make test        # fast suite; real-tmux regressions skipped
make test-all    # everything, before touching layout, streaming, the editor, or the prompt
```

[AGENTS.md](https://github.com/cruxwell/pcode/blob/master/AGENTS.md) has the worktree workflow and the traps worth knowing
before editing. Contributor notes (architecture, dependencies, profiling, prompt
caching, provider design) live in [`dev/`](https://github.com/cruxwell/pcode/tree/master/dev), outside the published docs.
