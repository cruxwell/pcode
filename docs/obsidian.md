# Planning in Obsidian

Read a plan in [Obsidian](https://obsidian.md/), and talk to the pcode session
working on it without switching to the terminal. Select a paragraph and ask
about it, leave comments, then send "review my comments" or "go ahead". Your
messages go to the session that wrote the plan, so they're answered in its
repository, with its environment (direnv credentials included) and with the
whole discussion so far.

It takes two pieces: plans in your vault (`/plan --obsidian`) and the
**pcode bridge** plugin. The plugin is desktop only.

## Plans in the vault

```text
/plan --obsidian Design the export pipeline
```

writes the plan to `plans/Plan — <project> — <title>.md` in your org's vault
instead of `.pcode/plans/`. It needs no setup: with no org configured for the
directory, the plan goes in a shared `~/vaults/default`. The model is asked to
record the plan's session in its front matter (`session: 7c1d72d9-…`), which is
how the plugin finds it. If a plan is missing it, *pcode bridge: Set this
note's session* adds it.

Bare `/plan` resumes the active plan, or else the newest local one.
`/plan --obsidian` with no request resumes the active plan only when it's in
the vault, and otherwise starts a new one there. `/plan status` shows the
active path, and `/plan off` leaves the file in place. Saying "go ahead" in the
conversation starts implementation.

### Orgs and vaults

To keep work and personal plans apart, give each directory root its own vault
in `~/.config/pcode/obsidian.toml`:

```toml
vault_root = "~/vaults"   # each org's vault defaults to <vault_root>/<name>;
                          # directories outside every org use <vault_root>/default

[[org]]
name = "personal"
root = "~/p"

[[org]]
name = "acme"
root = "~/wd"
vault = "~/Documents/acme-vault"   # optional
```

The file lives in your config directory, never in a repository, so org names
stay on your machine. If you used project briefs before, rename your old
`memory.toml` to `obsidian.toml`; until you do, `/plan --obsidian` stops with
an error rather than using the shared vault. A broken config or an unwritable vault is an error, not a
silent fallback to another vault.

### Answering in the note

Answer the plan's open questions in place, or add a callout under any
paragraph:

```markdown
> [!comment] Cover the retry path too.
```

With your next message, from the terminal or from Obsidian, the model receives
a diff of your edits to the active plan. It acts on your answers and comments,
then folds them in or removes them. Edit between turns: simultaneous model and
user edits are not conflict-free.

## Installing the plugin

From a pcode checkout, with Node.js installed:

```sh
make obsidian-plugin VAULT=~/vaults/personal
```

This builds the plugin, copies it into the vault and enables it (adding it to
the vault's `.obsidian/community-plugins.json`). If Obsidian is open, reload
it. Community plugins must be turned on for the vault (*Settings → Community
plugins*). If it can't find `pcode`, set its path in the
plugin's settings. Apps opened from the Dock don't get your shell's `PATH`,
or any other variable your shell profile sets. If you set `PCODE_HOST_DIR` or
`XDG_STATE_HOME` there, Obsidian looks in the default place and finds no
sessions.

## Keep the session reachable

The plugin talks to [background sessions](sessions.md#background-sessions).
In the terminal running the plan, type `/detach` once: the terminal stays on
the session and the session can now take messages from Obsidian. Keep that
terminal open. An unwatched session [stops once idle](sessions.md#idle-hosts-stop),
and the plugin then tells you how to bring it back. Starting pcode with
`--host` (or `session_host on`) skips the `/detach`.

## What you can do

Right-click in a note, use the command palette (`pcode bridge: …`), or open
the panel from the ribbon:

- **Ask pcode…**: type a question about the selected text (or the note). It
  goes to the session's conversation as a normal message, and the reply
  streams into the pcode panel. Follow-ups are just more questions, since
  they're all one conversation. A question waits if a turn is already running.
  Enter sends; Shift+Enter starts a new line.
- **Add comment**: inserts a `> [!comment]` callout under the paragraph. pcode
  sees it, with the rest of your edits, the next time you send anything.
- **Review my comments**: asks the session to act on your comments and
  answers in the note.
- **Go ahead**: approves the plan and starts implementation (after a
  confirmation you can turn off).

Each reply in the panel can be copied or inserted into the note as a
`> [!note] pcode` callout. **Stop following** stops showing a reply; the turn
itself keeps running, and your terminal shows it as usual.

A note without `session:` can be linked by hand: *pcode bridge: Set this
note's session* lists the running sessions. That works for any note, not just
plans.

## How it works

The plugin only runs the `pcode` command: `pcode --hosts --json` to find the
session, and `pcode --attach <host> -p` with your message on stdin to send it
([Scripting a running host](sessions.md#scripting-a-running-host)). Nothing
fires when you save a note; a message goes only when you send one.
