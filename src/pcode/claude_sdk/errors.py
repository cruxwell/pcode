"""Exceptions for failed `claude:` requests, and what to tell the user about them."""

from pydantic_ai.exceptions import ModelAPIError, ModelHTTPError

MISSING_SDK = (
    "claude: models need pcode's optional `claude` extra, which is not installed. "
    "From a pcode checkout run `make install`, "
    "or `uv tool install --python 3.15 --editable '.[claude]'`."
)


class ClaudeHTTPError(ModelHTTPError):
    """An API error the CLI reported for a `claude:` request."""


class ClaudeProcessError(ModelAPIError):
    """The CLI process ended mid-request; a retry starts another (transient)."""


class ClaudeConnectionError(ModelAPIError):
    """The CLI's request got no HTTP response: dropped, refused or timed out (transient).

    Verified against the bundled CLI: all three arrive as a `server_error` with
    no `api_error_status`, where a real 500 or 529 carries its status. With the
    CLI's own retries off, pcode's transient retry covers these, as it covers
    the same failures on its other providers.
    """


class ClaudeStartError(ModelAPIError):
    """The CLI process could not be started or resumed."""


class ClaudeSDKMissing(ValueError):
    """pcode was installed without its `claude` extra."""


def failure_hint(error: BaseException) -> str | None:
    """What to do about a failed `claude:` request, or None for any other provider."""
    seen = set()
    while error is not None and id(error) not in seen and len(seen) < 16:
        seen.add(id(error))
        if isinstance(error, ClaudeSDKMissing):
            return MISSING_SDK
        if isinstance(error, ClaudeConnectionError):
            # The CLI's own text stays in the diagnostics log.
            return (
                "Claude Code got no response from Anthropic (connection dropped, refused "
                "or timed out). Check network/proxy connectivity and retry when ready."
            )
        if isinstance(error, ClaudeHTTPError):
            body = error.body if isinstance(error.body, dict) else {}
            kind = (body.get("error") or {}).get("type")
            if error.status_code in (401, 403) or kind == "authentication_failed":
                return "Claude Code is not signed in, or its login expired. Run /login claude."
            return None
        if isinstance(error, ClaudeStartError):
            return f"{error.message} Check the Claude Code install, or run /login claude."
        if isinstance(error, ClaudeProcessError):
            return f"{error.message} Retry; pcode starts a new Claude Code process."
        error = error.__cause__ or error.__context__
    return None
