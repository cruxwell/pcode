# Project briefs

Ask "where are we on billing?" and get the answer from one note: what the
project is, where it's at, how it works, what's wrong with it, and what's
next. The bundled `basic_memory` extension keeps one **brief** per project in
an [Obsidian](https://obsidian.md/) vault per org, and the model keeps the
briefs current as you work. A **plan** uses the same format, describing the
project as it would be after a change, so it reads exactly like the brief it
will become.

It ships off. Turn it on with:

```
/extensions on basic_memory
```

## Orgs and vaults

An org is a directory root. Every directory under it belongs to that org, and
each org has its own vault, so nothing links across orgs. Create
`~/.config/pcode/memory.toml`:

```toml
vault_root = "~/vaults"   # each org's vault defaults to <vault_root>/<name>
auto_sync = false         # default for /brief sync in every org

[[org]]
name = "personal"
root = "~/p"

[[org]]
name = "acme"
root = "~/wd"
auto_sync = true          # optional, per org
```

The file lives in your config directory, never in a repository, so org names
stay on your machine. Keep vaults out of `~/basic-memory`: that's
[Basic Memory](https://docs.basicmemory.com/)'s own default project, and it
won't register a project inside another one. Outside every org root, the
extension adds nothing but a `/brief` command that explains how to set it up.

Then run `/brief setup` from a directory in the org. It writes the format
note, a `Home.md` dashboard and the note styling into the vault, and registers
the vault with Basic Memory if `bm` is installed. Open the folder as a vault in
Obsidian and pin `Home.md`.

## Briefs

A brief lives at `briefs/<Name>.md`. It covers whatever scope you name: a
repository, a subsystem, or an initiative that spans several repositories.
It's built from evidence: the code (README, layout, main modules, git
history) and the sessions that worked there.

```
/brief new coder-pydantic
/brief new "Repo onboarding" focus on the rollout to the first teams, skip the infra
```

What to write and how is in the vault's `Brief format.md`. The model reads it
before every write, so editing it changes how briefs are written from then
on:

- **Sections:** TL;DR, *Where it's at*, *What it does*, *How it works*
  (decisions with their reasons and rejected alternatives), *Shortcomings &
  risks*, *Next steps*, *Open questions* and a *Log*.
- **Properties:** `health`, `stage`, `tldr` and `next` feed the dashboard;
  `priority` is yours to set.
- **The rule:** rewrite, don't append. The body is always the current truth,
  and only the *Log* grows.

Sessions and briefs don't map one to one. A session can change several
briefs, or none, and the model names the briefs it changed.

## Keeping briefs current

- **`/brief sync on`** reminds the model, with each of your messages, to
  update the affected briefs when something a newcomer would need changes: a
  milestone, a decision, a new shortcoming. Routine progress doesn't count.
  `/brief sync off` stops it. The default comes from `auto_sync`.
- **`/brief update [focus]`** asks for an update now.
- **`/brief import`** lists this directory's recent sessions.
  `/brief import ID [ID ...] [-- focus]` folds them into the briefs they
  change. The model gets a trimmed digest of each session and reads full turns
  where it needs them.
- **`/brief`** lists the org's briefs by priority, with their open plans.

## Plans

`/plan` keeps plans in `.pcode/plans/` by default, with or without this
extension. Add `--obsidian` to review an architecture proposal in Obsidian and
answer its open questions there instead:

```text
/plan --obsidian Design the export pipeline
```

Configure an org and vault in `memory.toml` as described [above](#orgs-and-vaults).
You do not need to enable `basic_memory` or run `/brief setup`. The command creates
only the missing `Brief format.md` and `briefs/` directory, keeping your existing
format unchanged. It does not set up dashboards, styling, session imports or sync,
and it does not open Obsidian automatically. Missing or invalid configuration is
an error, not a silent fallback to a local plan.

The plan lives at `briefs/Plan — <project> — <title>.md`: a *proposed brief* with a
banner saying it isn't implemented yet. It links an existing brief when there is
one, but can stand alone. With memory off, the model maintains only the plan, not
project briefs. Saying "go ahead" in the conversation marks it approved and starts
implementation. When the work ships, the model marks it implemented. The plan
stays as the record of why the change was made.

Where a plan lives is chosen per plan, never by the memory setting. Bare `/plan`
resumes the active plan, or else the newest local one. `/plan --local` or
`/plan --obsidian` with no request resumes the active plan only when it is at that
destination, and otherwise starts a new one there without moving the old one. `/plan status` shows the
active path, and `/plan off` leaves the file in place.

With memory enabled, approval and completion also update the linked brief. If
you ran `/brief setup`, the proposed and approved notes have yellow and blue tints.

## Answering in the note

Answer a plan's or brief's open questions in place, or add a callout under any
paragraph:

```markdown
> [!comment] Cover the retry path too.
```

With your next terminal message, the model receives a diff of your edits. With
memory off, only the active Obsidian plan is watched. With memory on, this covers
vault notes the model has read or written in this session. It acts on your answers
and comments, removes them, and records what changed in the note's *Log*.

Edit between turns: simultaneous model and user edits are not conflict-free.
Saving a note does not trigger a model turn or start implementation by itself.

## Cost

There are no extra tools. The model edits the notes with its normal file
tools, and the extension adds one short paragraph to the system prompt in an
org directory. While `/brief sync` is on, each of your messages carries a
two-line reminder. Your edits are sent only when you've made some.
