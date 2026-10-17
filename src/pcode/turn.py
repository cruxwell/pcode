"""What one turn owns while it runs, separate from what the session owns."""

from dataclasses import dataclass, field

from pydantic_ai.messages import ModelMessage
from pydantic_ai.usage import RunUsage
from pydantic_ai_harness.planning import InMemoryPlanStore

from pcode.retries import RequestCheckpoint


@dataclass
class TurnContext:
    """The conversation state a single turn reads, sends and writes back.

    This is the state that must never be shared between two turns: each field
    describes one branch at one moment, so a second turn writing into it would
    not race so much as quietly answer with the wrong conversation. Holding it
    in one object means a turn can be handed its own, and the per-run
    capabilities can bind to *that* turn rather than to the runtime.

    The runtime keeps one of these for the active branch and exposes its fields
    under their original names, so callers that ask a runtime for `history` or
    `plan_store` still get the branch they are looking at.
    """

    run_id: str = ""
    # Settled history for this branch: what the next request carries.
    history: list[ModelMessage] = field(default_factory=list)
    # Finished `!command` exchanges riding on the next request; see `record_shell`.
    pending_shell: list[ModelMessage] = field(default_factory=list)
    plan_store: InMemoryPlanStore = field(default_factory=InMemoryPlanStore)
    # Detached delegations killed before they reported (`pcode.background_delegation`).
    abandoned_delegations: list[str] = field(default_factory=list)
    # The last request as actually sent, for retry and for resend safety.
    checkpoint: RequestCheckpoint = field(default_factory=RequestCheckpoint)
    # What the request in flight carries, published for the footer and /status.
    # `None` between turns, when `history` is the answer instead.
    context_history: list[ModelMessage] | None = None
    # Auto-compaction summarizes in its own run, whose usage reaches neither the
    # turn's result nor `after_model_request`; the turn adds it to session totals.
    compaction_usage: RunUsage = field(default_factory=RunUsage)
    # The model the turn's latest request went to, which a mid-turn `/model`
    # changes; a failure is reported against it.
    model: object = None

    def messages(self) -> list[ModelMessage]:
        """What to send: settled history plus anything queued behind it."""
        return self.history + self.pending_shell
