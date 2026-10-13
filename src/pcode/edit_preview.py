"""Transient, line-buffered projections of tool arguments; never execute them."""

from copy import deepcopy
from pathlib import Path
from time import monotonic

from pydantic_ai.messages import (
    FunctionToolCallEvent,
    FunctionToolResultEvent,
    PartDeltaEvent,
    PartEndEvent,
    PartStartEvent,
    ToolCallPart,
    ToolCallPartDelta,
)
from pydantic_core import from_json

from pcode.edits import MAX_SOURCE, edit_text
from pcode.runtime import EditPreview


class StreamingEditPreview:
    def __init__(self, root: Path | None = None):
        self.root = (root or Path.cwd()).resolve()
        self.parts = {}
        self.shown = {}
        self.updated = {}
        self.blocked = set()

    def update(self, event):
        clear = []
        if isinstance(event, PartStartEvent):
            if not isinstance(event.part, ToolCallPart):
                return clear
            index = event.index
            if index in self.shown:
                clear.append(EditPreview(f"edit-preview:{index}"))
                self.shown.pop(index, None)
            self.blocked.discard(index)
            self.updated.pop(index, None)
            self.parts[index] = deepcopy(event.part)
        elif isinstance(event, PartDeltaEvent) and isinstance(event.delta, ToolCallPartDelta):
            index = event.index
            if index not in self.parts or index in self.blocked:
                return clear
            self.parts[index] = event.delta.apply(self.parts[index])
        elif isinstance(event, PartEndEvent) and isinstance(event.part, ToolCallPart):
            index = event.index
            if index in self.blocked:
                return clear
            self.parts[index] = deepcopy(event.part)
        elif isinstance(event, (FunctionToolCallEvent, FunctionToolResultEvent)):
            call_id = event.part.tool_call_id
            for index, part in list(self.parts.items()):
                if part.tool_call_id == call_id:
                    self.parts.pop(index)
                    self.updated.pop(index, None)
                    if self.shown.pop(index, None) is not None:
                        clear.append(EditPreview(f"edit-preview:{index}"))
            return clear
        else:
            return clear

        part = self.parts[index]
        if part.tool_name not in ("edit_file", "write_file", "run_code"):
            return clear
        if isinstance(part.args, str) and len(part.args) > MAX_SOURCE * 2:
            self.blocked.add(index)
            self.parts.pop(index)
            if self.shown.pop(index, None) is not None:
                clear.append(EditPreview(f"edit-preview:{index}"))
            return clear
        now = monotonic()
        if isinstance(event, PartDeltaEvent) and now - self.updated.get(index, 0) < 0.05:
            return clear
        self.updated[index] = now
        try:
            if isinstance(part.args, str):
                complete = from_json(part.args, allow_partial=True)
                partial = from_json(part.args, allow_partial="trailing-strings")
            else:
                complete = partial = part.args
        except ValueError:
            return clear
        if not isinstance(complete, dict) or not isinstance(partial, dict):
            return clear
        if part.tool_name == "run_code":
            return clear + self._code_preview(index, partial, complete)
        path = complete.get("path")
        # Never expose content until the complete path is known and checked.
        permitted = isinstance(path, str)
        if permitted:
            try:
                resolved = (self.root / path).resolve()
                permitted = not Path(path).is_absolute() and resolved.is_relative_to(self.root)
            except OSError, ValueError, RuntimeError:
                permitted = False
        if not permitted:
            if self.shown.pop(index, None) is not None:
                clear.append(EditPreview(f"edit-preview:{index}"))
            return clear
        lines = []
        fields = (
            [("content", "+")]
            if part.tool_name == "write_file"
            else [("old_text", "-"), ("new_text", "+")]
        )
        pairs = [(partial, complete)]
        if part.tool_name == "edit_file" and isinstance(partial.get("replacements"), list):
            finished = complete.get("replacements", [])
            if not isinstance(finished, list):
                finished = []
            pairs = [
                (item, finished[i] if i < len(finished) and isinstance(finished[i], dict) else {})
                for i, item in enumerate(partial["replacements"])
                if isinstance(item, dict)
            ]
        for proposed, finished in pairs:
            for field, prefix in fields:
                text = proposed.get(field)
                if not isinstance(text, str) or len(text) > MAX_SOURCE:
                    continue
                # Sanitize before clipping; unfinished strings expose only whole lines.
                text = edit_text(text)
                if field not in finished:
                    text = text[: text.rfind("\n") + 1]
                lines.extend(prefix + line for line in text.splitlines())
        preview = EditPreview(f"edit-preview:{index}", edit_text(path), "\n".join(lines)[-8192:])
        return clear + self._show(index, preview)

    def _code_preview(self, index: int, partial: dict, complete: dict) -> list:
        """Project a sandboxed snippet as it streams, before it is ever executed."""
        code = partial.get("code")
        if not isinstance(code, str) or len(code) > MAX_SOURCE:
            if self.shown.pop(index, None) is not None:
                return [EditPreview(f"edit-preview:{index}")]
            return []
        # Sanitize before clipping; an unfinished string exposes only whole lines.
        text = edit_text(code)
        if "code" not in complete:
            text = text[: text.rfind("\n") + 1]
        if not text:
            # No complete line yet: an empty box would only flash open and shut.
            return []
        return self._show(
            index,
            EditPreview(f"edit-preview:{index}", "run_code", text[-8192:], kind="code"),
        )

    def _show(self, index: int, preview: EditPreview) -> list:
        if preview == self.shown.get(index):
            return []
        self.shown[index] = preview
        return [preview]
