"""Durable, append-only reminders that keep a provider's cached prefix intact."""

from pydantic_ai.messages import ModelRequest, UserPromptPart


def last_reminder(messages, tag: str) -> str | None:
    """Return the most recent reminder text for `tag`, or None when none was sent.

    Detection is by content, not message metadata: Pydantic AI merges consecutive
    `ModelRequest`s when history is resumed and keeps only its own reserved
    metadata namespace, so a marker stored there survives a run but not a resume.
    Only a `UserPromptPart` that *starts* with the tag counts, so a tool result or
    quoted prompt that merely mentions it is not mistaken for a sent reminder.
    """
    for message in reversed(messages):
        if not isinstance(message, ModelRequest):
            continue
        for part in reversed(message.parts):
            content = getattr(part, "content", None)
            if isinstance(part, UserPromptPart) and isinstance(content, str):
                if content.startswith(tag):
                    return content
    return None


def append_reminder(ctx, request_context, tag: str, text: str) -> None:
    """Append `text` as durable history unless the last reminder already says it.

    Appending, rather than moving a mutable tail, keeps previously sent messages
    byte-identical so the provider's cached prefix stays reusable as the
    conversation grows.
    """
    messages = request_context.messages
    if not messages or not isinstance(messages[-1], ModelRequest):
        return
    previous = last_reminder(messages, tag)
    if previous == text:
        return
    # Stamped here: the framework fills these in only after every before-hook
    # has run, which is after the request checkpoint has copied the request.
    reminder = ModelRequest(
        parts=[UserPromptPart(content=text)],
        run_id=ctx.run_id,
        conversation_id=ctx.conversation_id,
    )
    # Both lists: the request is what gets sent, `ctx.messages` the run's history.
    request_context.messages = [*messages, reminder]
    ctx.messages.append(reminder)
