"""Write roots for the file tools and an OS sandbox around the model's shell.

Opt in with `/extensions on sandbox`. The policy lives in `pcode.sandbox`:
writes only under the repository, temp, cache and package dirs, and paths you
grant; reads anywhere except credential files. `/allow-writes PATH` grants a directory
or file for this session, `/allow-writes --global PATH` for every session (saved
in `sandbox.json` beside preferences.json), and `/allow-writes` alone shows the
policy.
"""

from pathlib import Path

from pydantic_ai import ModelRetry

from pcode import remote_profile, sandbox
from pcode.jobs import COMMAND_SANDBOX

DEFAULT_ENABLED = False

# `save_file` and `upload_file` are the browser's: one writes what a page
# serves, the other hands local files to a page.
WRITE_TOOLS = frozenset({"write_file", "edit_file", "create_directory", "save_file"})
READ_TOOLS = frozenset({"read_file", "list_files", "grep", "upload_file"})

INSTRUCTIONS = (
    "File writes and shell commands run under a write policy: the workspace's "
    "repository, temp, cache and package directories, and paths the user "
    "granted are writable; pcode's config, `.pcode/` and `.git/hooks/` are not; credential "
    "files are unreadable. A refused write fails with a message or "
    "'Operation not permitted'. Do not work around it; ask the user to run "
    "`/allow-writes PATH` when a task needs another location."
)


def setup(pcode) -> None:
    workspace = Path(pcode.workspace)
    base = sandbox.base_roots(workspace)

    def policy() -> sandbox.Policy:
        """Rebuilt per call, so edits to sandbox.json and new grants apply at once."""
        return sandbox.Policy.build(base, grants=sandbox.SESSION_GRANTS)

    def enforced(load):
        # A broken config blocks the call rather than dropping the policy.
        try:
            return load()
        except ValueError as error:
            raise ModelRetry(
                f"The sandbox policy is invalid ({error}); ask the user to fix it."
            ) from error

    @pcode.hooks.on.before_tool_execute
    async def guard_files(ctx, *, call, tool_def, args):
        name = tool_def.name
        if name not in WRITE_TOOLS and name not in READ_TOOLS:
            return args
        paths = args.get("paths")
        raws = [str(p) for p in paths] if isinstance(paths, list) else [args.get("path") or "."]
        current = enforced(policy)
        for raw in raws:
            check(name, str(raw), current)
        return args

    def check(name: str, raw: str, current: sandbox.Policy) -> None:
        target = sandbox.tool_target(raw, workspace)
        if name in WRITE_TOOLS and not current.can_write(target):
            pcode.ui.notify(f"Blocked write to {target}", "warning")
            if current.guards(target):
                reason = f"{raw} is in a protected location."
            else:
                reason = f"{raw} is outside the writable paths. {current.explain()}"
            raise ModelRetry(
                f"{reason} If this write is intended, ask the user to run `/allow-writes <path>`."
            )
        if not current.can_read(target):
            pcode.ui.notify(f"Blocked read of {target}", "warning")
            raise ModelRetry(f"{raw} holds credentials and is not readable.")

    @pcode.hooks.on.tool_execute
    async def sandbox_shell(ctx, *, call, tool_def, args, handler):
        if tool_def.name != "shell":
            return await handler(args)
        config = enforced(sandbox.load_config)
        # A remote host's profile fixes the shell sandbox on.
        if config.get("shell_sandbox", True) is False and remote_profile.active() is None:
            return await handler(args)
        if sandbox.backend() is None:
            raise ModelRetry(
                "Shell commands are disabled: no OS sandbox is available "
                "(sandbox-exec on macOS, bwrap on Linux). Tell the user."
            )
        current = enforced(policy)
        token = COMMAND_SANDBOX.set(lambda directory: sandbox.command_prefix(current, directory))
        try:
            return await handler(args)
        finally:
            COMMAND_SANDBOX.reset(token)

    def allow_writes(argument: str) -> None:
        text = argument.strip()
        persist = text == "--global" or text.startswith("--global ")
        text = text.removeprefix("--global").strip()
        if text.startswith("--"):
            raise ValueError("Usage: /allow-writes [--global] PATH")
        if not text:
            if persist:
                raise ValueError("Usage: /allow-writes [--global] PATH")
            pcode.ui.notify(_summary(policy()))
            return
        # The rest is one path, spaces included.
        path = sandbox.real(text, workspace)
        if not path.exists():
            raise ValueError(f"{path} does not exist.")
        if persist:
            sandbox.add_global_grant(path)
            where = "every session"
        else:
            if path not in sandbox.SESSION_GRANTS:
                sandbox.SESSION_GRANTS.append(path)
            where = "this session"
        pcode.ui.notify(f"Writable for {where}: {path}")

    def _summary(current: sandbox.Policy) -> str:
        shell = sandbox.backend() or "unavailable, shell disabled"
        if sandbox.load_config().get("shell_sandbox", True) is False:
            shell = "off (sandbox.json)"
        lines = [
            "Writable:",
            *(f"  {root}" for root in current.write),
            "Read-only inside those: "
            + ", ".join([*map(str, current.protected), ".pcode/", ".git/hooks/"]),
            "Unreadable:",
            *(f"  {pattern}" for pattern in current.deny_read),
            f"Shell sandbox: {shell}",
            f"Config: {sandbox.config_path()}",
        ]
        return "\n".join(lines)

    pcode.instructions(INSTRUCTIONS)
    pcode.register_command(
        "/allow-writes",
        "Allow writes to a directory or file: this session, or --global for all",
        allow_writes,
        complete_paths=True,
    )
