"""`claude:` models: Claude Code's own login through the Claude Agent SDK, no proxy.

`ClaudeModel` (in `model`) is an `AnthropicModel` whose requests never touch
its HTTP client. Each conversation keeps one `claude` CLI process (the one
bundled with `claude-agent-sdk`) alive across requests. pcode's tools are served to that
process as an in-process MCP server whose handlers *park*: the model calls a
tool, the CLI invokes the handler, the streamed assistant message ends, and
Pydantic AI runs the tool itself. The next request carries the result, which
releases the parked handler, and the CLI goes on to its next API call. The CLI
streams raw Anthropic events, so parsing reuses `AnthropicModel`'s
streamed-response code. `session` holds one such process (`ClaudeSession`);
`cli` the binary and environment it runs with.

pcode's history stays the source of truth; the CLI transcript is a disposable
copy. `messages` normalizes and hashes that history so it can be compared. A
request continues a live process only when that process holds exactly the
history before the new user message. Otherwise it forks the CLI transcript at
the last assistant message both share (`resume` + `fork_session` +
`resume_session_at`: structured history, warm cache), found through a persisted
index of history hashes (`resume`), and failing that starts over with the
history replayed as text. `session_pool` makes that choice
and keeps idle processes within limits. See dev/anthropic-providers.md.

`ClaudeWorkspace` (in `workspace`) points each request's CLI at the agent's
workspace, and `errors` holds the failures and what to tell the user about
them. pcode holds no credentials here: the CLI signs in itself (`/login claude`).

Tests patch the submodule that defines a name (`pcode.claude_sdk.session_pool`),
never this package: a re-export is a copy, so patching it changes nothing the
submodules read. Tuning knobs and pool state are left out of the re-exports for
that reason.
"""

from pcode.claude_sdk.cli import CLI_ENV, LOGIN_ENV, cli_path
from pcode.claude_sdk.errors import (
    MISSING_SDK,
    ClaudeConnectionError,
    ClaudeHTTPError,
    ClaudeProcessError,
    ClaudeSDKMissing,
    ClaudeStartError,
    failure_hint,
)
from pcode.claude_sdk.messages import REPLAY_INTRO, lineage, normalize, replay
from pcode.claude_sdk.model import PREFIX, ClaudeModel, ClaudeProvider, claude_model
from pcode.claude_sdk.resume import ForkPoint, ResumeIndex, index_path
from pcode.claude_sdk.session import (
    SERVER,
    TOOL_PREFIX,
    TOOL_USE_ID,
    ClaudeSession,
    SessionConfig,
)
from pcode.claude_sdk.session_pool import (
    Checkout,
    SessionPool,
    pool,
    shutdown,
)
from pcode.claude_sdk.workspace import CWD_SETTING, ClaudeWorkspace

__all__ = [
    "CLI_ENV",
    "CWD_SETTING",
    "LOGIN_ENV",
    "MISSING_SDK",
    "PREFIX",
    "REPLAY_INTRO",
    "SERVER",
    "TOOL_PREFIX",
    "TOOL_USE_ID",
    "Checkout",
    "ClaudeConnectionError",
    "ClaudeHTTPError",
    "ClaudeModel",
    "ClaudeProcessError",
    "ClaudeProvider",
    "ClaudeSDKMissing",
    "ClaudeSession",
    "ClaudeStartError",
    "ClaudeWorkspace",
    "ForkPoint",
    "ResumeIndex",
    "SessionConfig",
    "SessionPool",
    "claude_model",
    "cli_path",
    "failure_hint",
    "index_path",
    "lineage",
    "normalize",
    "pool",
    "replay",
    "shutdown",
]
