# Side questions (`/btw`)

`/btw QUESTION` (or `/side QUESTION`, the same command) asks about what the
model is doing **while it is doing it**. The
question runs as a separate request alongside the turn, against the same
context. The turn is not interrupted or steered, and the question stays out of
the conversation unless you choose to keep it.

```text
❯ refactor the parser and make the tests pass
  ◜ Running shell · uv run pytest
❯ /btw why did you pick a recursive descent parser?
  ◈ Side question asked beside the conversation…
  ◈ Side answer ready (why did you pick a recursive descent parser?). Opening it.
```

While a side question runs it has its own spinner row in the live panel above
the editor, below the turn's, in the muted shade: the question, what the model
is doing for it, and how long it has taken. Up to three show; more fold into a
count that `/btw` expands.

The viewer opens by itself as soon as an answer is ready, since the point of a
side question is reading the answer while the turn is still running. A bare
`/btw` opens it at any other time. While an answer is still arriving, the popup
streams it.

- **↑ / ↓:** move through the questions, or scroll the answer when it has focus.
- **Enter:** read the selected thread full width, with the question list
  hidden; **Escape** brings the list back. A viewer that opens on a single
  thread starts full width, and Escape shows the list once a second arrives.
- **Tab:** move between the question list, the answer pane and the follow-up
  editor. While the [command menu](#commands) is open, Tab and Shift+Tab move
  through it instead.
- **Ctrl+R:** type a [follow-up](#following-up) to the selected answer.
- **Ctrl+S / Ctrl+T:** [bring the thread into the conversation](#keeping-a-thread),
  as a summary or merged into the tree.
- **Ctrl+Y:** copy from the selected thread with the same layout as
  [`/copy`](commands.md#slash-commands): the list holds each answered question
  in the thread and its answer, with a row for every quote and code block under
  the answer, and the pane beside it highlights exactly what Enter (or Ctrl+Y
  again) copies. The newest answer is selected first. Copying also works
  mid-stream, taking what had arrived when you pressed Ctrl+Y. Escape or Ctrl+C
  goes back to the thread without copying,
  and neither changes the thread or the follow-up draft. A thread holding a
  single plain answer is copied straight away.
- **Ctrl+O:** pick a link from the selected thread's questions and answers and
  open it in the browser, as `/links` does for the conversation.
- **Ctrl+K:** stop every running side question, keeping the records.
- **Ctrl+C:** stop the running side questions, as Ctrl+K does; with nothing
  running, close the popup.
- **Escape:** close the popup and restore the editor draft (from a full-width
  thread it first brings the list back). Enter closes it too when there is no
  list to pick from.

The `r`/`y`/`o`/`s`/`t`/`k` actions work from the list, the answer and the
follow-up editor alike. By default, hold Ctrl while pressing the letter; a
configured [shortcut prefix](commands.md#shortcut-prefix) changes these keys.
Ctrl+/ browses the current popup's help.

### Commands

The follow-up editor also takes those actions as commands, which a menu
completes as you type `/`. Enter runs the highlighted one, and the start of a
single name (`/co`) is enough on its own:

| Command | Does |
| --- | --- |
| `/copy` | Choose a question, answer, quote or code block from this thread to copy, as Ctrl+Y |
| `/links` | Open a link from the thread, as Ctrl+O |
| `/summarize [focus]` | Summarize into the conversation, keeping what *focus* says, as Ctrl+S |
| `/merge` | Merge into the conversation, as Ctrl+T |
| `/stop` | Stop running side questions, as Ctrl+K |

A follow-up that starts with a path of more than one part, such as
`/etc/hosts`, is still sent as a question; one like `/tmp` reads as a command
name, so put a word before it.

To keep the answers out of the way until you ask for them, turn auto-open off:

```
pcode config set btw_auto_open off   # Default on; applies immediately
```

With it off, a ready answer only prints its transcript notice and waits for a
bare `/btw`. Auto-open never interrupts a popup or command already using the
terminal (it queues behind it), and it does nothing when the viewer is already
open, because an open viewer follows new answers on its own.

The footer counts side questions that are `running` and answers that are
`ready` (settled but not yet opened). Ctrl+C at the prompt stops running side
questions only when nothing else is in flight, so an interrupt aimed at the turn
never throws away the side question as well.

## Following up

The viewer has an editor under the answer for asking a follow-up, so a side
question can become a short back-and-forth without leaving the popup. Press
**Ctrl+R** (or Tab to it), type, and press **Enter** to send; **Ctrl+J** (or
Shift+Enter, where the terminal reports it) adds a line. The draft grows to six
rows before it scrolls, and **PgUp / PgDn** scroll the answer while you type.
**Escape** leaves the editor and keeps the draft, returning to the list, or to
the answer when the list is hidden; from there Escape works as above. A bare
`/btw` opens on the newest thread with the cursor in this editor, so a
follow-up is just typed. When the viewer opens by itself because an answer
arrived, it starts on the list (or the answer, for a single thread) instead,
since you may be mid-keystroke at the main prompt.

A follow-up joins the selected question's **thread**. The list shows one row per
thread, with a follow-up count, and the answer pane shows the whole exchange in
order, opening on the newest question. Each follow-up:

- **continues the thread, not the conversation.** It sees what the previous
  answer saw, plus that answer, so the prompt cache covers it; what the main
  turn did since does not reach the thread.
- **runs where the thread began:** the same model, effort and conversation id,
  even if `/model` or `/effort` has changed the conversation's since. A thread
  started with `$MODEL` or `+LEVEL` keeps them; a `/btw` that fanned out to
  several models is one thread per model. A follow-up can start with one
  `$MODEL[+LEVEL]` or `+LEVEL` word of its own, read as in `/btw`, to switch
  models: `$MODEL` moves the thread to that model, and a bare `+LEVEL` keeps
  the thread's model at another effort. The next follow-up stays where this one
  went. A switched follow-up still sees the whole thread, but starts without its
  prompt cache.
- **waits for the answer before it.** Sending while the newest answer is still
  arriving is refused in the editor's title, and the draft stays. If a
  follow-up fails, the next one continues from the last answer that arrived; a
  thread with no answer at all cannot be followed up, so ask again with `/btw`.

Follow-ups are side questions in every other way: same limits, same refused
tools, same footer counts and ready notices, and nothing joins the conversation
until you [keep the thread](#keeping-a-thread). The viewer lists up to 20
threads, dropping the oldest settled thread whole; the session keeps them all
(see [saved with the session](#saved-with-the-session)).

## Keeping a thread

A thread that turned up something worth keeping can be brought into the
conversation from the viewer, in two ways. Both close the viewer, and both wait
for the running turn the way forking in `/tree` does: the conversation's history
cannot change under a turn that is about to write it back. Pressing either key
mid-turn says so in the header and does nothing else.

**Ctrl+S: Summarize into the conversation.** The editor asks for optional
instructions ("keep only the decisions", "what should change in the plan?");
press **Enter** with nothing typed to summarize as is, or **Escape** to cancel
and get your follow-up draft back. The summary is asked *in the thread*, on its
model and after its last answer, so it reuses the thread's cache. It is then
added to the conversation's current branch as one exchange: a message naming the
thread's questions (and your instructions), answered by the summary. The next
turn reads it like any earlier reply, and the rest of the history is untouched,
so the conversation's cache still covers everything before it. While the summary
runs, the footer shows it like `/compact`, prompts you send wait behind it, and
Ctrl+C cancels it with the conversation unchanged.

**Ctrl+T: Merge into the conversation.** The thread is already a branch of the
[conversation tree](conversation-tree.md), forked from the point where it was
asked (see [saved with the session](#saved-with-the-session)). Merging moves the
conversation onto it when that loses nothing. What happens depends on what
happened since:

- **Nothing:** the thread was asked while idle and the conversation has not
  moved, so the thread is simply its continuation. The conversation switches to
  its last answer and the transcript shows the questions and answers.
- **Anything else** (the thread was asked mid-turn, or turns, `/compact` or a
  `/tree` switch came after): the thread stays a branch, and the conversation
  stays where it is. Open `/tree` to switch to it. Moving there automatically
  would drop whatever the conversation did after the question was asked.

Merged history is the thread's exactly, framing included, so the model can
tell those exchanges were side questions. `/resend` refuses on a merged
question or a summary, since resending would answer it again as a real turn;
send a message instead. A thread asked before `/new` or `/resume` belongs to
that other conversation and cannot be kept here. The viewer's list marks a kept
thread `merged` or `summarized`; summarizing it again adds another summary.

## What a side question can and cannot do

A side question runs on the **conversation's own agent**: the same model,
instructions, tools, enabled MCP servers and model settings as the turn beside
it (unless you [choose another model](#choosing-the-model)). The provider's
prompt cache therefore covers everything but the question itself, so a side
question costs little more than the question.

Tools work as they do in a turn. The model can read files, search the web, use
MCP tools, and run shell commands or edit files if the question calls for it,
with the usual permission checks. The exceptions are the tools that change the
conversation itself: the plan (`write_plan`, `add_task`, `update_task_status`
and the rest; `read_plan` is fine) and delegation (`delegate_task`,
`integrate_task`, `discard_task`). A call to one is refused, and the model
carries on answering. If the answer implies work, send it as a normal message.

Nothing about a side question joins the conversation:

- no change to the model's history, so the next real turn is unaffected;
- no change to where the conversation is: its tree node is a branch beside it
  (see below), and resuming or replaying the session draws the conversation
  without it.

That holds until you [keep the thread](#keeping-a-thread), which is the one
deliberate way in.

### Saved with the session

Each question is saved in the session when its answer settles (answered,
stopped or failed), as a node in the [conversation tree](conversation-tree.md):
a `btw:` row forked from where the thread was asked, with each follow-up under
the answer it continued. That happens even while a turn is running, without
touching the turn. So a thread outlives the session: after `/resume`, `/btw`
lists it again, read, and a follow-up continues from its saved history.
`/tree` shows it as a branch you can switch to, like any turn, and the
session's search finds it. A question still running when pcode exits is not
saved.

What it does share is the context it was asked against and the session's token
totals: the request really happened, so `/status` counts it.

## Choosing the model

Leading `$` words pick the model a side question runs on; the rest of the line
is the question.

```text
❯ /btw $anthropic:claude-sonnet-5 is this migration safe?
❯ /btw $claude:claude-opus-5-5 $openai-codex:gpt-6-astra second opinions on the plan?
```

Typing `$` in a `/btw` line completes model names from the same catalog as the
`/model` picker, matching any part of the name (`$opus` finds
`anthropic:claude-opus-…`). It works for each `$` word in the leading run; once
the question starts, `$` is ordinary text. A normal prompt can start with one
`$PROVIDER:MODEL[+LEVEL]` or `+LEVEL` word to
[pick its model](commands.md#slash-commands) for that turn alone, and `$`
completes there too.

- **No `$`:** the conversation's model, sharing its prompt cache as described
  above. Naming the conversation's own model is the same thing.
- **Another model:** the same agent, tools, history and framing, but the
  model's own settings: its defaults and its saved `/effort`, never the
  conversation model's. It starts **without the conversation's cache**, so its
  first request pays full price for the whole conversation.
- **Several models:** one side question per model, started together. Each has
  its own row, answer, error and `errors.log` entry, and one failing does not
  affect the others. Rows, the viewer list, and the ready notices carry a short
  model label (the name without its provider, unless two would look the same).
  Repeated models collapse to one, and at most 4 models can be named at once.

A name that cannot be resolved (unknown provider, missing credentials or SDK)
fails the whole `/btw` command before any side question starts. `/btw $MODEL`
with no question is an error too.

### Choosing the effort

A `+LEVEL` suffix sets the reasoning effort for one side question, using the
levels `/effort` accepts: `low`, `medium`, `high`, `xhigh` and `default`. A bare
`+LEVEL` word asks on the conversation's model; on a `$` word it applies to
that model alone.

```text
❯ /btw +low what was the last file edited?
❯ /btw $openai:gpt-5+high $anthropic:claude-opus-4-5+low which approach is safer?
❯ /btw +low +xhigh is this lock ordering right?
```

- **No suffix:** the model's usual effort, as above.
- **The conversation's model:** it stays on the conversation's own path with
  that effort in place of the current one, for this question only. A different
  effort can miss the conversation's prompt cache.
- **Another model:** the effort replaces that model's saved `/effort`.
- **`default`:** drops the effort setting, so the provider's own default
  applies, exactly as `/effort default` does.

The same model at two efforts is two side questions; labels show the effort
(`gpt-5 · high`, or just `low` on the conversation's model) so their answers
stay apart. The effort is only read from the leading words, and only from the
last `+` in a model word when a real level follows, so model ids that contain
`+` still work. An unknown level fails the command, and so does an effort on a
model `/effort` cannot set (it supports OpenAI/Codex models and the Anthropic
and Claude Code models whose profile has effort control). Typing `+` in the
leading words completes the levels.

## Which context it sees

A side question sees what the running turn is working with right now, not the
state before the turn started, up to the last completed step. A tool call that
has not returned yet is left out, and so is the text streaming beside it.

Side questions are bounded: 12 model requests and 300 seconds each. They are not
retried, and they do not survive exiting pcode.

When one fails or times out, the viewer shows the error and the traceback is
appended to the session's `errors.log`, the same file failed turns write to,
under a `run aside <id>` header with the question. A stopped side question
writes nothing.

## Parallel work and `/tree`

`/tree` opens while a turn is running, but only to read: the header says
`read-only while working` and Enter does not switch context, since the running
turn would overwrite the switch when it finishes. Browse the tree now, fork when
the turn ends, and use `/btw` to ask about a branch in the meantime.

Several side questions can run at once, each with the context available when it
was asked. Two conversation turns cannot run in parallel, from `/tree` or
anywhere else; `/btw` is the parallel work that is possible. A shell command a
side question runs is a real job in the shared job list, though, so while a turn
is editing files, prefer questions that only need reading.
