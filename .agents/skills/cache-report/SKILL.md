---
name: cache-report
description: Analyze prompt-cache behavior in pcode's saved sessions and run the deterministic caching checks. Use when investigating high token use, verifying a change to plan reminders, prompt caching, compaction or message history, or when asked how caching is performing.
---

# Cache report

Saved sessions record the provider's own cache verdict for every request, so past
traffic is the cheapest regression signal for caching work: no credentials, no
spend, and real tool loops instead of a synthetic script.

Run from the repository root.

## Analyze past sessions

```sh
make cache-report                                  # latest session
uv run python scripts/cache_report.py all -n 10    # recent sessions
uv run python scripts/cache_report.py <id> -v      # per-request tokens
uv run python scripts/cache_report.py all --check  # exit 1 on a regression
```

Output is content-free: counts, digests, and token totals only, never message
text. Keep it that way -- prompts hold file contents and command output.

## Verify a change

These are deterministic and free. They assert request structure, which is the
part we control:

```sh
uv run pytest -q tests/test_planning_cache.py tests/test_reminders.py \
  tests/test_prompt_cache.py tests/test_cache_report.py
uv run ruff check .
```

A caching change is not verified until a *new* session has been recorded with it
and `cache_report.py` reads healthy. Wire tests prove the request is stable; only
the provider's reported reads prove reuse.

## Reading the result

- `read share` -- cached reads over total input. Healthy long sessions run high;
  a cold start and short sessions legitimately sit low.
- `eligible requests reused` -- the load-bearing check. Each request after a
  cacheable prefix should read most of the previous request's input.
- `Reads pinned at N tokens` -- the regression signature: the prefix stopped
  advancing while the conversation grew, so the tail was rewritten every request.
- `replaced settled history` -- compaction, retries and branch switches do this
  legitimately; anything else invalidates the cached prefix from that point.
- `duplicate plan reminder(s)` -- deduplication is not matching previously sent
  text, so history grows with repeated reminders.

## When numbers look wrong

1. Check `~/.local/state/pcode/cache-diagnostics/` for request fingerprints; they
   name the message index that moved.
2. Read `dev/prompt-caching.md` for the upstream design history and the traps
   (minimum cacheable size, metadata that does not survive a resume).
3. Confirm the session actually ran the code under test -- an editable install
   only takes effect for processes started after the change.
