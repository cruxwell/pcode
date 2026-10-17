# Working in a repository

## Repository instructions (`AGENTS.md` / `CLAUDE.md`)

The main agent and workers always load the workspace's own `CLAUDE.md` and
`AGENTS.md`. Two settings control whether they also pick up files from parent
directories and from subdirectories:

```sh
pcode config set repo_context_walk_up off   # Only workspace-local files at startup
pcode config set repo_context_walk_up on    # Also inherit ancestors (default)
pcode config set repo_context_nested pointer  # Notify the agent of nested files
pcode config set repo_context_nested contents # Inject nested instruction contents
pcode config set repo_context_nested off      # Disable nested discovery (default)
```

The same commands work as `/config` inside pcode. They are read when an agent is
created (launch, resume, or a model change), so restart pcode to apply a change
reliably. `pcode config unset KEY` restores the default. Turning both off still
loads the workspace's own instructions; neither setting blocks explicit file
reads or removes instructions already in a saved conversation.

**Parent directories (`repo_context_walk_up`).** For a workspace under your home
folder, the walk stops at home (inclusive); elsewhere it goes to the filesystem
root. A symlinked workspace inherits from its real location, and a `.git`
directory does not stop the walk. Instructions load broadest first, workspace
last, with `CLAUDE.md` before `AGENTS.md` in each directory (both load when they
differ). The startup banner lists the files loaded without printing them.

**Subdirectories (`repo_context_nested`).** After the agent reads, lists, or
greps inside a directory of the workspace, pcode checks that directory for an
instruction file. `pointer` adds a note telling the agent to read it if
relevant; `contents` adds the file's text to the conversation. Each directory is
surfaced at most once per turn, and only its first match (`CLAUDE.md` before
`AGENTS.md`) is used. Directories skipped over when jumping straight to a deeper
file are not checked, and shell commands, writes, and edits never trigger it.
This works whether or not the parent walk is on.

The agent is also told which `.claude`, `.agents`, `.codex`, and `.grok` asset
files exist in the workspace, as a list of paths only: nothing is read and no
hooks run. Discovered instructions are sent to the selected model, so review
inherited and nested files when working in a shared directory tree.

## Ponytail (opt-in)

The bundled `ponytail` extension adds the ruleset from
[DietrichGebert/ponytail](https://github.com/DietrichGebert/ponytail) to the
instructions: climb a ladder before writing code (does this need to exist,
does the codebase already have it, does the stdlib, does a native feature, can it
be one line) and never simplify away validation, error handling, security, or
accessibility. It ships off; `/extensions on ponytail` turns it on.

`/ponytail lite|full|ultra|off` sets the intensity, `/ponytail status` reports
it. `lite` names the lazier alternative and lets you pick, `full` (the default)
enforces the ladder, `ultra` challenges the requirement itself, and `off` keeps
the command but injects nothing. A change applies on the reload the command asks
for, not mid-turn.

The level is not per session. It is read from `PONYTAIL_DEFAULT_MODE`, then
`defaultMode` in `$XDG_CONFIG_HOME/ponytail/config.json` (or
`~/.config/ponytail/config.json`), the same file ponytail's plugins for other
agents use, so one level covers all of them. The command writes that file and
says so when the environment variable overrides what it just saved.

The ruleset reaches this conversation only: a `delegate_task` sub-agent does not
inherit it. The review, audit, and debt skills the upstream plugins ship are not
included; copy them under `.agents/skills/` if you want them as
[skill commands](#skills-as-slash-commands).

## Learning from a session (`/reflect`)

When a session went sideways before it went right, `/reflect` turns that into a
change to your instructions, skills or extensions so the next session doesn't
repeat it:

```
/reflect
/reflect the tmux test that kept timing out
```

The model reviews the conversation for moments where an attempt failed and a
later one worked, or where the same mistake repeated. From those it picks the
one lesson the evidence best supports and proposes the cheapest change that
carries it, as exact diffs. Nothing changes until you approve, and "nothing
worth changing" is a normal answer.

**Where changes go.** Habits that would help in any project go to your user
scope; anything about this codebase goes to the project. The prompt lists the
exact files for this machine, worked out the same way pcode finds them for
the workspace:

| Scope | Instructions | Skills | Extensions |
| --- | --- | --- | --- |
| User | `CLAUDE.md` / `AGENTS.md` in your home directory | `skill_dirs` entries outside the workspace (default `~/.agents/skills`) | `extensions/` in pcode's config directory |
| Project | `CLAUDE.md` / `AGENTS.md` in the workspace | `.agents/skills` and relative `skill_dirs` entries | `.pcode/extensions` |

Instruction files pcode loads from directories between the two (the root of a
monorepo, or a folder holding all of one employer's repositories) are offered
for lessons about everything under them. A worktree's copy of the main
checkout's file is not: the worktree's own file is the project file. User
instructions are only offered when pcode loads them, so with
`repo_context_walk_up` off, or a workspace outside your home, cross-project
habits go to a user skill instead.

The model prefers a skill or a hook over another always-on instruction line,
since every line is paid for on every turn. It reads each target before
editing it: a file that says it's generated from elsewhere (by a dotfiles
manager, for instance), or only points at another file, is edited at that
source, and rules a file states about its own contents still apply.

**Lesson log.** Each reflection with something to propose appends an entry to
`$XDG_STATE_HOME/pcode/reflect/lessons.md` (`~/.local/state/...` by default),
declined proposals included. Later reflections read it, so a declined idea
isn't proposed again without new evidence, and a lesson that didn't stick gets
a stronger form instead of a reworded line.

The approach is adapted from [Harness²](https://arxiv.org/abs/2610.14224),
which found that harness changes grounded in what a run actually did, rather
than in guesses, carry over to later tasks.

## Skills as slash commands

Every `SKILL.md` under those asset directories becomes a command, so you can
invoke a skill deliberately instead of hoping the model notices it. A skill in
`.claude/skills/cache-report/SKILL.md` is named after its directory:

```
/skill:cache-report check the last session
```

The command sends a normal message asking the model to read that file and follow
it, with anything you type after the command appended. It is queued like a typed
message, so send mode, steering, and Ctrl+C behave as usual. The skill body is
not preloaded; the model reads the file itself.

`skill_dirs` adds directories searched after the asset directories, so skills
can live outside the workspace:

```sh
pcode config set skill_dirs '~/.agents/skills:.agents/skills'   # default
pcode config set skill_dirs '~/.agents/skills:/opt/team/skills' # share a checkout
pcode config set skill_dirs ''                                  # asset directories only
```

Entries are separated by `:`, `~` expands to your home directory, and a relative
entry resolves against the workspace. Each directory is searched recursively.
Asset directories come first and the first skill with a given name wins, so a
workspace skill shadows a user-level one.

Naming follows `skill_commands`: `prefix` gives `/skill:NAME` (default), `bare`
gives `/NAME`, `both` registers the bare name as an alias of the prefixed one,
and `off` registers nothing. A bare name that collides with a built-in command is
dropped and the built-in wins. Skills are found at launch, so restart after
adding one or changing these settings. The startup banner lists the registered
commands, and each skill's frontmatter `description` labels it in the completion
menu.

### MCP servers a skill needs

A skill that only works with certain [MCP servers](mcp.md) can list them under
the frontmatter's `metadata` (the Agent Skills spec's slot for client-specific
fields, so other assistants reading the file ignore it):

```yaml
---
name: triage-incident
description: Triage an open incident from PagerDuty and Sentry.
metadata:
  pcode-mcp-servers: pagerduty, sentry
---
```

`/skill:NAME` then enables each listed server that is configured in `mcp.json`
and not already on, exactly as `/mcp enable` would (including an OAuth browser
sign-in if needed), and sends the skill's prompt once they are up. A server that
fails to enable is reported and stays off while the rest proceed; Ctrl+C during
sign-in cancels the prompt too. Names missing from `mcp.json` get a warning.
Servers cannot change under a running turn, so invoking the skill mid-turn
prints the `/mcp enable` commands to run afterward instead.

This applies only to the slash command. When the model decides on its own to
read a `SKILL.md`, nothing is enabled.

## One git worktree per session

Several sessions editing one checkout trample each other: one session's
`git checkout` or `stash` eats another's uncommitted edits. pcode can give each
session its own worktree and make that the workspace, so file tools, the shell,
repository instructions, and the saved session all point there. The model needs
no instructions and relative paths cannot land in the main checkout by mistake.

```sh
pcode --worktree                      # .worktrees/pcode-<session-id-prefix>, branch of the same name
pcode --worktree fix-thing            # .worktrees/pcode-fix-thing
pcode config project set worktree on  # default for this repository (committed in .pcode/)
pcode config set worktree on          # default for every git repository
pcode --no-worktree                   # stay in the current checkout this once
```

The worktree lives under `.worktrees/` in the main checkout (added to
`.git/info/exclude`, so `git status` stays clean without touching `.gitignore`)
and branches from the main checkout's current branch, reusing a branch of that
name if one exists. Session worktrees and branches are always prefixed `pcode-`,
so `git worktree list` and `git branch` show which ones pcode made. Starting
pcode inside an existing worktree, outside git, or with `--continue` never
creates another one. Resuming a session (`pcode -c`, or
`pcode -C .worktrees/NAME -c` from elsewhere) lands back in its worktree.

Git cannot install dependencies or copy untracked config, so after checkout pcode
runs two optional scripts inside the new worktree, each with `PCODE_MAIN`,
`PCODE_WORKTREE`, and `PCODE_BRANCH` set:

| Script | Runs |
| --- | --- |
| `~/.config/pcode/worktree-setup` | always (for what every repo needs: `direnv allow`, copying `.envrc`) |
| `<repo>/.pcode/worktree-setup` | only in a trusted repository (launch prompt, or `project_extensions on`), since it is code shipped with the repo |

An executable script runs directly (give it a shebang); anything else runs
through `sh`. A non-zero exit aborts the launch and removes the half-made
worktree.

Inside the session:

- `/worktree` shows the branch and what is unmerged.
- `/worktree merge` merges the main branch into the worktree (so conflicts are
  resolved there, never in the main checkout) and then fast-forwards the main
  branch. It works while a turn is running, so work the model already committed
  can land without waiting, but refuses a worktree with uncommitted changes.
- `/worktree finish` merges, then removes the worktree and its branch and quits.
- `/worktree remove` deletes the directory once it is merged and clean; the
  branch stays.
- `/worktree list` shows every worktree.
- `/worktree clean` removes every other worktree of the repository (and its
  branch) with nothing uncommitted, nothing untracked, and nothing the main
  branch lacks. Anything else is listed with the reason it was kept, and a
  worktree locked with `git worktree lock` is skipped. It also works from the
  main checkout.

Nothing is ever forced. Actions other than `merge` wait for a running turn to
finish. `/resume` can pick a session from another worktree of the repository and
move this session there; see [sessions](sessions.md).

When a merge stops on conflicts it names the files, and `/worktree resolve`
hands them to the model with the branch names, asking it to resolve each so both
sides survive, run the tests, and commit the merge (never abort it). Run
`/worktree merge` or `finish` again afterwards. pcode never starts a merge or
asks the model to resolve one without you typing the command; a conflict at exit
prints the resume command and the same hint.

Workers still share the session's workspace by default, even with
`worktree=on`. To give each worker its own checkout, also set
`worker_isolation=on` (default `off`). Isolated tasks use `task-<id>` branches
starting at the parent session's current commit, not the main branch, and
`/worktree list` shows their owner and status. Pending tasks, and parents that
own them, are protected from removal and `finish` until the task's result is
integrated or explicitly discarded. Clean integrated task checkouts can be
cleaned up even before the parent merges, and a task whose setup failed keeps
its checkout for inspection. See [worker worktrees](tools.md#worker-worktrees).

Leaving a session tidies its own worktree (one pcode made, prefixed `pcode-`;
hand-made ones are only reported):

| State on exit | What happens |
| --- | --- |
| Untouched: clean, nothing unmerged | Removed with its branch, no question. A session that never had a turn is deleted too; otherwise it is repointed at the main checkout so `pcode -c` still works. |
| Committed but unmerged | `worktree_exit`: `ask` (default) prompts `Merge and remove the worktree? [Y/n]`; `merge` does it silently; `keep` leaves it. A merge that conflicts or cannot fast-forward keeps everything and prints how to resume. |
| Uncommitted changes | Kept, with the resume command. pcode does not commit for you at exit. |

`--print` has nobody to ask, so it only does the untouched cleanup. Merging
never pushes; push from the main checkout when you are ready. None of this
happens while another session is still open in the same worktree (a
[copied session](sessions.md#continuing-a-session-that-is-open-elsewhere) and its
original, for instance): the worktree is kept with a note naming that session.
