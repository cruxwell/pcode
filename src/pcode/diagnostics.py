"""Useful provider errors without HTTP headers or credential values."""

import os
import re
import sysconfig
import traceback
from importlib.metadata import version
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

RESTART = "Restart pcode to load it; /resume picks this conversation back up."


def stale_install() -> str | None:
    """Why this process's code may no longer match what is on disk.

    pcode imports much of itself lazily, and the global install is editable, so
    a long-lived session that outlives a merge mixes old modules with new ones
    (a new module reading a setting the old one never defined), and one that
    outlives a reinstall on another Python finds its site-packages deleted.
    Both surface as unrelated-looking errors far from the cause.
    """
    import pcode

    if not Path(sysconfig.get_paths()["purelib"]).is_dir():
        return f"pcode was reinstalled since this session started. {RESTART}"
    try:
        changed = any(
            path.stat().st_mtime > pcode.STARTED
            for path in Path(pcode.__file__).parent.rglob("*.py")
        )
    except OSError:
        changed = True  # A file vanished mid-scan: the source is changing.
    return f"pcode's source changed since this session started. {RESTART}" if changed else None


# Formats that are a credential wherever they appear, so masking them never
# mangles ordinary text or code.
_TOKEN = re.compile(
    r"\b(?:sk-[A-Za-z0-9_-]{12,}|gh[pousr]_[A-Za-z0-9]{12,}|github_pat_[A-Za-z0-9_]{20,}|"
    r"glpat-[A-Za-z0-9_-]{20,}|xox[abeoprs]-[A-Za-z0-9-]{10,}|AIza[A-Za-z0-9_-]{35}|"
    r"(?:AKIA|ASIA)[0-9A-Z]{16}|eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+)\b"
)
# A whole key block; one cut short (streamed, or clipped by an output limit)
# hides through the end of the text, since its end marker may never come.
_PRIVATE_KEY = re.compile(
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|\Z)",
    re.DOTALL,
)
_KEY_END = re.compile(r"\s*-----END [A-Z ]*PRIVATE KEY-----\s*")
_BASE64_LINE = re.compile(r"[A-Za-z0-9+/=]+\s*")
# Keyword matching, for error reports and text that leaves this machine only:
# on screen it mangles ordinary code (`input_token: int`).
_ASSIGNMENT = re.compile(
    r"(?i)((?:api[_-]?key|access[_-]?token|refresh[_-]?token|id[_-]?token|"
    r"password|secret|authorization)[\"']?\s*[:=]\s*[\"']?)([^\s\"',;}]+)"
)


def mask_secrets(text: str) -> str:
    """Hide text that is certainly a credential, and strip terminal controls.

    For what is shown on screen. The model has already seen the text, so this
    only guards against a screen share or a pasted transcript; it matches only
    unambiguous formats and the values of this process's secret environment
    variables, never keyword assignments in the text.
    """
    for name, value in os.environ.items():
        if len(value) >= 8 and re.search(r"TOKEN|SECRET|PASSWORD|API_KEY|ACCESS_KEY", name):
            text = text.replace(value, "[redacted]")
    if "PRIVATE KEY-----" in text:
        text = _clipped_key_tail(_PRIVATE_KEY.sub("[private key redacted]", text))
    text = re.sub(r"(?i)\bBearer\s+[^\s\"',;}]+", "Bearer [redacted]", text)
    text = re.sub(r"(?i)(https?://)[^/\s@]+@", r"\1[redacted]@", text)
    text = _TOKEN.sub("[redacted]", text)
    return re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", "", text)


def _clipped_key_tail(text: str) -> str:
    """Mask a key block whose header was clipped away, from its end marker back.

    Walks back over the base64 lines before each orphaned end marker, the
    first of which may be partial. A loop, not a regex: an unanchored pattern
    retries from every character of long base64-like output (`seq`, hashes),
    which the live shell preview rescans on every chunk.
    """
    lines = text.split("\n")
    shown: list[str] = []
    for line in lines:
        if _KEY_END.fullmatch(line):
            while shown and _BASE64_LINE.fullmatch(shown[-1]):
                shown.pop()
            line = "[private key redacted]"
        shown.append(line)
    return "\n".join(shown)


def redact(text: str) -> str:
    """`mask_secrets` plus keyword assignments, for error reports and outbound text."""
    return _ASSIGNMENT.sub(r"\1[redacted]", mask_secrets(text))


# Enough for a deep cause chain through the agent graph, small enough that a
# failing loop cannot fill a session directory.
REPORT_CHARS = 32_000


def error_report(error: BaseException) -> str:
    """The frames `error_details` deliberately omits, redacted and bounded.

    Frames and exception messages only: locals are never captured, so a request
    object or credential held in a stack frame cannot reach the file. Keep the
    end when truncating -- the innermost frames are where the failure is.
    """
    text = redact("".join(traceback.format_exception(error)))
    if len(text) <= REPORT_CHARS:
        return text
    return "[earlier frames omitted]\n" + text[-REPORT_CHARS:]


def error_details(error: BaseException) -> dict:
    """Keep bounded, redacted causes without traceback locals or request objects."""
    return _error_details(error, set())


def _error_details(error: BaseException, seen: set[int]) -> dict:
    if id(error) in seen or len(seen) >= 16:
        return {"type": type(error).__name__, "truncated": True}
    seen.add(id(error))
    if isinstance(error, BaseExceptionGroup) and error.exceptions:
        return _error_details(error.exceptions[0], seen)
    result = {"type": type(error).__name__}
    status = getattr(error, "status_code", None)
    if isinstance(status, int):
        result["status"] = status
    body = getattr(error, "body", None)
    if isinstance(body, dict):
        detail = body.get("error", body)
        if isinstance(detail, dict):
            for key in ("message", "code", "param", "type"):
                value = detail.get(key)
                if isinstance(value, str):
                    result[f"provider_{key}"] = redact(value)[:4000]
    if "provider_message" not in result:
        result["message"] = redact(str(error))[:4000]
    if error.__cause__ is not None:
        result["cause"] = _error_details(error.__cause__, seen)
    elif error.__context__ is not None and not error.__suppress_context__:
        result["context"] = _error_details(error.__context__, seen)
    return result


TRANSIENT_TRANSPORT = frozenset(
    {
        "RemoteProtocolError",
        "APIConnectionError",
        "ConnectError",
        "ConnectTimeout",
        "ReadError",
        "ReadTimeout",
        "WriteError",
        "WriteTimeout",
        "APITimeoutError",
        "IncompleteRead",
        # A `claude:` CLI process ended mid-request; the retry starts another.
        "ClaudeProcessError",
        # A `claude:` request got no HTTP response; the retry forks the transcript.
        "ClaudeConnectionError",
    }
)


def transport_types(error: BaseException) -> set[str]:
    """Name every exception in the bounded cause chain, never its text."""
    names, detail = set(), error_details(error)
    while detail:
        names.add(detail["type"])
        detail = detail.get("cause", detail.get("context", {}))
    return names


def transient(error: BaseException) -> bool:
    """Whether a failed request can use the runtime's bounded retry budget.

    HTTP errors still need their own handling. A streamed overload has no HTTP
    status, though: the SDK raises APIError after the response already started.
    Recognize that explicit refusal without retrying arbitrary provider errors.
    """
    detail = error_details(error)
    retryable = False
    while detail:
        if "status" in detail:
            return False
        if detail["type"] in TRANSIENT_TRANSPORT:
            retryable = True
        if detail["type"] in {"APIError", "ModelAPIError"}:
            retryable |= (
                detail.get("provider_code") == "overloaded_error"
                or detail.get("provider_type") == "overloaded_error"
                or detail.get("provider_message", detail.get("message", ""))
                == "Our servers are currently overloaded. Please try again later."
            )
        detail = detail.get("cause", detail.get("context", {}))
    return retryable


def provider_context(model) -> dict[str, str]:
    """Best-effort configured route, never requests, headers, or proxy credentials.

    This names the parent model, not necessarily a failing delegated request.
    Unresolved model strings must not trigger credential loading during failure.
    """
    result = {}
    try:
        if isinstance(model, str):
            result["model"] = redact(model)
            return result
        result["model"] = redact(model.model_name)
        provider = model.provider
        if provider is not None:
            result["provider"] = redact(provider.name)
            url = urlsplit(str(provider.client.base_url))
            if url.scheme in {"http", "https"} and url.hostname:
                host = url.hostname
                if ":" in host:
                    host = f"[{host}]"
                if url.port is not None:
                    host += f":{url.port}"
                # Drop userinfo, query, and fragment even for unfamiliar secrets.
                result["base_url"] = redact(urlunsplit((url.scheme, host, url.path, "", "")))
    except Exception:
        # Diagnostics must never replace the original failure.
        pass
    return result


def quota_message(error: BaseException) -> str | None:
    """Classify bounded provider causes; never echo arbitrary response text."""
    detail = error_details(error)
    rate_limited = False
    http_status = None
    while detail:
        if http_status is None:
            http_status = detail.get("status")
        text = " ".join(
            str(detail.get(key, ""))
            for key in ("provider_code", "provider_type", "provider_message", "message")
        ).lower()
        if any(
            marker in text
            for marker in (
                "insufficient_quota",
                "no credits remaining",
                "credit balance is too low",
                "billing_hard_limit_reached",
                "exceeded your current quota",
            )
        ):
            return (
                "Provider quota or credits exhausted. Check usage and billing for the "
                "selected provider account; a new login may not help. "
                "See the saved session diagnostics."
            )
        rate_limited |= detail.get("status") == 429 or (
            "status" not in detail
            and any(marker in text for marker in ("rate_limit_error", "rate_limit_exceeded"))
        )
        detail = detail.get("cause", detail.get("context", {}))
    if rate_limited:
        return (
            f"Provider rate limit reached{f' (HTTP {http_status})' if http_status else ''}. "
            "Wait before retrying and check "
            "the selected account's usage limits. See the saved session diagnostics."
        )
    return None


def versions() -> dict[str, str]:
    return {
        package: version(package)
        for package in ("pcode", "pydantic-ai-slim", "pydantic-ai-harness")
    }
