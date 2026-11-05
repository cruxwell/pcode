# A mid-turn question answered only in thinking

## What happened

During a long turn the user steered in a question ("give me a breakdown of
callie-qa"). The agent replied in visible text, "I'll answer the callie-qa
question first, then get back to the code change", and ran two queries. It then
worked out the whole breakdown **in a thinking block**, which ended with "I'll
put the full breakdown in the final message". It never wrote it as text, went
back to the original task, and the final message left it out. The user saw a
promise and nothing else.

Rendering was not at fault. The journal (`transcript.jsonl`) and the stored
model messages hold no text block with the answer; it exists only as thinking.

A side question asked about it ("why did it never answer?") made things worse.
The side agent sees the main run's full history, thinking included. It quoted
the thinking as "a text block between tool calls" the user had been shown, and
wrote the first version of this note on that false premise.

## What changed

- **Steering reminder** (`pcode.steering.STEERING_REMINDER`): every delivery of
  steering is followed by a short note saying a question in it gets a visible
  answer now, because thinking is not shown and a deferred answer tends to get
  lost. It rides only requests that carry steering, after the history, so it
  costs nothing otherwise and leaves the cached prefix alone. The journal keeps
  only the user's own text.
- **Side-question framing** (`pcode.aside.ASIDE_FRAMING`): says the user may
  not see thinking, so don't assume they did.

## Side questions that "failed with a model API error"

The same session had two side questions fail with `Run failed
(UsageLimitExceeded). Check the model string, provider credentials, and
connectivity.` Neither was a provider error. Both asked for investigation
("find the root cause", "write a markdown file") and ran into pcode's own
12-request cap for side questions (`ASIDE_REQUEST_LIMIT`), which throws away
everything the run found.

- `error_message` now names a usage limit as one, not a provider failure.
- The side-question request cap is gone; the 300-second deadline is the only
  bound. That matches the worker, whose cap was dropped for the same reason
  (commit 18c1579): the run is visible and cancellable, and a cap discarded the
  answer of a run that was still making progress. An early version of this fix
  warned the agent two requests before the cap instead; removing the cap made
  that unnecessary.

## Not done

The model can still ignore the reminder; nothing checks that a steered question
got a visible answer. If it keeps happening, a check at turn end could compare
steering messages against the text written after them.
