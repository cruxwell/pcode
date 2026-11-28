"""`antigravity:` models: a Google AI Pro/Ultra subscription through `agy acp`.

pcode is an Agent Client Protocol front end here, the shape Google approves
for subscription use: the unmodified `agy acp` server owns the agent loop, its
tools and its sign-in, and pcode sends prompts and shows the turn. One pcode
request is one whole Antigravity turn. Its tool calls come back as
provider-executed (native) tool parts, and pcode's own tools are never offered
to it as function tools, so pcode's agent loop ends after every response
unless pcode has something to tell it. (pcode-owned tools such as delegation,
`/browser`, `/advisor`, and opt-in extensions are offered the ACP way, as an
MCP server Antigravity's own loop may call: see `pcode.antigravity_tools`.)

What pcode sends is what it would send any model in the user's session, as
other ACP clients do: the user's prompts, and the session's own events (a
finished background job or worker, a worker's question, a continuation),
written out as text since Antigravity has no pcode tool calls for them to
answer. Side uses (`/btw`, titles, summaries, an advisor) and delegated workers
each run in their own `agy acp` session (`side=True` or `worker=True`, with
scoped bridged pcode tools when active and none on plain completions), so they
never block or pollute the conversation's.

Context is Antigravity's own: `/compact` and automatic compaction send its
`/compact` command, and pcode's plan mode switches the session to its `plan`
mode.

One `agy acp` process serves one conversation. Each response is stamped
`<session ID>#<turn>`, and a ledger in pcode's state directory records each
session's newest turn. A request whose history ends at a session's newest turn
continues it, in the live process or, after a restart, through
`session/resume`. Anything else (a rewind or branch, whose session holds turns
the history dropped, or a switch from another model) starts a new session with
the earlier conversation replayed as text.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import os
import shutil
import time
import uuid
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pydantic_ai import RunContext
from pydantic_ai.messages import (
    BinaryContent,
    DataCapabilityPart,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    ModelResponseStreamEvent,
    NativeToolCallPart,
    NativeToolReturnPart,
    PartEndEvent,
    RetryPromptPart,
    TextContent,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models import Model, ModelRequestParameters, StreamedResponse
from pydantic_ai.settings import ModelSettings
from pydantic_ai.usage import RequestUsage

from pcode.steering import steering_pending

PREFIX = "antigravity:"
SYSTEM = "antigravity"
COMMAND = "agy"
PROTOCOL_VERSION = 1
EFFORT_SETTING = "antigravity_effort"
MODEL_CONFIG = "model"
THOUGHT_LEVEL_CONFIG = "thought_level"
EFFORT_ORDER = ("low", "medium", "high", "xhigh")
# Credentials and endpoints that would make `agy` bill an API key or a Cloud
# project, or send its requests somewhere else, instead of using the
# subscription it signed in with.
SCRUBBED_ENV = ("GEMINI_API_KEY", "GOOGLE_API_KEY", "GOOGLE_APPLICATION_CREDENTIALS")
SCRUBBED_PREFIXES = ("GOOGLE_CLOUD_", "GOOGLE_GENAI_", "GOOGLE_VERTEX_", "GOOGLE_GEMINI_")
NO_PROMPT = "Antigravity has nothing to answer here: send a prompt."
# Sent for a continuation that carries no message of its own (`/continue`).
CONTINUE = "Continue."
# Placeholder prompt in Pydantic AI's request when draining an unsolicited
# post-settle wake from `agy` (never sent over ACP as `session/prompt`).
PROVIDER_CONTINUATION_PROMPT = "[Antigravity continued from background work]"
COMPACT_COMMAND = "/compact"
PLAN_MODE = "plan"
AUTH_REQUIRED = -32000
LOGIN_HINT = "Run `/login antigravity` (or `agy acp --login` in a terminal) to sign in."
REPLAY_INTRO = (
    "This conversation started elsewhere; its earlier messages are replayed below as a "
    "transcript. Treat them as having happened and continue from the final message."
)
SIDE_REPLAY_INTRO = (
    "You are answering a side question beside the main conversation, which may still be "
    "running in the same workspace. Its transcript so far is below for context; answer "
    "only the side question that follows and do not continue or redo the main task."
)
# `agy acp` tail-truncates oversized `session/prompt` payloads (verified in
# agy 1.5.18 at 191,582 B with `<truncated N bytes>`), which drops
# `</conversation>` and the user's prompt when a replayed transcript runs past
# the limit. Keep `REPLAY_MAX_BYTES = 100_000` / `PROMPT_BUDGET_BYTES = 140_000`
# as the safety ceiling so the newest turns and the prompt always arrive intact.
REPLAY_MAX_BYTES = 100_000
PROMPT_BUDGET_BYTES = 140_000
# Minimum byte budget needed before clipping an oversized replay entry, so a
# clipped entry always keeps its role tag and real content instead of a stub.
MIN_CLIP_BYTES = 64
# Tiered replay compaction: keep this many newest messages at full fidelity
# while shrinking or dropping older tool calls/returns first.
REPLAY_RECENT_MESSAGES = 12
REPLAY_OLD_TOOL_CALL_CHARS = 150
REPLAY_OLD_TOOL_RETURN_CHARS = 200
REPLAY_SPILL_TTL_SECONDS = 24 * 60 * 60
REPLAY_OMITTED = "[Earlier conversation omitted to fit the replay limit.]"
REPLAY_CONDENSED = "[Earlier tool calls/outputs were condensed to fit the replay limit.]"
REPLAY_TOOLS_DROPPED = "[Earlier tool calls/outputs were omitted to fit the replay limit.]"
# Seconds a side session's process is kept for a follow-up.
SIDE_IDLE = 120.0
# How long a cancelled turn may take to report that it stopped.
CANCEL_GRACE = 10.0
# How often a running turn checks for the user's steering messages.
STEER_POLL = 0.25
# Quiet seconds that end a stretch of text Antigravity writes between turns.
IDLE_QUIET = 1.5
# Quiet seconds after a held turn's background work finishes and its follow-up
# reply closes (`usage_update`), in case Antigravity immediately starts another tool.
DEFERRED_FINALIZE_DEBOUNCE = 3.0
# Safety cap while waiting for an unsolicited follow-up reply's `usage_update`.
OPEN_REPLY_QUIET_LIMIT = 120.0
# Longest a held turn waits for a native `invoke_subagent` to report back. `agy`
# gives no signal when one dies silently, or when it reported before settlement
# and the parent replied with text only; past this the turn ends, and a late
# report still opens a provider continuation turn.
SUBAGENT_WAIT_LIMIT = 1800.0
# Tool titles Antigravity leaves in_progress across `session/prompt` settlement.
SCHEDULE_TITLES = frozenset({"Run schedule", "Running schedule"})
# Tool names/titles for Antigravity's native async subagent launcher: `agy`
# marks the call `completed` as soon as the subagent spawns, then settles
# `session/prompt` while the subagent runs and later wakes the parent.
SUBAGENT_LAUNCH_NAMES = frozenset({"invoke_subagent"})
SUBAGENT_LAUNCH_TITLES = frozenset({"Run invoke_subagent"})
# Sessions remembered in the ledger; older ones replay instead of resuming.
LEDGER_SIZE = 500


class AntigravityError(RuntimeError):
    """A failure `agy acp` reported, or a request pcode refuses to send, phrased for the user."""


def installed() -> bool:
    return shutil.which(COMMAND) is not None


def _state_dir() -> Path:
    state = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state")
    return state / "pcode"


def models_cache() -> Path:
    return _state_dir() / "antigravity-models.json"


def _read_json(path: Path, kind: type) -> Any:
    try:
        value = json.loads(path.read_text())
    except OSError, ValueError:
        return kind()
    return value if isinstance(value, kind) else kind()


def _write_json(path: Path, value: object) -> None:
    with contextlib.suppress(OSError):
        path.parent.mkdir(parents=True, exist_ok=True)
        # Per process: two pcode processes must not share one temporary file.
        temporary = path.with_name(f"{path.name}.{os.getpid()}.tmp")
        temporary.write_text(json.dumps(value))
        temporary.replace(path)


@contextlib.contextmanager
def _locked(path: Path) -> Iterator[None]:
    """Hold an exclusive lock beside `path` for a read-modify-write across processes."""
    import fcntl

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(path.with_name(path.name + ".lock"), "a")
    except OSError:
        yield  # Unlocked is still better than no ledger at all.
        return
    with handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield


def _config_option(result: dict, config_id: str) -> dict | None:
    options = result.get("configOptions")
    if not isinstance(options, list):
        return None
    for option in options:
        if isinstance(option, dict) and (
            option.get("id") == config_id or option.get("category") == config_id
        ):
            return option
    return None


def _config_id(result: dict, default_id: str) -> str:
    option = _config_option(result, default_id)
    found = option.get("id") if isinstance(option, dict) else None
    return found if isinstance(found, str) and found else default_id


def _config_value(result: dict, config_id: str) -> str:
    option = _config_option(result, config_id)
    value = option.get("currentValue") if isinstance(option, dict) else None
    return value if isinstance(value, str) else ""


def _option_values(option: dict | None) -> list[str]:
    if not isinstance(option, dict):
        return []
    raw = option.get("options")
    if not isinstance(raw, list):
        return []
    values: list[str] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        if isinstance(item.get("value"), str) and item["value"]:
            values.append(item["value"])
        elif isinstance(item.get("options"), list):
            for nested in item["options"]:
                if (
                    isinstance(nested, dict)
                    and isinstance(nested.get("value"), str)
                    and nested["value"]
                ):
                    values.append(nested["value"])
    return values


def _offered(result: dict) -> list[str]:
    return _option_values(_config_option(result, MODEL_CONFIG))


def _fit_thought_level(wanted: str, offered: tuple[str, ...]) -> str | None:
    """Closest `offered` thinking level to `wanted`, or None when none are offered."""
    if not offered:
        return None
    if wanted in offered:
        return wanted
    if wanted not in EFFORT_ORDER:
        return None
    target = EFFORT_ORDER.index(wanted)
    ranked = [
        (abs(EFFORT_ORDER.index(level) - target), EFFORT_ORDER.index(level) > target, level)
        for level in offered
        if level in EFFORT_ORDER
    ]
    return min(ranked)[2] if ranked else None


def _read_models_cache() -> dict[str, Any]:
    return _read_json(models_cache(), dict)


def cached_models() -> tuple[str, ...]:
    """Base model IDs the last Antigravity session offered, for the picker."""
    models = _read_models_cache().get("models")
    if not isinstance(models, list):
        return ()
    return tuple(name for name in models if isinstance(name, str) and name)


def cached_thought_levels(model_name: str) -> tuple[str, ...] | None:
    """Thinking levels Antigravity last offered for `model_name`, or None if unseen."""
    table = _read_models_cache().get("thought_levels")
    if not isinstance(table, dict):
        return None
    levels = table.get(model_name)
    if not isinstance(levels, list):
        return None
    return tuple(level for level in levels if isinstance(level, str) and level)


def cached_default_thought_level(model_name: str) -> str | None:
    """Default thinking level Antigravity started `model_name` on, or None if unseen."""
    table = _read_models_cache().get("default_thought_levels")
    if not isinstance(table, dict):
        return None
    level = table.get(model_name)
    return level if isinstance(level, str) and level else None


def _remember_models(result: dict) -> None:
    names = _offered(result)
    if not names or list(cached_models()) == names:
        return
    path = models_cache()
    with _locked(path):
        stored = _read_json(path, dict)
        stored["models"] = names
        _write_json(path, stored)


def _remember_thought_levels(
    model_name: str, levels: tuple[str, ...], default_level: str | None = None
) -> None:
    if not model_name:
        return
    values = list(levels)
    levels_same = cached_thought_levels(model_name) == levels
    default_same = (
        default_level is None or cached_default_thought_level(model_name) == default_level
    )
    if levels_same and default_same:
        return
    path = models_cache()
    with _locked(path):
        stored = _read_json(path, dict)
        table = stored.get("thought_levels")
        table = dict(table) if isinstance(table, dict) else {}
        table[model_name] = values
        stored["thought_levels"] = table
        if default_level is not None:
            defaults = stored.get("default_thought_levels")
            defaults = dict(defaults) if isinstance(defaults, dict) else {}
            defaults[model_name] = default_level
            stored["default_thought_levels"] = defaults
        _write_json(path, stored)


def _windows_path() -> Path:
    return _state_dir() / "antigravity-windows.json"


# In-memory mirror of `_windows_path()`, keyed by path so rendering hits disk
# at most once per state dir and tests that swap `XDG_STATE_HOME` reload cleanly.
_windows_loaded_path: Path | None = None
_windows: dict[str, int] = {}


def _load_windows() -> dict[str, int]:
    global _windows_loaded_path, _windows
    from pcode.model_metadata import positive_int

    path = _windows_path()
    if _windows_loaded_path != path:
        raw = _read_json(path, dict)
        _windows = {
            name: size
            for name, value in raw.items()
            if isinstance(name, str) and (size := positive_int(value)) is not None
        }
        _windows_loaded_path = path
    return _windows


def cached_window(model_name: str) -> int | None:
    """Last context window size Antigravity reported for `model_name`, if any."""
    return _load_windows().get(model_name)


def _remember_window(model_name: str, size: int) -> None:
    from pcode.model_metadata import positive_int

    valid = positive_int(size)
    if not model_name or valid is None:
        return
    if _load_windows().get(model_name) == valid:
        return
    path = _windows_path()
    with _locked(path):
        stored = {
            name: value
            for name, value in _read_json(path, dict).items()
            if isinstance(name, str) and positive_int(value) is not None
        }
        stored[model_name] = valid
        _write_json(path, stored)
    _windows[model_name] = valid


def _ledger_path() -> Path:
    return _state_dir() / "antigravity-sessions.json"


def newest_turn(session_id: str) -> int | None:
    """The newest turn pcode prompted in this Antigravity session, if it knows."""
    turn = _read_json(_ledger_path(), dict).get(session_id)
    return turn if isinstance(turn, int) else None


def _record_turn(session_id: str, turn: int) -> None:
    # A lost update could put back an older turn and let a rewind resume a
    # session holding turns its history dropped, so writers take turns.
    with _locked(_ledger_path()):
        ledger = _read_json(_ledger_path(), dict)
        ledger.pop(session_id, None)
        ledger[session_id] = turn  # Last, so trimming drops the oldest.
        _write_json(_ledger_path(), dict(list(ledger.items())[-LEDGER_SIZE:]))


def child_env() -> dict[str, str]:
    return {
        k: v
        for k, v in os.environ.items()
        if k not in SCRUBBED_ENV and not k.startswith(SCRUBBED_PREFIXES)
    }


def _spawn(coroutine) -> asyncio.Task:
    """Run `coroutine` as a task kept alive until it finishes."""
    task = asyncio.ensure_future(coroutine)
    _background.add(task)
    task.add_done_callback(_background.discard)
    task.add_done_callback(lambda t: t.cancelled() or t.exception())
    return task


def _supports_system_prompt_append(initialize_result: dict) -> bool:
    """Whether `agy` advertises `_meta.antigravity.systemPrompt.append` on `initialize`."""
    caps = initialize_result.get("agentCapabilities")
    meta = caps.get("_meta") if isinstance(caps, dict) else None
    antigravity = meta.get("antigravity") if isinstance(meta, dict) else None
    system_prompt = antigravity.get("systemPrompt") if isinstance(antigravity, dict) else None
    return isinstance(system_prompt, dict) and system_prompt.get("append") is True


class Connection:
    """One `agy acp` process: JSON-RPC over its stdin and stdout.

    Notifications for a session go to whichever queue a turn installed;
    permission requests are allowed once each, as pcode runs its own tools
    without asking.
    """

    def __init__(self, cwd: str) -> None:
        self.cwd = cwd
        self.process: asyncio.subprocess.Process | None = None
        self.pending: dict[int, asyncio.Future] = {}
        self.updates: asyncio.Queue | None = None
        self.next_id = 0
        self.reader: asyncio.Task | None = None
        self.stderr: list[str] = []
        # Where updates go while no turn is running: Antigravity keeps working
        # after a turn ends (a background sub-agent's report, its follow-up).
        self.idle: Callable[[dict], None] | None = None
        self.supports_system_prompt_append = False

    @property
    def alive(self) -> bool:
        return (
            self.process is not None
            and self.process.returncode is None
            and self.reader is not None
            and not self.reader.done()
        )

    async def start(self) -> dict:
        from importlib.metadata import PackageNotFoundError, version

        try:
            pcode_version = version("pcode")
        except PackageNotFoundError:
            pcode_version = "0"
        self.process = await asyncio.create_subprocess_exec(
            COMMAND,
            "acp",
            # pcode approves every request anyway (see `_answer`), and agy
            # 1.5.13 never forwards a background sub-agent's permission
            # request, which then waits about a minute and fails.
            "--dangerously-skip-permissions",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            # Never onto the terminal UI; kept only to explain a failed start.
            stderr=asyncio.subprocess.PIPE,
            cwd=self.cwd,
            env=child_env(),
            # One JSON-RPC message per line, and a session/new reply is long.
            limit=16 * 1024 * 1024,
        )
        self.reader = _spawn(self._read())
        _spawn(self._drain_stderr())
        result = await self.request(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                # No client file or terminal access: Antigravity edits files
                # itself and reports diffs, as its own CLI does.
                "clientCapabilities": {
                    "fs": {"readTextFile": False, "writeTextFile": False},
                    "terminal": False,
                },
                "clientInfo": {"name": "pcode", "title": "pcode", "version": pcode_version},
            },
        )
        if result.get("protocolVersion") != PROTOCOL_VERSION:
            raise AntigravityError(
                f"Antigravity speaks ACP version {result.get('protocolVersion')!r}; "
                f"pcode needs {PROTOCOL_VERSION}. Update the Antigravity CLI."
            )
        self.supports_system_prompt_append = _supports_system_prompt_append(result)
        return result

    async def _drain_stderr(self) -> None:
        assert self.process and self.process.stderr
        while line := await self.process.stderr.readline():
            self.stderr = [*self.stderr[-19:], line.decode(errors="replace").rstrip()]

    async def _read(self) -> None:
        assert self.process and self.process.stdout
        stopped = False
        try:
            while line := await self.process.stdout.readline():
                try:
                    message = json.loads(line)
                    if isinstance(message, dict):
                        await self._dispatch(message)
                except ValueError, TypeError, KeyError, AttributeError:
                    continue  # One malformed message must not stop the conversation.
        except asyncio.CancelledError:
            stopped = True  # By `close()`, which ends the process itself.
            raise
        finally:
            gone = AntigravityError(self._exit_reason())
            for future in self.pending.values():
                if not future.done():
                    future.set_exception(gone)
            self.pending.clear()
            if self.updates is not None:
                self.updates.put_nowait(gone)
            # A reader that failed leaves nobody to answer the process, so it
            # must not outlive it.
            if not stopped and self.process is not None and self.process.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    self.process.kill()

    def _exit_reason(self) -> str:
        detail = next((line for line in reversed(self.stderr) if line.strip()), "")
        return "Antigravity (agy acp) exited" + (f": {detail}" if detail else ".")

    async def _dispatch(self, message: dict) -> None:
        method = message.get("method")
        if method is None:
            future = self.pending.pop(message.get("id"), None)
            if future is not None and not future.done():
                if "error" in message:
                    future.set_exception(rpc_error(message["error"]))
                else:
                    future.set_result(message.get("result") or {})
                if self.updates is not None:
                    # In line, before any update read after it: agy writes a
                    # prompt's reply and what it says after the turn back to
                    # back, and a done-callback would run after both.
                    self.updates.put_nowait(future)
        elif method == "session/update":
            update = (message.get("params") or {}).get("update") or {}
            if self.updates is not None:
                self.updates.put_nowait(update)
            elif self.idle is not None:
                self.idle(update)
        elif "id" in message:
            with contextlib.suppress(AntigravityError, OSError):
                await self._answer(message)

    async def _answer(self, message: dict) -> None:
        if message["method"] == "session/request_permission":
            options = (message.get("params") or {}).get("options") or []
            # Once, never "always": that would write a lasting rule into the
            # user's Antigravity settings.
            chosen = next((o for o in options if o.get("kind") == "allow_once"), None)
            refused = next((o for o in options if o.get("kind") == "reject_once"), None)
            if chosen or refused:
                outcome = {"outcome": "selected", "optionId": (chosen or refused)["optionId"]}
            else:
                outcome = {"outcome": "cancelled"}
            await self._send(
                {"jsonrpc": "2.0", "id": message["id"], "result": {"outcome": outcome}}
            )
            return
        error = {"code": -32601, "message": f"pcode does not support {message['method']}"}
        await self._send({"jsonrpc": "2.0", "id": message["id"], "error": error})

    async def _send(self, message: dict) -> None:
        if not self.alive:
            raise AntigravityError(self._exit_reason())
        assert self.process and self.process.stdin
        self.process.stdin.write(json.dumps(message).encode() + b"\n")
        await self.process.stdin.drain()

    def begin(self, method: str, params: dict) -> asyncio.Future:
        """Send a request now; its reply resolves the returned future."""
        self.next_id += 1
        request_id = self.next_id
        future = asyncio.get_running_loop().create_future()
        # A caller that was cancelled never reads the reply; don't let a
        # failure nobody awaits be logged as unretrieved.
        future.add_done_callback(lambda f: f.cancelled() or f.exception())
        self.pending[request_id] = future

        async def send() -> None:
            try:
                await self._send(
                    {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}
                )
            except (AntigravityError, OSError) as error:
                self.pending.pop(request_id, None)
                if not future.done():
                    future.set_exception(
                        error
                        if isinstance(error, AntigravityError)
                        else AntigravityError(self._exit_reason())
                    )

        _spawn(send())
        return future

    async def request(self, method: str, params: dict) -> dict:
        return await self.begin(method, params)

    async def notify(self, method: str, params: dict) -> None:
        await self._send({"jsonrpc": "2.0", "method": method, "params": params})

    async def close(self) -> None:
        process, self.process = self.process, None
        if process is not None and process.returncode is None:
            if process.stdin:
                process.stdin.close()
            try:
                await asyncio.wait_for(process.wait(), 3)
            except TimeoutError:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), 3)
                except TimeoutError:
                    process.kill()
                    await process.wait()
        if self.reader:
            self.reader.cancel()


def rpc_error(error: dict) -> AntigravityError:
    text = str(error.get("message") or "request failed")
    data = error.get("data")
    if isinstance(data, (str, dict)) and data:
        text += f": {data if isinstance(data, str) else json.dumps(data)[:300]}"
    if error.get("code") == AUTH_REQUIRED:
        text += f". {LOGIN_HINT}"
    return AntigravityError(f"Antigravity: {text}")


# Every live connection, so pcode's exit can stop them, and the tasks running
# beside a turn, which would otherwise be garbage-collected mid-flight.
_connections: set[Connection] = set()
_background: set[asyncio.Task] = set()


async def shutdown() -> None:
    # Processes first, each closing its stdin and waiting, so `agy` can save
    # the session `--continue` resumes; cancelling their readers first would
    # leave nothing to answer them.
    connections = list(_connections)
    _connections.clear()
    await asyncio.gather(*(c.close() for c in connections), return_exceptions=True)
    from pcode.antigravity_tools import close_all

    await close_all()
    for task in list(_background):
        task.cancel()


async def _close(connection: Connection) -> None:
    _connections.discard(connection)
    await connection.close()


@dataclass
class _Session:
    connection: Connection
    session_id: str
    turns: int = 0
    # Prompts of the newest turns that ended without a reply pcode kept
    # (cancelled, or failed partway): pcode keeps such a request but not its
    # partial response, so the history is that many stamps behind.
    interrupted: list[str] = field(default_factory=list)
    # The mode the session started in, when it offers `PLAN_MODE`, and whether
    # pcode's plan mode was last applied to it as on.
    first_mode: str | None = None
    planning: bool = False
    # Fingerprint of the MCP tools and instructions this `agy` process loaded.
    # A change (e.g. `/advisor` toggled mid-session) drops and resumes the session
    # so `agy` re-runs `initialize` and `tools/list`.
    tools_signature: tuple[Any, str] | None = None
    # Tool call IDs marked as daemons (`IsDaemon: True`) so their late updates
    # never spuriously open a post-settle continuation turn.
    daemon_tools: set[str] = field(default_factory=set)
    # Thinking levels `configOptions` offered for this model (`None` when
    # `configOptions` wasn't reported, `()` when the model has no thinking control),
    # the level it started on (for `/effort default`), its active level, and the
    # `configId` the server advertised for it.
    thought_levels: tuple[str, ...] | None = None
    default_thought_level: str | None = None
    thought_level: str | None = None
    thought_config_id: str = THOUGHT_LEVEL_CONFIG


def _stamp(session_id: str, turn: int) -> str:
    return f"{session_id}#{turn}"


def _parse_stamp(response: ModelResponse) -> tuple[str, int] | None:
    if response.provider_name != SYSTEM or not response.provider_response_id:
        return None
    session_id, _, turn = response.provider_response_id.partition("#")
    return (session_id, int(turn)) if session_id and turn.isdigit() else None


def _is_daemon_input(update: dict) -> bool:
    raw_input = update.get("rawInput")
    return isinstance(raw_input, dict) and (
        raw_input.get("IsDaemon") is True or raw_input.get("isDaemon") is True
    )


def _is_wake_update(
    update: dict, *, buffering: bool = False, daemon_tools: set[str] | None = None
) -> bool:
    """Whether an unsolicited `session/update` is work worth a continuation turn."""
    kind = update.get("sessionUpdate")
    if kind in {"tool_call", "tool_call_update"}:
        call_id = str(update.get("toolCallId") or "")
        if _is_daemon_input(update):
            if daemon_tools is not None and call_id:
                daemon_tools.add(call_id)
            return False
        if daemon_tools is not None and call_id and call_id in daemon_tools:
            return False
        return True
    content = update.get("content")
    text = content.get("text", "") if isinstance(content, dict) else ""
    if kind == "agent_thought_chunk":
        return bool(text)
    if kind == "agent_message_chunk":
        return bool(text if buffering else text.strip())
    return False


class AntigravityModel(Model):
    """See the module docstring. One instance serves one conversation."""

    def __init__(self, model_name: str, *, side: bool = False, worker: bool = False) -> None:
        super().__init__()
        self._model_name = model_name
        self._side = side
        self._worker = worker
        self._session: _Session | None = None
        self._lock = asyncio.Lock()
        # Shows what Antigravity says between turns; set by the runtime.
        self.listener: Callable[[str], None] | None = None
        # Reports titles Antigravity assigns its session (`session_info_update`).
        self.title_listener: Callable[[str], None] | None = None
        # Wakes the controller to open a continuation turn when `agy` emits
        # unsolicited work after a turn ended (post-settle continuation).
        self.wake_listener: Callable[[], None] | None = None
        self._after_turn: list[str] = []
        self._quiet: asyncio.TimerHandle | None = None
        self._wake_buffer: list[dict] = []
        self._wake_pending = False
        # Set on user cancel so trailing cancel frames cannot reopen a turn;
        # cleared on the next normal prompt.
        self._wake_quarantined = False
        self._continuing_wake = False
        # The context Antigravity last reported using, in tokens.
        self.used: int | None = None
        # Compact before a prompt once `used` reaches this; set per request by
        # `AutoCompaction`, with the notice it shows.
        self.compact_at: int | None = None
        self._compacted_at: int | None = None
        self._expiry: asyncio.TimerHandle | None = None
        self._waiters = 0
        self.compaction_notice: Callable[[str], None] = lambda text: None
        # Whether pcode's plan mode is on; set per request by the plan-mode extension.
        self.plan_mode = False
        # Observed `thought_level` options for this model (`None` until a session reports
        # `configOptions`, `()` when the model offers no thinking level).
        self.thought_levels: tuple[str, ...] | None = None
        from pcode.antigravity_tools import ToolBridge

        # pcode's bridged tools, which Antigravity may call (see antigravity_tools).
        # Side questions and delegated workers receive scoped bridged tools (e.g.
        # read-only session/host tools), excluding parent-only controls.
        self.tools = ToolBridge(
            notify=self._show,
            wake_starting=lambda: self._wake_pending or self._continuing_wake,
            side=side,
            worker=worker,
        )

    def _history_matches_session(self, history: list[ModelMessage] | None) -> bool:
        if history is None or self._session is None:
            return True
        last = next((m for m in reversed(history) if isinstance(m, ModelResponse)), None)
        stamp = _parse_stamp(last) if last is not None else None
        return stamp == (self._session.session_id, self._session.turns)

    def has_pending_wake(self, history: list[ModelMessage] | None = None) -> bool:
        """Whether unsolicited post-turn updates are buffered for the live session."""
        return (
            bool(self._wake_buffer)
            and self._session is not None
            and self._session.connection.alive
            and self._history_matches_session(history)
        )

    def sync_history(self, history: list[ModelMessage]) -> None:
        """Drop a live session whose state no longer matches `history` (e.g. after `/tree`)."""
        self.clear_wake()
        if self._session is not None and not self._history_matches_session(history):
            self.reset()

    def clear_wake(self, *, quarantine: bool = False) -> None:
        """Discard buffered post-turn updates and any pending wake request."""
        was_pending = self._wake_pending or bool(self._wake_buffer)
        self._wake_buffer.clear()
        self._wake_pending = False
        if quarantine:
            self._wake_quarantined = True
            if (
                was_pending
                and not self._lock.locked()
                and self._session is not None
                and self._session.connection.alive
            ):
                _spawn(
                    self._session.connection.notify(
                        "session/cancel", {"sessionId": self._session.session_id}
                    )
                )
        if self.tools is not None:
            self.tools.abort_wake()

    def _drain_wake(self) -> list[dict]:
        buffered = list(self._wake_buffer)
        self._wake_buffer.clear()
        self._wake_pending = False
        return buffered

    def _flush_wake_as_notes(self) -> None:
        """Surface any buffered wake frames as notes before a user prompt takes over."""
        buffered = self._drain_wake()
        if not buffered:
            return
        text_parts: list[str] = []
        for update in buffered:
            kind = update.get("sessionUpdate")
            content = update.get("content")
            if kind == "agent_message_chunk" and isinstance(content, dict):
                text_parts.append(str(content.get("text", "")))
            elif kind == "tool_call_update" and update.get("status") in {"completed", "failed"}:
                if text := "".join(text_parts).strip():
                    self._show(f"Antigravity, after the turn:\n{text}")
                text_parts.clear()
                title = update.get("title") or update.get("name") or "a tool"
                failed = " (failed)" if update.get("status") == "failed" else ""
                self._show(f"Antigravity, after the turn, ran {title}{failed}")
        if text := "".join(text_parts).strip():
            self._show(f"Antigravity, after the turn:\n{text}")

    @contextlib.contextmanager
    def continuation(self) -> Iterator[None]:
        """Mark the next `request_stream` as draining an unsolicited wake (no `session/prompt`)."""
        self._continuing_wake = True
        try:
            yield
        finally:
            self._continuing_wake = False

    def _title_update(self, update: dict) -> None:
        title = update.get("title")
        if isinstance(title, str) and title.strip() and self.title_listener is not None:
            with contextlib.suppress(Exception):  # A display failure must not stop agy.
                self.title_listener(title)

    def _usage_update(self, update: dict) -> None:
        from pcode.model_metadata import positive_int, record_context_window

        used = update.get("used")
        if type(used) is int and used >= 0:
            self.used = used
        size = positive_int(update.get("size"))
        if size is not None:
            record_context_window(self, size, source=SYSTEM)
            if self._model_name:
                _remember_window(self._model_name, size)

    def _idle_update(self, update: dict) -> None:
        """Handle updates Antigravity emits between turns.

        When a controller `wake_listener` is wired, substantive updates are
        buffered and open a continuation turn (without sending `session/prompt`),
        so tools, edits, markdown, and bridged MCP calls work normally. Without
        a controller (e.g. standalone `agent.run`), fall back to transcript notes.
        """
        kind = update.get("sessionUpdate")
        content = update.get("content")
        if kind == "session_info_update":
            self._title_update(update)
            return
        if kind == "usage_update":
            self._usage_update(update)
            # A short wake reply can finish before its continuation turn starts;
            # replay the closing usage_update with the buffered output so the
            # continuation settles in DEFERRED_FINALIZE_DEBOUNCE instead of 120s.
            if (
                self.wake_listener is not None
                and not (self._side or self._worker)
                and not self._wake_quarantined
                and self._wake_buffer
            ):
                self._wake_buffer.append(update)
            return
        if self.wake_listener is not None and not (self._side or self._worker):
            if self._wake_quarantined:
                return
            daemons = self._session.daemon_tools if self._session is not None else None
            if _is_wake_update(update, buffering=bool(self._wake_buffer), daemon_tools=daemons):
                self._wake_buffer.append(update)
                if not self._wake_pending:
                    self._wake_pending = True
                    with contextlib.suppress(Exception):
                        self.wake_listener()
            return
        if kind == "agent_message_chunk" and isinstance(content, dict):
            self._after_turn.append(str(content.get("text", "")))
            if self._quiet is not None:
                self._quiet.cancel()
            self._quiet = asyncio.get_running_loop().call_later(IDLE_QUIET, self._flush_idle)
        elif kind == "tool_call_update" and update.get("status") in {"completed", "failed"}:
            self._flush_idle()
            title = update.get("title") or update.get("name") or "a tool"
            failed = " (failed)" if update.get("status") == "failed" else ""
            self._show(f"Antigravity, after the turn, ran {title}{failed}")

    def _flush_idle(self) -> None:
        if self._quiet is not None:
            self._quiet.cancel()
            self._quiet = None
        text, self._after_turn = "".join(self._after_turn).strip(), []
        if text:
            self._show(f"Antigravity, after the turn:\n{text}")

    def _show(self, text: str) -> None:
        if self.listener is not None:
            with contextlib.suppress(Exception):  # A display failure must not stop agy.
                self.listener(text)

    @property
    def model_name(self) -> str:
        return self._model_name

    @property
    def system(self) -> str:
        return SYSTEM

    def _arm_expiry(self) -> None:
        if not (self._side or self._worker) or self._waiters:
            return
        if self._expiry is not None:
            self._expiry.cancel()

        def expire() -> None:
            self._expiry = None
            if not self._lock.locked() and not self._waiters:
                self.reset()

        # Nothing retires a side model made for one job (a title, a
        # summary); a follow-up after this replays into a new session.
        with contextlib.suppress(RuntimeError):
            self._expiry = asyncio.get_running_loop().call_later(SIDE_IDLE, expire)

    def _detach_session(self) -> None:
        """Detach `self._session` synchronously and close its connection in the background.

        Used on cancellation inside `_lock`, so a delayed `_drop()` can never
        race with a newer session the next request opens after `_lock` releases.
        """
        session, self._session = self._session, None
        if session is not None:
            session.connection.idle = None
            _spawn(_close(session.connection))

    def _detach_idle(self) -> None:
        if self._session is not None:
            self._session.connection.idle = None
        if self._quiet is not None:
            self._quiet.cancel()
            self._quiet = None
        self._after_turn = []
        self.clear_wake()
        self._wake_quarantined = False

    def reset(self) -> None:
        """Drop the live `agy` session for a new conversation, keeping listeners wired."""
        if self._expiry is not None:
            self._expiry.cancel()
            self._expiry = None
        self._detach_idle()
        with contextlib.suppress(RuntimeError):
            asyncio.get_running_loop()

            async def drop() -> None:
                async with self._lock:
                    await self._drop()
                    if self.tools is not None:
                        await self.tools.close()

            _spawn(drop())

    def retire(self) -> None:
        """Stop this model's process once any turn on it ends; for a replaced agent."""
        # What this conversation's agy said after its last turn must not
        # land in whichever conversation the view shows next.
        self.listener = None
        self.title_listener = None
        self.wake_listener = None
        self.reset()

    async def request(
        self,
        messages: list[ModelMessage],
        model_settings: ModelSettings | None,
        model_request_parameters: ModelRequestParameters,
    ) -> ModelResponse:
        async with self.request_stream(
            messages, model_settings, model_request_parameters
        ) as stream:
            async for _ in stream:
                pass
        return stream.get()

    @asynccontextmanager
    async def request_stream(
        self,
        messages: list[ModelMessage],
        model_settings: ModelSettings | None,
        model_request_parameters: ModelRequestParameters,
        run_context: RunContext[Any] | None = None,
    ) -> AsyncIterator[StreamedResponse]:
        from pcode.claude_sdk.workspace import CWD_SETTING

        continuing = self._continuing_wake
        self._continuing_wake = False
        prompt: list[dict] = []
        interrupted: list = []
        earlier: list[ModelMessage] = []
        cwd = str((model_settings or {}).get(CWD_SETTING) or os.getcwd())
        raw_effort = (model_settings or {}).get(EFFORT_SETTING)
        effort = raw_effort if isinstance(raw_effort, str) and raw_effort else None
        plain_side = self._side and not getattr(model_request_parameters, "function_tools", None)
        if not continuing:
            # A new user turn lifts any cancel quarantine from an earlier Ctrl+C.
            self._wake_quarantined = False
            # Before the lock or a process: a refusal must cost nothing.
            prompt, interrupted = user_prompt(messages, side=self._side)
            if plain_side:
                # A plain completion (a title, a summary, an advisor's answer): its
                # instructions are the task. A side question's are pcode's own
                # system prompt, about tools Antigravity doesn't have.
                parts = getattr(model_request_parameters, "instruction_parts", None) or []
                task = "\n\n".join(part.content for part in parts) or (
                    getattr(messages[-1], "instructions", None) or ""
                )
                if task.strip():
                    prompt = [{"type": "text", "text": task}, *prompt]
            # Anything said since the last turn shows before this one starts.
            self._flush_wake_as_notes()
            self._flush_idle()
            earlier = messages[:-1]
            if interrupted:
                earlier = [*earlier, ModelRequest(parts=interrupted)]
        self._waiters += 1
        try:
            await self._lock.acquire()
        finally:
            self._waiters -= 1
        if self._expiry is not None:
            self._expiry.cancel()
            self._expiry = None
        handed_off = False
        # Set first: a new session lists the tools as this turn has them.
        # Plain side completions (titles, summaries, advisor) bridge no tools.
        if self.tools is not None:
            self.tools.begin_turn(None if plain_side else run_context)
        try:
            if continuing:
                session = self._session
                if (
                    session is None
                    or not session.connection.alive
                    or not self._history_matches_session(messages[:-1])
                ):
                    self.clear_wake()
                    reason = (
                        session.connection._exit_reason()
                        if session is not None and not session.connection.alive
                        else "Antigravity session is not running."
                    )
                    raise AntigravityError(reason)
                stream = AntigravityStream(
                    model_request_parameters=model_request_parameters,
                    model_id=self._model_name,
                    session=session,
                    continuation=True,
                    drain_wake=self._drain_wake,
                    run_context=run_context,
                    notify=self._show,
                    on_usage=self._usage_update,
                    on_title=self._title_update,
                )
            else:
                session, replay = await self._prepare(
                    earlier,
                    _asked(interrupted),
                    cwd,
                    prompt_bytes=_text_bytes(prompt),
                    run_context=run_context,
                    allow_spill=not plain_side,
                )
                await self._apply_effort(session, effort)
                if not (self._side or self._worker):
                    await self._ready(session, compact=session.turns > 0)
                stream = AntigravityStream(
                    model_request_parameters=model_request_parameters,
                    model_id=self._model_name,
                    session=session,
                    prompt=[*replay, *prompt],
                    replayed=len(replay),
                    run_context=run_context,
                    notify=self._show,
                    on_usage=self._usage_update,
                    on_title=self._title_update,
                )
            try:
                yield stream
            finally:
                if stream.unfinished or (stream.failed and stream.events_seen):
                    # As pcode keeps the request: the prompt, then any steering.
                    session.interrupted.append("\n".join(t for t in stream.asked if t))
                if stream.unfinished:
                    # Quarantine trailing cancel updates so a user Ctrl+C never
                    # immediately spawns a post-settle continuation turn.
                    self._wake_quarantined = True
                    self.clear_wake()
                    # Stopped mid-turn. The run's cancellation is level-triggered,
                    # so waiting for Antigravity to confirm here would itself be
                    # cancelled; a task does it, keeping the lock so the next
                    # prompt waits for the turn to really stop.
                    _spawn(self._settle(stream))
                    handed_off = True
                elif stream.broken:
                    await asyncio.shield(self._drop())
        finally:
            if self.tools is not None:
                self.tools.cancel_calls()
            if not handed_off:
                try:
                    # The next prompt waits for a cancelled worker's cleanup.
                    if self.tools is not None:
                        await asyncio.shield(self.tools.drained())
                finally:
                    self._lock.release()
                    self._arm_expiry()

    def _sync_thought_config(
        self, session: _Session, result: dict, *, record_default: bool, fresh: bool = False
    ) -> None:
        """Record `thought_level` options and current level from a `configOptions` reply."""
        if not isinstance(result.get("configOptions"), list):
            return
        active_model = _config_value(result, MODEL_CONFIG)
        if self._model_name and active_model and active_model != self._model_name:
            return
        option = _config_option(result, THOUGHT_LEVEL_CONFIG)
        levels = tuple(_option_values(option))
        current = _config_value(result, THOUGHT_LEVEL_CONFIG) or None
        session.thought_levels = levels
        session.thought_level = current
        session.thought_config_id = _config_id(result, THOUGHT_LEVEL_CONFIG)
        model_name = self._model_name or active_model
        if record_default:
            if fresh:
                session.default_thought_level = current
            else:
                cached_default = cached_default_thought_level(model_name) if model_name else None
                session.default_thought_level = (
                    cached_default if cached_default in levels else current
                )
        self.thought_levels = levels
        if model_name:
            _remember_thought_levels(
                model_name, levels, default_level=current if (record_default and fresh) else None
            )

    async def _apply_effort(self, session: _Session, effort: str | None) -> None:
        """Align the session's ACP `thought_level` config option with `effort`."""
        if session.thought_levels == ():
            return
        if effort is None:
            target = session.default_thought_level
        elif session.thought_levels is not None:
            target = _fit_thought_level(effort, session.thought_levels)
        else:
            target = effort
        if target is None or target == session.thought_level:
            return
        try:
            updated = await session.connection.request(
                "session/set_config_option",
                {
                    "sessionId": session.session_id,
                    "configId": session.thought_config_id,
                    "value": target,
                },
            )
        except AntigravityError:
            if not session.connection.alive:
                self._detach_session()
            raise
        except BaseException:
            # Cancelled partway: the session may be mid-command, so start it over.
            self._detach_session()
            raise
        if isinstance(updated, dict) and isinstance(updated.get("configOptions"), list):
            self._sync_thought_config(session, updated, record_default=False)
        else:
            session.thought_level = target

    async def _ready(self, session: _Session, *, compact: bool) -> None:
        """Before a prompt: put the session in pcode's mode, and make room in its context."""
        connection = session.connection
        try:
            if session.first_mode is not None and self.plan_mode != session.planning:
                # Only when pcode's plan mode changes: Antigravity leaves its
                # own once the user approves the plan, and must not be put back.
                wanted = PLAN_MODE if self.plan_mode else session.first_mode
                session.planning = self.plan_mode
                # A refusal leaves the mode alone; the prompt still goes.
                with contextlib.suppress(AntigravityError):
                    await connection.request(
                        "session/set_mode", {"sessionId": session.session_id, "modeId": wanted}
                    )
            limit, used = self.compact_at, self.used
            # Not again until the context has grown: a compaction that freed
            # little would otherwise rerun before every prompt.
            grown = self._compacted_at is None or (used or 0) > self._compacted_at * 1.2
            if compact and limit is not None and used is not None and used >= limit and grown:
                self.compaction_notice("Compacting context automatically…")
                with contextlib.suppress(AntigravityError):
                    await self._compact(session)
        except BaseException:
            # Cancelled partway: the session may be mid-command, so start it over.
            self._detach_session()
            raise

    async def _compact(self, session: _Session, focus: str = "") -> None:
        """Have Antigravity compact its own context. Not a turn of the conversation."""
        text = f"{COMPACT_COMMAND} {focus}".strip()
        previous = session.connection.idle

        def during_compact(update: dict) -> None:
            kind = update.get("sessionUpdate")
            if kind == "usage_update":
                self._usage_update(update)
            elif kind == "session_info_update":
                self._title_update(update)

        session.connection.idle = during_compact
        try:
            await session.connection.request(
                "session/prompt",
                {"sessionId": session.session_id, "prompt": [{"type": "text", "text": text}]},
            )
        finally:
            if session.connection.idle is during_compact:
                session.connection.idle = previous
        self._compacted_at = self.used

    async def compact(
        self, history: list[ModelMessage], cwd: str, focus: str = ""
    ) -> tuple[int | None, int | None] | None:
        """`/compact`: the context used before and after, or None with nothing to compact.

        `history` finds the conversation's session, resuming it after a restart.
        """
        earlier, asked = list(history), []
        if earlier and isinstance(earlier[-1], ModelRequest):
            asked = _asked(earlier[-1].parts)
        async with self._lock:
            session, replay = await self._prepare(earlier, asked, cwd, allow_spill=False)
            if replay or not session.turns:
                # A session that never saw this conversation holds none of it.
                return None
            before = self.used
            try:
                await self._compact(session, focus)
            except BaseException:
                self._detach_session()
                raise
            return before, self.used

    async def _settle(self, stream: AntigravityStream) -> None:
        try:
            if not await stream.stop():
                await self._drop()
        except Exception:
            await self._drop()
        finally:
            try:
                if self.tools is not None:
                    await self.tools.drained()
            finally:
                self._lock.release()
                self._arm_expiry()

    async def _drop(self) -> None:
        session, self._session = self._session, None
        if session is not None:
            session.connection.idle = None
            await _close(session.connection)

    async def _prepare(
        self,
        earlier: list[ModelMessage],
        interrupted: list[str],
        cwd: str,
        *,
        prompt_bytes: int = 0,
        run_context: RunContext[Any] | None = None,
        allow_spill: bool = True,
    ) -> tuple[_Session, list[dict]]:
        """The session to prompt, and any replay to send before the new prompt.

        `interrupted` holds the prompts of unanswered requests ending `earlier`.
        """
        last = next((m for m in reversed(earlier) if isinstance(m, ModelResponse)), None)
        stamp = _parse_stamp(last) if last is not None else None
        session = self._session
        current_tools = self.tools.signature() if self.tools is not None else None
        if session is not None:
            current = (session.session_id, session.turns)
            # The session's extra turns are the interrupted requests this
            # history kept (with no reply before them when they were its first).
            ahead = len(session.interrupted)
            behind = session.turns - ahead
            same = stamp == current or (
                ahead > 0
                and interrupted == session.interrupted
                and stamp == ((session.session_id, behind) if behind > 0 else None)
            )
            # Another process (a fork resumed elsewhere) may have moved it on.
            # No entry at all means the ledger can't be kept, not that it moved.
            ours = newest_turn(session.session_id) in (None, session.turns)
            # None means no turn context (e.g. `/compact` or unstreamed): keep the session.
            tools_match = current_tools is None or session.tools_signature == current_tools
            if (
                session.connection.alive
                and session.connection.cwd == cwd
                and same
                and ours
                and tools_match
            ):
                if stamp == current:
                    # A kept reply: nothing after it is unanswered any more.
                    session.interrupted = []
                return session, []
            await self._drop()
        resumable = (
            not (self._side or self._worker)
            and stamp is not None
            and _all_from(earlier, stamp[0])
            and newest_turn(stamp[0]) == stamp[1]
        )
        connection = Connection(cwd)
        _connections.add(connection)
        try:
            await connection.start()
            servers = await self.tools.servers() if self.tools is not None else []
            result = None
            if resumable:
                assert stamp is not None
                with contextlib.suppress(AntigravityError):
                    result = await connection.request(
                        "session/resume",
                        {"sessionId": stamp[0], "cwd": cwd, "mcpServers": servers},
                    )
                    result = {**result, "sessionId": stamp[0]}
            replay = result is None
            if result is None:
                params: dict[str, Any] = {"cwd": cwd, "mcpServers": servers}
                # Sent on session/new only: agy keeps the appended prompt for the
                # session's lifetime (not updated on session/resume).
                if connection.supports_system_prompt_append:
                    if self.tools is not None:
                        append: str | None = self.tools.system_prompt_append()
                    else:
                        from pcode.antigravity_tools import repo_context_instructions

                        append = repo_context_instructions(run_context)
                    if append:
                        params["_meta"] = {"antigravity": {"systemPrompt": {"append": append}}}
                result = await connection.request("session/new", params)
            _remember_models(result)
            session_id = result["sessionId"]
            config_source = result
            current_model = _config_value(result, MODEL_CONFIG)
            if self._model_name and self._model_name != current_model:
                try:
                    updated = await connection.request(
                        "session/set_config_option",
                        {
                            "sessionId": session_id,
                            "configId": _config_id(result, MODEL_CONFIG),
                            "value": self._model_name,
                        },
                    )
                except AntigravityError as error:
                    offered = _offered(result) or list(cached_models())
                    raise AntigravityError(
                        f"{error} Models this account offers: {', '.join(offered)}"
                        if offered
                        else str(error)
                    ) from error
                # Never fall back to `result`: it describes the server's default model.
                config_source = updated if isinstance(updated, dict) else {}
                _remember_models(config_source)
        except BaseException:
            # Cancelled here, an await would be cancelled too; close beside it.
            _spawn(_close(connection))
            raise
        # Attach only once the session is on `self._model_name`, so an early
        # `usage_update` for `session/new`'s default model isn't cached under it.
        connection.idle = self._idle_update
        turns = stamp[1] if not replay and stamp is not None else 0
        self._session = _Session(connection, session_id, turns, tools_signature=current_tools)
        self._sync_thought_config(self._session, config_source, record_default=True, fresh=replay)
        if replay:
            # A new session: nothing is known about its context yet.
            self.used = self._compacted_at = None
        modes = result.get("modes") or {}
        available = [m.get("id") for m in modes.get("availableModes") or [] if isinstance(m, dict)]
        if PLAN_MODE in available:
            current = modes.get("currentModeId")
            self._session.planning = current == PLAN_MODE
            self._session.first_mode = (
                current
                if current != PLAN_MODE
                else next((m for m in available if m != PLAN_MODE), None)
            )
        blocks: list[dict] = []
        if replay and earlier:
            budget = min(REPLAY_MAX_BYTES, PROMPT_BUDGET_BYTES - prompt_bytes)
            intro = SIDE_REPLAY_INTRO if self._side else REPLAY_INTRO
            spill = _spill_replay if allow_spill else None
            text = (
                replay_text(earlier, intro=intro, max_bytes=budget, spill=spill)
                if budget > 0
                else ""
            )
            if text:
                blocks = [{"type": "text", "text": text}]
        return self._session, blocks


def _all_from(earlier: list[ModelMessage], session_id: str) -> bool:
    """Whether every response in `earlier` came from this one Antigravity session."""
    responses = [m for m in earlier if isinstance(m, ModelResponse)]
    return bool(responses) and all((_parse_stamp(m) or ("",))[0] == session_id for m in responses)


def _markers() -> tuple[str, ...]:
    from pcode.live import CANCELLED_MARKER, UNANSWERED_MARKER

    return (CANCELLED_MARKER, UNANSWERED_MARKER)


def _render_parts(parts: list) -> tuple[list[dict], list[str]]:
    """Render one turn's request parts to ACP blocks, matching what `user_prompt` sends."""
    blocks: list[dict] = []
    unsupported: list[str] = []
    for part in parts:
        if isinstance(part, UserPromptPart):
            if (
                isinstance(part.content, str)
                and part.content.strip() == PROVIDER_CONTINUATION_PROMPT
            ):
                continue
            more, refused = content_blocks(part.content)
        elif isinstance(part, ToolReturnPart):
            text = f"[pcode] {part.tool_name} returned:\n{part.model_response_str()}"
            more, refused = [{"type": "text", "text": text}], []
        elif isinstance(part, RetryPromptPart):
            more, refused = [{"type": "text", "text": part.model_response()}], []
        elif isinstance(part, DataCapabilityPart) and getattr(part, "tool_name", None):
            # Only background tool results: other DataCapabilityPart subclasses
            # (PlanReminderPart, RepoContext) describe pcode's own loop.
            more, refused = _data_blocks(part)
        else:
            continue  # Instructions for pcode's own loop, which Antigravity doesn't run.
        blocks += more
        unsupported += refused
    return blocks, unsupported


def _asked(parts: list) -> list[str]:
    """Each unanswered prompt's text in a merged request, to compare with what was sent.

    Every one ends in one of pcode's markers.
    """
    markers = _markers()
    asked: list[str] = []
    segment: list = []
    for part in parts:
        if isinstance(part, UserPromptPart) and part.content in markers:
            blocks, _ = _render_parts(segment)
            if not blocks:
                blocks = [{"type": "text", "text": CONTINUE}]
            asked.append(_asked_blocks(blocks))
            segment = []
            continue
        segment.append(part)
    return asked


def _asked_blocks(blocks: list[dict]) -> str:
    return "\n".join(b["text"] for b in blocks if b.get("type") == "text")


def user_prompt(messages: list[ModelMessage], *, side: bool = False) -> tuple[list[dict], list]:
    """The new request as ACP content blocks.

    Besides the user's message it may hold what pcode tells any model: job
    and worker notices, a background result, tool results a turn on another
    model left (after a mid-turn `/model`), a retry prompt. Antigravity made
    none of those calls, so each is written out as text. A continuation with
    no message at all (`/continue`) sends `CONTINUE`.

    A request a cancelled or failed turn left unanswered ends in one of pcode's
    markers (see `live.mark_unanswered`), and Pydantic AI merges the next prompt
    into it. Its parts up to the marker are returned second: they belong to
    the history, not the new prompt.

    For a `/btw` side question (`side=True`), a mid-turn request may hold the
    in-flight turn's prompt or tool results ahead of the side question's own
    `UserPromptPart`; those earlier parts are returned second so they join the
    replayed context rather than the side prompt.
    """
    request = messages[-1] if messages else None
    if not isinstance(request, ModelRequest):
        raise AntigravityError(NO_PROMPT)
    markers = _markers()
    marker = max(
        (
            i
            for i, part in enumerate(request.parts)
            if isinstance(part, UserPromptPart) and part.content in markers
        ),
        default=-1,
    )
    if side:
        last_user = max(
            (
                i
                for i in range(marker + 1, len(request.parts))
                if isinstance(request.parts[i], UserPromptPart)
            ),
            default=-1,
        )
        if last_user < 0:
            raise AntigravityError(NO_PROMPT)
        interrupted, parts = list(request.parts[:last_user]), request.parts[last_user:]
    else:
        interrupted, parts = list(request.parts[: marker + 1]), request.parts[marker + 1 :]
    blocks, unsupported = _render_parts(parts)
    if unsupported:
        raise AntigravityError(
            f"Antigravity can take text and images here, not {', '.join(sorted(set(unsupported)))}."
        )
    if not blocks:
        blocks = [{"type": "text", "text": CONTINUE}]
    return blocks, interrupted


def _data_blocks(part: DataCapabilityPart) -> tuple[list[dict], list[str]]:
    """Something a capability hands the model (a background tool's result), labelled as such."""
    content = part.model_content()
    if not isinstance(content, (str, list, tuple)):
        content = json.dumps(content, default=str)
    blocks, refused = content_blocks(content)
    if not blocks:
        return [], refused
    tool = getattr(part, "tool_name", None)
    failed = " (failed)" if getattr(part, "outcome", None) == "failed" else ""
    label = f"[pcode] Result of the {tool} call that was running{failed}:" if tool else "[pcode]"
    return [{"type": "text", "text": label}, *blocks], refused


def content_blocks(content) -> tuple[list[dict], list[str]]:
    """User content as ACP blocks, and the type names of any it can't carry."""
    blocks: list[dict] = []
    unsupported: list[str] = []
    for item in [content] if isinstance(content, str) else list(content):
        if isinstance(item, TextContent):
            item = item.content
        if isinstance(item, str):
            if item.strip():
                blocks.append({"type": "text", "text": item})
        elif isinstance(item, BinaryContent) and item.is_image:
            data = base64.b64encode(item.data).decode()
            blocks.append({"type": "image", "mimeType": item.media_type, "data": data})
        else:
            unsupported.append(type(item).__name__)
    return blocks, unsupported


def _clip(text: str, limit: int = 2000) -> str:
    return text if len(text) <= limit else text[:limit] + " …"


def _utf8_len(text: str) -> int:
    return len(text.encode("utf-8"))


def _text_bytes(blocks: list[dict]) -> int:
    return sum(_utf8_len(b["text"]) for b in blocks if b.get("type") == "text")


def _clip_bytes(text: str, limit: int) -> str:
    """Clip `text` so its UTF-8 encoding is at most `limit` bytes."""
    if limit <= 0:
        return ""
    encoded = text.encode("utf-8")
    if len(encoded) <= limit:
        return text
    suffix = " …"
    suffix_bytes = _utf8_len(suffix)
    if limit <= suffix_bytes:
        return encoded[:limit].decode("utf-8", errors="ignore")
    prefix = encoded[: limit - suffix_bytes].decode("utf-8", errors="ignore")
    return f"{prefix}{suffix}" if prefix else ""


def _replay_spill_dir() -> Path:
    return _state_dir() / "replays"


def _spill_replay(text: str) -> Path | None:
    """Write the full unbounded replay transcript to a private file and sweep old ones."""
    try:
        directory = _replay_spill_dir()
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(directory, 0o700)
        cutoff = time.time() - REPLAY_SPILL_TTL_SECONDS
        with contextlib.suppress(OSError):
            for existing in directory.iterdir():
                if existing.suffix != ".txt":
                    continue
                with contextlib.suppress(OSError):
                    if existing.stat().st_mtime < cutoff:
                        existing.unlink()
        path = directory / f"{uuid.uuid4().hex}.txt"
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            handle = os.fdopen(fd, "w", encoding="utf-8")
        except BaseException:
            os.close(fd)
            with contextlib.suppress(OSError):
                path.unlink()
            raise
        try:
            with handle:
                handle.write(text)
        except BaseException:
            with contextlib.suppress(OSError):
                path.unlink()
            raise
        os.chmod(path, 0o600)
        return path
    except OSError:
        return None


def _replay_notice(notice: str, spill_path: Path | None) -> str:
    if spill_path is None:
        return notice
    return (
        f"{notice} [Full transcript saved to {spill_path} — "
        "use read_file or grep if exact earlier details are needed.]"
    )


def _replay_entries(
    messages: list[ModelMessage],
    *,
    recent_messages: int | None = None,
    old_tool_call_chars: int | None = None,
    old_tool_return_chars: int | None = None,
    drop_old_tools: bool = False,
) -> list[str]:
    cutoff = 0 if recent_messages is None else max(0, len(messages) - recent_messages)
    entries: list[str] = []
    for index, message in enumerate(messages):
        older = index < cutoff
        for part in message.parts:
            if isinstance(part, UserPromptPart):
                items = [part.content] if isinstance(part.content, str) else list(part.content)
                content = " ".join(
                    item.content if isinstance(item, TextContent) else item
                    for item in items
                    if isinstance(item, (str, TextContent))
                )
                if content.strip() and content.strip() != PROVIDER_CONTINUATION_PROMPT:
                    entries.append(f"[User]\n{content}")
            elif isinstance(part, DataCapabilityPart) and getattr(part, "tool_name", None):
                blocks, _ = _data_blocks(part)
                text = _asked_blocks(blocks)
                if text.strip():
                    entries.append(f"[User]\n{_clip(text)}")
            elif isinstance(part, TextPart) and part.content.strip():
                entries.append(f"[Assistant]\n{part.content}")
            elif isinstance(part, (NativeToolCallPart, ToolCallPart)):
                if older and drop_old_tools:
                    continue
                limit = old_tool_call_chars if older and old_tool_call_chars is not None else 500
                raw_args = (
                    part.args if isinstance(part.args, str) else json.dumps(part.args, default=str)
                )
                args = _clip(raw_args, limit)
                entries.append(f"[Assistant ran {part.tool_name}: {args}]")
            elif isinstance(part, (NativeToolReturnPart, ToolReturnPart)):
                if older and drop_old_tools:
                    continue
                limit = (
                    old_tool_return_chars if older and old_tool_return_chars is not None else 2000
                )
                entries.append(
                    f"[{part.tool_name} returned: {_clip(part.model_response_str(), limit)}]"
                )
    return entries


def replay_text(
    messages: list[ModelMessage],
    *,
    intro: str = REPLAY_INTRO,
    max_bytes: int | None = REPLAY_MAX_BYTES,
    spill: Callable[[str], Path | None] | None = None,
) -> str:
    """Earlier turns as a transcript, for a session that never saw them.

    When `max_bytes` is set and the full transcript would exceed it, compacts
    in tiers before dropping turns:
    1. Condense older tool calls/returns while keeping the newest messages,
       user prompts, assistant prose, and background worker results intact.
    2. Drop older tool calls/returns altogether while preserving prose and
       the newest messages.
    3. Fall back to keeping the newest entries that fit within `max_bytes`.
    """
    full_entries = _replay_entries(messages)
    header = f"{intro}\n\n<conversation>\n"
    footer = "\n</conversation>"
    body = "\n\n".join(full_entries)
    full = f"{header}{body}{footer}" if body else f"{header}</conversation>"
    if max_bytes is None or _utf8_len(full) <= max_bytes:
        return full

    minimal_plain = f"{header}{REPLAY_OMITTED}{footer}"
    if _utf8_len(minimal_plain) > max_bytes:
        return ""

    spill_path = spill(full) if spill is not None else None
    tail_entries = full_entries

    if len(messages) > REPLAY_RECENT_MESSAGES:
        condensed_entries = _replay_entries(
            messages,
            recent_messages=REPLAY_RECENT_MESSAGES,
            old_tool_call_chars=REPLAY_OLD_TOOL_CALL_CHARS,
            old_tool_return_chars=REPLAY_OLD_TOOL_RETURN_CHARS,
        )
        condensed_notice = _replay_notice(REPLAY_CONDENSED, spill_path)
        condensed_body = "\n\n".join(condensed_entries)
        condensed = (
            f"{header}{condensed_notice}\n\n{condensed_body}{footer}"
            if condensed_body
            else f"{header}{condensed_notice}{footer}"
        )
        if _utf8_len(condensed) <= max_bytes:
            return condensed

        dropped_entries = _replay_entries(
            messages,
            recent_messages=REPLAY_RECENT_MESSAGES,
            drop_old_tools=True,
        )
        dropped_notice = _replay_notice(REPLAY_TOOLS_DROPPED, spill_path)
        dropped_body = "\n\n".join(dropped_entries)
        dropped = (
            f"{header}{dropped_notice}\n\n{dropped_body}{footer}"
            if dropped_body
            else f"{header}{dropped_notice}{footer}"
        )
        if _utf8_len(dropped) <= max_bytes:
            return dropped
        tail_entries = dropped_entries

    omitted_notice = _replay_notice(REPLAY_OMITTED, spill_path)
    omitted_header = f"{header}{omitted_notice}\n\n"
    budget = max_bytes - _utf8_len(omitted_header) - _utf8_len(footer)
    if budget <= 0:
        minimal = f"{header}{omitted_notice}{footer}"
        return minimal if _utf8_len(minimal) <= max_bytes else minimal_plain

    kept: list[str] = []
    used = 0
    for entry in reversed(tail_entries):
        sep = 2 if kept else 0
        size = _utf8_len(entry) + sep
        if used + size > budget:
            remaining = budget - used - sep
            if _utf8_len(entry) > budget and remaining >= MIN_CLIP_BYTES:
                clipped = _clip_bytes(entry, remaining)
                if clipped:
                    kept.append(clipped)
            break
        kept.append(entry)
        used += size

    if kept:
        kept.reverse()
        return f"{omitted_header}{'\n\n'.join(kept)}{footer}"
    return f"{header}{omitted_notice}{footer}"


def _tool_args(update: dict) -> dict:
    raw = update.get("rawInput")
    args = dict(raw) if isinstance(raw, dict) else {}
    # Antigravity capitalizes argument names (`Path`, `CommandLine`); pcode's
    # tool rows read lower-case ones.
    args = {k[:1].lower() + k[1:]: v for k, v in args.items()}
    for key in ("commandLine", "command_line"):
        if args.get(key) and not args.get("command"):
            args["command"] = args[key]
            break
    if update.get("title") and "title" not in args:
        args["title"] = update["title"]
    return args


def _tool_output(update: dict) -> object:
    texts = []
    for block in update.get("content") or []:
        if not isinstance(block, dict):
            continue
        if block.get("type") == "diff":
            texts.append(f"Edited {block.get('path', '')}")
        elif isinstance(inner := block.get("content"), dict) and inner.get("type") == "text":
            texts.append(str(inner.get("text", "")))
    if texts:
        return "\n".join(texts)
    raw = update.get("rawOutput")
    return raw if raw not in (None, {}) else ""


@dataclass
class _ToolState:
    """Tracked metadata for a tool call in the current turn."""

    kind: str = ""
    title: str = ""
    is_daemon: bool = False
    status: str = ""

    @property
    def active(self) -> bool:
        """Whether this non-daemon tool is still pending or running."""
        return self.status in {"pending", "in_progress"} and not self.is_daemon

    @property
    def outlives_prompt(self) -> bool:
        """Whether `agy` keeps running this tool after `session/prompt` settles."""
        if self.status != "in_progress":
            return False
        if self.title in SCHEDULE_TITLES:
            return True
        return self.kind == "execute" and not self.is_daemon


def _is_subagent_launch(name: str, state: _ToolState) -> bool:
    """Whether a tool call is Antigravity's native async `invoke_subagent` launcher."""
    return name in SUBAGENT_LAUNCH_NAMES or state.title in SUBAGENT_LAUNCH_TITLES


@dataclass
class AntigravityStream(StreamedResponse):
    """One Antigravity turn as Pydantic AI stream events."""

    model_id: str = ""
    session: _Session | None = None
    prompt: list[dict] = field(default_factory=list)
    # How many of `prompt`'s blocks replay earlier history.
    replayed: int = 0
    # True for a post-settle continuation turn: drains buffered wake updates
    # and listens for unsolicited `agy` updates without sending `session/prompt`.
    continuation: bool = False
    drain_wake: Callable[[], list[dict]] | None = None
    # The run, for the user's steering messages; None outside an agent run.
    run_context: RunContext[Any] | None = None
    # Tells the user something about the turn (the model's `_show`).
    notify: Callable[[str], None] = lambda text: None
    # Records context window sizes reported by ACP `usage_update` notifications.
    on_usage: Callable[[dict], None] = lambda update: None
    # Reports session titles emitted by ACP `session_info_update` notifications.
    on_title: Callable[[dict], None] = lambda update: None
    started: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    # The newest prompt's reply. A steering message is a further prompt, and
    # Antigravity ends the one before it as soon as it arrives.
    reply: asyncio.Future | None = field(default=None, init=False)
    replies: list[asyncio.Future] = field(default_factory=list, init=False)
    # What this turn has told Antigravity: the prompt, then each steering message.
    asked: list[str] = field(default_factory=list, init=False)
    broken: bool = field(default=False, init=False)
    # True while `session/prompt` has settled (or in a continuation turn) and we
    # are waiting for background tools and unsolicited follow-up replies to finish.
    holding: bool = field(default=False, init=False)
    # True when the most recent non-daemon tool activity was a completed native
    # `invoke_subagent` launch; cleared as soon as another non-daemon tool runs.
    subagent_launched: bool = field(default=False, init=False)
    # True while holding after `session/prompt` (or a held reply's `usage_update`)
    # waiting for that launched subagent's first substantive wakeup update.
    awaiting_subagent: bool = field(default=False, init=False)
    part_count: int = field(default=0, init=False)
    part_kind: str = field(default="", init=False)
    calls: dict[str, tuple[str, dict]] = field(default_factory=dict, init=False)
    call_inputs: set[str] = field(default_factory=set, init=False)
    tools: dict[str, _ToolState] = field(default_factory=dict, init=False)
    text_seen: bool = field(default=False, init=False)
    events_seen: bool = field(default=False, init=False)
    failed: bool = field(default=False, init=False)
    context_used: int | None = field(default=None, init=False)
    # Deferred steering commits: sent to `agy` immediately, committed to the
    # transcript/journal once the in-flight model step and its tools finish.
    pending_steer_commits: list[Callable[[], None]] = field(default_factory=list, init=False)
    flush_after_part_end: list[Callable[[], None]] = field(default_factory=list, init=False)
    step_open: bool = field(default=False, init=False)
    step_emitted: bool = field(default=False, init=False)

    def __aiter__(self) -> AsyncIterator[ModelResponseStreamEvent]:
        base = super().__aiter__()

        async def iterate() -> AsyncIterator[ModelResponseStreamEvent]:
            try:
                async for event in base:
                    yield event
                    if isinstance(event, PartEndEvent) and self.flush_after_part_end:
                        self._run_commits(self.flush_after_part_end)
            finally:
                self._flush_all_steers()

        return iterate()

    @staticmethod
    def _run_commits(commits: list[Callable[[], None]]) -> None:
        batch = list(commits)
        commits.clear()
        for commit in batch:
            commit()

    def _stage_pending_steers(self) -> None:
        """Commit pending steering now, or right after the open text/thinking part closes."""
        if not self.pending_steer_commits:
            return
        if self.part_kind in {"text", "thinking"}:
            # Force the next step's output to start a new part so `PartEndEvent`
            # settles step 1's message/thinking ahead of the steering record.
            self._vendor("steer")
            self.flush_after_part_end.extend(self.pending_steer_commits)
            self.pending_steer_commits.clear()
        else:
            self._vendor("steer")
            self._run_commits(self.pending_steer_commits)

    def _flush_all_steers(self) -> None:
        self._run_commits(self.flush_after_part_end)
        self._run_commits(self.pending_steer_commits)

    @property
    def model_name(self) -> str:
        return self.model_id

    @property
    def provider_name(self) -> str:
        return SYSTEM

    @property
    def provider_url(self) -> str | None:
        return None

    @property
    def timestamp(self) -> datetime:
        return self.started

    @property
    def unfinished(self) -> bool:
        return (self.reply is not None and not self.reply.done()) or self.holding

    def _has_outliving_tools(self) -> bool:
        return any(tool.outlives_prompt for tool in self.tools.values())

    def _has_running_tools(self) -> bool:
        return any(tool.active for tool in self.tools.values())

    def _should_hold(self, reply: asyncio.Future) -> bool:
        if reply.cancelled() or reply.exception() is not None:
            return False
        if reply.result().get("stopReason") == "cancelled":
            return False
        return self._has_outliving_tools() or self.subagent_launched

    def _vendor(self, kind: str) -> str:
        """A part ID that starts a new part whenever the kind of output changes."""
        if kind != self.part_kind:
            self.part_kind = kind
            self.part_count += 1
        return f"{kind}-{self.part_count}"

    async def _get_event_iterator(self) -> AsyncIterator[ModelResponseStreamEvent]:
        assert self.session is not None
        session = self.session
        connection = session.connection
        # Stamped up front so an interrupted reply, which pcode keeps, still
        # names the session that holds the rest of it.
        session.turns += 1
        self.provider_response_id = _stamp(session.session_id, session.turns)
        _record_turn(session.session_id, session.turns)
        updates: asyncio.Queue = asyncio.Queue()
        connection.updates = updates
        if self.continuation:
            # Drain synchronously right after installing `updates` so no frame
            # can be lost or reordered between idle buffering and live streaming.
            if self.drain_wake is not None:
                for buffered in self.drain_wake():
                    updates.put_nowait(buffered)
            # Matches `_asked()` when `PROVIDER_CONTINUATION_PROMPT` is filtered out.
            self.asked.append(CONTINUE)
        else:
            # What the user asked, not the replay sent ahead of it.
            self._send_prompt(
                self.prompt, updates, asked=_asked_blocks(self.prompt[self.replayed :])
            )
        try:
            async for event in self._events(updates, session):
                yield event
        except Exception:
            self.failed = True
            raise
        finally:
            if connection.updates is updates:
                connection.updates = None
                # Only forward leftover updates after a clean turn end: on cancel
                # or failure, `request_stream` quarantines the session next, so
                # forwarding here would queue a phantom continuation wake first.
                if not self.unfinished and not self.failed:
                    while not updates.empty():
                        leftover = updates.get_nowait()
                        if isinstance(leftover, dict) and connection.idle is not None:
                            connection.idle(leftover)
        if not self.text_seen:
            # A reply with no text reads as empty, and Pydantic AI would ask
            # again: that request would be pcode's, not the user's.
            yield self._parts_manager.handle_part(
                vendor_part_id="text-end", part=TextPart("(Antigravity finished without a reply.)")
            )

    async def _events(
        self, updates: asyncio.Queue, session: _Session
    ) -> AsyncIterator[ModelResponseStreamEvent]:
        loop = asyncio.get_running_loop()
        reply_open = False
        quiet_deadline: float | None = None
        flush_deadline: float | None = None
        # When the current wait for a launched native subagent gives up.
        subagent_deadline: float | None = None
        self.step_open = not self.continuation
        self.step_emitted = False
        if self.continuation:
            self.holding = True
            quiet_deadline = loop.time() + DEFERRED_FINALIZE_DEBOUNCE
        while True:
            if self.run_context is not None and steering_pending(self.run_context):
                previous = self.reply
                await self._steer(updates)
                if self.reply is not previous:
                    self.holding = False
                    self.subagent_launched = False
                    self.awaiting_subagent = False
                    reply_open = False
                    quiet_deadline = None
            timeout = STEER_POLL
            if flush_deadline is not None:
                remaining = flush_deadline - loop.time()
                if remaining <= 0:
                    flush_deadline = None
                    if (
                        self.pending_steer_commits
                        and not self.step_open
                        and not self._has_running_tools()
                    ):
                        self._stage_pending_steers()
                else:
                    timeout = min(timeout, remaining)
            if self.holding and quiet_deadline is not None:
                remaining = quiet_deadline - loop.time()
                if remaining <= 0:
                    self.holding = False
                    break
                timeout = min(timeout, remaining)
            try:
                update = await asyncio.wait_for(updates.get(), timeout)
            except TimeoutError:
                if flush_deadline is not None and loop.time() >= flush_deadline:
                    flush_deadline = None
                    if (
                        self.pending_steer_commits
                        and not self.step_open
                        and not self._has_running_tools()
                    ):
                        self._stage_pending_steers()
                if self.holding and quiet_deadline is not None and loop.time() >= quiet_deadline:
                    self.holding = False
                    break
                continue
            if isinstance(update, BaseException):
                self.holding = False
                self.broken = True
                raise update
            if isinstance(update, asyncio.Future):
                if update is self.reply and not self.holding:
                    if not self._should_hold(update):
                        self.holding = False
                        break
                    self.holding = True
                    self.step_open = False
                    self.awaiting_subagent = self.subagent_launched
                    self.subagent_launched = False
                    reply_open = False
                    quiet_deadline = None
                    if self.awaiting_subagent:
                        subagent_deadline = loop.time() + SUBAGENT_WAIT_LIMIT
                        # A tool still running keeps the timer paused; when it finishes,
                        # that update counts as a wake and ends the subagent wait.
                        if not self._has_running_tools():
                            quiet_deadline = subagent_deadline
                continue  # A prompt a steering message took over, or held for background work.
            kind = update.get("sessionUpdate")
            content = update.get("content")
            chunk_text = content.get("text", "") if isinstance(content, dict) else ""
            if kind in {"agent_thought_chunk", "agent_message_chunk"} and chunk_text:
                if (
                    self.pending_steer_commits
                    and not self.step_open
                    and not self._has_running_tools()
                ):
                    flush_deadline = None
                    self._stage_pending_steers()
                self.step_open = True
                self.step_emitted = True
            elif (
                kind == "tool_call"
                and self.pending_steer_commits
                and not self.step_open
                and not self._has_running_tools()
                and flush_deadline is None
            ):
                # Step 2 started directly with a tool call (no text/thinking preamble).
                self._stage_pending_steers()
            substantive_wake = _is_wake_update(
                update, buffering=reply_open, daemon_tools=session.daemon_tools
            )
            if self.holding and self.awaiting_subagent and substantive_wake:
                self.awaiting_subagent = False
            for event in self._map(update):
                self.events_seen = True
                yield event
            if kind == "usage_update":
                if self.step_emitted:
                    self.step_open = False
                    self.step_emitted = False
                    if self.pending_steer_commits and not self._has_running_tools():
                        # Brief grace window in case a tool_call from this step follows immediately.
                        flush_deadline = loop.time() + 0.05
            elif kind in {"tool_call", "tool_call_update"}:
                self.step_open = False
                self.step_emitted = True
                if self._has_running_tools():
                    flush_deadline = None
                elif self.pending_steer_commits:
                    flush_deadline = None
                    self._stage_pending_steers()
            if self.holding:
                if kind == "usage_update":
                    reply_open = False
                    if self.subagent_launched:
                        self.awaiting_subagent = True
                        self.subagent_launched = False
                        subagent_deadline = loop.time() + SUBAGENT_WAIT_LIMIT
                elif substantive_wake:
                    reply_open = True
                # Any active non-daemon tool (including long MCP delegations) pauses
                # the quiet timer while holding. While awaiting a native subagent, only
                # non-wake updates land here, and they keep its fixed deadline.
                if self._has_running_tools():
                    quiet_deadline = None
                elif self.awaiting_subagent:
                    quiet_deadline = subagent_deadline
                else:
                    limit = OPEN_REPLY_QUIET_LIMIT if reply_open else DEFERRED_FINALIZE_DEBOUNCE
                    quiet_deadline = loop.time() + limit
        if self.reply is None:
            self._finish({"stopReason": "end_turn"})
            return
        try:
            result = self.reply.result()
        except AntigravityError:
            if not self.events_seen and len(self.replies) == 1:
                # Refused before it started (signed out, unknown model): the
                # session never saw this turn.
                session.turns -= 1
                _record_turn(session.session_id, session.turns)
            raise
        self._finish(result)

    def _send_prompt(
        self, blocks: list[dict], updates: asyncio.Queue, *, asked: str | None = None
    ) -> None:
        assert self.session is not None
        reply = self.session.connection.begin(
            "session/prompt", {"sessionId": self.session.session_id, "prompt": blocks}
        )
        # The future itself marks where its prompt's updates end.
        reply.add_done_callback(updates.put_nowait)
        self.reply = reply
        self.replies.append(reply)
        self.asked.append(_asked_blocks(blocks) if asked is None else asked)

    async def _steer(self, updates: asyncio.Queue) -> None:
        """Send the user's steering messages into the running turn, as Antigravity's own clients do.

        Each is a further `session/prompt`; Antigravity folds it into the work
        in progress. Only the messages go: never pcode's steering reminder.
        They are added to this turn's request as well, as pcode's other
        models get them, so the history says what Antigravity was told.
        """
        from pcode.retries import RequestCheckpoint
        from pcode.steering import take_user_steering_deferred

        ctx = self.run_context
        assert ctx is not None
        # The run's request, and the copy pcode restores if this turn is
        # cancelled or fails: both must say what Antigravity was told.
        requests = [ctx.messages[-1]] if ctx.messages else []
        for capability in ctx.capabilities.values():
            if isinstance(capability, RequestCheckpoint) and capability.messages:
                requests.append(capability.messages[-1])
        messages, commit = await take_user_steering_deferred(ctx)
        sent_any = False
        for message in messages:
            blocks, refused = content_blocks(message)  # Only text and images can go.
            if refused:
                self.notify(
                    f"Antigravity can't take {', '.join(sorted(set(refused)))}; "
                    "sent the rest of that message."
                )
            for request in requests:
                if isinstance(request, ModelRequest):
                    request.parts.append(UserPromptPart(message))
            if blocks:
                self._send_prompt(blocks, updates)
                sent_any = True
        if messages:
            if not sent_any or (not self.step_open and not self._has_running_tools()):
                self.pending_steer_commits.append(commit)
                self._stage_pending_steers()
            else:
                self.pending_steer_commits.append(commit)

    def _finish(self, result: dict) -> None:
        stop = result.get("stopReason")
        reasons = {"end_turn": "stop", "max_tokens": "length", "refusal": "content_filter"}
        self.finish_reason = reasons.get(stop, "stop")
        details: dict[str, Any] = {}
        if stop:
            details["stop_reason"] = stop
        if self.context_used is not None:
            details["context_used"] = self.context_used
        self.provider_details = details or None
        # Each prompt reports its own usage; a steered turn is all of them.
        # Unsolicited continuation turns (no `session/prompt` reply) or replies
        # without `usage` leave `_usage` empty instead of copying `context_used`
        # into `input_tokens`, which would count context fill as uncached billing
        # tokens with `cache_read_tokens=0`; the context meter reads
        # `provider_details["context_used"]` directly.
        total = RequestUsage()
        for reply in self.replies:
            if not reply.done() or reply.cancelled() or reply.exception():
                continue
            usage = reply.result().get("usage") or {}
            cached = int(usage.get("cachedReadTokens") or 0)
            written = int(usage.get("cachedWriteTokens") or 0)
            total += RequestUsage(
                input_tokens=int(usage.get("inputTokens") or 0) + cached + written,
                cache_read_tokens=cached,
                cache_write_tokens=written,
                output_tokens=int(usage.get("outputTokens") or 0),
            )
        self._usage = total

    def _map(self, update: dict) -> Iterator[ModelResponseStreamEvent]:
        kind = update.get("sessionUpdate")
        content = update.get("content")
        text = content.get("text", "") if isinstance(content, dict) else ""
        if kind == "usage_update":
            self.on_usage(update)
            used = update.get("used")
            if type(used) is int and used >= 0:
                self.context_used = used
        elif kind == "session_info_update":
            self.on_title(update)
        elif kind == "agent_message_chunk" and text:
            self.text_seen = self.text_seen or bool(text.strip())
            yield from self._parts_manager.handle_text_delta(
                vendor_part_id=self._vendor("text"), content=text
            )
        elif kind == "agent_thought_chunk" and text:
            yield from self._parts_manager.handle_thinking_delta(
                vendor_part_id=self._vendor("thinking"), content=text
            )
        elif kind in {"tool_call", "tool_call_update"}:
            yield from self._tool(update)

    def _tool(self, update: dict) -> Iterator[ModelResponseStreamEvent]:
        call_id = str(update.get("toolCallId") or "")
        if not call_id:
            return
        state = self.tools.setdefault(call_id, _ToolState())
        if self.session is not None and call_id in self.session.daemon_tools:
            state.is_daemon = True
        if isinstance(update.get("kind"), str) and update["kind"]:
            state.kind = update["kind"]
        if isinstance(update.get("title"), str) and update["title"]:
            state.title = update["title"]
        raw_input = update.get("rawInput")
        if isinstance(raw_input, dict):
            if raw_input.get("IsDaemon") is True or raw_input.get("isDaemon") is True:
                state.is_daemon = True
                if self.session is not None:
                    self.session.daemon_tools.add(call_id)
            if not state.kind and any(
                isinstance(raw_input.get(k), str) and raw_input[k].strip()
                for k in ("CommandLine", "commandLine", "command_line", "command")
            ):
                state.kind = "execute"
        was_terminal = state.status in {"completed", "failed"}
        status = update.get("status")
        if isinstance(status, str) and status:
            state.status = status
        known = self.calls.get(call_id)
        args = _tool_args(update)
        if state.title and "title" not in args:
            args["title"] = state.title
        has_input = isinstance(raw_input, dict) and bool(raw_input)
        # The first report may carry no arguments yet (only a title); a later one fills them in.
        if not was_terminal and (known is None or (call_id not in self.call_inputs and has_input)):
            name = known[0] if known else str(update.get("name") or update.get("kind") or "tool")
            if has_input:
                self.call_inputs.add(call_id)
            self.calls[call_id] = (name, args)
            self._vendor("tool")
            yield self._parts_manager.handle_part(
                vendor_part_id=f"call-{call_id}",
                part=NativeToolCallPart(
                    tool_name=name, args=args, tool_call_id=call_id, provider_name=SYSTEM
                ),
            )
        if not was_terminal:
            tool_name = str(update.get("name") or "") or (
                self.calls[call_id][0] if call_id in self.calls else ""
            )
            if _is_subagent_launch(tool_name, state):
                if status == "completed":
                    self.subagent_launched = True
            elif not state.is_daemon:
                self.subagent_launched = False
        if status in {"completed", "failed"}:
            name, _ = self.calls[call_id]
            self._vendor("tool")
            yield self._parts_manager.handle_part(
                vendor_part_id=f"return-{call_id}",
                part=NativeToolReturnPart(
                    tool_name=name,
                    content=_tool_output(update),
                    tool_call_id=call_id,
                    provider_name=SYSTEM,
                    outcome="success" if status == "completed" else "failed",
                ),
            )

    async def close_stream(self) -> None:
        await self._cancel()

    async def _cancel(self) -> None:
        if self.session is None or not self.unfinished:
            return
        with contextlib.suppress(AntigravityError, OSError):
            await self.session.connection.notify(
                "session/cancel", {"sessionId": self.session.session_id}
            )

    async def stop(self) -> bool:
        """Cancel an unfinished turn and wait for it to end; whether it did.

        A turn left running would keep spending the subscription's quota, and the
        next prompt would steer it instead of starting a new one.
        """
        await self._cancel()
        if self.reply is None:
            return True
        try:
            await asyncio.wait_for(asyncio.shield(self.reply), CANCEL_GRACE)
        except Exception:
            return False
        return True


def unwrap(model: Any) -> AntigravityModel | None:
    """The `AntigravityModel` behind `model` and any wrappers around it, if there is one."""
    for _ in range(8):
        if isinstance(model, AntigravityModel) or model is None:
            return model
        model = getattr(model, "wrapped", None)
    return None


def antigravity_model(model: str, *, side: bool = False, worker: bool = False) -> AntigravityModel:
    name = model.removeprefix(PREFIX).strip()
    if not installed():
        raise ValueError(
            "Antigravity CLI (agy) is not installed: https://antigravity.google/docs/cli/"
        )
    return AntigravityModel(name, side=side, worker=worker)
