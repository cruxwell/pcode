"""The advisor: a stronger model the session's model may consult mid-turn.

`advisor_model` is the saved default and `/advisor` changes it until pcode
exits. Each turn resolves the choice afresh (`DynamicCapability`), so a change
needs no agent rebuild and reaches the next turn.

Two paths, as Harness's `Advisor` offers them:

- Native, when the executor is a Claude model on the API key route and its
  profile carries Anthropic's advisor tool. The advisor runs inside the
  executor's own request and reads the whole transcript; with the newest
  advisors its advice comes back encrypted, so it is never shown.
- Local otherwise (Claude Code logins, other providers): an `advisor` function
  tool runs a separate request on the advisor model, resolved on pcode's own
  logins as /btw resolves one. It sees only the prompt the executor writes.

A side question runs on the same agent, so it gets the same tools (and keeps
the turn's prompt cache); `AsideGuard` refuses the local tool there.
"""

import logging
from dataclasses import dataclass, field

# Pydantic AI is imported where used: the controller holds a choice from launch.
from pcode.auth import FABLE_5_2_MODELS
from pcode.preferences import ANTHROPIC_PROVIDERS, load_preferences

log = logging.getLogger(__name__)

SETTING = "advisor_model"
TOOL = "advisor"

GUIDANCE = (
    "You have an `advisor` tool that consults a stronger model ({name}). Call it before "
    "committing to an approach on non-trivial or high-impact work, when you are stuck after "
    "repeated failed attempts, and before declaring complex work done. Skip it for routine steps."
)


def saved_advisor() -> str | None:
    """The `advisor_model` default, or None when unset."""
    return (load_preferences().get(SETTING) or "").strip() or None


@dataclass
class AdvisorChoice:
    """The session's advisor model name, or None for no advisor."""

    name: str | None = field(default_factory=saved_advisor)
    # Native advisors by (name, model ID). They hold no credentials, unlike a local
    # one, which is resolved each turn so a /login or /logout reaches it.
    _native: dict = field(default_factory=dict, repr=False)


def native_id(name: str) -> str | None:
    """The Anthropic model ID of a Claude advisor name, whichever login names it."""
    provider, _, model = name.partition(":")
    return model if provider in ANTHROPIC_PROVIDERS and model else None


def native_pairing(name: str, executor) -> str | None:
    """The advisor's model ID when `executor` can run it natively, else None.

    Anthropic validates the pairing itself and fails the request when it is
    wrong; the one rule known here is that a Fable/Mythos 5.2 executor takes
    only a 5.2 advisor, and anything else is sent through the local path.
    """
    from pydantic_ai.native_tools import AdvisorTool

    model_id = native_id(name)
    if model_id is None or getattr(executor, "canonical_system", None) != "anthropic":
        return None
    if AdvisorTool not in executor.profile.get("supported_native_tools", ()):
        return None
    if FABLE_5_2_MODELS.match(executor.model_name) and not FABLE_5_2_MODELS.match(model_id):
        return None
    return model_id


def same_model(name: str, executor) -> bool:
    """Whether `name` is the executor itself, across Claude's two logins (best effort)."""
    if executor.system in ANTHROPIC_PROVIDERS and native_id(name) is not None:
        return native_id(name) == executor.model_name
    return name == f"{executor.system}:{executor.model_name}"


def _local_model(name: str):
    """`name` on pcode's own logins, with the settings it would have as a /btw model."""
    from pydantic_ai.models.wrapper import WrapperModel
    from pydantic_ai.settings import merge_model_settings

    from pcode.agent import side_model

    chosen = side_model(name)
    settings = dict(chosen.settings or {})
    if getattr(chosen.model, "system", None) == "claude":
        from pcode.claude_sdk.model import KEEP_WARM_SETTING

        # A process of its own, stopped once it answers, so it cannot evict
        # the conversation's warm one.
        settings[KEEP_WARM_SETTING] = False

    class Configured(WrapperModel):
        @property
        def settings(self):
            return merge_model_settings(self.wrapped.settings, settings or None)

    return Configured(chosen.model)


def _build(name: str, model_id: str | None):
    from pydantic_ai.capabilities import AbstractCapability, CombinedCapability
    from pydantic_ai_harness.advisor import Advisor

    class Guidance(AbstractCapability):
        def get_instructions(self):
            return GUIDANCE.format(name=name)

    if model_id is not None:
        advisor = Advisor(f"anthropic:{model_id}", mode="native")
    else:
        advisor = Advisor(_local_model(name), mode="local")
    return CombinedCapability([advisor, Guidance()])


def advisor_capability(choice: AdvisorChoice):
    """The main agent's advisor, resolved from `choice` once per turn."""
    from pydantic_ai.capabilities import DynamicCapability

    def resolve(ctx):
        name = choice.name
        executor = ctx.model
        if not name or same_model(name, executor):
            return None
        model_id = native_pairing(name, executor)
        if model_id is None:
            try:
                return _build(name, None)
            except ValueError as error:
                # Checked when chosen; a login lost since must not fail the turn.
                log.warning("Advisor %s unavailable: %s", name, error)
                return None
        key = (name, model_id)
        if key not in choice._native:
            choice._native[key] = _build(name, model_id)
        return choice._native[key]

    return DynamicCapability(resolve)
