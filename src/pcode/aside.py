"""Side questions asked beside a running turn; no terminal imports.

A side question ("btw") reuses the conversation's context but is never part of
it: once settled it is journaled as a branch of the conversation tree that never
becomes the active one (see `conversation_tree.side_record`), and it cannot be
steered or resent. It runs on the
conversation's own agent, with the same instructions and tool definitions, so
its requests reuse the provider cache the conversation has already paid for.
That is also why its framing travels in the question rather than the system
prompt: anything added ahead of the history would miss the cache.
"""

import asyncio
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from time import monotonic
from uuid import uuid4

from pcode.error_report import error_message
from pcode.preferences import EFFORTS

# One side question is a question, not a second conversation: it gets a small
# request budget and a hard deadline so a confused run cannot spend a session's
# worth of tokens in the background where nobody is watching it.
ASIDE_REQUEST_LIMIT = 12
ASIDE_TIMEOUT_SECONDS = 300
# Side questions accumulate over a long session; keep the recent threads
# readable rather than growing the viewer without bound.
ASIDE_HISTORY = 20
# `/btw $a $b QUESTION` fans out one side question per model. Each one sends
# the whole conversation, uncached on any model but the conversation's own.
ASIDE_MODEL_LIMIT = 4
MODEL_MARK = "$"
# `+high` asks on the conversation's model at that effort; `$model+high` on
# another model. The levels are the ones /effort accepts.
EFFORT_MARK = "+"
# How every provider prefix is spelled (`openai-codex`, `google-gla`).
PROVIDER_NAME = re.compile(r"[a-z][a-z0-9_-]*")
ASIDE_MODEL_USAGE = (
    f"Usage: /btw [$PROVIDER:MODEL[+EFFORT] | +EFFORT ...] QUESTION (EFFORT: {'|'.join(EFFORTS)})"
)

ASIDE_FRAMING = (
    "[Side question] The user is asking a side question about the conversation so far. "
    "Answer only this question, briefly, preferring what the conversation already shows "
    "over fresh investigation. Your answer appears in a popup beside the conversation "
    "and is not added to it, so do not address the main agent, continue its task, "
    "or promise work. Tools work normally, but the plan and delegation tools are "
    "unavailable here."
)


# The first question's framing is already in the history a follow-up re-sends,
# so this only has to say the same rules still hold.
FOLLOW_UP_FRAMING = (
    "[Side question follow-up] The user is following up on their side question above. "
    "The same rules apply: answer only this, briefly; it is not added to the conversation."
)


def framed(question: str) -> str:
    """The user message a side question is sent as."""
    return f"{ASIDE_FRAMING}\n\nQuestion: {question}"


def framed_follow_up(question: str) -> str:
    """The user message a follow-up to a side answer is sent as."""
    return f"{FOLLOW_UP_FRAMING}\n\nQuestion: {question}"


# Asked in the thread, where its questions and answers are, so the summary
# reuses the thread's cache; `summary_request` is what the conversation records.
SUMMARY_FRAMING = (
    "[Side thread summary] Summarize this side discussion for the main conversation, "
    "which never saw it: what was asked, what was found, and anything that should change "
    "the ongoing work. Be brief and concrete, with no preamble, and do not use tools."
)


def framed_summary(instructions: str) -> str:
    """The user message asking a thread to summarize itself, with the user's focus if any."""
    return SUMMARY_FRAMING + (f"\n\nFocus: {instructions}" if instructions else "")


def summary_request(questions: list[str], instructions: str = "") -> str:
    """The user message a thread's summary is recorded under in the conversation.

    The conversation never saw the thread, so this names its questions rather
    than pointing at a discussion "above" that is not there.
    """
    lines = [
        "[Side thread summary] Beside this conversation I asked side questions it did not see:"
    ]
    lines += [f"- {' '.join(question.split())}" for question in questions]
    lines.append("Summarize what came of them for the rest of this conversation.")
    if instructions:
        lines.append(f"Focus: {instructions}")
    return "\n".join(lines)


@dataclass(frozen=True)
class Bridge:
    """A request to bring a side thread into the conversation.

    `merge` adds the thread to the conversation tree as it is; `summary` adds a
    summary of it to the active branch, focused by `instructions` when given.
    """

    thread: str
    action: str
    instructions: str = ""


@dataclass(frozen=True)
class SideReply:
    """A side answer, and what a follow-up needs to continue from it.

    `messages` is the run's whole history, conversation context included, so a
    follow-up re-sends exactly the prefix this run just cached. `agent`,
    `model` and `settings` are what it ran with, opaque here: a follow-up asks
    the same model the same way even after /model or /effort changed the
    conversation's.
    """

    answer: str
    messages: list = field(default_factory=list, repr=False)
    conversation_id: str = ""
    agent: object = field(default=None, repr=False)
    model: object = None
    settings: object = None


@dataclass(frozen=True)
class SideTarget:
    """Where one side question runs: a model and the effort to ask it at.

    An empty `model` is the conversation's own; an empty `effort` keeps the
    effort that model already gets.
    """

    model: str = ""
    effort: str = ""


def split_effort(word: str) -> tuple[str, str]:
    """Split `model+high` into the model and its effort.

    Model ids can hold `:` and `/`, so only the part after the last `+` is
    considered, and only a real level counts: anything else stays in the name.
    """
    name, mark, effort = word.rpartition(EFFORT_MARK)
    if mark and effort in EFFORTS:
        return name, effort
    return word, ""


def _target(word: str) -> SideTarget:
    if word.startswith(EFFORT_MARK):
        effort = word.removeprefix(EFFORT_MARK)
        if effort not in EFFORTS:
            raise ValueError(f"Unknown effort `{word}`. {ASIDE_MODEL_USAGE}")
        return SideTarget(effort=effort)
    model, effort = split_effort(word.removeprefix(MODEL_MARK))
    if not model:
        raise ValueError(f"A model name must follow `{MODEL_MARK}`. {ASIDE_MODEL_USAGE}")
    return SideTarget(model, effort)


def parse_models(argument: str) -> tuple[list[SideTarget], str]:
    """Split `$model[+effort] [+effort ...] question` into targets and the question.

    Only leading `$` and `+` words name targets, so either one inside the
    question is text. Repeated targets collapse to one, in the order first
    given; the same model at two efforts is two targets.
    """
    models: list[SideTarget] = []
    rest = argument.strip()
    while rest.startswith((MODEL_MARK, EFFORT_MARK)):
        word, *tail = rest.split(maxsplit=1)
        rest = tail[0] if tail else ""
        target = _target(word)
        if target not in models:
            models.append(target)
    if models and not rest:
        raise ValueError(f"No question after the model. {ASIDE_MODEL_USAGE}")
    if len(models) > ASIDE_MODEL_LIMIT:
        raise ValueError(
            f"At most {ASIDE_MODEL_LIMIT} models per side question; got {len(models)}."
        )
    return models, rest


def prompt_target(text: str) -> tuple[SideTarget | None, str]:
    """Split a leading `$PROVIDER:MODEL[+EFFORT]` or `+EFFORT` word off a prompt.

    A prompt is free text, so unlike `/btw` nothing here is refused: a word
    only names a target when it reads as one unmistakably, a model with its
    provider (`$openai:gpt-5`, never `$HOME` or `$PATH:/usr/bin`, whose
    uppercase "provider" no provider is named like) or a real effort level
    (`+high`, never `+1`), and anything else leaves the prompt as typed. One
    word only, since one prompt runs one turn.
    """
    word, *tail = text.lstrip().split(maxsplit=1) or [""]
    rest = tail[0] if tail else ""
    if word.startswith(EFFORT_MARK):
        effort = word.removeprefix(EFFORT_MARK)
        return (SideTarget(effort=effort), rest) if effort in EFFORTS else (None, text)
    if word.startswith(MODEL_MARK):
        model, effort = split_effort(word.removeprefix(MODEL_MARK))
        provider, colon, name = model.partition(":")
        if colon and PROVIDER_NAME.fullmatch(provider) and name:
            return SideTarget(model, effort), rest
    return None, text


def _leading_word(argument: str) -> str | None:
    """The word being typed, while every word so far is a `$` or `+` target."""
    if not argument or argument[-1].isspace():
        return None
    words = argument.split()
    if not all(word.startswith((MODEL_MARK, EFFORT_MARK)) for word in words):
        return None
    return words[-1]


def model_fragment(argument: str) -> str | None:
    """The partial model name being typed in `/btw` arguments, if any.

    Only a word in the leading run of `$` and `+` words completes as a model;
    once the question has started, a `$` is ordinary text. A word already
    carrying `+` is choosing an effort instead; see `effort_fragment`.
    """
    word = _leading_word(argument)
    if word is None or not word.startswith(MODEL_MARK) or EFFORT_MARK in word:
        return None
    return word.removeprefix(MODEL_MARK)


def effort_fragment(argument: str) -> str | None:
    """The partial effort being typed after `+` in a leading `/btw` word, if any."""
    word = _leading_word(argument)
    if word is None or EFFORT_MARK not in word:
        return None
    return word.rpartition(EFFORT_MARK)[2]


def model_labels(models: list[SideTarget]) -> dict[SideTarget, str]:
    """Short labels: the model without its provider, unless two would collide.

    An effort joins the label, so answers at different efforts stay apart; the
    conversation's own model shows the effort alone.
    """
    names = list(dict.fromkeys(target.model for target in models if target.model))
    short = {name: name.partition(":")[2] or name for name in names}
    counts: dict[str, int] = {}
    for label in short.values():
        counts[label] = counts.get(label, 0) + 1
    labels = {}
    for target in models:
        label = short.get(target.model, "")
        if label and counts[label] > 1:
            label = target.model
        labels[target] = " \u00b7 ".join(part for part in (label, target.effort) if part)
    return labels


def exchanges(thread: list["Aside"], messages: list) -> list[tuple["Aside", int]]:
    """The thread's questions that `messages` answered, each with where its answer ends.

    `messages` is the history the thread's newest answer ran with, so it holds
    every question the chain answered; a failed or stopped follow-up never
    joined it and is skipped. A question starts at the request carrying its
    framed text, and its answer runs to the next one's start, or to the end.
    """
    from pydantic_ai.messages import ModelRequest, UserPromptPart

    def asks(message, text: str) -> bool:
        return isinstance(message, ModelRequest) and any(
            isinstance(part, UserPromptPart) and part.content == text for part in message.parts
        )

    starts: list[tuple[Aside, int]] = []
    index = 0
    for number, aside in enumerate(thread):
        text = framed_follow_up(aside.question) if number else framed(aside.question)
        found = next((i for i in range(index, len(messages)) if asks(messages[i], text)), None)
        if found is not None:
            starts.append((aside, found))
            index = found + 1
    ends = [start for _, start in starts[1:]] + [len(messages)]
    return [(aside, end) for (aside, _), end in zip(starts, ends)]


def settled_context(messages: list) -> list:
    """The longest prefix of `messages` whose tool calls all have results.

    The newest context available while a turn runs is the request in flight,
    which usually ends in tool calls that have not returned yet. Providers
    reject a history with an unanswered call, so a side question is asked
    against the last point where the conversation was balanced.

    The provider stack is imported here, not at module import: the terminal
    reaches its first frame without loading it. See `dev/dependencies.md`.
    """
    from pydantic_ai.messages import (
        NativeToolCallPart,
        NativeToolReturnPart,
        RetryPromptPart,
        ToolCallPart,
        ToolReturnPart,
    )

    calls = (ToolCallPart, NativeToolCallPart)
    results = (ToolReturnPart, NativeToolReturnPart, RetryPromptPart)
    outstanding: set[str] = set()
    balanced = 0
    for index, message in enumerate(messages, start=1):
        for part in message.parts:
            if isinstance(part, calls):
                outstanding.add(part.tool_call_id)
            elif isinstance(part, results):
                outstanding.discard(part.tool_call_id)
        if not outstanding:
            balanced = index
    return list(messages[:balanced])


@dataclass
class Aside:
    """One side question and whatever of its answer has arrived."""

    question: str
    id: str = field(default_factory=lambda: uuid4().hex[:8])
    # The model named with `/btw $MODEL`, and its short display form. Empty when
    # the question was asked without one, on the conversation's own model.
    model: str = ""
    label: str = ""
    # The effort named with `+EFFORT`; empty when the model's own applies.
    effort: str = ""
    # running → answered / failed / cancelled / timed out.
    status: str = "running"
    answer: str = ""
    activity: str = "Waiting for model…"
    error: str = ""
    started: float = field(default_factory=monotonic)
    finished: float | None = None
    # Whether the answer has been opened in the viewer since it settled.
    read: bool = False
    # The id of the thread's first question; a follow-up shares it.
    thread: str = ""
    # Where a thread's first question was asked: the conversation, and its tree
    # node active then. Merging forks the thread from that node.
    conversation: str = ""
    base: str | None = None
    # The tree node this question continues: `base` for a thread's first, the
    # answer it follows up for the rest. Its own node, once settled, is `id`.
    parent: str | None = None
    # Whether it is in the conversation tree, journaled when it settled: a
    # branch off `parent` that outlives this process (see `side_record`).
    recorded: bool = False
    # The provider conversation id its run carried, for a follow-up's cache.
    session_id: str = ""
    # How the thread was brought into the conversation, e.g. "merged".
    bridged: str = ""
    # What a follow-up continues from, once answered. Only a thread's newest
    # answer keeps one: each holds a copy of the conversation.
    reply: SideReply | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        self.thread = self.thread or self.id

    @property
    def running(self) -> bool:
        return self.status == "running"

    @property
    def continuable(self) -> bool:
        """Whether a follow-up can continue from this answer.

        From its reply while this process holds one, else from its node: a
        thread read back from the journal continues from the saved history.
        """
        return self.reply is not None or (self.recorded and self.status == "answered")

    @property
    def elapsed(self) -> float:
        return (self.finished if self.finished is not None else monotonic()) - self.started

    def settle(self, status: str, *, error: str = "") -> None:
        self.status = status
        self.error = error
        self.activity = ""
        self.finished = monotonic()

    def state(self) -> str:
        """Short status for the viewer list and footer, e.g. `answered 12s`."""
        return f"{self.status} {self.elapsed:.0f}s"


class Asides:
    """Every side question this session asked, oldest first.

    A follow-up asked in the viewer is a side question of its own, sharing its
    thread with the question it follows; the viewer shows each thread as one
    conversation.
    """

    def __init__(self) -> None:
        self.items: list[Aside] = []
        self._tasks: dict[str, asyncio.Task] = {}
        # Set by the terminal to repaint while an answer streams and to announce
        # one that settled. Off-terminal callers (tests, `--print`) need neither.
        self.on_update: Callable[[Aside], None] = lambda aside: None
        self.on_settle: Callable[[Aside], None] = lambda aside: None
        # Given the exception of a question that failed or timed out, so the
        # frames can be kept where the session keeps turn failures.
        self.on_failure: Callable[[Aside, BaseException], None] = lambda aside, error: None

    @property
    def running(self) -> int:
        return sum(aside.running for aside in self.items)

    @property
    def unread(self) -> int:
        return sum(not aside.running and not aside.read for aside in self.items)

    def latest(self) -> Aside | None:
        """The answer a bare `/btw` should open: newest unread, else newest."""
        unread = [aside for aside in self.items if not aside.running and not aside.read]
        return (unread or self.items or [None])[-1]

    def threads(self) -> list[list[Aside]]:
        """Each thread's questions in order, threads in the order they began."""
        threads: dict[str, list[Aside]] = {}
        for aside in self.items:
            threads.setdefault(aside.thread, []).append(aside)
        return list(threads.values())

    def thread(self, thread: str) -> list[Aside]:
        return [aside for aside in self.items if aside.thread == thread]

    def follows(self, thread: str) -> Aside:
        """The answer a follow-up in `thread` continues from.

        Raises `ValueError` saying why when there is none to continue: the
        newest question is still running, or no answer in it arrived.
        """
        asked = self.thread(thread)
        if not asked:
            raise ValueError("That side question is gone; ask again with /btw")
        if asked[-1].running:
            raise ValueError("Wait for this answer first")
        for aside in reversed(asked):
            if aside.continuable:
                return aside
        raise ValueError("No answer to follow up on; ask again with /btw")

    def start(
        self,
        question: str,
        work: Callable[[Aside], Awaitable[SideReply | None]],
        *,
        model: str = "",
        label: str = "",
        effort: str = "",
        thread: str = "",
        conversation: str = "",
        base: str | None = None,
        parent: str | None = None,
    ) -> Aside:
        """Register a side question and run `work` for it in the background.

        `work` returns the reply a follow-up would continue from. `thread`
        makes it a follow-up in that thread rather than a new one;
        `conversation` and `base` say where a new one was asked. `parent` is
        the node it continues, which defaults to `base`.
        """
        aside = Aside(
            question=question,
            model=model,
            label=label,
            effort=effort,
            thread=thread,
            conversation=conversation,
            base=base,
            parent=parent if parent is not None else base,
        )
        self.items.append(aside)
        self._trim()
        self._tasks[aside.id] = asyncio.create_task(self._run(aside, work))
        # Shown as asked, before the first word of an answer arrives.
        self.on_update(aside)
        return aside

    def restore(self, asides: list[Aside]) -> list[Aside]:
        """Add threads read back from the journal, ahead of any asked since.

        Questions already here are kept as they are. Returns the ones added.
        """
        known = {aside.id for aside in self.items}
        added = [aside for aside in asides if aside.id not in known]
        # In place: the live panel shares this list.
        self.items[:0] = added
        self._trim()
        kept = {aside.id for aside in self.items}
        return [aside for aside in added if aside.id in kept]

    def _trim(self) -> None:
        """Drop the oldest settled threads beyond `ASIDE_HISTORY`, whole.

        A running question owns a live task, so its thread stays; a thread
        loses no single question, or its conversation would read wrong.
        """
        threads = self.threads()
        excess = len(threads) - ASIDE_HISTORY
        for asked in threads:
            if excess <= 0:
                break
            if not any(aside.running for aside in asked):
                # In place: the live panel shares this list.
                self.items[:] = [aside for aside in self.items if aside.thread != asked[0].thread]
                excess -= 1

    def cancel(self) -> int:
        """Stop every running side question; returns how many were stopped."""
        tasks = [task for task in self._tasks.values() if not task.done()]
        for task in tasks:
            if not task.cancelling():
                task.cancel()
        return len(tasks)

    async def close(self) -> None:
        self.cancel()
        tasks = list(self._tasks.values())
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def update(self, aside: Aside, *, answer: str, activity: str) -> None:
        aside.answer = answer
        aside.activity = activity
        self.on_update(aside)

    async def _run(self, aside: Aside, work: Callable[[Aside], Awaitable[None]]) -> None:
        try:
            async with asyncio.timeout(ASIDE_TIMEOUT_SECONDS):
                reply = await work(aside)
        except TimeoutError as error:
            aside.settle("timed out", error=f"No answer within {ASIDE_TIMEOUT_SECONDS}s.")
            self._failed(aside, error)
        except asyncio.CancelledError:
            aside.settle("cancelled")
            raise
        except Exception as error:
            aside.settle("failed", error=error_message(error))
            self._failed(aside, error)
        else:
            aside.settle("answered")
            if reply is not None:
                aside.reply = reply
                # Follow-ups continue from the newest answer; an older one
                # would only hold another copy of the conversation.
                for older in self.thread(aside.thread):
                    if older is not aside:
                        older.reply = None
        finally:
            self._tasks.pop(aside.id, None)
            self.on_settle(aside)

    def _failed(self, aside: Aside, error: BaseException) -> None:
        # Diagnostics must never replace the failure being diagnosed.
        try:
            self.on_failure(aside, error)
        except Exception:
            pass
