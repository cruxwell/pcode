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
- The SDK params for web search `20260209`/`20260318` and web fetch
  `20260209`/`20260309`/`20260318` all accept `allowed_callers`.

## Upstream tasks

Decision: one comment on #7633, not a separate issue. It's the same settings
shape, the PR already touches the version selection and the implicit-tool set,
and the bypass is a bug in the PR itself.

- [ ] Comment on #7633: add `allowed_callers` to both Anthropic settings
      TypedDicts, pass it through on `20260209`+ params, skip the implicit
      code-execution names when it excludes code execution, and point out the
      profile bypass. Lead with the shell confusion and the numbers above; ZDR
      eligibility is a second reason. Offer to push the change or a follow-up PR.
- [ ] If the author wants to keep scope, open an issue linking #7633 and send
      the follow-up PR from it.
- [ ] Docs: in the same comment, ask for a note on `WebSearchTool` /
      `WebFetchTool` that the `20260209`+ tools give the model an implicit
      code-execution tool, which competes with any local shell tool.
- [ ] Once released, swap the profile workaround for
      `WebSearchTool(provider_settings={"anthropic": {"allowed_callers": ["direct"]}})`
      wherever it was applied.

## pcode

pcode is exposed too: `extensions/web_research.py` adds native `WebSearch` in
`auto` mode, and native `WebFetch` for `anthropic:` models (`claude:` goes through
the CLI and is unaffected). pcode's `shell` sits beside it, so the same mix-up can
happen in an interactive session; it's just more visible there.

- [ ] Check saved sessions for `srvtoolu_` `code_execution` calls to see how
      often it happens here.
- [ ] If it does, apply the same interim fix for `anthropic:` models, or wait for
      the upstream setting if that lands soon.
