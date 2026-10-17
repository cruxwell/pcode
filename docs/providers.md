# Providers and models

## Authentication

For `openai-codex:` models, run `/login openai-codex` to sign in with your
ChatGPT account in the browser. No Codex CLI is required. Credentials are stored
in `codex-credentials.json` under `$PCODE_CONFIG_DIR`, otherwise
`$XDG_CONFIG_HOME/pcode` (default `~/.config/pcode`);
`PCODE_CODEX_CREDENTIALS_FILE` overrides the full path. The file is owner-only,
and refreshed tokens are saved for later launches.

- pcode's own login wins. Without it, pcode reads the Codex CLI's `auth.json` as
  a read-only fallback (`CODEX_HOME` is honored); tokens refreshed from it stay in
  memory. A malformed pcode login is reported as an error rather than silently
  switching accounts.
- `/logout openai-codex` removes only pcode's login, never the CLI's. New models
  then fall back to the CLI login if there is one; the active model keeps its
  cached token.
- The browser callback uses fixed port 1455, so close other login flows using it
  first. On a headless machine, run the Codex CLI's `codex login --device-auth`
  instead.
- This provider never falls back to `OPENAI_API_KEY`. Which models you can use
  depends on your account. Authentication errors never show raw provider
  responses or credential values.

For ordinary OpenAI API models, use `openai:<model-id>` with `OPENAI_API_KEY` in
your environment. For Anthropic API models, use `anthropic:<model-id>` with
`ANTHROPIC_API_KEY`; to use a Claude subscription instead, pick a
[Claude Code model](#claude-code-provider). Use the exact model ID your account
offers; pcode does not remap aliases. Every provider in the table below works with the default install except
`claude:`, which needs the [`claude` extra](#claude-code-provider).

## Supported providers

Any `provider:model-id` string that Pydantic AI accepts works with `--model` and
`/model`. The model picker offers a provider when its variable below is *set*
(the value is never read). "Catalog" says whether the picker suggests model IDs
or only accepts IDs you type.

| Prefix | Enabled by | Catalog |
| --- | --- | --- |
| `anthropic` | `ANTHROPIC_API_KEY` | yes |
| `openai-codex` | pcode login, then CLI credential file (`CODEX_HOME` honored) | yes (all OpenAI IDs) |
| `openai`, `openai-chat`, `openai-responses` | `OPENAI_API_KEY` | yes |
| `claude` | Claude Code config (`~/.claude`, `~/.claude.json`, or `CLAUDE_CONFIG_DIR`) or `claude` on `PATH` | yes (Anthropic IDs) |
| `google` | `GOOGLE_API_KEY` or `GEMINI_API_KEY` | yes |
| `google-cloud` | `GOOGLE_CLOUD_PROJECT` or `GOOGLE_APPLICATION_CREDENTIALS` | yes |
| `bedrock` | `AWS_BEARER_TOKEN_BEDROCK`, `AWS_ACCESS_KEY_ID`, or `AWS_PROFILE` | yes |
| `bedrock-mantle` | `AWS_BEARER_TOKEN_BEDROCK` | yes |
| `groq` | `GROQ_API_KEY` | yes |
| `xai` | `XAI_API_KEY` | yes |
| `deepseek` | `DEEPSEEK_API_KEY` | yes |
| `cerebras` | `CEREBRAS_API_KEY` | yes |
| `crusoe` | `CRUSOE_API_KEY` | yes |
| `moonshotai` | `MOONSHOTAI_API_KEY` | yes |
| `heroku` | `HEROKU_INFERENCE_KEY` | yes |
| `snowflake` | `SNOWFLAKE_TOKEN` and `SNOWFLAKE_ACCOUNT` | yes |
| `zai` | `ZAI_API_KEY` | yes |
| `azure` | `AZURE_OPENAI_API_KEY` and `AZURE_OPENAI_ENDPOINT` | typed IDs |
| `github-copilot` | `GITHUB_COPILOT_API_KEY` (or `_TOKEN`/`COPILOT_GITHUB_TOKEN`) and `GITHUB_COPILOT_BASE_URL` (or `_API_BASE`/`COPILOT_API_URL`) | typed IDs |
| `openrouter` | `OPENROUTER_API_KEY` | typed IDs |
| `vercel` | `VERCEL_AI_GATEWAY_API_KEY` or `VERCEL_OIDC_TOKEN` | typed IDs |
| `fireworks` | `FIREWORKS_API_KEY` | typed IDs |
| `together` | `TOGETHER_API_KEY` | typed IDs |
| `nebius` | `NEBIUS_API_KEY` | typed IDs |
| `ovhcloud` | `OVHCLOUD_API_KEY` | typed IDs |
| `alibaba` | `ALIBABA_API_KEY` or `DASHSCOPE_API_KEY` | typed IDs |
| `sambanova` | `SAMBANOVA_API_KEY` | typed IDs |
| `ollama` | `OLLAMA_BASE_URL` | typed IDs |
| `vllm` | `VLLM_BASE_URL` | typed IDs |

The catalog is a static list of known models, not what your account is entitled
to. Providers not in the table (for example `mistral`, `cohere`, `huggingface`,
`litellm`) still work from `--model` once you install their Pydantic AI extra,
but the picker does not offer them. Apart from Codex and Claude Code, every
provider is plain API-key access with no login in pcode: set the variable in your shell before launching.

## Choose a model in the terminal

Use `/model` or Ctrl+L to open the model picker. Type to filter, use ↑/↓ to
select, and press Enter to apply. Filtering matches provider and model names,
including joined word prefixes: `anthopus` finds Anthropic Opus, `codluna` finds
Codex Luna, and `opus anth` works too. Escape, Ctrl+C, or Ctrl+L closes the
picker without changing the model or your draft. For a model not listed, type its
full `provider:model-id` (for example `anthropic:claude-opus-5`).

The picker offers each provider in the [supported providers](#supported-providers)
table whose credentials are configured, plus the current provider even with a
custom model ID. Opening it makes no network requests and never reads credential
contents: it only checks that a credential file exists or a variable is set.
`PCODE_LLM_PROXY` does not affect which models are offered.

Models are grouped by provider and family, newest version first (Opus 5 before
Opus 4.8; 4.10 before 4.9), and filtering keeps that order. The current model is
marked, not pinned to the top. Undated aliases come before dated snapshots of the
same version.

Suggestions are not an entitlement list; the provider checks access when you use
the model. A typed `provider:model-id` is accepted for any supported provider,
configured or not, so you can export a key after launch. If no provider is
configured, run `/login claude` or `/login openai-codex`, or set
`ANTHROPIC_API_KEY`, first.

**Changing models continues the current conversation.** History, session ID,
plan, tool panel, usage totals, transcript, and draft are kept, and the saved
session records the new model so resuming uses it. Use `/new` to start over
instead. Selecting the current model does nothing, and a failed switch leaves the
conversation intact. Switching from the offline preview starts a live
conversation without restarting pcode.

The picker also works while a run or queued messages are active. Like
steering, the switch lands at the running turn's next request: the request in
flight finishes on the old model, and everything after it, including the rest of
that turn, uses the new one. `/effort` mid-run works the same way. A turn started
with `$PROVIDER:MODEL` or `+EFFORT` stays on its own model to the end. To switch
without waiting for the request in flight, cancel it with Ctrl+C and send again.

## Saved model and effort defaults

A model chosen with `/model` becomes the default for future launches once the
selection takes effect. `/effort` and Ctrl+N/Ctrl+P save the effort for the
current model only, and also save that model as the default. Preferences live in
`~/.config/pcode/preferences.json` (or `$XDG_CONFIG_HOME/pcode/preferences.json`),
separate from saved conversations and unaffected by `--no-save`.

- `pcode` with no model argument reuses the saved model; with none saved it opens
  the offline preview. `--theme-preview` always stays offline.
- Saved effort applies per model, in new and resumed conversations. A model you
  never set uses the `effort` default. `/effort default` restores the provider
  default for the current model.
- `-m` / `--model` overrides the saved model for one launch, and `--continue`
  uses the session's model. Neither changes the saved default. Model and provider
  names are passed through as given.
- `pcode config unset KEY` resets one default.

## Reasoning effort

For OpenAI/Codex, Anthropic, and Claude Code models, Ctrl+N raises
effort and Ctrl+P lowers it; or use `/effort low|medium|high|xhigh`. `/effort`
alone shows the current setting and `/effort default` removes the override. Each
model remembers its own level. The footer shows the selected effort.

- Shortcuts stop at the lowest and highest levels. From the provider default they
  start at medium, so Ctrl+N selects high and Ctrl+P selects low.
- Changes apply from the next request, mid-turn too, and keep your draft.
- Effort does not change the model's thinking configuration. Not every model
  accepts every level; the provider still validates it.
- On Anthropic and Claude Code, only models that support effort accept
  it (Opus 4.5+ and Sonnet 4.6+, among others). For any other model, `/effort`
  refuses a level and the footer shows `n/a`; `/effort default` still clears a
  saved level. `xhigh` becomes Anthropic's `max` where the model has no native
  `xhigh`.
- The offline preview and other providers show `n/a`.

## Claude Code provider

A `claude:` model uses your Claude subscription through Claude Code's own login,
with no proxy to install or run:

```sh
pcode -m claude:claude-sonnet-5   # then /login claude if Claude Code is not signed in
```

The Claude Code CLI comes bundled, so you need neither Node.js nor a separate
`claude` install. Because the bundle is large (over 200 MB installed), it is the
optional `claude` extra. The Homebrew formula and `make install` include it;
elsewhere install `pcode[claude]`, for example
`uv tool install --python 3.15 --editable '.[claude]'` from a checkout. Without it, `claude:`
models are left out of `/model`, and naming one tells you what to install.

pcode keeps one Claude Code process per conversation and runs every tool itself.
A tool round does not start a new process.
[Anthropic provider options](https://github.com/cruxwell/pcode/blob/master/dev/anthropic-providers.md#direct-sdk-provider)
has the design and measurements.

### Signing in

`/login claude` runs `claude auth login` in your browser and writes Claude Code's
usual login (`CLAUDE_CONFIG_DIR` is honored), so an existing Claude Code sign-in
already works. pcode never reads or stores the credential. A request that fails
because the login is missing or expired tells you to run `/login claude`.

`ANTHROPIC_API_KEY`, `ANTHROPIC_AUTH_TOKEN`, `ANTHROPIC_BASE_URL`, and Claude
Code's cloud routes (`CLAUDE_CODE_USE_BEDROCK`, `CLAUDE_CODE_USE_VERTEX`, and the
like) are cleared for `claude:` requests and `/login claude`. The key you use for
`anthropic:` models can never bill or redirect a `claude:` request.

### What carries over

Conversations continue with a warm prompt cache after restarting pcode,
`--continue`, `/tree`, a retry, `/btw`, or a model or effort change. When the
history never went through Claude Code (you switched from another provider) or
was rewritten by compaction, the earlier conversation is replayed once, which
writes it to the cache again.

- Memory: each session runs a Claude Code process while a turn is in progress.
  The first costs about 300 MB and each further one about 110–135 MB. After a turn
  ends, each session keeps its process for ten minutes so the next turn starts
  immediately (`claude_idle_processes` and `claude_idle_minutes`; `0` processes
  stops each one when its turn ends). A process waiting on tool results, such as
  a parent waiting for delegated tasks, is kept up to thirty minutes. When less
  than a tenth of the machine's memory is free, pcode stops idle processes within
  a minute. All of them stop when pcode exits.
- Transcripts, tool output included, are stored in Claude Code's own store
  (`~/.claude/projects/`) and appear in `claude --resume` for the workspace. Your
  Claude Code settings are not loaded, so settings such as `cleanupPeriodDays` do
  not apply to these runs.
- Use full model IDs such as `claude:claude-sonnet-5`. An alias like
  `claude:opus` works, but pcode cannot look up its context window, so set
  `PCODE_CONTEXT_WINDOW` for compaction. pcode's output limit is passed on as
  `CLAUDE_CODE_MAX_OUTPUT_TOKENS`, capped at the model's maximum; for a model
  pcode has no limits for, Claude Code's default applies.
- The model sees tool names as `mcp__pcode__<name>`; pcode displays its own names.
- Anthropic server tools are not available. Claude Code's own WebSearch and
  WebFetch take the place of web search and fetch (see
  [web search](tools.md#web-search)); there is no code execution.
- Text pcode adds beside tool results (steering, MCP server notices)
  reaches the model as a message the user sent while it was working.
- Thinking is shown in summarized form.

## Model-only HTTP proxy

Set `PCODE_LLM_PROXY` to send Codex model requests, and only those, through an
HTTP proxy:

```sh
PCODE_LLM_PROXY=http://127.0.0.1:8080 pcode --model openai-codex:gpt-5.6-sol
```

- HTTP and HTTPS proxy URLs work (HTTPS traffic uses CONNECT). Unset or blank
  means no proxy.
- Only `openai-codex:` models use it, including Codex token refresh and model
  calls from delegated workers. Other providers ignore it, so you can leave it set when
  switching providers.
- The Codex client then ignores other proxy settings, including `NO_PROXY`, and
  environment-based TLS configuration, so use a proxy that tunnels HTTPS without
  needing a custom CA from the environment.
- Exa requests and shell commands keep their normal HTTP setup. pcode does not set
  or change `HTTP_PROXY`, `HTTPS_PROXY`, or `ALL_PROXY`, so tools may still use
  those if they are set.
- Keep proxy URLs that contain credentials out of prompts and diagnostics.
