# Extending pcode

pcode is meant to be bent to your workflow. The quickest way is to ask it: pcode
knows how its own extension system works, so "add a /standup command that
lists yesterday's commits" or "block any shell command that force-pushes to
main" is a normal request. It writes the file and tells you to `/reload`.

## Extensions: one Python file

An extension is a Python file with a `setup(pcode)` function. Drop it in
`~/.config/pcode/extensions/` for every workspace, or in a repository's
`.pcode/extensions/` to share it with the team. `/reload` picks up changes
without restarting or losing the conversation, and a broken extension is
reported with its error while the session keeps working.

An extension can add:

- **Tools** the model can call: wrap your ticket tracker, deploy script or
  internal API.
- **Guardrails** on tool calls: inspect every call before it runs, rewrite its
  arguments, or refuse it with a message the model reads and adapts to.
- **Slash commands** for you to run.
- **Instructions** added to every conversation.
- **Sub-agents** with their own tools and instructions, which the agent can
  delegate to like the built-in worker.

A complete example that keeps the agent away from secret files:

```python
"""Refuse to edit secret files and offer a /secrets command to list them."""

from fnmatch import fnmatch

from pydantic_ai import ModelRetry

PROTECTED = (".env", ".env.*", "*.pem", "*.tfvars")
EDIT_TOOLS = {"write_file", "edit_file"}


def setup(pcode):
    def protected(path: str) -> bool:
        name = path.rsplit("/", 1)[-1]
        return any(fnmatch(name, pattern) for pattern in PROTECTED)

    @pcode.hooks.on.before_tool_execute
    async def guard(ctx, *, call, tool_def, args):
        if tool_def.name in EDIT_TOOLS and protected(str(args.get("path", ""))):
            pcode.ui.notify(f"Refused to edit {args['path']}", "warning")
            raise ModelRetry(f"{args['path']} holds secrets; ask the user to edit it.")
        return args

    pcode.instructions("Never write to .env, key, or tfvars files; ask the user instead.")

    pcode.register_command(
        "/secrets",
        "Show which file patterns are write-protected",
        lambda _: pcode.ui.notify("Protected: " + ", ".join(PROTECTED)),
    )
```

Extensions are plain [Pydantic AI](https://ai.pydantic.dev/) underneath, so
anything a Pydantic AI capability can do, an extension can do.

Several of pcode's own features ship as extensions: web search, the browser,
session recall. `/extensions` lists every extension with what it added,
`/extensions off NAME` turns one off (bundled ones included), and a file of the
same name in your extensions directory replaces a bundled one.

Extensions run in-process with your permissions. A repository's
`.pcode/extensions/` only loads once you've trusted that repository, since it's
code someone else wrote; see
[trusting a repository's own code](../configuration.md#trusting-a-repositorys-own-code).

The [extension guide](https://github.com/cruxwell/pcode/blob/master/src/pcode/extension_guide.md)
covers the full API. It's the same document pcode reads when you ask it to
write one.

## Skills: reusable prompts

For a workflow that's instructions rather than code, write a skill: a
`SKILL.md` file under `.agents/skills/NAME/` (or `.claude/skills/NAME/`) in a
repository, or `~/.agents/skills/NAME/` for yourself. Each skill becomes a
slash command (`/skill:NAME`), and the agent is told which skill files the
workspace has, so it can also read one on its own. See
[skills as slash commands](../workspace.md#skills-as-slash-commands).

## Repository instructions

pcode reads `AGENTS.md` and `CLAUDE.md` from the workspace, and from parent
directories up to your home folder, so build commands, conventions and known
traps reach every session without repeating them. An untracked
`AGENTS.local.md` (or `CLAUDE.local.md`) beside them holds personal notes you
don't want to commit. See
[repository instructions](../workspace.md#repository-instructions-agentsmd-claudemd).

## Settings

Everything else is a setting: themes and syntax styles, vi editing, a leader
key for shortcuts, what goes into scrollback, send mode, and more. Set them
globally with `pcode config set`, or per repository in a committed
`.pcode/preferences.json`. See [configuration](../configuration.md).
