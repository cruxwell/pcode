"""One CLI process and the pcode history its transcript holds."""

import asyncio
import json
import logging
import os
import time
from collections import deque
from collections.abc import AsyncIterator, Callable
from contextlib import suppress
from dataclasses import dataclass
from tempfile import mkstemp
from typing import Any

from pydantic_ai.exceptions import ModelAPIError

from pcode.claude_sdk.cli import CLI_ENV
from pcode.claude_sdk.errors import (
    ClaudeConnectionError,
    ClaudeHTTPError,
    ClaudeProcessError,
    ClaudeStartError,
)
from pcode.claude_sdk.messages import _blocks, _mcp_content, _result_ids
from pcode.claude_sdk.resume import ForkPoint
from pcode.claude_sdk.web import result_block, server_tool_use

logger = logging.getLogger(__name__)

SERVER = "pcode"
# How the CLI names an MCP tool to the model; pcode's own names are restored.
TOOL_PREFIX = f"mcp__{SERVER}__"
# The CLI sends the model's tool_use id with each MCP call under this key.
TOOL_USE_ID = "claudecode/toolUseId"
CLOSE_TIMEOUT_SECONDS = 5.0
# A finished turn's result follows its last message within milliseconds.
TURN_END_TIMEOUT_SECONDS = 60.0
# The CLI calls a tool's handler as the tool_use block ends; allow for a slow
# machine before deciding it never will (a call it refused or could not parse).
CALL_TIMEOUT_SECONDS = 10.0
# `connect` returns before the CLI has listed pcode's tools; a message sent in
# that gap goes out with none (verified with 2.1.285), so wait for them.
TOOLS_READY_TIMEOUT_SECONDS = 30.0
TOOLS_READY_POLL_SECONDS = 0.02
# The CLI can run its web tools before a pcode call made beside them, so that call
# reaches pcode only once they finish. The CLI bounds each fetch and search itself.
WEB_TOOLS_TIMEOUT_SECONDS = 300.0
# Statuses for the CLI's error kinds when it reports no HTTP status itself.
# `server_error` has none on purpose: without a status it is a connection failure
# (`ClaudeConnectionError`), while a real 5xx or 529 always carries its status.
ERROR_STATUS = {
    "authentication_failed": 401,
    "rate_limit": 429,
    "invalid_request": 400,
}


@dataclass(frozen=True)
class SessionConfig:
    """Everything fixed for the life of one CLI process."""

    model: str
    cwd: str
    system_prompt: str
    tools: str  # canonical JSON of [{name, description, input_schema}]
    effort: str | None = None
    thinking: str | None = None  # canonical JSON of the thinking setting
    # pcode's output ceiling; the CLI clamps it to the model's own limit.
    max_tokens: int | None = None
    # The CLI's own web tools to offer (`web.CLI_WEB_TOOLS`), which it runs itself.
    web_tools: tuple[str, ...] = ()

    def options(self, server, resume: ForkPoint | None, stderr, prompt_file: str) -> Any:
        from claude_agent_sdk import ClaudeAgentOptions

        thinking = json.loads(self.thinking) if self.thinking else None
        if thinking is not None and thinking.get("type") != "disabled":
            thinking = {"display": "summarized", **thinking}
        return ClaudeAgentOptions(
            # No settings, CLAUDE.md, plugins, other MCP servers or built-in
            # tools beyond the web ones pcode asked for: the model sees pcode's
            # prompt and pcode's tools only.
            tools=list(self.web_tools),
            setting_sources=[],
            strict_mcp_config=True,
            mcp_servers={SERVER: {"type": "sdk", "name": SERVER, "instance": server}},
            allowed_tools=[f"mcp__{SERVER}", *self.web_tools],
            # Left unset, CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC makes the CLI
            # report "auto", which org-managed hooks may block outright. The
            # allowed tools above never prompt, so "default" changes nothing else.
            permission_mode="default",
            system_prompt={"type": "file", "path": prompt_file},
            include_partial_messages=True,
            model=self.model,
            cwd=self.cwd,
            env={
                **CLI_ENV,
                **(
                    {"CLAUDE_CODE_MAX_OUTPUT_TOKENS": str(self.max_tokens)}
                    if self.max_tokens
                    else {}
                ),
            },
            effort=self.effort,
            thinking=thinking,
            # Readable thinking for scrollback when no thinking setting says so.
            extra_args={} if thinking is not None else {"thinking-display": "summarized"},
            resume=resume.session_id if resume else None,
            fork_session=resume is not None,
            resume_session_at=resume.uuid if resume else None,
            stderr=stderr,
        )


def _client_factory(options) -> Any:
    from claude_agent_sdk import ClaudeSDKClient

    return ClaudeSDKClient(options)


def _block_events(block: dict, index: int) -> list[dict]:
    """A whole content block as the start and stop events that carry it."""
    return [
        {"type": "content_block_start", "index": index, "content_block": block},
        {"type": "content_block_stop", "index": index},
    ]


def _sum(first: dict, second: dict) -> dict:
    """Two messages' usage as one: counts add up, anything else is the latest."""
    total = dict(first)
    for key, value in second.items():
        if isinstance(value, dict):
            earlier = total.get(key)
            total[key] = _sum(earlier if isinstance(earlier, dict) else {}, value)
        elif isinstance(value, int) and not isinstance(value, bool):
            earlier = total.get(key)
            total[key] = value + (earlier if isinstance(earlier, int) else 0)
        else:
            total[key] = value
    return total


class _Diverged(ClaudeProcessError):
    """The CLI's transcript stopped matching pcode's history (it acted on its own)."""

    live = False  # raised while continuing a live process, which a fork can replace


class ClaudeSession:
    """One live CLI process and the pcode history its transcript holds."""

    def __init__(self, config: SessionConfig, resume: ForkPoint | None = None) -> None:
        self.config = config
        self.resume = resume
        # History hashes the transcript holds, always ending on an assistant message.
        self.chain: list[str] = []
        # Tool calls the last response made, whose results the CLI is waiting for.
        self.open_tool_ids: tuple[str, ...] = ()
        self.cli_session_id: str | None = None
        self.response_uuid: str | None = None
        # Whether the current response was read through its message_stop.
        self.complete = False
        # Whether it called a tool by a name the CLI does not offer (pcode's bare
        # name). The CLI refuses such a call, and a transcript that holds one
        # teaches the model to repeat it, so it must never be forked.
        self.stray_call = False
        self.busy = False
        self.dead = False
        self.last_used = time.monotonic()
        # Set by the pool: stop an idle process that went its own way.
        self.retire: Callable[[], None] | None = None
        self._client: Any = None
        self._prompt_file: str | None = None
        self._pump_task: asyncio.Task | None = None
        self._queue: asyncio.Queue = asyncio.Queue()
        self._slots: dict[str, asyncio.Future] = {}
        # Calls the CLI parked with pcode, and results it got from pcode (through
        # a released handler, or sent as input). Any other result is its own.
        self._called: set[str] = set()
        self._answered: set[str] = set()
        # Web tool calls the CLI is running itself (id -> name, input), until
        # their results arrive; and the tool names in the message streaming now.
        self._web_calls: dict[str, tuple[str, dict]] = {}
        self._message_tools: list[str] = []
        # Pulsed on every change a waiter may be waiting for.
        self._changed = asyncio.Event()
        self._last_uuid: str | None = None
        # Whether pcode asked for the next message; anything else is the CLI's own.
        self._expecting = False
        self._final = False
        self._turn_done = asyncio.Event()
        self._turn_done.set()
        self._api_error: tuple[str | None, str] | None = None
        self._stderr: deque[str] = deque(maxlen=20)
        self._closed = False

    # Lifecycle

    async def connect(self, factory=None) -> None:
        # A file, not argv: a large prompt would pass Linux's 128 KiB argument
        # limit, and argv is readable by every local user. mkstemp makes it 0600.
        descriptor, self._prompt_file = mkstemp(prefix="pcode-claude-", suffix=".md")
        with os.fdopen(descriptor, "w") as file:
            file.write(self.config.system_prompt)
        options = self.config.options(
            self._server(), self.resume, self._stderr.append, self._prompt_file
        )
        self._client = (factory or _client_factory)(options)
        try:
            await self._client.connect()
        except Exception as error:
            self.dead = True
            self._remove_prompt_file()
            raise ClaudeStartError(
                self.config.model, f"Claude Code could not start: {self._detail(error)}"
            ) from error
        if not self._closed:
            try:
                await self._tools_ready()
            except BaseException as error:
                stopped, self._closed, self.dead = self._closed, True, True
                self._remove_prompt_file()
                with suppress(Exception):
                    await self._client.disconnect()
                if stopped and isinstance(error, Exception):
                    message = "Claude Code was stopped."
                    raise ClaudeProcessError(self.config.model, message) from error
                raise
        if self._closed:  # closed while connecting: `close` found nothing to stop
            with suppress(Exception):
                await self._client.disconnect()
            raise ClaudeProcessError(self.config.model, "Claude Code was stopped.")
        self._pump_task = asyncio.create_task(self._pump(), name="claude-sdk-pump")

    async def _tools_ready(self) -> None:
        """Wait until the CLI offers every pcode tool to the model.

        A request sent before then declares no tools, so the model calls them
        by the bare names in pcode's prompt, which the CLI refuses.
        """
        expected = len(json.loads(self.config.tools))
        state = "not listed"
        try:
            async with asyncio.timeout(TOOLS_READY_TIMEOUT_SECONDS):
                while True:
                    status = await self._client.get_mcp_status()
                    server = next(
                        (s for s in status.get("mcpServers") or [] if s.get("name") == SERVER),
                        None,
                    )
                    if server is not None:
                        state = str(server.get("status"))
                        if state == "connected" and len(server.get("tools") or []) >= expected:
                            return
                        if state in ("failed", "needs-auth", "disabled"):
                            break
                    await asyncio.sleep(TOOLS_READY_POLL_SECONDS)
        except TimeoutError:
            pass
        raise ClaudeProcessError(
            self.config.model, f"Claude Code did not load pcode's tools ({state})."
        )

    async def close(self) -> None:
        """Stop the process without letting it act on its own first.

        A process closed while parked records the missing results as errors and
        carries on (verified: two more billed requests), so interrupt the turn.
        """
        if self._closed:
            return
        self._closed = self.dead = True
        self._changed.set()
        # A reader still waiting (pcode exiting mid-request) must not wait forever.
        self._queue.put_nowait(
            ("error", ClaudeProcessError(self.config.model, "Claude Code was stopped."), None)
        )
        client = self._client
        if client is not None:
            if not self._turn_done.is_set():
                with suppress(Exception):
                    async with asyncio.timeout(CLOSE_TIMEOUT_SECONDS):
                        await client.interrupt()
            with suppress(Exception):
                await client.disconnect()
        if self._pump_task is not None:
            self._pump_task.cancel()
        for future in self._slots.values():
            if not future.done():
                future.cancel()
        self._remove_prompt_file()

    def _remove_prompt_file(self) -> None:
        if self._prompt_file is not None:
            with suppress(OSError):
                os.unlink(self._prompt_file)
            self._prompt_file = None

    def _detail(self, error: BaseException | None = None) -> str:
        from pcode.diagnostics import redact

        detail = "; ".join(list(self._stderr)[-3:]) or (str(error) if error else "")
        return redact(detail or (type(error).__name__ if error else "no detail"))[:500]

    def _diverge(self, why: str) -> None:
        """Retire a process whose transcript no longer matches pcode's history."""
        if self.dead:
            return
        logger.debug("Claude Code session diverged: %s", why)
        self.dead = True
        self._changed.set()
        if self._expecting:
            # A reader waits for the answer to its input, but what the CLI sends
            # next follows its own history: drop it and fail the reader (the
            # runtime's retry forks from the last shared message).
            self._expecting = False
            error = ClaudeProcessError(self.config.model, f"Claude Code went its own way: {why}.")
            self._queue.put_nowait(("error", error, None))
        if not self.busy and self.retire is not None:
            self.retire()  # interrupts whatever it started on its own

    # Tool handlers

    def _server(self) -> Any:
        # Built on mcp 2's constructor callbacks: the SDK helper would hide the
        # request `_meta` that carries the tool_use id.
        import mcp.types as types
        from mcp.server import Server

        tools = [
            types.Tool.model_validate(
                {
                    "name": tool["name"],
                    "description": tool.get("description") or "",
                    "inputSchema": tool["input_schema"],
                    # Keep results inline: pcode already spills large ones.
                    "_meta": {"anthropic/maxResultSizeChars": 10_000_000},
                }
            )
            for tool in json.loads(self.config.tools)
        ]

        async def list_tools(ctx, params):
            return types.ListToolsResult(tools=tools)

        async def call_tool(ctx, params):
            meta = params.meta
            extra = meta if isinstance(meta, dict) else (getattr(meta, "model_extra", None) or {})
            tool_use_id = extra.get(TOOL_USE_ID)
            if not isinstance(tool_use_id, str):
                self._diverge("a tool call without an id")
                return types.CallToolResult(
                    content=[
                        types.TextContent(type="text", text="pcode could not match this call.")
                    ],
                    isError=True,
                )
            self._called.add(tool_use_id)
            self._changed.set()
            # Parked until pcode runs the tool and sends its result.
            try:
                block = await self._slot(tool_use_id)
            finally:
                self._slots.pop(tool_use_id, None)  # never hold results once answered
            self._answered.add(tool_use_id)
            return types.CallToolResult.model_validate(
                {"content": _mcp_content(block), "isError": bool(block.get("is_error"))}
            )

        return Server(SERVER, on_list_tools=list_tools, on_call_tool=call_tool)

    def _slot(self, tool_use_id: str) -> asyncio.Future:
        future = self._slots.get(tool_use_id)
        if future is None:
            future = self._slots[tool_use_id] = asyncio.get_running_loop().create_future()
        return future

    # Input

    async def _write(self, content: list[dict]) -> None:
        async def message():
            yield {
                "type": "user",
                "message": {"role": "user", "content": content},
                "parent_tool_use_id": None,
            }

        await self._client.query(message())

    async def _until(self, condition: Callable[[], bool]) -> None:
        while not condition():
            self._changed.clear()
            await self._changed.wait()

    def _ask(self) -> None:
        """Expect the CLI's next message; raise if it has already gone its own way."""
        if self.dead:
            raise _Diverged(self.config.model, "Claude Code continued on its own.")
        self._expecting = True
        self.complete = False

    async def start(self, content: list[dict]) -> None:
        """Begin a turn with a new user message."""
        try:
            async with asyncio.timeout(TURN_END_TIMEOUT_SECONDS):
                # Or until it goes its own way, which then need not run to the end.
                await self._until(lambda: self._turn_done.is_set() or self.dead)
        except TimeoutError:
            raise _Diverged(self.config.model, "Claude Code did not end its turn.") from None
        self._ask()
        self._turn_done.clear()
        # The last turn ended, so any web call still listed will never answer.
        self._web_calls.clear()
        # A fork may start with results for the calls its transcript ends on.
        self._answered.update(b["tool_use_id"] for b in content if b.get("type") == "tool_result")
        await self._write(content)

    async def answer(self, message: dict) -> None:
        """Continue a parked turn: release its handlers, plus any new user input.

        The CLI parks a call as its tool_use block ends. One that never arrives
        was refused or unparseable, and the CLI has answered it itself.
        Input written before the results is already queued when they arrive, so
        the CLI sends both in one request (verified with 2.1.283).
        """

        def reached() -> bool:
            return bool(self._called & set(self.open_tool_ids)) or self.dead

        try:
            # The CLI may run its web tools first; until they finish, a call
            # not reaching pcode yet is no sign of a refusal.
            async with asyncio.timeout(WEB_TOOLS_TIMEOUT_SECONDS):
                await self._until(lambda: reached() or not self._web_calls)
            async with asyncio.timeout(CALL_TIMEOUT_SECONDS):
                await self._until(reached)
        except TimeoutError:
            self._diverge("no tool call reached pcode")
        self._ask()
        blocks = _blocks(message)
        other = [b for b in blocks if b.get("type") != "tool_result"]
        if other:
            await self._write(other)
        for block in blocks:
            if block.get("type") == "tool_result":
                future = self._slot(block["tool_use_id"])
                if not future.done():
                    future.set_result(block)
        self.open_tool_ids = ()

    async def send(self, message: dict) -> None:
        if self.open_tool_ids:
            await self.answer(message)
        else:
            await self.start(_blocks(message))

    # Output

    async def _pump(self) -> None:
        """Drain the SDK (its buffer holds only 100 messages) and track the turn."""
        from claude_agent_sdk.types import (
            AssistantMessage,
            ResultMessage,
            StreamEvent,
            SystemMessage,
            UserMessage,
        )

        error: Exception | None = None
        stop_reason = None
        wanted = False
        try:
            async for message in self._client.receive_messages():
                if getattr(message, "parent_tool_use_id", None):
                    continue
                if isinstance(message, StreamEvent):
                    event = message.event
                    kind = event.get("type")
                    if kind == "message_start":
                        stop_reason = None
                        self._message_tools = []
                        wanted, self._expecting = self._expecting, False
                        if not wanted:
                            # A request of its own (a nudge after a thinking-only
                            # reply, output-limit recovery, a retried tool call).
                            self._diverge("a message pcode did not ask for")
                    elif kind == "content_block_start":
                        block = event.get("content_block") or {}
                        if block.get("type") == "tool_use":
                            name = str(block.get("name", ""))
                            self._message_tools.append(name)
                            if name in self.config.web_tools:
                                self._web_calls[block.get("id")] = (name, {})
                    elif kind == "message_delta":
                        stop_reason = (event.get("delta") or {}).get("stop_reason") or stop_reason
                    elif kind == "message_stop" and stop_reason != "tool_use":
                        # The turn ends here; absorb its result message.
                        self._final = True
                    elif kind == "message_stop" and wanted and self._web_only():
                        # The CLI runs its web tools and asks again: that next
                        # message continues the response pcode asked for.
                        self._expecting = True
                    if wanted:
                        self._queue.put_nowait(("event", event, self._last_uuid))
                elif isinstance(message, AssistantMessage):
                    self._last_uuid = message.uuid or self._last_uuid
                    for block in message.content:
                        call = getattr(block, "id", None)
                        if call in self._web_calls:
                            self._web_calls[call] = (block.name, dict(block.input or {}))
                    if message.error:
                        text = " ".join(getattr(b, "text", "") for b in message.content)
                        self._api_error = (message.error, text.strip())
                elif isinstance(message, UserMessage):
                    self._check_results(message)
                elif isinstance(message, ResultMessage):
                    self.cli_session_id = message.session_id or self.cli_session_id
                    final, self._final = self._final, False
                    if message.is_error:
                        self.dead = True
                    self._turn_done.set()
                    self._changed.set()
                    # A result after the final message is expected; any other
                    # ends the turn without one, which a waiting reader must hear.
                    if not final or message.is_error:
                        self._queue.put_nowait(("result", message, None))
                elif isinstance(message, SystemMessage) and message.subtype == "init":
                    self.cli_session_id = message.data.get("session_id") or self.cli_session_id
        except asyncio.CancelledError:
            raise
        except Exception as cause:
            error = cause
        self.dead = True
        self._turn_done.set()
        self._changed.set()
        stopped = ClaudeProcessError(
            self.config.model, f"Claude Code stopped: {self._detail(error)}"
        )
        stopped.__cause__ = error
        self._queue.put_nowait(("error", stopped, None))

    def _check_results(self, message) -> None:
        """Notice the CLI answering one of pcode's calls itself (a refused input).

        Checked against results pcode actually handed over, not ones it merely
        sent: with calls made one at a time, the CLI can refuse a later call
        after pcode has sent every result.
        """
        content = message.content if isinstance(message.content, list) else []
        for block in content:
            tool_use_id = getattr(block, "tool_use_id", None)
            if tool_use_id in self._web_calls:
                # One of the CLI's own web calls: its result is part of the response.
                name, args = self._web_calls.pop(tool_use_id)
                result = result_block(name, tool_use_id, args, block.content, bool(block.is_error))
                self._queue.put_nowait(("web_result", result, None))
                self._changed.set()
            elif tool_use_id and tool_use_id not in self._answered and not self._closed:
                self._diverge(f"Claude Code answered tool call {tool_use_id} itself")

    def _web_only(self) -> bool:
        """Whether the message just streamed called the CLI's web tools and nothing else."""
        return bool(self._message_tools) and all(
            name in self.config.web_tools for name in self._message_tools
        )

    async def response(self) -> AsyncIterator[dict]:
        """Raw stream events of the CLI's next assistant message, through message_stop.

        A message whose only calls are the CLI's web tools is not the end: the
        CLI runs them and sends another, which is folded into this one as an
        Anthropic server tool loop would be. Block indices run on across the
        messages, the web calls become server tool blocks with their results
        inserted after them, and usage is the sum of every message folded in.
        Results of web calls made beside a pcode call arrive after that
        response ended, so they open the next one.
        """
        tool_ids: list[str] = []
        web_ids: list[str] = []
        stop_reason = None
        self.stray_call = False
        started = False
        early: list[dict] = []  # web results that came before this message did
        shift = emitted = 0  # added to the current message's indices; blocks so far
        folded: dict = {}  # usage of the messages already folded in
        usage: dict = {}  # usage of the current message, as it accumulates
        while True:
            kind, value, uuid = await self._queue.get()
            if kind == "error":
                self._queue.put_nowait((kind, value, uuid))  # every later reader fails too
                raise value
            if kind == "result":
                self.dead = True
                raise self._result_error(value)
            if kind == "web_result":
                if not started:
                    early.append(value)
                    continue
                for event in _block_events(value, emitted):
                    yield event
                emitted += 1
                continue
            event_type = value.get("type")
            if event_type == "message_start":
                tool_ids, web_ids, stop_reason = [], [], None
                usage = dict((value.get("message") or {}).get("usage") or {})
                if started:
                    # A continuation: its usage joins the response's, the rest is known.
                    yield {"type": "message_delta", "delta": {}, "usage": _sum(folded, usage)}
                else:
                    started = True
                    yield value
                    for block in early:
                        for event in _block_events(block, emitted):
                            yield event
                        emitted += 1
                shift = emitted
                continue
            if isinstance(value.get("index"), int):
                value = {**value, "index": value["index"] + shift}
                emitted = max(emitted, value["index"] + 1)
            if event_type == "content_block_start":
                block = value.get("content_block") or {}
                wire = str(block.get("name", ""))
                if block.get("type") == "tool_use" and wire in self.config.web_tools:
                    web_ids.append(block.get("id"))
                    value = {**value, "content_block": server_tool_use(block)}
                elif block.get("type") == "tool_use":
                    tool_ids.append(block.get("id"))
                    self.stray_call = self.stray_call or not wire.startswith(TOOL_PREFIX)
                    name = wire.removeprefix(TOOL_PREFIX)
                    value = {**value, "content_block": {**block, "name": name}}
            elif event_type == "message_delta":
                stop_reason = (value.get("delta") or {}).get("stop_reason") or stop_reason
                usage.update(value.get("usage") or {})
                folding = stop_reason == "tool_use" and web_ids and not tool_ids
                if folding or folded:
                    delta = dict(value.get("delta") or {})
                    if folding:
                        delta.pop("stop_reason", None)  # the response goes on
                    value = {**value, "delta": delta, "usage": _sum(folded, usage)}
            elif event_type == "message_stop":
                if stop_reason == "tool_use" and web_ids and not tool_ids:
                    folded = _sum(folded, usage)
                    continue
                # Settled before handing it over, in case the reader stops here.
                self.open_tool_ids = tuple(tool_ids) if stop_reason == "tool_use" else ()
                self.response_uuid = uuid
                self.complete = True
                yield value
                return
            yield value

    def _result_error(self, result) -> Exception:
        kind, text = self._api_error or (None, "")
        self._api_error = None
        text = text or result.result or "; ".join(result.errors or []) or result.subtype
        if kind == "server_error" and not result.api_error_status:
            return ClaudeConnectionError(self.config.model, text)
        status = result.api_error_status or ERROR_STATUS.get(kind or "")
        if status:
            body = {"type": "error", "error": {"type": kind or result.subtype, "message": text}}
            return ClaudeHTTPError(status, self.config.model, body)
        return ModelAPIError(self.config.model, f"Claude Code ended the turn: {text}")

    # Matching

    def accepts(self, config: SessionConfig, messages: list[dict], chain: list[str]) -> bool:
        """Whether `messages` is this transcript plus exactly one new user message."""
        known = len(self.chain)
        if self.busy or self.dead or not known or config != self.config:
            return False
        if len(messages) != known + 1 or chain[known - 1] != self.chain[-1]:
            return False
        message = messages[known]
        return message["role"] == "user" and _result_ids(message) == set(self.open_tool_ids)
