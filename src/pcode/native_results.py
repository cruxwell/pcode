"""Repair a history whose native-tool results the current login cannot read.

Anthropic returns each server-side `web_search` hit as an opaque
`encrypted_content` blob that only the organization it was issued to can
decrypt. Replay is therefore credential-bound: switch to a key from a different
organization (a new `ANTHROPIC_API_KEY`, another `PCODE_CONFIG_DIR`) and the
request comes back as
`400 Invalid encrypted_content in search_result block`. The blobs live in the
message history, so without a repair every later request in that session fails
the same way and the conversation is stranded.

Dropping them is the whole repair. The page text inside a blob is unrecoverable,
but the titles and URLs beside it are plaintext, and the model's own reading of
the search sits in the same response. Thinking signatures cross accounts
untouched (verified against the live endpoint), so they are left alone.
"""

from collections.abc import Iterable

from pydantic_ai.exceptions import ModelHTTPError
from pydantic_ai.messages import (
    ModelMessage,
    ModelResponse,
    ModelResponsePart,
    NativeToolCallPart,
    NativeToolReturnPart,
    TextPart,
)

# Enough to place the search without turning one dead result set into a wall.
MAX_SOURCES = 10


def unreadable_native_results(error: BaseException) -> bool:
    """Whether the provider rejected replayed results it cannot decrypt.

    Deliberately narrow: a 400 naming `encrypted_content` is the provider
    saying this history is unsendable as it stands, which no amount of retrying
    changes. Every other 400 is a request pcode built wrong.
    """
    return (
        isinstance(error, ModelHTTPError)
        and error.status_code == 400
        and "encrypted_content" in str(error.body)
    )


def drop_unreadable_results(messages: Iterable[ModelMessage]) -> int:
    """Replace credential-bound native results with a plain trace, in place.

    Returns how many results were dropped. The part lists are rewritten through
    a slice assignment because the same `ModelResponse` objects are shared by
    the turn's history, the request already checkpointed, and the next
    snapshot: replacing the messages would repair only one of them.
    """
    dropped = 0
    for message in messages:
        if not isinstance(message, ModelResponse):
            continue
        stale = {
            part.tool_call_id
            for part in message.parts
            if isinstance(part, NativeToolReturnPart) and _encrypted(part.content)
        }
        if not stale:
            continue
        queries = {
            part.tool_call_id: _query(part)
            for part in message.parts
            if isinstance(part, NativeToolCallPart) and part.tool_call_id in stale
        }
        kept: list[ModelResponsePart] = []
        for part in message.parts:
            if isinstance(part, NativeToolCallPart) and part.tool_call_id in stale:
                # The call cannot outlive its result: an unpaired server tool
                # use is rejected in its own right.
                continue
            if isinstance(part, NativeToolReturnPart) and part.tool_call_id in stale:
                kept.append(TextPart(content=_note(part, queries.get(part.tool_call_id))))
                dropped += 1
                continue
            kept.append(part)
        message.parts[:] = kept
    return dropped


def _encrypted(content: object) -> bool:
    """Whether anything in a result carries provider-encrypted text."""
    if isinstance(content, dict):
        return "encrypted_content" in content or any(_encrypted(v) for v in content.values())
    if isinstance(content, list):
        return any(_encrypted(item) for item in content)
    return False


def _query(part: NativeToolCallPart) -> str | None:
    try:
        args = part.args_as_dict()
    except Exception:
        return None
    query = args.get("query")
    return query if isinstance(query, str) else None


def _note(part: NativeToolReturnPart, query: str | None) -> str:
    items = part.content if isinstance(part.content, list) else [part.content]
    sources = [
        f"{item.get('title') or item['url']}: {item['url']}"
        for item in items
        if isinstance(item, dict) and item.get("url")
    ]
    searched = f" for {query!r}" if query else ""
    listed = "".join(f"\n- {source}" for source in sources[:MAX_SOURCES])
    found = f" Pages found:{listed}" if listed else ""
    return (
        f"[pcode: the {part.tool_name} results{searched} were encrypted for a different "
        f"account and cannot be replayed, so they were dropped from this conversation. "
        f"Search again if you still need them.{found}]"
    )
