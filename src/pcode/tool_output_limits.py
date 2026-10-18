"""Configure Harness's production-time tool reduction, independently of compaction."""

import os
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

from pydantic_ai.messages import ToolReturn
from pydantic_ai_harness.tool_output_limits import (
    Band,
    LocalFileStore,
    Spill,
    ToolOutputLimits,
    Truncate,
    TruncationStrategy,
    indented_json,
)

from pcode.images import has_files
from pcode.preferences import SETTINGS, load_preferences
from pcode.shell import REDUCED_SHELL_OUTPUT, split_envelope

# Tools that bound and page their own output (result limits, character offsets). A
# spill pages by line instead, and their JSON keeps a turn's text on one line,
# so the spill is unreadable through read_tool_result and only adds a hop.
# Exempt means not truncated either: their own caps (read_session's max_chars,
# search_sessions' limit) are all that bound them.
SELF_PAGING_TOOLS = ("search_sessions", "read_session")


def tool_results_path() -> Path:
    """Stable across workspaces and restarts so saved spill handles remain readable."""
    state = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state")
    return state / "pcode" / "tool-results"


class CodingToolOutputLimits(ToolOutputLimits):
    """Keep a job's control envelope outside the reduction budget.

    The envelope is how the model reaches a command that is still running. It
    is a few short lines and must never be the part that gets spilled to a file.
    """

    def get_instructions(self):
        # Harness pages by line and caps reads at 50k chars, so one very long line
        # cannot be fully retrieved that way. Both agents have unrestricted shell.
        return (
            "Large tool results may be stored with a read_tool_result handle. "
            "Read only the slices needed. If a single long line hits its output cap, "
            f"use shell to read a character range from {tool_results_path()}/<handle>."
        )

    async def after_tool_execute(self, ctx, *, call, tool_def, args, result):
        if has_files(result):
            # Reduction measures serialized text. A file serialized is base64: past
            # the threshold it would be spilled or cut, and the model would get a
            # preview of the encoding instead of the picture. So only the text
            # beside the files is reduced, as one block, and the files ride along.
            value = result.return_value if isinstance(result, ToolReturn) else result
            items = list(value) if isinstance(value, (list, tuple)) else [value]
            text = "".join(item for item in items if isinstance(item, str))
            if not text:
                return result
            reduced = await super().after_tool_execute(
                ctx, call=call, tool_def=tool_def, args=args, result=text
            )
            if reduced == text:
                return result
            value = reduced.return_value if isinstance(reduced, ToolReturn) else reduced
            kept = [value, *(item for item in items if not isinstance(item, str))]
            return replace(result, return_value=kept) if isinstance(result, ToolReturn) else kept
        split = (
            split_envelope(result)
            if isinstance(result, str) and call.tool_name in {"shell", "wait_for_job", "job_output"}
            else None
        )
        if split is None:
            return await super().after_tool_execute(
                ctx, call=call, tool_def=tool_def, args=args, result=result
            )
        output, envelope = split
        reduced = await super().after_tool_execute(
            ctx, call=call, tool_def=tool_def, args=args, result=output
        )
        if reduced == output:
            return result
        value = reduced.return_value if isinstance(reduced, ToolReturn) else reduced
        # The marker tells the reader the body was reduced, not cut by the command.
        value = f"{REDUCED_SHELL_OUTPUT}\n{value}\n{envelope}"
        return replace(reduced, return_value=value) if isinstance(reduced, ToolReturn) else value


def create_tool_output_limits() -> ToolOutputLimits:
    """Snapshot saved settings; all reduction and retrieval behavior stays upstream."""
    preferences = load_preferences()

    def value(suffix: str) -> str:
        key = f"tool_output_{suffix}"
        result = preferences.get(key, SETTINGS[key].default)
        assert result is not None
        return result

    mode = value("mode")
    threshold = int(value("threshold"))
    truncate = Truncate(
        max_chars=int(value("max_chars")),
        strategy=TruncationStrategy(value("strategy")),
    )
    action = (
        Spill(preview_chars=int(value("preview_chars")), then=truncate)
        if mode == "spill"
        else truncate
    )
    retention = int(value("retention_hours"))
    return CodingToolOutputLimits(
        bands=[] if mode == "off" else [Band(over=threshold, action=action)],
        per_tool={name: [] for name in SELF_PAGING_TOOLS},
        store=LocalFileStore(
            base_dir=tool_results_path(),
            cleanup_after=timedelta(hours=retention) if retention else None,
        ),
        # Structured results (e.g. MCP) need real lines for useful read-back paging.
        serializer=indented_json,
        # Keep read_tool_result even when reduction is off or truncate-only:
        # resumed history can still contain handles produced in spill mode.
    )
