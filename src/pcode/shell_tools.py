"""The shell tool, expressed as jobs: one way to run, two ways to wait.

Harness's persistent `shell` launches every command detached and differs only
in whether the call blocks, which is the right shape. What it lacks is a name
for the thing that outlives the call. Without one, a command that is still
running can only be handed back as a PID and two paths, so the model's only way
to learn it finished is to poll with `sleep`, an interrupted wait kills the
command, and nothing can list what a session left running.

This toolset keeps the execution model and adds the missing noun. `shell`
returns either a finished result or a job handle; `wait_for_job` blocks on a
handle without re-running anything; completions are delivered by
`pcode.job_notices` at the next model request. Polling from the model is
therefore never the right move, and the instructions say so.
"""

from __future__ import annotations

import codecs
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import anyio
from pydantic_ai import RunContext
from pydantic_ai.exceptions import ModelRetry
from pydantic_ai.tools import AgentDepsT
from pydantic_ai.toolsets import AbstractToolset
from pydantic_ai_harness.shell import (
    CommandFinishedEvent,
    CommandOutputEvent,
    CommandStartedEvent,
    Shell,
    ShellToolset,
)

# Private: pcode's job tools turn errors into retries exactly as Harness's do.
from pydantic_ai_harness.shell._policy import recoverable

from pcode.jobs import OUTPUT_TAIL_BYTES, Job, JobRegistry, format_duration, registry
from pcode.steering import steering_pending

# Long enough for a real build or test suite, short enough that the tool call
# returns before provider request timeouts and the conversation keeps its
# request/response cadence.
MAX_WAIT_SECONDS = 270.0

_POLL_INTERVAL = 0.05
# Bounds the events one wait emits into the live preview, matching the
# model-visible tail so "capped" means the same thing in both places.
_EVENT_BUDGET_BYTES = OUTPUT_TAIL_BYTES
# `until_output` keeps reading past the preview budget, but not without end.
_MATCH_READ_BYTES = 1_048_576
# What it matches against: a readiness banner is a line, not a log.
_MATCH_BUDGET_CHARS = 262_144


class JobShellToolset(ShellToolset[AgentDepsT]):
    """Harness's shell toolset with its persistent tool replaced by job tools."""

    def __init__(self, *, jobs: JobRegistry, workdir: Path, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._jobs = jobs
        # Jobs run on this host through `pcode.jobs`, not `ctx.workspace`, so
        # the toolset keeps its own start directory (upstream's is deprecated).
        self._workdir = workdir
        # Harness registered its own persistent `shell` from the tool list.
        # Drop it and register ours under the same name, so the model sees one
        # shell tool and the UI wiring keyed on that name still matches.
        self.tools.pop("shell", None)
        command_metadata = {"code_arg_name": "command", "code_arg_language": "shell"}
        self.add_function(self.shell, name="shell", metadata=command_metadata)
        self.add_function(self.wait_for_job, name="wait_for_job")
        self.add_function(self.job_output, name="job_output")
        self.add_function(self.stop_job, name="stop_job")
        self.add_function(self.list_jobs, name="list_jobs")

    async def for_run(self, ctx: RunContext[AgentDepsT]) -> AbstractToolset[AgentDepsT]:
        """Per-run instance, but the registry is shared: jobs outlive the run."""
        return JobShellToolset[AgentDepsT](
            jobs=self._jobs,
            workdir=self._workdir,
            allowed_commands=self._allowed_commands,
            denied_commands=self._denied_commands,
            denied_operators=self._denied_operators,
            default_timeout=self._default_timeout,
            max_output_chars=self._max_output_chars,
            max_file_bytes=self._max_file_bytes,
            persist_cwd=self._persist_cwd,
            allow_interactive=self._allow_interactive,
            env=self._env,
            denied_env_patterns=self._denied_env_patterns,
            tools=self._tools,
            id=self.id,
        )

    async def __aexit__(self, *args: Any) -> None:
        """Leave jobs alone on run teardown; the registry owns their lifetime."""
        await super().__aexit__(*args)

    # -- tools ---------------------------------------------------------------

    @recoverable
    async def shell(
        self,
        ctx: RunContext[AgentDepsT],
        command: str,
        *,
        background: bool = False,
        timeout: float | None = None,
        purpose: str = "",
    ) -> str:
        """Run a shell command and wait for its output and exit status.

        Commands run under `/bin/sh -c`, not the user's login shell, so write
        POSIX sh: no fish or zsh syntax, and no bash-only features such as
        `<(...)` or `[[ ]]`. Wrap a command that needs bash in
        `bash -c '...'`.

        If you have independent work to do while it runs, pass
        `background=True` and you get a job handle immediately instead.

        Either way the command runs the same way and outlives this call. When a
        wait ends before the command does -- it exceeded `timeout`, or the user
        sent a follow-up -- you get a job handle rather than a result, and the
        command keeps running.

        You are told when a job finishes: its exit status reaches you
        automatically before your next model request. Don't `sleep` in a
        separate call to pass time, and don't re-run a command to learn how the
        first one went. To watch a long job, call `wait_for_job` with
        `until_output` matching the line you care about (an early failure, a
        readiness message). When the source only gives snapshots, a
        `while ...; do ...; sleep N; done` loop inside one background command
        is fine; the rule is about your own turns, not the script.

        The reported exit status is the pipeline's last command, so
        `make test | tail` exits 0 even when make fails; read the output.

        Args:
            command: The shell command to run.
            background: True to get a job handle at once instead of waiting.
            timeout: Seconds to wait before handing back a job handle (max 270).
            purpose: Why you are running this, at most 8 words, present tense
                (e.g. "running the end-to-end suite"). Give it for background
                or long-running commands, which are shown away from this
                call; leave it empty for quick ones.
        """
        self._check_command(command)
        wait = self._wait_seconds(timeout)
        job = self._jobs.launch(
            command,
            cwd=self._workdir,
            env=self._resolve_env(),
            background=background,
            purpose=purpose,
        )
        await ctx.emit(
            CommandStartedEvent(
                tool_call_id=ctx.tool_call_id, command=command, pid=job.supervisor_pid
            )
        )
        if background:
            await self._emit_finished(ctx, job, truncated=False)
            return self._handle(job, reason="Started in the background.")
        return await self._wait(ctx, job, timeout=wait)

    @recoverable
    async def wait_for_job(
        self,
        ctx: RunContext[AgentDepsT],
        job_id: str,
        *,
        timeout: float | None = None,
        until_output: str | None = None,
    ) -> str:
        """Wait for a job started by `shell`. It is not re-run.

        Returns the job's output and exit status once it finishes, or a job
        handle again if the wait ends first. Use this when you need a
        background job's result before you can continue.

        Pass `until_output` to wait for readiness instead of exit: a server
        that never exits is ready when its log says so.

        Args:
            job_id: The job to wait for, e.g. "j3".
            timeout: Seconds to wait before handing back a job handle (max 270).
            until_output: Regular expression; stop waiting at the first match.
        """
        job = self._require(job_id)
        pattern = self._compile(until_output)
        if not job.running:
            return self._result(job)
        await ctx.emit(
            CommandStartedEvent(
                tool_call_id=ctx.tool_call_id, command=job.command, pid=job.supervisor_pid
            )
        )
        return await self._wait(ctx, job, timeout=self._wait_seconds(timeout), match=pattern)

    @recoverable
    async def job_output(self, job_id: str) -> str:
        """Read a job's output so far without waiting for it.

        Use this to inspect progress. It is not a way to find out whether a job
        finished: that is reported to you automatically.

        Args:
            job_id: The job to read, e.g. "j3".
        """
        job = self._require(job_id)
        self._jobs.refresh()
        if not job.running:
            return self._result(job)
        return self._handle(job, reason="Still running.")

    @recoverable
    async def stop_job(self, job_id: str) -> str:
        """Stop a running job and everything it started.

        Args:
            job_id: The job to stop, e.g. "j3".
        """
        job = self._require(job_id)
        stopped = self._jobs.stop(job)
        # This call is the report; a notice at the next request would only tell
        # the model what it just did.
        job.announced.add("model")
        elapsed = format_duration(job.elapsed)
        if not stopped:
            return f"[{job.id} · {job.outcome()} · {elapsed}] Already finished."
        return f"[{job.id} · stopped · {elapsed}] {job.label()}"

    @recoverable
    async def list_jobs(self) -> str:
        """List this session's jobs, running first, with their status."""
        self._jobs.refresh()
        jobs = sorted(self._jobs.jobs.values(), key=lambda job: (not job.running, job.started_at))
        if not jobs:
            return "No jobs have been started."
        return "\n".join(job.summary() for job in jobs)

    # -- waiting -------------------------------------------------------------

    async def _wait(
        self,
        ctx: RunContext[AgentDepsT],
        job: Job,
        *,
        timeout: float,
        match: re.Pattern[str] | None = None,
    ) -> str:
        """Stream the job's log until it exits, matches, times out, or is abandoned.

        Cancellation is the interesting case. The command is not ours to kill
        just because the caller stopped listening: a follow-up means "stop
        waiting", and only an explicit interrupt means "stop working". The
        policy lives on the registry because the app knows which one happened
        and this coroutine does not.
        """
        stream = _OutputStream(job, ctx, matching=match is not None)
        matched = False
        released = False
        generation = self._jobs.release_generation
        job.waiting = True
        try:
            with anyio.move_on_after(timeout):
                while True:
                    self._jobs.refresh()
                    if not job.running:
                        break
                    await stream.emit()
                    if match is not None and match.search(stream.matchable):
                        matched = True
                        break
                    # The release signal handles existing waits. Input may
                    # also arrive after tool admission but before this wait, or
                    # become deliverable once MCP activation finishes.
                    if self._jobs.release_generation != generation or steering_pending(ctx):
                        released = True
                        break
                    await anyio.sleep(_POLL_INTERVAL)
            await stream.drain()
        except BaseException:
            if self._jobs.cancel_policy == "stop":
                self._jobs.stop(job)
            else:
                job.detached = True
                self._jobs.mark_waited(job)
            raise
        finally:
            job.waiting = False
        await self._emit_finished(ctx, job, truncated=stream.truncated)
        if matched:
            self._jobs.mark_waited(job)
            return self._handle(job, reason="Matched until_output; the command is still running.")
        if job.running:
            self._jobs.mark_waited(job)
            if released:
                reason = "The user sent a follow-up, so the wait ended; the command did not."
            else:
                reason = f"The wait ended after {format_duration(timeout)}, not the command."
            return self._handle(job, reason=f"{reason} It was not killed.")
        return self._result(job)

    async def _emit_finished(
        self, ctx: RunContext[AgentDepsT], job: Job, *, truncated: bool
    ) -> None:
        await ctx.emit(
            CommandFinishedEvent(
                tool_call_id=ctx.tool_call_id,
                pid=job.supervisor_pid,
                output_path=str(job.output_path),
                status_path=str(job.status_path),
                exit_code=job.exit_code,
                truncated=truncated,
                total_lines=None,
            )
        )

    # -- result rendering ----------------------------------------------------

    def _result(self, job: Job) -> str:
        """A finished job: its output and one status line, with no handles.

        Handles are what you need to come back to a command later. A command
        that is over has nothing to come back to, and repeating a PID and two
        paths on every `ls` both wastes context and teaches the model that
        every command is something to be managed.

        Handing over the result is the model's report of the exit, so it gets
        no second notice at its next request and no wake-up turn for it.
        """
        job.announced.add("model")
        output, truncated = self._jobs.read_output(job)
        lines = [output.rstrip("\n")] if output.strip() else []
        status = f"[{job.id} · {job.outcome()} · {format_duration(job.elapsed)}]"
        if truncated:
            status += f"\nEarlier output was dropped; the full log is at {job.output_path}"
        lines.append(status)
        return "\n".join(lines)

    def _handle(self, job: Job, *, reason: str) -> str:
        """A running job: what it has printed, plus how to get back to it."""
        output, truncated = self._jobs.read_output(job, max_bytes=OUTPUT_TAIL_BYTES // 2)
        lines = []
        if output.strip():
            lines.append(output.rstrip("\n"))
            if truncated:
                lines.append(f"[earlier output is in {job.output_path}]")
        lines.append(
            f"[{job.id} · running · pid {job.pid or job.supervisor_pid} · "
            f"{format_duration(job.elapsed)}] {reason}\n"
            f"Command: {job.command}\n"
            f"Its exit status will reach you automatically; do not sleep or poll for it. "
            f'Use wait_for_job("{job.id}") to block on it, job_output("{job.id}") to see '
            f'progress, stop_job("{job.id}") to stop it.'
        )
        return "\n".join(lines)

    # -- helpers -------------------------------------------------------------

    def _require(self, job_id: str) -> Job:
        job = self._jobs.get(job_id)
        if job is None:
            known = ", ".join(sorted(self._jobs.jobs)) or "none"
            raise ModelRetry(f"No job {job_id!r}. Known jobs: {known}.")
        return job

    def _wait_seconds(self, timeout: float | None) -> float:
        if timeout is None:
            return min(self._default_timeout, MAX_WAIT_SECONDS)
        if timeout <= 0:
            raise ModelRetry("timeout must be greater than zero.")
        return min(timeout, MAX_WAIT_SECONDS)

    def _compile(self, pattern: str | None) -> re.Pattern[str] | None:
        if pattern is None:
            return None
        try:
            return re.compile(pattern)
        except re.error as error:
            raise ModelRetry(f"until_output is not a valid regular expression: {error}") from error


class _OutputStream:
    """Incremental reader that turns a job's log into bounded UI events.

    Emitting and matching have different budgets on purpose. The preview is a
    transient view and stays small; `until_output` has to keep reading, because
    a chatty server would otherwise print its readiness banner past the end of
    a stream nobody is still watching.
    """

    def __init__(self, job: Job, ctx: RunContext[Any], *, matching: bool = False) -> None:
        self.job = job
        self.ctx = ctx
        self.matching = matching
        self.offset = 0
        self.matchable = ""
        self.truncated = False
        self.decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")

    async def emit(self) -> bool:
        budget = _MATCH_READ_BYTES if self.matching else _EVENT_BUDGET_BYTES
        if self.offset >= budget:
            self.truncated = True
            return False
        # Never read across the preview boundary, so a chunk is either wholly
        # showable or wholly beyond it and the decoder stays in step.
        limit = _EVENT_BUDGET_BYTES if self.offset < _EVENT_BUDGET_BYTES else budget
        try:
            with self.job.output_path.open("rb") as source:
                source.seek(self.offset)
                chunk = source.read(limit - self.offset)
        except OSError:
            return False
        showable = self.offset < _EVENT_BUDGET_BYTES
        self.offset += len(chunk)
        if not chunk:
            return False
        text = self.decoder.decode(chunk)
        if not text:
            return True
        if showable:
            await self.ctx.emit(CommandOutputEvent(text=text))
        else:
            self.truncated = True
        self.matchable = (self.matchable + text)[-_MATCH_BUDGET_CHARS:]
        return True

    async def drain(self) -> None:
        while await self.emit():
            pass


@dataclass
class JobShell(Shell[AgentDepsT]):
    """`Shell`, but its commands are jobs this session can name and come back to.

    Commands start in `workdir` with `env` as their whole environment: they
    run through `pcode.jobs` on this host, not through `ctx.workspace`, whose
    local backend passes on only `PATH` and `HOME`.
    """

    workdir: Path = field(default_factory=Path.cwd, kw_only=True)

    def _make_toolset(self) -> JobShellToolset[AgentDepsT]:
        return JobShellToolset[AgentDepsT](
            jobs=registry(),
            workdir=Path(self.workdir),
            allowed_commands=self.allowed_commands,
            denied_commands=self.denied_commands,
            denied_operators=self.denied_operators,
            default_timeout=self.default_timeout,
            max_output_chars=self.max_output_chars,
            max_file_bytes=self.max_file_bytes,
            persist_cwd=self.persist_cwd,
            allow_interactive=self.allow_interactive,
            env=self.env,
            denied_env_patterns=self.denied_env_patterns,
            tools=self.tools,
            id=self.id or "shell",
        )
