"""Tool presentation with visible inputs and sanitized command failure excerpts."""

import ast
import json
import re
from collections import Counter
from pathlib import Path
from typing import TYPE_CHECKING

from rich.text import Text

from pcode.code_mode import SANDBOXED_TOOLS
from pcode.diagnostics import strip_controls

if TYPE_CHECKING:
    from pcode.jobs import JobRegistry

LABELS = {
    # Verb + object, so a group tally ("Read file ✓ 15 · Run shell ✓ 1") says
    # what each count acted on without the per-row path or command beside it.
    "run_code": "Run code",
    "delegate_task": "Delegate task",
    "read_file": "Read file",
    "read_tool_result": "Read results",
    "write_file": "Write file",
    "edit_file": "Edit file",
    "search_files": "Search code",
    "find_files": "Find files",
    "list_directory": "List directory",
    "create_directory": "Create directory",
    "file_info": "File info",
    "shell": "Run shell",
    "wait_for_job": "Wait for job",
    "job_output": "Read job output",
    "stop_job": "Stop job",
    "list_files": "Find files",
    "grep": "Search code",
    "run_command": "Run shell",
    "start_command": "Start command",
    "check_command": "Check command",
    "stop_command": "Stop command",
    "write_plan": "Write plan",
    "search_tools": "Find tools",
    "search_sessions": "Search sessions",
    "read_session": "Read session",
    "inventory_agent_context": "Context",
}


def plain(value: str, limit: int | None = 90) -> str:
    """Remove controls (including bidi/ANSI introducers) and bound terminal text."""
    text = "".join(c if c.isprintable() else " " for c in value)
    return text if limit is None or len(text) <= limit else text[: limit - 1] + "…"


def label(name: str) -> str:
    return LABELS.get(name, plain(name))


def argument(value: str) -> str:
    return plain(strip_controls(value).replace("\n", r"\n").replace("\t", r"\t"), limit=None)


def command_text(value: str) -> str:
    """Preserve layout without allowing terminal controls."""
    value = strip_controls(_ANSI.sub("", value))
    return "\n".join(plain(line.expandtabs(4), limit=None) for line in value.split("\n"))


_CD_PREFIX = re.compile(
    r"^cd\s+(?P<dir>'[^']*'|\"[^\"]*\"|[^\s;&|]+)[ \t]*(?:&&|;|\n)[ \t\n]*",
)


def _is_current_directory(value: str) -> bool:
    if len(value) > 1 and value[0] == value[-1] and value[0] in "'\"":
        value = value[1:-1]
    if not value or "$" in value or "`" in value:
        return False
    try:
        return Path(value).expanduser().resolve() == Path.cwd().resolve()
    except OSError:
        return False


def _strip_redundant_cd(text: str) -> str:
    """Drop a leading `cd <dir> &&` that only names the directory we are in.

    Agents habitually prefix commands with a `cd` to the workspace root, which
    costs a preview line's worth of width and says nothing. A `cd` elsewhere is
    real information, so it survives.
    """
    while (match := _CD_PREFIX.match(text)) and _is_current_directory(match.group("dir")):
        text = text[match.end() :]
    return text


def command_preview(value: str) -> str:
    """Structural preview, not an inferred claim about a script's purpose."""
    text = _strip_redundant_cd(command_text(value).lstrip())
    lines = text.splitlines()
    first = next((line.strip() for line in lines if line.strip()), "(empty command)")
    # Inline interpreter payloads are implementation detail, not useful labels.
    inline = re.search(r"\b(?:python[\d.]*|node|ruby|perl)\s+(-c|-e)\s+", first)
    if inline and (len(first) > 100 or len(lines) > 1):
        first = first[: inline.end()].rstrip() + " … [inline code hidden]"
    elif len(lines) > 1:
        first += f" … [{len(lines) - 1} more lines]"
    return plain(first, limit=100)


def tool_summary_lines(
    name: str,
    detail: str = "",
    *,
    failed: bool = False,
    elapsed_seconds: float | None = None,
    command: str = "",
    width: int | None = None,
) -> list[Text]:
    """The settled-tool line, including any command preview, as scrollback draws it.

    Shared so a browsed conversation looks like the one that scrolled past:
    scrollback and the session browser differ only in how much of `detail`
    they pass, which each caller decides before calling.
    """
    elapsed = f" · {elapsed_seconds:.1f}s" if elapsed_seconds is not None else ""
    # The marker alone reports failure: a summary line keeps one style so a
    # failed call does not shout louder than the diagnostic that follows it.
    marker = "✗" if failed else "✓"
    preview = " · " + command_preview(command) if command else ""
    lines = [Text(f"{marker} {label(name)}{detail}{elapsed}{preview}", style="pcode.thinking")]
    for line in lines:
        line.no_wrap = True
        line.overflow = "ellipsis"
        if width is not None:
            line.truncate(width, overflow="ellipsis")
    return lines


def sandboxed_calls(code: str) -> list[str]:
    """Report the tool calls a snippet makes by parsing it, not by guessing.

    Names inside strings or comments cannot masquerade as calls, and a snippet
    that does not parse simply reports nothing rather than a wrong summary.
    """
    try:
        tree = ast.parse(code)
    except SyntaxError, ValueError:
        return []
    counts = Counter(
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in SANDBOXED_TOOLS
    )
    return [f"{tool} ×{count}" if count > 1 else tool for tool, count in counts.items()]


def code_preview(code: str) -> str:
    """Summarize a sandboxed snippet by the calls it makes and its size."""
    lines = sum(bool(line.strip()) for line in code.splitlines())
    size = f"{lines} lines" if lines != 1 else "1 line"
    calls = sandboxed_calls(code)
    return plain(f"{' · '.join(calls)} · {size}" if calls else size, limit=100)


def stated_purpose(args: dict) -> str:
    """The model's own short label for a call, sanitized, or "" when absent.

    Only jobs it expects to come back to carry one, so every surface that shows
    it must also work without it.
    """
    purpose = args.get("purpose")
    if not isinstance(purpose, str) or not purpose.strip():
        return ""
    return plain(argument(" ".join(purpose.split())), 60)


def execution_mode(name: str, args: dict) -> str:
    """How the model asked a command to run: "background", "foreground", or "".

    `shell` decides per call; the sandbox tools decide by which tool was picked.
    Everything else has nothing to say here, and says nothing.
    """
    if name == "shell":
        return "background" if args.get("background") else "foreground"
    if name == "start_command":
        return "background"
    if name == "run_command":
        return "foreground"
    return ""


def invocation(name: str, args: dict) -> str:
    """The sanitized command a command tool ran, or "" for every other tool.

    Scrollback previews a settled command from this, so an event built without
    it settles as a bare "Run" row that never says what ran.
    """
    command = args.get("command")
    if name in {"shell", "run_command", "start_command"} and isinstance(command, str):
        return command_text(command)
    return ""


def subject(name: str, args: dict, jobs: "JobRegistry") -> tuple[str, str]:
    """The sanitized command a call is about and its stated purpose, each "" if none.

    A command tool's come from its own arguments. A job tool names only an id,
    so its come from the `shell` call that started that job: otherwise a wait
    says a job is being waited on but never what the job runs. An id `jobs`
    does not know leaves both empty rather than guessing.
    """
    if name in JOB_HANDLE_TOOLS:
        job_id = args.get("job_id")
        job = jobs.get(job_id) if isinstance(job_id, str) else None
        if job is None:
            return "", ""
        name, args = "shell", {"command": job.command, "purpose": job.purpose}
    return invocation(name, args), stated_purpose(args)


def assignment(name: str, args: dict) -> tuple[str, str]:
    """A delegation's sanitized agent name and task label, or ("", "") for any other tool.

    The label is the model's stated purpose when it gave one: a task usually
    opens with setup ("Repository: ...") that says nothing about the work.
    """
    if name != "delegate_task":
        return "", ""
    agent = args.get("agent_name")
    task = args.get("task")
    return (
        plain(argument(agent), 60) if isinstance(agent, str) else "agent unavailable",
        stated_purpose(args)
        or (plain(argument(task), 160) if isinstance(task, str) else "assignment unavailable"),
    )


# Harness keys a spill `<run_id>/<tool_call_id>.<retry>`, plus `.content` for
# the content part of a `ToolReturn`.
_SPILL_HANDLE = re.compile(r"[^/]+/(.+)\.\d+(?:\.content)?")
# Harness clamps a read to this many lines (its private `_MAX_READ_LINES`).
_MAX_STORED_READ_LINES = 1000


def spilled_call_id(args: dict) -> str:
    """The id of the call whose output a `read_tool_result` call reads, or "" if unknown."""
    handle = args.get("handle")
    match = _SPILL_HANDLE.fullmatch(handle) if isinstance(handle, str) else None
    return match[1] if match else ""


def stored_source(args: dict, called: dict[str, str]) -> str:
    """The tool, among `called` (call id to tool name), that spilled what `args` reads."""
    return called.get(spilled_call_id(args), "")


def stored_slice(args: dict, source: str = "") -> str:
    """What a `read_tool_result` call reads, in place of its opaque handle.

    The handle is a run id and a provider call id, which tell a reader nothing.
    The tool that produced the output and the window read from it do.
    """
    where = f"{plain(source, 40)} output" if source else "stored output"
    pattern = args.get("pattern")
    matching = isinstance(pattern, str) and bool(pattern)
    if matching:
        where = f"{json.dumps(plain(argument(pattern), 40), ensure_ascii=False)} in {where}"
    offset, limit = args.get("offset", 0), args.get("limit", 200)
    if not (isinstance(offset, int) and isinstance(limit, int)) or offset < 0 or limit < 1:
        return where
    limit = min(limit, _MAX_STORED_READ_LINES)
    unit = "matches" if matching else "lines"
    if args.get("from_end") is True:
        span = (
            f"last {limit} {unit}"
            if not offset
            else f"{unit} {offset + 1}–{offset + limit} from end"
        )
    else:
        span = f"{unit} {offset + 1}–{offset + limit}"
    return f"{where} · {span}"


def session_recall(name: str, args: dict) -> str:
    """What a recall call looks for: the query searched, or the session being read."""
    if name == "search_sessions":
        query = args.get("query")
        if not isinstance(query, str):
            return "query unavailable"
        parts = [json.dumps(plain(argument(query), 80), ensure_ascii=False)]
    else:
        session_id = args.get("session_id")
        if not isinstance(session_id, str):
            return "session unavailable"
        title = session_name(session_id)
        shown = argument(session_id)[:8]  # A short id, as /resume lists them.
        parts = [f"{json.dumps(title, ensure_ascii=False)} ({shown})" if title else shown]
        offset = args.get("offset")
        if isinstance(offset, int) and not isinstance(offset, bool) and offset > 0:
            parts.append(f"from char {offset}")
    scope = args.get("scope")
    if isinstance(scope, str) and scope != "project":
        parts.append(f"scope {plain(scope, 20)}")
    if isinstance(args.get("after"), str) and args["after"].strip():
        parts.append("next page")
    return " · ".join(parts)


_SESSION_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")
_session_names: dict[str, tuple[int, str]] = {}


def session_name(session_id: str) -> str:
    """A saved session's /rename name, else its auto title, or "" if unknown.

    Status rows redraw often, so the metadata is reread only when it changes:
    a title named after the first lookup still shows on the next one. Only the
    default session root is consulted; a session elsewhere shows its id alone.
    """
    if not _SESSION_ID.fullmatch(session_id):
        return ""  # Never let a model-supplied id walk out of the session root.
    from pcode.sessions import SessionError, read_info, session_root

    directory = session_root() / session_id
    try:
        stamp = (directory / "session.json").stat().st_mtime_ns
    except OSError:
        return ""
    key = str(directory)
    cached = _session_names.get(key)
    if cached and cached[0] == stamp:
        return cached[1]
    try:
        info = read_info(directory)
    except SessionError:
        title = ""  # Cached too, so an unreadable file is not reparsed every redraw.
    else:
        title = plain(argument(info.name or info.title or ""), 60)
    _session_names[key] = (stamp, title)
    return title


def target(name: str, args: dict, source: str = "") -> str:
    """`source` names the tool whose stored output a `read_tool_result` call reads."""
    if name == "run_code":
        code = args.get("code")
        return code_preview(code) if isinstance(code, str) else "code unavailable"
    if name == "delegate_task":
        return " · ".join(assignment(name, args))
    if name in {"shell", "run_command", "start_command"}:
        command = args.get("command")
        shown = command_preview(command) if isinstance(command, str) else "command unavailable"
        # A stated purpose leads, but never replaces the command: the row has to
        # keep saying what actually ran, not only what it was meant to do.
        purpose = stated_purpose(args)
        return f"{purpose} · {shown}" if purpose else shown
    if name in JOB_HANDLE_TOOLS:
        # The id is all these calls are given: without it the status line says
        # a job is being waited on but not which one. `subject` adds the rest.
        job_id = args.get("job_id")
        return argument(job_id) if isinstance(job_id, str) else "job unavailable"
    if name == "read_tool_result":
        return stored_slice(args, source)
    if name in {"search_sessions", "read_session"}:
        return session_recall(name, args)
    if name == "search_tools":
        queries = args.get("queries")
        if not isinstance(queries, list):
            return "queries unavailable"
        return ", ".join(argument(item) for item in queries if isinstance(item, str))
    if name in {"get_page", "web_fetch", "web_search"}:
        # `web_fetch` is Anthropic's native fetch; the local tools keep Exa's names.
        key = "query" if name == "web_search" else "url"
        value = args.get(key)
        return argument(value) if isinstance(value, str) else f"{key} unavailable"
    if name in {
        "read_file",
        "write_file",
        "edit_file",
        "search_files",
        "find_files",
        "list_files",
        "grep",
        "list_directory",
        "create_directory",
        "file_info",
    }:
        value = args.get("path", ".")
        if not isinstance(value, str):
            return "path unavailable"
        path = Path(value)
        if path.is_absolute():
            try:
                value = str(path.relative_to(Path.cwd()))
            except ValueError:
                pass
        where = plain(value)
        if name in {"search_files", "find_files", "list_files", "grep"} and isinstance(
            args.get("pattern"), str
        ):
            pattern = json.dumps(argument(args["pattern"]), ensure_ascii=False)
            where = f"{pattern} in {where}"
            glob = args.get("glob" if name == "grep" else "include_glob")
            if name in {"search_files", "grep"} and isinstance(glob, str):
                where += f" · glob {json.dumps(argument(glob), ensure_ascii=False)}"
        return where
    return ""


def failure_reason(content: object) -> str:
    """Show actionable tool feedback, not a lossy classification of the error."""
    if isinstance(content, list):
        # Pydantic validation errors include raw inputs; retain their locations
        # and messages without dumping those inputs into the transcript.
        text = "\n".join(
            f"{'.'.join(map(str, item.get('loc', ())))}: {item['msg']}".lstrip(": ")
            for item in content
            if isinstance(item, dict) and isinstance(item.get("msg"), str)
        )
    else:
        text = str(content) if content is not None else ""
    return command_text(text).strip() or "No error details returned."


_ANSI = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b\[[0-?]*[ -/]*[@-~]")


def command_error(content: object) -> str:
    """Prefer stderr, or stdout for tools such as pytest; keep a bounded diagnostic tail."""
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        # Validation errors contain an input field: show only their messages.
        text = "\n".join(
            item["msg"]
            for item in content
            if isinstance(item, dict) and isinstance(item.get("msg"), str)
        )
    else:
        return "No error output returned."
    text = re.sub(r"\n?\[exit code: -?\d+\]\s*$", "", text)
    stderr = re.search(r"(?:^|\n)\[stderr\]\n(.*)", text, re.DOTALL)
    if stderr and stderr[1].strip():
        text = stderr[1]
    else:
        text = re.sub(r"(?:^|\n)\[(?:stdout|stderr)\]\n?", "\n", text)
    # Remove escape sequences whole, before `plain` turns their introducer into a space.
    text = strip_controls(_ANSI.sub("", text))
    lines = [plain(line.expandtabs(4), limit=None) for line in text.splitlines()]
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    if not lines or lines == ["(no output)"]:
        return "No error output returned."
    truncated = len(lines) > 200
    excerpt = "\n".join(lines[-200:])
    if len(excerpt) > 32000:
        excerpt = excerpt[-32000:]
        truncated = True
    return ("… earlier error output truncated\n" if truncated else "") + excerpt


def result_detail(
    name: str, args: dict, content: object, outcome: str, source: str = ""
) -> tuple[str, bool]:
    """Extract metrics from known Harness formats, with a safe unknown-format fallback."""
    where = target(name, args, source)
    text = content if isinstance(content, str) else ""
    failed = outcome != "success"
    # Harness 0.31 reports plan validation failures as ordinary string returns.
    # Do not hide these along with successful panel-only updates.
    if name in PLAN_TOOLS and (
        text.startswith(
            (
                "Plan not updated:",
                "No changes applied.",
                "Invalid status ",
                "Cannot ",
                "A step cannot ",
            )
        )
        or text.endswith("not found.")
    ):
        failed = True
    if failed:
        prefix = "Retry requested" if outcome == "retry" else "Failed"
        result = f"{prefix} · {failure_reason(content)}"
    elif text.startswith("[Tool output too large (") and name != "shell":
        # Do not count the preview's lines/matches as if it were the full result.
        result = "Output stored · preview in context"
    elif name == "read_tool_result":
        # Harness heads every slice with `[handle '…': N matching line(s); showing M]`.
        match = re.match(
            r"\[handle .*?: ([\d,]+) matching line\(s\); showing (\d+)(, output capped)?\]", text
        )
        if match:
            total, shown = int(match[1].replace(",", "")), int(match[2])
            noun = "line" if total == 1 else "lines"
            result = f"{total:,} {noun}" if shown == total else f"{shown:,} of {total:,} {noun}"
            if match[3]:
                result += " · output capped"
        elif text.startswith("[No stored tool result"):
            result, failed = "No stored result", True
        else:
            result = "Read finished"
    elif name == "delegate_task":
        # A normal return without a lifecycle end can be a rejected delegation
        # (e.g. max_calls exhausted), not proof the child completed.
        result = "Returned"
    elif name == "read_file":
        numbers = re.findall(r"^\s*(\d+)\t", text, re.MULTILINE)
        result = (
            f"lines {numbers[0]}–{numbers[-1]} · {len(numbers)} lines"
            if numbers
            else "Read finished"
        )
        if re.search(r"^\.\.\. \(\d+ more lines\. Use offset=", text, re.MULTILINE):
            result += " · truncated"
        elif text.endswith("(empty file)\n"):
            result = "0 lines · empty file"
    elif name in {"search_files", "grep"}:
        matches = re.findall(r"^(.+?):\d+:", text, re.MULTILINE)
        result = (
            f"{len(matches)} matches in {len(set(matches))} files"
            if matches
            else ("No matches" if text.strip() == "No matches found." else "Search finished")
        )
    elif name == "list_directory":
        entries = text.splitlines()
        files = sum(bool(re.search(r"\(\d+ bytes\)$", line)) for line in entries)
        directories = sum(line.endswith("/") for line in entries)
        result = (
            f"{files} files · {directories} directories"
            if files or directories
            else "Listing finished"
        )
        if text == "(empty directory)":
            result = "0 files · 0 directories"
    elif name in {"find_files", "list_files"}:
        if text.strip() == "No matches found.":
            result = "No matches"
        else:
            count = sum(
                bool(line) and not line.startswith("[... truncated") for line in text.splitlines()
            )
            result = f"{count} entries found"
    elif name == "write_file":
        match = re.match(r"Wrote (\d+) chars \((\d+) lines\)", text)
        result = f"{match[2]} lines written · {match[1]} chars" if match else "Write finished"
    elif name == "edit_file":
        old, new = args.get("old_text"), args.get("new_text")
        result = (
            f"+{len(new.splitlines())} −{len(old.splitlines())} replacement lines"
            if (isinstance(old, str) and isinstance(new, str))
            else "Edit finished"
        )
    elif name in JOB_TOOLS:
        # Job tools close with a `[jN · outcome · elapsed]` marker. Live `shell`
        # calls use CommandFinishedEvent as the authoritative status instead.
        result, failed = job_status(text)
        # The summary opens with the job id, so the target would only repeat it.
        if result.startswith(f"{where} "):
            where = ""
    elif name in {"run_command", "start_command", "check_command", "stop_command"}:
        # Foreground nonzero exits and completed background processes carry this marker.
        match = re.search(r"\[exit code: (-?\d+)\]\s*$", text)
        if text.startswith("[Error:"):
            failed = True
            result = failure_reason(text)
        elif re.fullmatch(r"\[Command timed out after [\d.]+s\]", text):
            failed = True
            result = "Timed out"
        elif match:
            code = int(match[1])
            failed = code != 0
            result = f"exit {code}" if failed else ""
            if code == 127:
                result += " · Executable not found"
        elif text == "(no output)" or text.startswith(("[stdout]\n", "[stderr]\n")):
            result = ""
        else:
            result = ""  # No redundant success text for unknown formats.
    elif name == "write_plan":
        items = args.get("items")
        if isinstance(items, list) and all(isinstance(item, dict) for item in items):
            done = sum(item.get("status") == "completed" for item in items)
            active = next(
                (i + 1 for i, item in enumerate(items) if item.get("status") == "in_progress"), None
            )
            result = f"{len(items)} steps · {done} complete"
            if active:
                result += f" · working on step {active}"
        else:
            result = "Plan updated"
    elif name == "inventory_agent_context":
        try:
            data = json.loads(text) if text else content
            roots = data.get("roots") if isinstance(data, dict) else None
            if not isinstance(roots, list) or not all(isinstance(root, dict) for root in roots):
                raise ValueError
            count = sum(bool(root.get("exists")) for root in roots)
            result = (
                f"{count} assistant configuration directories"
                if count
                else "No assistant configuration directories found"
            )
        except ValueError, TypeError:
            result = "Assistant configuration inspected"
    else:
        result = ""
    if name in {"search_files", "find_files", "list_directory", "list_files", "grep"} and re.search(
        r"^\[\.\.\. truncated at \d+ (?:entries|matches|files|lines)\]$", text, re.MULTILINE
    ):
        result += " · truncated"
    return (f"{where} → {result}" if where and result else where or result), failed


# Provider of the `claude:` CLI's own WebSearch/WebFetch (`claude_sdk/web.py`).
# Both run inside Claude Code, and its fetch hands back a model's answer
# about the page rather than the page, so their results say where they came from.
CLAUDE_CODE_PROVIDER = "claude"


def _url_hits(content: list) -> list[dict]:
    """Search hits proper: the CLI can record its summary on a hit with no URL."""
    return [item for item in content if isinstance(item, dict) and item.get("url")]


def native_result_detail(
    name: str, args: dict, content: object, outcome: str, provider: str = ""
) -> tuple[str, bool]:
    """Summarize a provider-executed tool (native web search or fetch) from its return part.

    Providers return structured content rather than Harness text: Anthropic's
    search yields a list of result blocks or an error block, so count what can
    be counted and otherwise only report success or failure.
    """
    where = target(name, args)
    via = " · via Claude Code" if provider == CLAUDE_CODE_PROVIDER else ""
    error = isinstance(content, dict) and str(content.get("type", "")).endswith("error")
    if outcome != "success" or error:
        reason = ""
        if isinstance(content, dict):
            reason = str(content.get("message") or content.get("error_code") or "").strip()
            reason = reason.splitlines()[0][:200] if reason else ""
        return f"{where} → Failed" + (f" · {plain(reason)}" if reason else "") + via, True
    if isinstance(content, list):
        count = len(_url_hits(content))
        return f"{where} → {count} result{'s' if count != 1 else ''}{via}", False
    return where + via, False


def native_result_projection(content: object, provider: str = "") -> object:
    """What `/tools` shows for a provider-executed web tool: its readable text.

    Anthropic's native web search returns each hit with an `encrypted_content`
    blob that only Anthropic can decrypt (it is replayed to the model on later
    requests, never readable client-side). Listing title, URL and age is the
    whole readable payload; the blob would otherwise bury it in base64. A fetch
    is its page text, and a failure its message; anything else is left as is.
    """
    claude_code = provider == CLAUDE_CODE_PROVIDER
    if isinstance(content, dict):
        if str(content.get("type", "")).endswith("error"):
            return str(content.get("message") or content.get("error_code") or "Failed.")
        document = content.get("content")
        source = document.get("source") if isinstance(document, dict) else None
        # A native fetch of a PDF carries base64, which is no use to read here.
        if (
            content.get("type") != "web_fetch_result"
            or not isinstance(source, dict)
            or source.get("type") != "text"
        ):
            return content
        header = [str(content.get("url") or "(no url)")]
        if document.get("title"):
            header.insert(0, str(document["title"]))
        if claude_code:
            header.append("Claude Code's answer about the page, not the page text")
        return "\n".join(header) + "\n\n" + str(source.get("data") or "")
    if not isinstance(content, list) or not all(
        isinstance(item, dict) and item.get("type") == "web_search_result" for item in content
    ):
        return content
    hits = _url_hits(content)
    summary = next((item["summary"] for item in content if item.get("summary")), "")
    count = f"{len(hits)} result{'s' if len(hits) != 1 else ''}" if hits else "No results."
    if any(item.get("encrypted_content") for item in hits):
        count += " (page text is encrypted for the provider; only the model can read it)"
    lines = [count]
    for item in hits:
        lines.append("")
        lines.append(str(item.get("title") or "(untitled)"))
        lines.append(str(item["url"]))
        if item.get("page_age"):
            lines.append(f"Age: {item['page_age']}")
    if summary:
        who = "Claude Code's summary" if claude_code else "Summary"
        lines += ["", f"{who}:", str(summary)]
    return "\n".join(lines)


# Tools that act on a job `shell` started, named by its id, instead of running one.
JOB_HANDLE_TOOLS = frozenset({"wait_for_job", "job_output", "stop_job"})

# Tools whose result carries a job marker, not command output of their own.
JOB_TOOLS = JOB_HANDLE_TOOLS | {"shell"}

# Shell-facing tools whose captured output can be mirrored into scrollback.
COMMAND_TOOLS = frozenset(
    {"shell", "run_command", "start_command", "check_command", "stop_command"}
)

# Tools whose success is already told by a diff, so a summary row would repeat it.
EDIT_TOOLS = frozenset({"write_file", "edit_file"})


def shell_status(exit_code: int | None) -> tuple[str, bool]:
    if exit_code is None:
        return "Running", False
    if exit_code == 0:
        return "", False
    return f"exit {exit_code}" + (" · Executable not found" if exit_code == 127 else ""), True


# `pcode.shell_tools` ends every job result with this marker, so one parser
# covers a finished command, an abandoned wait, and a status lookup alike.
_JOB_MARKER = re.compile(r"\[(j\d+) · (running|stopped|exit (-?\d+)) · [^]]*\]", re.MULTILINE)


# A finished job's marker, alone on the last line. Anything after the bracket
# (a stopped job's label, a still-running job's instructions) means the line
# carries more than a block heading can show, so it is left in place.
_FINISHED_MARKER = re.compile(r"(?:\A|\n)\[(j\d+) · exit -?\d+ · [^]\n]*\]\Z")


def split_outcome(output: str) -> tuple[str, str]:
    """Split off a trailing `[jN · exit C · elapsed]` line, returning the job id.

    A block heading already carries the outcome marker and the elapsed time, so
    the job's name is all that footer adds in the UI.
    """
    marker = _FINISHED_MARKER.search(output)
    if marker is None:
        return output, ""
    return output[: marker.start()], marker[1]


def job_status(text: str) -> tuple[str, bool]:
    """Summarize a job result: its id, and what became of the command."""
    match = None
    for match in _JOB_MARKER.finditer(text):
        pass  # The last marker is this call's own outcome.
    if match is None:
        return "", False
    identity, outcome, code = match[1], match[2], match[3]
    if outcome == "running":
        return f"{identity} · still running", False
    if outcome == "stopped":
        return f"{identity} · stopped", False
    detail, failed = shell_status(int(code))
    return f"{identity} · {detail}" if detail else "", failed


# Successful planning calls update the pinned panel; failures remain in scrollback.
PLAN_TOOLS = frozenset(
    {
        "write_plan",
        "read_plan",
        "add_task",
        "update_task_status",
        "update_task_statuses",
        "remove_task",
        "add_subtask",
        "set_dependency",
        "get_available_tasks",
    }
)


def delegation_detail(args: dict, outcome: str) -> tuple[str, bool]:
    state = {
        "ok": "Completed",
        "timeout": "Timed out",
        "budget": "Usage budget exhausted",
        "failed": "Failed",
        "contained": "Child failed",
    }.get(outcome, "Did not complete")
    return f"{target('delegate_task', args)} → {state}", outcome != "ok"
