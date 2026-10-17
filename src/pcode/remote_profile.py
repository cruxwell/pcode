"""The fixed execution profile for a session host nobody is watching.

A host started by a remote control (`pcode --email-listen`) runs turns with no
one at the terminal to stop them, and pcode has no approval step. So the
listener chooses a profile locally, before any message arrives, and nothing
sent remotely can change it:

- the bundled `sandbox` extension is on whatever `extensions_off` says, its
  shell sandbox cannot be switched off, and it also refuses reads of the
  listener's own state and credentials, and keychain lookups from the shell;
- the host's environment is built from an allowlist, not inherited whole;
- MCP servers stay off unless the profile opts in (they run unsandboxed);
- each turn has a wall-clock limit and a budget of model requests and tool
  calls, shared with its sub-agents; reaching one ends the turn with a status
  that says so;
- the session always gets a fresh worktree, which is kept when the host stops.

`spawn_host(profile=...)` hands the profile to `pcode.host`, which calls
`activate` before anything else is built; the rest of pcode asks `active()`.
"""

from __future__ import annotations

import json
import time
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field

# What a turn ending at a limit says, first in its message: the gateway reads
# a turn that failed or was cancelled with this as one that hit a limit.
LIMIT_PREFIX = "Turn limit reached"

# Exact names a remote host may inherit. Provider credentials come from
# `pcode.models.ENV_PROVIDERS` as well (see `allowed_env_names`).
_ENV_NAMES = frozenset(
    {
        "PATH",
        "HOME",
        "USER",
        "LOGNAME",
        "SHELL",
        "TMPDIR",
        "TERM",
        "LANG",
        "LANGUAGE",
        "TZ",
        "XDG_CONFIG_HOME",
        "XDG_CACHE_HOME",
        "XDG_STATE_HOME",
        "XDG_DATA_HOME",
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_BASE_URL",
        "OPENAI_BASE_URL",
        "AZURE_OPENAI_ENDPOINT",
        "AWS_REGION",
        "AWS_DEFAULT_REGION",
        # Bedrock's key id is a provider variable; the rest of the credential with it.
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "REQUESTS_CA_BUNDLE",
        "PCODE_CONFIG_DIR",
        "PCODE_SESSION_DIR",
        "PCODE_CREDENTIALS_FILE",
        "PCODE_CODEX_CREDENTIALS_FILE",
        "PCODE_ANTHROPIC_AUTH",
        "PCODE_LLM_PROXY",
        "PCODE_MCP_CONFIG",
        "PYDANTIC_AI_NO_BANNER",
        # Which pcode the host imports, as for the listener.
        "PYTHONPATH",
        "VIRTUAL_ENV",
    }
)
_ENV_PREFIXES = ("LC_",)

# Mach services the shell sandbox may not reach: the keychain, where the
# listener keeps its mailbox credential.
KEYCHAIN_SERVICES = (
    "com.apple.SecurityServer",
    "com.apple.securityd",
    "com.apple.security.keychaind",
)


@dataclass(frozen=True)
class RemoteProfile:
    """What a remote host may do; serialised onto its command line (no secrets)."""

    # Extra unreadable paths (globs, `~` allowed): the listener's state, credentials.
    deny_read: tuple[str, ...] = ()
    # Default MCP servers start, as in a local session.
    mcp: bool = False
    # Per turn, sub-agents included; 0 is no limit.
    turn_minutes: float = 30.0
    max_requests: int = 100
    max_tool_calls: int = 100
    # The commit or branch the session's worktree starts from; None: the mainline.
    base: str | None = None
    # Who started it, for `pcode --hosts` and the host log.
    origin: str = "remote"

    def to_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True)

    @classmethod
    def from_json(cls, text: str) -> RemoteProfile:
        data = json.loads(text)
        if not isinstance(data, dict):
            raise ValueError("A remote profile must be a JSON object.")
        known = {name for name in cls.__dataclass_fields__}
        if unknown := set(data) - known:
            raise ValueError(f"Unknown remote profile keys: {', '.join(sorted(unknown))}")
        if "deny_read" in data:
            data["deny_read"] = tuple(str(entry) for entry in data["deny_read"])
        return cls(**data)

    def describe(self) -> list[str]:
        """The profile as lines for a terminal or an email."""

        def limit(value, unit: str) -> str:
            return f"{value:g} {unit}" if value else "no limit"

        return [
            "Sandbox: on (writes confined to the session worktree and caches; "
            "credentials and the listener's state unreadable; no keychain)",
            "Worktree: a fresh one per session, kept afterwards; nothing is merged",
            "Environment: allowlisted (provider keys, PATH, HOME, locale) only",
            f"MCP servers: {'default servers start' if self.mcp else 'off'}",
            "Per turn: "
            + ", ".join(
                (
                    limit(self.turn_minutes, "min"),
                    limit(self.max_requests, "model requests"),
                    limit(self.max_tool_calls, "tool calls"),
                )
            ),
            "Project extensions and worktree-setup: only if the repository is already trusted",
        ]


_ACTIVE: RemoteProfile | None = None


def activate(profile: RemoteProfile | None) -> None:
    """Apply `profile` to this process; a host calls it once, before building anything."""
    global _ACTIVE
    _ACTIVE = profile
    BUDGET.profile = profile
    BUDGET.start()


def active() -> RemoteProfile | None:
    return _ACTIVE


def allowed_env_names(environ: Mapping[str, str]) -> set[str]:
    from pcode.models import ENV_PROVIDERS

    provider = {name for groups in ENV_PROVIDERS.values() for group in groups for name in group}
    names = _ENV_NAMES | provider
    return {
        name for name in environ if name in names or any(name.startswith(p) for p in _ENV_PREFIXES)
    }


def scrubbed_env(environ: Mapping[str, str]) -> dict[str, str]:
    """The environment a remote host starts with: allowlisted names only."""
    return {name: environ[name] for name in sorted(allowed_env_names(environ))}


class TurnLimitReached(RuntimeError):
    """A remote turn used up a budget. Not transient, so no retry spends more."""

    # Its message is fixed text: `error_message` shows it as is.
    sanitized = True


@dataclass
class TurnBudget:
    """What the running turn has used, across the agent and every sub-agent."""

    profile: RemoteProfile | None = None
    requests: int = 0
    tool_calls: int = 0
    started: float = field(default_factory=time.monotonic)
    # Set once a limit is hit, so every later request or call fails at once.
    reached: str = ""

    def start(self) -> None:
        self.requests = self.tool_calls = 0
        self.started = time.monotonic()
        self.reached = ""

    def _check(self) -> None:
        if self.reached:
            raise TurnLimitReached(self.reached)

    def _over(self, reason: str) -> None:
        self.reached = f"{LIMIT_PREFIX}: {reason}. The turn was stopped."
        raise TurnLimitReached(self.reached)

    def charge_request(self) -> None:
        self._check()
        profile = self.profile
        if profile is None:
            return
        if profile.turn_minutes and time.monotonic() - self.started > profile.turn_minutes * 60:
            self._over(f"{profile.turn_minutes:g} minutes")
        self.requests += 1
        if profile.max_requests and self.requests > profile.max_requests:
            self._over(f"{profile.max_requests} model requests")

    def charge_tool_call(self) -> None:
        self._check()
        profile = self.profile
        if profile is None:
            return
        self.tool_calls += 1
        if profile.max_tool_calls and self.tool_calls > profile.max_tool_calls:
            self._over(f"{profile.max_tool_calls} tool calls")

    def expire(self) -> str:
        """The wall clock ran out: later requests fail; returns what to show."""
        profile = self.profile
        minutes = profile.turn_minutes if profile is not None else 0
        self.reached = f"{LIMIT_PREFIX}: {minutes:g} minutes. The turn was stopped."
        return self.reached


# One per process: a host runs one turn at a time, and sub-agents share it.
BUDGET = TurnBudget()
