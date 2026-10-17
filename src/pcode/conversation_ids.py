"""Conversation ids for runs on a model other than the conversation's.

Per-conversation state is keyed on the conversation id (Harness's cache-bust
marks, for one), so a request from another model under the conversation's id
would be compared against the conversation's own cache. A run on another model
(`/btw $MODEL`, a `$MODEL` prompt) carries an id of its own, derived from the
conversation's so anything that needs the session it belongs to can still find
it.
"""

import re
from uuid import uuid4

KINDS = ("btw", "turn")
_SUFFIX = re.compile(rf"\.(?:{'|'.join(KINDS)})-[0-9a-f]{{8}}$")


def model_conversation(conversation_id: str, kind: str) -> str:
    """A fresh id for one run of `kind` on another model, within `conversation_id`."""
    assert kind in KINDS
    return f"{conversation_id}.{kind}-{uuid4().hex[:8]}"


def base_conversation(conversation_id: str | None) -> str | None:
    """The conversation (and saved session) a possibly per-model id belongs to."""
    if conversation_id is None:
        return None
    return _SUFFIX.sub("", conversation_id)
