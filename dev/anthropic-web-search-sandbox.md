# Anthropic web search brings a hidden code sandbox

Task notes, 2026-10-16. Upstream work in pydantic-ai (Harness ships from the same
repo), plus what it means for pcode.

## The problem

For newer Claude models, pydantic-ai sends `WebSearchTool` as
`web_search_20260209` and `WebFetchTool` as `web_fetch_20260209`. From those
versions on, Anthropic's `allowed_callers` defaults to `["code_execution_20260120"]`
("dynamic filtering"), and the API quietly gives the model a server-side
`code_execution` sandbox to filter results in. That tool is in no request's
`tools`, so nothing on our side shows it was offered.

An agent that also has a `shell` tool now has two places to run commands, and
sometimes picks the wrong one. The sandbox's cwd is `/root`, it has no `gh`, no
credentials and no checkout, so `gh search prs ...` reports "command not found",
`grep` over the repo finds nothing, and a file edit there never reaches the
workspace. The model often reads that as an environment problem and moves on.

Measured in a headless agent on pydantic-ai 2.55 with Claude Opus and Sonnet 5.5,
web search on and the Harness `Coder` shell:

- 126 of 478 read-only triage runs sent commands to the sandbox, 36 of them
  never using the real `shell` (so their PR checks never ran).
- 3 of 33 coding runs did, one of them a Python heredoc rewriting a source file.

In a trace it shows up as a tool call with a `srvtoolu_` id named
`code_execution`, and no matching `execute_tool` span.

## What exists upstream (as of v2.57.0)

- Anthropic's documented opt-out is `allowed_callers: ["direct"]` on the web
  tool, which keeps the newer version without the sandbox
  ([web search docs](https://platform.claude.com/docs/en/agents-and-tools/tool-use/web-search-tool),
  [server tools](https://platform.claude.com/docs/en/agents-and-tools/tool-use/server-tools#zdr-and-allowed-callers)).
- pydantic-ai doesn't expose it. [#4844](https://github.com/pydantic/pydantic-ai/pull/4844),
  which added the `_20260209` tools, says it "does not add a public
  `dynamic_filtering` or ZDR opt-out flag".
- The only lever is the profile key `anthropic_supports_dynamic_filtering=False`,
  which falls back to `web_search_20250305` / `web_fetch_20250910`. It works, but
  it also blocks any later version, such as `20260318` for `response_inclusion`.
- Open [#7633](https://github.com/pydantic/pydantic-ai/pull/7633) adds
  `provider_settings` to `WebSearchTool`, `WebFetchTool` and `AdvisorTool` with
  `AnthropicWebSearchToolSettings` / `AnthropicWebFetchToolSettings`, covering only
  `response_inclusion` and `use_cache`. That's the natural home for this.
- No issue reports the sandbox being mistaken for the agent's shell.

Checked 2026-10-16 against the #7633 diff and the locked Anthropic SDK:

- #7633's only approvals are from the `github-actions` review bot, and that bot
  asked @DouweM to confirm the API shape (#7338 left it as a maintainer call). So
  the shape isn't settled yet and an extra field costs no re-review.
- As written, #7633 bypasses the profile workaround: a `response_inclusion` or
  `use_cache` setting returns the `20260318` param before the
  `supports_dynamic_filtering` check, so a profile with it set to `False` gets
  the sandbox back.
- It also marks `code_execution` and friends as enabled server tools whenever a
  `20260209`/`20260318` web tool is present. With `allowed_callers: ["direct"]`
  that should be skipped, or a hallucinated `code_execution` call is no longer
  dropped (the #6401 fix).
- Every web tool param in the SDK accepts `allowed_callers`, the 2025 versions
  included. Anthropic's docs say the `20260209` versions default to the code
  execution caller *only* (the model can't call `web_search` directly at all),
  and the earlier versions default to `["direct"]`.

## Upstream work (filed 2026-10-16)

We sent the PR first, then the issue, so a bot couldn't pick up the issue and
start its own fix. Upstream's CONTRIBUTING asks for maintainer alignment before
a PR, so expect a reply about the shape first.

- [x] PR [#10489](https://github.com/pydantic/pydantic-ai/pull/10489)
      (`aweis89:feat/anthropic-web-tools-allowed-callers`, worktree
      `~/p/pydantic-ai-allowed-callers`). It adds
      `AnthropicModelSettings.anthropic_web_tools_allowed_callers`, which is
      passed through on whichever web tool version is picked. The implicit
      `code_execution` names now come from the built tool params, so `['direct']`
      still drops hallucinated calls. Docs: "Web Tool Callers" in
      `docs/models/anthropic.md`. It's a model setting because #7633's
      `provider_settings` shape is still open, and the PR offers to move it there.
      Tests use mocks only, since there was no API key to record a cassette.
- [x] Issue [#10490](https://github.com/pydantic/pydantic-ai/issues/10490),
      closed by the PR.
- [x] [Comment on #7633](https://github.com/pydantic/pydantic-ai/pull/7633#issuecomment-6223937421)
      about the `20260318` bypass of the profile workaround.
- [ ] A maintainer has to add `pkg:core`; the Package Label check fails until
      then (we lack triage access).
- [ ] Not checked live: whether the API accepts `allowed_callers` on Vertex's
      `web_search_20250305`, and whether replaying `code_execution_20260120`
      caller history into a `['direct']` request is accepted.
- [ ] Once released, set `anthropic_web_tools_allowed_callers=['direct']` (or
      whatever shape lands) wherever the profile workaround was applied.

## pcode

`extensions/web_research.py` adds native `WebSearch` in `auto` mode, and native
`WebFetch` for `anthropic:` models (`claude:` goes through the CLI and is
unaffected). With a subscription login, `AnthropicOAuthModel.profile` already
narrows to the 2025 tool versions, because that endpoint confines the `20260209`
tools to a rate-limited sandbox. So only API-key `anthropic:` sessions get the
sandbox beside pcode's `shell`.

- [ ] Check saved API-key sessions for `srvtoolu_` `code_execution` calls to see
      how often it happens here.
- [ ] Once #10489 (or its replacement) is released, try
      `allowed_callers=['direct']` on the subscription endpoint. It may let
      OAuth sessions use the `20260209` tools (and domain filtering) again
      instead of the profile downgrade.
