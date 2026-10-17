"""Report prompt-cache reuse drops, from Harness's detector.

The full notice goes to the session journal for diagnosis; the terminal shows
only a short footer label (`footer_label`), not a scrollback line.

A drop is information, not a fault: /compact, a new tool, or an expired
provider cache all shorten what the next request can reuse. The notice says
how much was reused so the cost is visible; it never claims a cause.
"""

import re
import warnings
from dataclasses import dataclass, field

from pydantic_ai import CapabilityEvent
from pydantic_ai_harness.warn_on_cache_busts import (
    CacheBustWarning,
    CacheNotEnabledWarning,
    WarnOnCacheBusts,
)

from pcode.cache_diagnostics import CacheDiagnostics
from pcode.context_usage import compact_tokens
from pcode.tool_display import command_text


@dataclass(kw_only=True)
class CacheBustEvent(CapabilityEvent, namespace="pcode_cache", name="bust"):
    text: str


def notice_text(step: int, read: int, established: int, model: str, *, earlier_turn: bool) -> str:
    """One neutral line: what was reused, against what an earlier request cached."""
    source = "tokens cached in an earlier turn" if earlier_turn else "previously cached tokens"
    suffix = f" ({model})" if model else ""
    return f"Prompt cache: request {step} reused {read:,} of ~{established:,} {source}{suffix}."


_REUSE = re.compile(r"reused ([\d,]+) of ~([\d,]+)")


def footer_label(text: str) -> str:
    """The footer's short form of a notice, e.g. `cache miss 0/166k`.

    The notice itself is kept whole in the session journal for diagnosis; the
    footer only says a drop happened and how large it was.
    """
    found = _REUSE.search(text)
    if found is None:
        return "cache drop"
    read, established = (int(value.replace(",", "")) for value in found.groups())
    kind = "miss" if read == 0 else "drop"
    label = f"cache {kind} {compact_tokens(read)}/{compact_tokens(established)}"
    return f"sub-agent {label}" if text.startswith("Sub-agent:") else label


@dataclass
class CacheBustReporting(WarnOnCacheBusts):
    """Keep Harness's detector, thresholds, latch, and warning filters.

    Upstream keys its marks by `RunContext.conversation_id`, which pcode keeps
    stable per session, so the first request of a turn is compared with what
    the previous turn cached (until the conversation idles past the cache TTL).
    A notice after /compact is therefore expected: it reports the reset.
    """

    # Write each notice's fingerprint window to disk (the `debug` setting).
    dump_fingerprints: bool = False
    # `for_run` copies the capability with `replace()`, which re-initializes
    # `init=False` fields, so each run fingerprints its own requests -- matching
    # the upstream detector's per-run step numbering.
    diagnostics: CacheDiagnostics = field(
        init=False, default_factory=CacheDiagnostics, compare=False, repr=False
    )

    async def after_model_request(self, ctx, *, request_context, response):
        # Harness hooks are filters, not listeners: the return value replaces the
        # response (or request context, for `before_model_request`). A hook that
        # only records something must still return it, or the loss surfaces far
        # away as `AttributeError: 'NoneType' object has no attribute 'usage'`
        # from the upstream `WarnOnCacheBusts` this class extends.
        #
        # The pinned upstream hook does not suspend: only its synchronous warning
        # emission is captured, never model/tool execution or another task's work.
        # Emit outside this scope; ctx.emit can suspend. No global warning handler.
        # Which run set the mark this response is judged against, read before
        # upstream advances it. Same key as `CacheHealthDetector.observe`.
        # (A summed multi-pass usage, e.g. after web searches, no longer
        # raises the mark upstream, so pcode needs no correction for it.)
        key = (response.provider_name, response.provider_url, response.model_name)
        prior = self._state.detector.marks.get(key)
        with warnings.catch_warnings(record=True) as caught:
            result = await super().after_model_request(
                ctx, request_context=request_context, response=response
            )
        # Fingerprint every request, not just collapsing ones: diagnosing a
        # collapse needs the healthy request before it to compare against.
        # Part shapes vary by provider and capability, so a diagnostic that
        # cannot read one must degrade to silence rather than end the run.
        try:
            self.diagnostics.record(request_context, response)
        except Exception:
            self.diagnostics.records.clear()
        for warning in caught:
            if issubclass(warning.category, CacheBustWarning):
                # Report measured reuse, not upstream's speculative expiry cause
                # or an inferred number of tokens billed uncached.
                model = "/".join(
                    part for part in (response.provider_name, response.model_name) if part
                )
                text = notice_text(
                    self._state.step,
                    warning.message.cache_read_tokens,
                    warning.message.established_tokens,
                    model,
                    earlier_turn=prior is not None and prior.run_id != ctx.run_id,
                )
                text = "\n".join(part for part in (text, self.diagnostics.summary()) if part)
                path = self.diagnostics.dump() if self.dump_fingerprints else None
                if path is not None:
                    text += f"\nRequest fingerprints: {path}"
                await ctx.emit(CacheBustEvent(text=command_text(text)))
            elif issubclass(warning.category, CacheNotEnabledWarning):
                # pcode picks caching per provider (`cache_settings`), leaving
                # some off on purpose; and a stray warning would print over
                # the terminal UI.
                continue
            else:
                warnings.warn_explicit(
                    warning.message, warning.category, warning.filename, warning.lineno
                )
        return result
