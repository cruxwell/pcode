"""Scripted pcode sessions for docs screenshots.

A scene file defines ``TURNS`` (what the fake model answers) and ``STEPS``
(keys to send, text to wait for, shots to take), and optionally
``PREFERENCES``, ``SIZE`` and ``MODEL``. `run.py` starts each scene in a private
tmux server and plays its steps; inside the pane the scene calls `launch()`,
which starts the real `pcode` entry point with the model resolver patched to
return a scripted model. Everything else, tools included, is real and runs
against a throwaway repo.
"""

import asyncio
import json
import os
import re
import sys
from contextlib import asynccontextmanager
from dataclasses import dataclass

from pydantic_ai.messages import ModelRequest, ModelResponse, UserPromptPart
from pydantic_ai.models.function import DeltaThinkingPart, DeltaToolCall, FunctionModel


@dataclass
class Think:
    text: str


@dataclass
class Fail:
    """The provider request fails with `error`, as a real one would mid-stream."""

    error: Exception


class Call:
    """A tool call with the tool's own arguments, e.g. ``Call("shell", command="ls")``."""

    def __init__(self, tool: str, **args):
        self.tool, self.args = tool, args


# A response is a list of parts: `str` (answer text), `Think`, `Call`, or `Fail`. A
# turn is the list of responses, one per model request: pcode sends a new
# request after each round of tool results.


# pcode appends reminders (the plan, MCP notices) as user-prompt parts
# opening with a tag such as `<plan-reminder>`. They are not the user's prompt.
REMINDER = re.compile(r"\s*<[a-z-]+>")


def _latest_prompt(messages) -> tuple[str, int]:
    """The last user prompt, and how many model responses followed it."""
    responses = 0
    for message in reversed(messages):
        if isinstance(message, ModelResponse):
            responses += 1
        elif isinstance(message, ModelRequest):
            for part in message.parts:
                if isinstance(part, UserPromptPart):
                    content = part.content
                    text = content if isinstance(content, str) else " ".join(map(str, content))
                    if not REMINDER.match(text):
                        return text, responses
    return "", responses


class ScriptedModel(FunctionModel):
    """A FunctionModel that reports a realistic prompt size.

    FunctionModel estimates usage from message text alone, so the status line
    would claim a few hundred tokens for a session that really starts with
    pcode's ~20k-token system prompt and tool definitions.
    """

    base_tokens = 21_000

    @asynccontextmanager
    async def request_stream(self, *args, **kwargs):
        async with super().request_stream(*args, **kwargs) as response:
            response._usage.input_tokens += self.base_tokens
            yield response


def scripted_model(
    turns: dict[str, list[list]], *, profile=None, delay: float = 0.01
) -> FunctionModel:
    """Answer a prompt containing a ``turns`` key with that key's responses.

    Keys match by substring, so they only need to be distinctive. An unmatched
    prompt, or a turn that has run out of responses, gets "Done.".
    """
    call_ids = iter(range(1, 1_000_000))

    async def stream(messages, info):
        prompt, index = _latest_prompt(messages)
        responses = next((r for key, r in turns.items() if key in prompt), [])
        parts = responses[index] if index < len(responses) else ["Done."]
        for position, part in enumerate(parts):
            await asyncio.sleep(delay)
            if isinstance(part, Fail):
                raise part.error
            if isinstance(part, Think):
                yield {position: DeltaThinkingPart(content=part.text)}
            elif isinstance(part, Call):
                call = DeltaToolCall(
                    name=part.tool,
                    json_args=json.dumps(part.args),
                    tool_call_id=f"call_{next(call_ids)}",
                )
                yield {position: call}
            else:
                # Stream prose in small pieces, as a real model would.
                for start in range(0, len(part), 24):
                    yield part[start : start + 24]

    return ScriptedModel(stream_function=stream, model_name="scripted", profile=profile)


def launch(turns: dict[str, list[list]], *, model: str, preferences: dict | None = None):
    """Run the real pcode terminal in this process on a scripted model."""
    import pcode.agent
    from pcode.preferences import save_preferences

    # run.py's own overrides (`--iterm` picks the palette) win over the scene's.
    overrides = json.loads(os.environ.get("SCREENSHOT_PREFERENCES", "{}"))
    preferences = {**(preferences or {}), **overrides}
    if preferences:
        save_preferences(**preferences)
    # The status line reads effort support from the model's profile and the
    # context window from a catalog keyed by the real model name.
    from pydantic_ai.profiles.anthropic import anthropic_model_profile

    os.environ.setdefault("PCODE_CONTEXT_WINDOW", "1000000")
    profile = anthropic_model_profile(model.split(":", 1)[-1])
    fake = scripted_model(turns, profile=profile)
    pcode.agent.resolve_model = lambda name: fake
    from pcode.app import main

    sys.argv = ["pcode", "--model", model, "--no-host", "--no-worktree"]
    main()
