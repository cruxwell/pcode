---
type: format
tags: [brief]
---
# Brief format

The model reads this note before it writes or updates a **brief** or a **plan**. Edit it to change how they're written; the next update follows your version.

A **brief** explains one project as it is now: what it is, where it's at, how it works, what's wrong, what's next. A **plan** is a *proposed brief*: the same format, describing the project as it would be once the change is implemented.

## The rules

1. **One note answers the question.** Someone who missed every session (the newcomer test) must understand from the brief alone what the project is, what state it's in, how it works, what's broken and what to do next.
2. **Rewrite, don't append.** The body is always the current truth. Only the *Log* is append-only.
3. **Update when it matters.** A milestone, a decision, a new or fixed shortcoming, a changed next step. Not routine progress.
4. **Evidence, not memory.** Check claims against the code, git and sessions. When they disagree with the brief, change the brief, and cite what you used in `sources`.
5. **Change only what's out of date.** Edit the sections that are wrong and leave the rest exactly as it is. The user edits these notes too.
6. **Never change `priority`** unless the user asks. Only create a brief when the user asks (`/brief new`).
7. **No secrets.** Don't open files that hold credentials (`.env`, `*secret*`, token files, kubeconfigs), and never copy a token, key or password into a note. Name the file instead.

## Where things live

- Briefs: `briefs/<Name>.md`. The name is the project (`coder-pydantic`, `Repo onboarding`).
- Plans: `briefs/Plan — <project> — <title>.md`, with `project:` linking the brief they change.
- This vault holds one org. Never link notes in another org's vault.

## Scope: briefs, sessions and code are many-to-many

- A brief covers a **scope** you name: a repo, a subsystem, an initiative across repos. It's recorded in `scope:` (one line) and `dirs:` (org-relative directories).
- One session can change several briefs, or none. Update every brief it materially changed, and name them in your reply. Ask only when it's unclear whether a change belongs in a brief at all.
- To find a project's brief, match the current directory against the briefs' `dirs`.
- A brief can be built from the code itself (README, layout, main modules, git history), from sessions, or from both.
- When a brief outgrows its budgets, propose splitting it as a plan.

## Properties

| Key | Brief | Plan | Notes |
|---|---|---|---|
| `type` | `brief` | `plan` | |
| `state` | `live` | `proposed` → `approved` → `implemented` (or `dropped`) | |
| `cssclasses` | none | `[proposed]`, `[approved]` or `[implemented]`, matching `state` | Colours the note in Obsidian |
| `project` | | link to the brief, e.g. `"[[briefs/coder-pydantic]]"` | None for a brand-new project |
| `scope` | one line | one line | What the note covers |
| `dirs` | org-relative directories the note covers | same | How a session finds its brief |
| `health` | `on-track`, `at-risk` or `blocked` | same | |
| `stage` | `idea`, `design`, `prototype`, `beta` or `shipped` | the stage after the change | |
| `priority` | 1 (highest) to 5 | | Set by the user |
| `tldr` | one sentence | one sentence | Shown on the dashboard |
| `next` | the single next action | same | Shown on the dashboard |
| `reviewed` | date the whole brief was last checked | | Briefs only |
| `created` | | date the plan was written | Plans only |
| `plans` | links to open plans | | |
| `sources` | session IDs, plus code paths or commit ranges | same | |

## Sections

| Section | Answers | Budget |
|---|---|---|
| **TL;DR** (`> [!abstract]`) | What · Where · Next (a plan adds the cost to build) | 3 lines |
| **Where it's at** | Status line, then what changed since the last review. A plan says what's wrong with today and what's been decided. | ≤ 6 bullets |
| **What it does** (*What it will do*) | What a user gets, in their terms | ≤ 6 bullets |
| **How it works** (*How it will work*) | A Mermaid diagram, the pieces, key decisions each with *why* and *rejected* alternatives, and **How we know it works** | 1 diagram, ≤ 12 bullets |
| **Shortcomings & risks** | A table: problem, evidence, impact (🔴 🟠 🟡). In a plan, a mitigation instead of evidence. | ≤ 8 rows |
| **Next steps** | Ordered checkboxes, each with *done when* | ≤ 6 |
| **Open questions** | `> [!question]` callouts, each with *My lean* and an empty `**Answer:**` line | ≤ 5 |
| **Log** | Dated one-liners, newest first, linking sessions and plans | Append-only |
| **Sources** | Collapsed `> [!info]-` callout | |

Put retired decisions in a collapsed `> [!note]-` callout with the reason, not in the body. Briefs with open plans show them in a `> [!todo] Open plans` callout under the TL;DR.

## Plans

- A plan starts with a banner: `> [!todo] PROPOSED · not implemented`, saying it describes the project as it would be after the change.
- For a change to an existing project, write only the sections that change. Other sections say *Unchanged, see [[brief]]*.
- Transitions:

| To | The plan | The brief |
|---|---|---|
| `proposed` | Banner, `cssclasses: [proposed]` | An *Open plans* line: 📝 proposed |
| `approved` | `state: approved`, `cssclasses: [approved]`, banner `> [!info] APPROVED · being implemented` | ✅ approved |
| `implemented` | `state: implemented`, `cssclasses: [implemented]`, banner `> [!success] IMPLEMENTED on <date>`, linking the brief. It stays as the record of why. | The changed sections rewritten in present tense, a *Log* line, and the plan removed from *Open plans* and `plans` |
| `dropped` | `state: dropped`, with the reason in its *Log* | Removed from *Open plans*, with a *Log* line |

## Feedback from the user

The user answers open questions in place and adds `> [!comment]` callouts. Their edits arrive as a diff with their next message.

- **An answer:** fold it into the right section (a plan records decisions under *Where it's at*; a brief, under *How it works*), remove the question, and log it.
- **A comment:** act on it, remove the callout, and log what changed.
