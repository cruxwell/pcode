import asyncio
import os
import re
from dataclasses import asdict
from io import StringIO
from types import SimpleNamespace

import pytest
from pydantic_ai import Agent, ModelRetry
from pydantic_ai.models.function import DeltaToolCall, FunctionModel
from rich.console import Console

from pcode.live import AgentRuntime
from pcode.runtime import RunStatus, ToolSummary
from pcode.tool_display import (
    label,
    native_result_detail,
    native_result_projection,
    result_detail,
    spilled_call_id,
    subject,
    target,
)
from pcode.ui import Transcript


@pytest.mark.parametrize(
    "name,args,content,expected,failed",
    [
        (
            "read_file",
            {"path": "a.py"},
            "[a.py | 5 lines | hash:abc]\n     2\tx\n"
            "     3\ty\n... (2 more lines. Use offset=3 to continue reading.)\n",
            "a.py → lines 2–3 · 2 lines · truncated",
            False,
        ),
        (
            "read_file",
            {"path": "a.py"},
            "[a.py | 0 lines | hash:abc]\n(empty file)\n",
            "a.py → 0 lines · empty file",
            False,
        ),
        (
            "search_files",
            {"path": "src", "pattern": "ToolSummary", "include_glob": "*.py"},
            "a.py:2:private text\na.py:3:private text\nb.py:1:x",
            '"ToolSummary" in src · glob "*.py" → 3 matches in 2 files',
            False,
        ),
        ("search_files", {}, "No matches found.", ". → No matches", False),
        (
            "list_directory",
            {"path": "src"},
            "src/a.py  (10 bytes)\nsrc/sub/",
            "src → 1 files · 1 directories",
            False,
        ),
        ("list_directory", {}, "(empty directory)", ". → 0 files · 0 directories", False),
        (
            "write_file",
            {"path": "a.py"},
            "Wrote 20 chars (2 lines) to a.py. [hash:abc]",
            "a.py → 2 lines written · 20 chars",
            False,
        ),
        (
            "edit_file",
            {"path": "a.py", "old_text": "a", "new_text": "b\nc"},
            "Edited a.py.",
            "a.py → +2 −1 replacement lines",
            False,
        ),
        (
            "run_command",
            {"command": "pytest -q"},
            "[stderr]\nprivate text\n[exit code: 1]",
            "pytest -q → exit 1",
            True,
        ),
        (
            "run_command",
            {"command": "pytest"},
            "[stderr]\nprivate text\n[exit code: 127]",
            "pytest → exit 127 · Executable not found",
            True,
        ),
        (
            "run_command",
            {"command": "pytest"},
            "[Command timed out after 30.0s]",
            "pytest → Timed out",
            True,
        ),
        ("run_command", {"command": "pytest"}, "(no output)", "pytest", False),
        (
            "write_plan",
            {"items": [{"status": "completed"}, {"status": "in_progress"}]},
            "ok",
            "2 steps · 1 complete · working on step 2",
            False,
        ),
        (
            "inventory_agent_context",
            {},
            {"roots": [{"exists": False}]},
            "No assistant configuration directories found",
            False,
        ),
        ("inventory_agent_context", {}, {}, "Assistant configuration inspected", False),
        (
            "wait_for_job",
            {"job_id": "j4"},
            "output\n[j4 · exit 2 · 3.0s]",
            "j4 · exit 2",
            True,
        ),
        (
            "job_output",
            {"job_id": "j4"},
            "output\n[j4 · running · 3.0s]",
            "j4 · still running",
            False,
        ),
        ("stop_job", {}, "", "job unavailable", False),
        ("unknown_tool", {"secret": "private text"}, "private text", "", False),
    ],
)
def test_safe_result_metrics(name, args, content, expected, failed):
    assert result_detail(name, args, content, "success") == (expected, failed)


@pytest.mark.parametrize(
    ("media_type", "label"),
    [("image/png", "PNG"), ("image/jpeg", "JPEG"), ("image/webp", "WEBP")],
)
def test_reading_an_image_says_what_was_shown(media_type, label):
    from pydantic_ai.messages import BinaryContent

    content = ["[shot.png | header]\n", BinaryContent(data=b"x" * 12345, media_type=media_type)]
    assert result_detail("read_file", {"path": "shot.png"}, content, "success") == (
        f"shot.png → {label} image · 12,345 bytes",
        False,
    )


def test_a_read_that_returned_no_image_is_not_called_one():
    # Nothing in the list is a file: the generic fallback, not an image summary.
    assert result_detail("read_file", {"path": "a.py"}, ["text only"], "success") == (
        "a.py → Read finished",
        False,
    )


def test_retry_is_not_reported_as_retrying_and_shows_error_body():
    detail, failed = result_detail(
        "read_file", {"path": "a.py"}, "No such file: private text", "retry"
    )
    assert failed
    assert detail == "a.py → Retry requested · No such file: private text"


def test_targets_are_relative_bounded_and_show_shell_arguments(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert target("read_file", {"path": str(tmp_path / "a.py")}) == "a.py"
    assert target("read_file", {"path": ".env"}) == ".env"
    command = 'MODE=test python -c "print(123)" | head -n 1'
    assert target("run_command", {"command": command}) == command
    assert target("start_command", {"command": command}) == command
    assert "\x1b" not in target("read_file", {"path": "a\x1b[2J.py"})
    assert len(target("read_file", {"path": "a" * 500})) <= 90


def test_truncated_counts_are_marked():
    detail, _ = result_detail(
        "search_files", {}, "a.py:1:x\n[... truncated at 1 matches]", "success"
    )
    assert detail == ". → 1 matches in 1 files · truncated"


def test_compact_rows_and_old_and_new_event_shapes():
    stream = StringIO()
    transcript = Transcript(Console(file=stream, width=180, color_system=None))
    old = ToolSummary("read_file", "a.py · 2 lines")
    new = ToolSummary("run_command", "pytest → exit 1", True, "call-2", 0.25)
    transcript.events((old, ToolSummary(**asdict(new))))
    assert [line.rstrip() for line in stream.getvalue().splitlines()] == [
        "✓ Read file  a.py · 2 lines",
        "✗ Run shell · exit 1 · 0.2s",
    ]


def test_tool_lines_group_together_and_are_blank_separated_from_prose():
    from pcode.runtime import Message, Thinking
    from pcode.ui import Activity

    stream = StringIO()
    transcript = Transcript(Console(file=stream, width=80, color_system=None), activity=Activity())
    transcript.activity.thinking_mode = "scrollback"
    transcript.events(
        (
            Thinking("Reading first."),
            ToolSummary("read_file", "a.py · 2 lines"),
            ToolSummary("read_file", "b.py · 3 lines"),
            Message("Done."),
        )
    )
    assert [line.rstrip() for line in stream.getvalue().splitlines()] == [
        "Reading first.",
        "",
        "✓ Read file  a.py · 2 lines",
        "✓ Read file  b.py · 3 lines",
        "",
        "Done.",
        "",
    ]


def test_scrollback_reports_whether_the_live_panel_needs_its_own_blank_row():
    from pcode.runtime import Message

    transcript = Transcript(Console(file=StringIO(), width=80, color_system=None))
    assert transcript.ends_blank
    transcript.events((ToolSummary("read_file", "a.py · 2 lines"),))
    assert not transcript.ends_blank
    transcript.events((Message("Done."),))
    assert transcript.ends_blank


def test_tool_summary_lines_use_the_thinking_shade():
    stream = StringIO()
    transcript = Transcript(Console(file=stream, width=80, color_system="truecolor"))
    transcript.events((ToolSummary("read_file", "a.py · 2 lines"),))
    thinking = transcript.rich_theme.styles["pcode.thinking"]
    assert thinking.color is not None and thinking.dim
    assert thinking.render("x").split("x")[0] in stream.getvalue()


def test_parallel_calls_remain_active_until_last_result_and_keep_identity():
    async def run():
        slow_started = asyncio.Event()
        release_slow = asyncio.Event()
        requests = 0

        async def model(messages, info):
            nonlocal requests
            requests += 1
            if requests == 1:
                yield {
                    0: DeltaToolCall(
                        name="read_file", json_args='{"path":"slow.py"}', tool_call_id="slow"
                    ),
                    1: DeltaToolCall(
                        name="read_file", json_args='{"path":"fast.py"}', tool_call_id="fast"
                    ),
                }
            else:
                yield "Done"

        agent = Agent(FunctionModel(stream_function=model))

        @agent.tool_plain
        async def read_file(path: str) -> str:
            if path == "slow.py":
                slow_started.set()
                await release_slow.wait()
            else:
                # Deadlocks if the library stops executing these tools concurrently.
                await slow_started.wait()
            return f"[{path} | 1 lines | hash:abc]\n     1\tprivate text\n"

        events = []
        async for event in AgentRuntime(agent).stream("Read both"):
            events.append(event)
            if isinstance(event, RunStatus) and event.text.startswith(
                "Running read_file · slow.py"
            ):
                if any(isinstance(e, ToolSummary) and e.call_id == "fast" for e in events):
                    release_slow.set()
        summaries = [e for e in events if isinstance(e, ToolSummary)]
        assert [e.call_id for e in summaries] == ["fast", "slow"]
        assert all(e.elapsed_seconds is not None and e.elapsed_seconds >= 0 for e in summaries)
        assert summaries[0].detail.startswith("fast.py → lines 1–1")
        assert any(isinstance(e, RunStatus) and "Running 2 tools" in e.text for e in events)
        first = events.index(summaries[0])
        last = events.index(summaries[1])
        assert not any(
            isinstance(e, RunStatus) and e.text == "Waiting for model…" for e in events[first:last]
        )
        assert events[last + 1] == RunStatus("Waiting for model…")

    asyncio.run(asyncio.wait_for(run(), timeout=5))


def test_retry_feedback_and_actual_success_have_separate_summaries():
    async def run():
        requests = 0

        async def model(messages, info):
            nonlocal requests
            requests += 1
            if requests <= 2:
                yield {0: DeltaToolCall(name="retry_tool", json_args="{}")}
            else:
                yield "Done"

        agent = Agent(FunctionModel(stream_function=model))

        @agent.tool_plain
        def retry_tool() -> str:
            if requests == 1:
                raise ModelRetry("Invalid arguments: private text")
            return "private text"

        events = [e async for e in AgentRuntime(agent).stream("Try")]
        summaries = [e for e in events if isinstance(e, ToolSummary)]
        assert summaries[0].failed
        assert summaries[0].detail == "Retry requested · Invalid arguments: private text"
        assert not summaries[1].failed
        assert "private text" not in summaries[1].detail

    asyncio.run(run())


@pytest.mark.parametrize(
    "name,args,expected",
    [
        ("search_files", {"pattern": "ToolSummary", "path": "src"}, '"ToolSummary" in src'),
        ("search_files", {"pattern": ""}, '"" in .'),
        ("find_files", {"pattern": "**/*.py", "path": "tests"}, '"**/*.py" in tests'),
        ("search_files", {"pattern": "password", "path": "src"}, '"password" in src'),
    ],
)
def test_search_targets_include_patterns(name, args, expected):
    assert target(name, args) == expected


def test_a_job_tool_is_about_the_command_its_job_runs():
    jobs = {"j1": SimpleNamespace(command="deploy --token hunter2", purpose="  shipping\n it ")}
    assert subject("wait_for_job", {"job_id": "j1"}, jobs) == (
        "deploy --token hunter2",
        "shipping it",
    )
    assert subject("stop_job", {"job_id": "j2"}, jobs) == ("", "")
    assert subject("job_output", {}, jobs) == ("", "")
    # Every other call is about its own arguments, never a job's.
    assert subject("shell", {"command": "ls", "purpose": "look", "job_id": "j1"}, jobs) == (
        "ls",
        "look",
    )
    assert subject("read_file", {"path": "a.py"}, jobs) == ("", "")


def test_long_command_is_compact_without_numbers_or_details_hint():
    command = "pytest " + " ".join(f"tests/test_example_{i}.py" for i in range(20))
    assert len(target("run_command", {"command": command})) <= 100
    detail, failed = result_detail("run_command", {"command": command}, "(no output)", "success")
    stream = StringIO()
    transcript = Transcript(Console(file=stream, width=60, color_system=None))
    transcript.command_summary(ToolSummary("run_command", detail, failed, command=command))
    output = stream.getvalue()
    assert "…" in output
    assert "tests/test_example_19.py" not in output
    assert "exit 0" not in output
    assert "/tool" not in output
    assert "#" not in output
    assert len(output.splitlines()) == 1
    assert output.startswith("✓ Run shell · pytest ")
    assert len(output.rstrip()) == 60


@pytest.mark.parametrize("failed", [False, True])
def test_command_summary_keeps_timing_and_preview_on_one_line(failed):
    stream = StringIO()
    transcript = Transcript(Console(file=stream, width=80, color_system=None))
    transcript.command_summary(
        ToolSummary(
            "shell",
            "echo hello → exit 1" if failed else "echo hello",
            failed=failed,
            elapsed_seconds=0.8,
            command="echo hello",
        )
    )
    heading = "✗ Run shell · exit 1" if failed else "✓ Run shell"
    assert stream.getvalue().splitlines() == [f"{heading} · 0.8s · echo hello"]


@pytest.mark.parametrize("failed, outcome", [(False, "exit 0"), (True, "exit 2")])
@pytest.mark.parametrize("show_commands", [False, True])
def test_background_completion_has_compact_job_label(failed, outcome, show_commands):
    from pcode.runtime import JobFinished

    stream = StringIO()
    transcript = Transcript(Console(file=stream, width=100, color_system=None))
    transcript.command_scrollback = show_commands
    transcript.tool_error_scrollback = True
    transcript.tool_result(
        JobFinished(
            "shell",
            f"make test → j12 · {outcome}",
            failed=failed,
            elapsed_seconds=0.8,
            command="make test",
            result=f"test output\n[j12 · {outcome} · 0.8s]",
        )
    )
    output = stream.getvalue()
    marker = "✗" if failed else "✓"
    heading = f"{marker} Run shell · j12 · {outcome} · 0.8s"
    assert output.startswith(heading)
    assert output.count("j12") == 1
    assert "background" not in output
    if not show_commands:
        assert output.splitlines() == [f"{heading} · make test"]


def test_background_heading_matches_other_command_lines():
    from pcode.tool_display import tool_summary_lines

    (line,) = tool_summary_lines("shell", " · j3 · stopped", command="make test")
    assert line.plain == "✓ Run shell · j3 · stopped · make test"


def test_multiline_command_preview_is_compact_and_sanitized():
    from pcode.tool_display import command_text

    command = "python - <<'PY'\n    print('hello')\nPY"
    stream = StringIO()
    transcript = Transcript(Console(file=stream, width=80, color_system=None))
    transcript.command_summary(ToolSummary("run_command", "script → exit 0", command=command))
    assert "print('hello')" not in stream.getvalue()
    assert "1 more lines" not in stream.getvalue()
    assert "2 more lines" in stream.getvalue()
    assert "\\n" not in stream.getvalue()
    assert command_text("echo \x1b[2Jhello\u202e\n\tend") == "echo hello \n    end"


def test_command_preview_drops_a_cd_into_the_current_directory(tmp_path, monkeypatch):
    from pcode.tool_display import command_preview

    monkeypatch.chdir(tmp_path)
    here = str(tmp_path)
    assert command_preview(f"cd {here} && make test") == "make test"
    assert command_preview(f'cd "{here}" ; make test') == "make test"
    assert command_preview(f"cd {here}\nmake test\necho done") == "make test … [1 more lines]"
    assert command_preview("cd . && make test") == "make test"
    # A cd elsewhere is real information about where the command ran.
    (tmp_path / "sub").mkdir()
    assert command_preview("cd sub && make test") == "cd sub && make test"
    assert command_preview('cd "$REPO" && make test') == 'cd "$REPO" && make test'


@pytest.mark.parametrize("show", ["summary", "events"])
def test_failed_tool_summary_line_is_marked_only_by_its_symbol(show):
    """The ✗ carries the failure; the line keeps the styling a success would get."""

    def render(failed: bool) -> str:
        stream = StringIO()
        transcript = Transcript(
            Console(file=stream, width=80, force_terminal=True, color_system="truecolor")
        )
        if show == "summary":
            transcript.command_summary(
                ToolSummary("run_command", "pytest -q → exit 1", failed, command="pytest -q")
            )
        else:
            transcript.events((ToolSummary("read_file", "missing.py", failed),))
        return stream.getvalue()

    failure, success = render(True), render(False)
    assert "✗" in failure and "✓" in success
    codes = re.compile(r"\x1b\[[0-9;]*m")
    assert codes.findall(failure) == codes.findall(success)


def test_command_text_preserves_lines_without_controls():
    from pcode.tool_display import command_text

    assert command_text("client\x1b[31m --flag\x07\necho\tdone") == "client --flag\necho    done"


def test_command_and_query_controls_are_not_sent_to_terminal():
    assert (
        target("run_command", {"command": "echo first\necho second"})
        == "echo first … [1 more lines]"
    )
    for name, args in (
        ("run_command", {"command": "echo \x1b[2Jhello\u202e"}),
        ("search_files", {"pattern": "hello\x1b[2J\u202e"}),
    ):
        rendered = target(name, args)
        assert "\x1b" not in rendered
        assert "\u202e" not in rendered


@pytest.mark.parametrize(
    "name,args,result,expected",
    [
        ("run_command", {"command": "pytest -q tests/"}, "(no output)", "pytest -q tests/"),
        (
            "search_files",
            {"pattern": "ToolSummary", "path": "src"},
            "No matches found.",
            '"ToolSummary" in src',
        ),
    ],
)
def test_visible_inputs_reach_running_and_completed_events(name, args, result, expected):
    import json

    async def run():
        requests = 0

        async def model(messages, info):
            nonlocal requests
            requests += 1
            if requests == 1:
                yield {0: DeltaToolCall(name=name, json_args=json.dumps(args))}
            else:
                yield "Done"

        agent = Agent(FunctionModel(stream_function=model))

        @agent.tool_plain
        def run_command(command: str) -> str:
            return result

        @agent.tool_plain
        def search_files(pattern: str, path: str) -> str:
            return result

        events = [e async for e in AgentRuntime(agent).stream("Inspect")]
        assert any(isinstance(e, RunStatus) and expected in e.text for e in events)
        assert any(isinstance(e, ToolSummary) and expected in e.detail for e in events)

    asyncio.run(run())


@pytest.mark.parametrize(
    "content",
    ["(no output)", "[stdout]\nok", "[stderr]\nwarning", "[exit code: 0]", "other output"],
)
def test_successful_commands_have_no_redundant_status(content):
    assert result_detail("run_command", {"command": "echo hi"}, content, "success") == (
        "echo hi",
        False,
    )


@pytest.mark.parametrize(
    "name,args,expected",
    [
        ("get_page", {"url": "https://example.com/docs"}, "https://example.com/docs"),
        ("web_search", {"query": "python async tools"}, "python async tools"),
        ("get_page", {}, "url unavailable"),
        ("web_search", {"query": None}, "query unavailable"),
    ],
)
def test_web_tools_show_inputs_in_targets_and_results(name, args, expected):
    assert target(name, args) == expected
    assert result_detail(name, args, "private response", "success") == (expected, False)
    stream = StringIO()
    Transcript(Console(file=stream, width=180, color_system=None)).events(
        (ToolSummary(name, expected),)
    )
    assert expected in stream.getvalue()
    assert "Succeeded" not in stream.getvalue()


def _hit(url: str, **extra) -> dict:
    return {"type": "web_search_result", "title": url and "T", "url": url, **extra}


def test_claude_code_web_results_are_labelled_and_readable():
    hits = [_hit("https://a", summary="It says hi."), _hit("https://b")]
    args = {"query": "q"}
    assert native_result_detail("web_search", args, hits, "success", "claude") == (
        "q → 2 results · via Claude Code",
        False,
    )
    shown = native_result_projection(hits, "claude")
    assert shown == (
        "2 results\n\nT\nhttps://a\n\nT\nhttps://b\n\nClaude Code's summary:\nIt says hi."
    )
    # A summary kept with no parseable links is not a result.
    alone = [_hit("", summary="Only prose.")]
    assert native_result_detail("web_search", args, alone, "success")[0] == "q → 0 results"
    assert native_result_projection(alone) == "No results.\n\nSummary:\nOnly prose."

    page = {
        "type": "web_fetch_result",
        "url": "https://a",
        "retrieved_at": None,
        "content": {
            "type": "document",
            "title": None,
            "source": {"type": "text", "data": "The answer."},
        },
    }
    assert native_result_projection(page, "claude") == (
        "https://a\nClaude Code's answer about the page, not the page text\n\nThe answer."
    )
    assert native_result_projection(page) == "https://a\n\nThe answer."
    pdf = {**page, "content": {"type": "document", "source": {"type": "base64", "data": "JVBE"}}}
    assert native_result_projection(pdf) is pdf

    failure = {"type": "web_fetch_tool_result_error", "error_code": "unavailable"}
    failure["message"] = "Fetch failed\nstatus 503"
    detail = native_result_detail("web_fetch", {"url": "https://a"}, failure, "success", "claude")
    assert detail == ("https://a → Failed · Fetch failed · via Claude Code", True)
    assert native_result_projection(failure) == "Fetch failed\nstatus 503"


def test_tool_search_shows_its_queries():
    assert label("search_tools") == "Find tools"
    assert target("search_tools", {"queries": ["issues", "pull requests"]}) == (
        "issues, pull requests"
    )
    assert target("search_tools", {}) == "queries unavailable"


def test_session_search_shows_its_query_scope_and_paging():
    assert label("search_sessions") == "Search sessions"
    assert target("search_sessions", {"query": "status line"}) == '"status line"'
    assert (
        target("search_sessions", {"query": "x", "scope": "all", "after": "cursor"})
        == '"x" · scope all · next page'
    )
    assert target("search_sessions", {}) == "query unavailable"


def test_session_read_shows_the_sessions_name(tmp_path, monkeypatch):
    from pcode.sessions import SessionInfo

    monkeypatch.setenv("PCODE_SESSION_DIR", str(tmp_path))
    session_id = "0123456789abcdef"
    directory = tmp_path / session_id
    directory.mkdir()

    def save(**names):
        info = SessionInfo(
            id=session_id,
            model="m",
            workspace="/w",
            created="t",
            updated="t",
            packages={},
            **names,
        )
        (directory / "session.json").write_text(info.model_dump_json())

    save(title="Fix the status line")
    args = {"session_id": session_id, "turn_id": "t1"}
    assert target("read_session", args) == '"Fix the status line" (01234567)'
    save(title="Fix the status line", name="Renamed")
    stat = (directory / "session.json").stat()
    os.utime(directory / "session.json", ns=(stat.st_atime_ns, stat.st_mtime_ns + 10**9))
    assert target("read_session", {**args, "offset": 50, "scope": "session"}) == (
        '"Renamed" (01234567) · from char 50 · scope session'
    )
    assert target("read_session", {**args, "offset": True}) == '"Renamed" (01234567)'
    (directory / "session.json").write_text("not json")
    os.utime(directory / "session.json", ns=(stat.st_atime_ns, stat.st_mtime_ns + 2 * 10**9))
    assert target("read_session", args) == "01234567"
    assert target("read_session", {"session_id": "unknown-session"}) == "unknown-"
    assert target("read_session", {"session_id": "../" + session_id}) == "../01234"
    assert target("read_session", {}) == "session unavailable"


@pytest.mark.parametrize("outcome", ["retry", "failed"])
def test_tool_failure_shows_workspace_boundary_in_transcript(outcome):
    message = "Path '/' resolves outside the root directory."
    detail, failed = result_detail("list_directory", {"path": "/"}, message, outcome)
    assert failed
    assert message in detail
    stream = StringIO()
    Transcript(Console(file=stream, width=120, color_system=None)).events(
        (ToolSummary("list_directory", detail, failed=failed),)
    )
    assert message in stream.getvalue()
    assert "details withheld" not in stream.getvalue()


def test_tool_failure_preserves_feedback_without_controls():
    message = (
        "\x1b[31mAccess rejected\x1b[0m for /tmp/repo: "
        "synthetic-environment-credential; key=sk-0123456789ab; "
        "Bearer synthetic-bearer-value\nTry a workspace-relative path."
    )
    detail, failed = result_detail("read_file", {}, message, "retry")
    assert failed
    assert "Access rejected" in detail
    assert "/tmp/repo" in detail
    assert "Try a workspace-relative path." in detail
    assert "\x1b" not in detail


def test_tool_validation_failure_shows_location_and_message_without_raw_input():
    detail, failed = result_detail(
        "read_file",
        {},
        [
            {
                "loc": ("path",),
                "msg": "Input should be a valid string",
                "input": {"private": "raw input"},
            }
        ],
        "retry",
    )
    assert failed
    assert detail == ". → Retry requested · path: Input should be a valid string"
    assert "raw input" not in detail


@pytest.mark.parametrize("content", [None, "", []])
def test_empty_tool_failure_has_explicit_fallback(content):
    assert result_detail("read_file", {}, content, "retry") == (
        ". → Retry requested · No error details returned.",
        True,
    )


@pytest.mark.parametrize(
    "args,source,expected",
    [
        ({"handle": "run/toolu_1.0"}, "search_sessions", "search_sessions output · lines 1–200"),
        (
            {"handle": "run/toolu_1.0", "offset": 200, "limit": 50},
            "",
            "stored output · lines 201–250",
        ),
        ({"handle": "h", "from_end": True, "limit": 30}, "grep", "grep output · last 30 lines"),
        (
            {"handle": "h", "from_end": True, "offset": 30, "limit": 30},
            "",
            "stored output · lines 31–60 from end",
        ),
        ({"handle": "h", "pattern": "row 15"}, "shell", '"row 15" in shell output · matches 1–200'),
        ({"handle": "h", "limit": "many"}, "", "stored output"),
        ({"handle": "h", "limit": 5000}, "", "stored output · lines 1–1000"),
    ],
)
def test_stored_result_reads_name_their_source_and_window_not_the_handle(args, source, expected):
    assert target("read_tool_result", args, source) == expected


@pytest.mark.parametrize(
    "handle,call_id",
    [
        ("da0a06c7-e1ef/toolu_013Bk.0", "toolu_013Bk"),
        ("run/toolu_013Bk.1.content", "toolu_013Bk"),
        ("run/call.with.dots.12", "call.with.dots"),
        ("toolu_013Bk", ""),
        ("run/toolu_013Bk", ""),
    ],
)
def test_spill_handles_resolve_to_the_call_that_spilled(handle, call_id):
    assert spilled_call_id({"handle": handle}) == call_id


@pytest.mark.parametrize(
    "content,expected,failed",
    [
        ("[handle 'r/c.0': 132 matching line(s); showing 132]\n{", "132 lines", False),
        ("[handle 'r/c.0': 3,000 matching line(s); showing 200]\nx", "200 of 3,000 lines", False),
        (
            "[handle 'r/c.0': 1 matching line(s); showing 1, output capped]\nx",
            "1 line · output capped",
            False,
        ),
        (
            "[No stored tool result for handle 'r/c.0'. Use the exact handle]",
            "No stored result",
            True,
        ),
    ],
)
def test_stored_result_read_summaries(content, expected, failed):
    assert result_detail("read_tool_result", {"handle": "r/c.0"}, content, "success", "grep") == (
        f"grep output · lines 1–200 → {expected}",
        failed,
    )
