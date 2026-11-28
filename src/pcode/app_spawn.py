"""`pcode --host --spawn-only`: start a session host for Pcode.app, and say where it is.

Pcode.app's native composer talks to the session host over its socket, so a
pane has no terminal to run `pcode --host` on. The app runs this instead, from
the same login-shell environment a pane's terminal would get, so the host
inherits it (direnv credentials, `PATH`). It prints one JSON line on stdout and
exits; the host keeps running in its own session:

    {"id": ..., "socket": ..., "log": ..., "pid": ..., "existing": false}

`existing` is true when `-c SESSION` named a session a host already holds:
then nothing is spawned and the app attaches to that host.

Project trust is asked by the app, not here, since nobody reads this process's
stdin. With `--spawn-only` (or `=ask`) a repository whose launch-time code is
not yet trusted gets `{"trust": {"key": ..., "code": [...]}}` back and nothing
is started; the app asks, then runs again with `=trust` (record trust, then
spawn) or `=skip` (spawn with that code skipped for this launch).

Failures print `{"error": message}` and exit 1.
"""

import argparse
import json
import sys
from pathlib import Path

SPAWN_MODES = ("ask", "trust", "skip")


def _emit(payload: dict) -> None:
    sys.stdout.write(json.dumps(payload) + "\n")
    sys.stdout.flush()


def spawn_only(args: argparse.Namespace) -> int:
    """Run `--spawn-only` for parsed `args`; returns the exit status."""
    try:
        _emit(spawn_for_app(args))
    except (LookupError, OSError, ValueError) as error:
        from pcode.error_report import error_message

        _emit({"error": error_message(error)})
        return 1
    return 0


def spawn_for_app(args: argparse.Namespace) -> dict:
    """Spawn (or find) the host for the pane `args` describe, as `_run_hosted` would."""
    from pcode.app import _resumed_workspace, _running_host
    from pcode.host_protocol import socket_path
    from pcode.preferences import load_preferences, rejected_project_keys, set_project_root
    from pcode.project_trust import is_trusted, project_code, prompt_trust, trust, trust_key
    from pcode.remote import app_host_id, spawn_host
    from pcode.sessions import SessionError, read_info, resolve_session

    mode = args.spawn_only or "ask"
    workspace = (args.workspace or Path.cwd()).resolve()
    resume, saved_model = None, None
    if args.resume:
        if not args.fork:
            running = _running_host(args.resume, args.session_dir, workspace)
            if running is not None:
                return {
                    "id": running.id,
                    "socket": str(socket_path(running.id)),
                    "log": running.log or "",
                    "pid": running.pid,
                    "existing": True,
                }
        path = resolve_session(args.resume, args.session_dir, workspace)
        info = read_info(path)
        workspace = _resumed_workspace(info, args.workspace).resolve()
        resume, saved_model = path.name, info.model
    if not workspace.is_dir():
        raise SessionError(f"Workspace {workspace} is not an existing directory.")
    set_project_root(workspace)
    if rejected := rejected_project_keys():
        print(
            f"pcode: ignoring user-only settings in .pcode/preferences.json: "
            f"{', '.join(rejected)} (set them with `pcode config set`)",
            file=sys.stderr,
        )
    code = project_code(workspace)
    if code and not is_trusted(workspace):
        if mode == "ask":
            return {"trust": {"key": str(trust_key(workspace)), "code": [str(p) for p in code]}}
        if mode == "trust":
            trust(workspace)
        else:
            prompt_trust(workspace, ask=None)  # Notes on stderr that the code is skipped.
    model = saved_model or args.model or load_preferences().get("model")
    identity, process, log = spawn_host(
        identity=app_host_id(),
        model=model,
        workspace=workspace,
        resume=resume,
        session_dir=args.session_dir,
        no_save=args.no_save,
        worktree=args.worktree,
        no_worktree=args.no_worktree,
        fork=args.fork,
    )
    return {
        "id": identity,
        "socket": str(socket_path(identity)),
        "log": str(log),
        "pid": process.pid,
        "existing": False,
    }
