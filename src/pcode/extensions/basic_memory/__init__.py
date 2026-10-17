"""Project briefs: one living note per project, in an Obsidian vault per org.

Off by default (`/extensions on basic_memory`). Each org is a directory root
(`~/c`, `~/p`, ...) with its own vault, configured in `memory.toml` in pcode's
config directory, which is never in a repository, so org names stay local.

A **brief** explains a project as it is: what it is, where it's at, how it
works, what's wrong, what's next. A **plan** is a proposed brief in the same
format; `/plan --obsidian` writes it into the vault. How both
are written lives in the vault's `Brief format.md`, which the model reads
before writing, so the user can change the format without touching code.

There are no memory tools: the model edits the notes with its normal file
tools. What this module adds is only:

- where the vault is, in the system prompt (static, so it stays cached);
- `/brief`: list, `new NAME [focus]`, `update [focus]`, `import ID...`,
  `sync on|off`, `setup`, each a prompt for the model or a quick local action;
- a per-message reminder while `/brief sync on`: update briefs when something
  a newcomer would need changes, not as you go;
- the user's own edits to notes the model has read or written, sent back as a
  diff with their next message, so answers and `> [!comment]` callouts in
  Obsidian reach the model without being retyped.

Known limits: an edit the user makes mid-turn to a note the model then edits
again is folded into the model's edit and not reported.
"""

import asyncio
import difflib
import json
import re
import shlex
import shutil
import tempfile
import tomllib
from dataclasses import dataclass
from pathlib import Path

from pydantic_ai.messages import ModelRequest, ToolReturnPart, UserPromptPart

from pcode.conversation_ids import base_conversation
from pcode.diagnostics import redact
from pcode.steering import run_request

DEFAULT_ENABLED = False  # Opt-in: /extensions on basic_memory.

NAME = "basic_memory"
CONFIG_NAME = "memory.toml"
# Not ~/basic-memory: that's Basic Memory's own default project, and it refuses
# to register a project nested inside another.
DEFAULT_VAULT_ROOT = "~/vaults"
# `/plan --obsidian` outside every configured org: one shared vault under vault_root.
DEFAULT_VAULT = "default"
BRIEFS = "briefs"
FORMAT_NOTE = "Brief format.md"
SNIPPET = "memory-states"
STATE_FILE = "memory.json"
TAG = "<memory>"
FILES = Path(__file__).parent
NOTE_TOOLS = frozenset({"read_file", "write_file", "edit_file"})
DIFF_LINES = 80

SYNC = (
    "Brief sync is on. If this turn changed something a newcomer to a project would need (a "
    "milestone, a decision, a new or fixed shortcoming, a changed next step), update each "
    "affected brief per the brief format before your final reply, and name the briefs you "
    "changed. Don't update for routine progress."
)
EDITED = (
    "These notes changed since you last read or wrote them, most likely edited by the "
    "user. Treat answers and "
    "`> [!comment]` callouts as feedback: act on each, then fold the answer in or remove the "
    "comment, and add a Log line, as the brief format describes."
)
INSTRUCTIONS = """\
Project briefs for org "{org}" live in {briefs}/: one Markdown note per project explaining \
what it is, where it's at, how it works, what's wrong and what's next. Plans are proposed \
briefs in the same folder. This workspace is `{dir}` in that org. Before creating or updating \
a brief or plan, read {format} and follow it. Find a project's brief by matching this \
directory against the `dirs` property of the notes in that folder, and edit the notes with \
read_file and edit_file directly. When asked where a project stands, start from its brief. \
Only create a brief when the user asks."""


# --- Configuration -------------------------------------------------------------------


@dataclass(frozen=True)
class Org:
    name: str
    root: Path
    vault: Path
    auto_sync: bool = False


def config_path() -> Path:
    from pcode.preferences import config_dir

    return config_dir() / CONFIG_NAME


def safe_name(text: str, limit: int = 90) -> str:
    """A title as a file name Obsidian can link to: no path or link syntax characters."""
    text = re.sub(r"\.md$", "", text.strip(), flags=re.IGNORECASE)
    text = re.sub(r'[\\/:*?"<>|#^\[\]\n\r\t]+', " ", text)
    text = re.sub(r"\s+", " ", text).strip(" .")
    if len(text) > limit:  # Cut at a word boundary when there is one.
        cut = text[: limit + 1]
        text = cut.rsplit(" ", 1)[0] if " " in cut else text[:limit]
    return text.rstrip(" .")


def read_config(path: Path) -> dict:
    """`memory.toml` as a table; a missing file is empty, a broken one raises ValueError."""
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise ValueError(f"{path}: {error}") from None


def vault_root(data: dict, path: Path) -> Path:
    value = data.get("vault_root", DEFAULT_VAULT_ROOT)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{path}: vault_root must be a nonempty path string")
    return Path(value).expanduser()


def load_orgs(path: Path | None = None) -> list[Org]:
    """Orgs from `memory.toml`; a missing file means none, a broken one raises ValueError."""
    path = path or config_path()
    data = read_config(path)
    default_sync = bool(data.get("auto_sync", False))
    root = vault_root(data, path)
    entries = data.get("org", [])
    if not isinstance(entries, list):
        raise ValueError(f"{path}: org must be an array of [[org]] tables")
    orgs = []
    for entry in entries:
        if not isinstance(entry, dict) or any(
            not isinstance(entry.get(key), str) or not entry[key].strip()
            for key in ("name", "root")
        ):
            raise ValueError(f"{path}: every [[org]] needs a name and a root")
        if "vault" in entry and (not isinstance(entry["vault"], str) or not entry["vault"].strip()):
            raise ValueError(f"{path}: vault must be a nonempty path string")
        name = str(entry["name"])
        if safe_name(name) != name:
            raise ValueError(f"{path}: org name {name!r} must be a plain file name")
        orgs.append(
            Org(
                name=name,
                root=Path(str(entry["root"])).expanduser().resolve(),
                vault=Path(str(entry.get("vault") or root / name)).expanduser().resolve(),
                auto_sync=bool(entry.get("auto_sync", default_sync)),
            )
        )
    return orgs


def find_org(orgs: list[Org], path: Path) -> Org | None:
    """The org whose root holds `path`; the deepest root wins when roots nest."""
    path = path.resolve()
    matches = [org for org in orgs if path == org.root or org.root in path.parents]
    return max(matches, key=lambda org: len(org.root.parts), default=None)


@dataclass(frozen=True)
class Place:
    """Where a session happened: its org, repo label and org-relative dir."""

    org: Org
    repo: str | None
    dir: str


def locate(orgs: list[Org], workspace: Path, checkout: Path | None) -> Place | None:
    """Label `workspace`. `checkout` is its repository's main checkout, if any.

    A worktree counts as its main checkout, so every session in a repository
    carries the same dir. A directory under an org root but outside any repo
    (a work root, a notes folder) is labelled by itself, with no repo.
    """
    workspace = workspace.resolve()
    org = find_org(orgs, checkout) if checkout else None
    base = checkout if org else workspace
    org = org or find_org(orgs, workspace)
    if org is None:
        return None
    repo = checkout.name if checkout and base == checkout and checkout != org.root else None
    relative = base.relative_to(org.root).as_posix()
    return Place(org, repo, relative or ".")


def project_checkout(path: Path) -> Path | None:
    from pcode.worktree import project_checkout as checkout

    return checkout(path)


def place_for(workspace: Path) -> Place | None:
    """The org place of `workspace`; a broken config raises ValueError."""
    return locate(load_orgs(), workspace, project_checkout(workspace))


# --- Notes ---------------------------------------------------------------------------


def frontmatter(text: str) -> dict[str, str]:
    """Top-level `key: value` lines of a note's frontmatter, as text. Enough for listing."""
    match = re.match(r"---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|$)", text, re.S)
    values = {}
    for line in (match.group(1) if match else "").splitlines():
        found = re.match(r"^([A-Za-z0-9_-]+):[ \t]*(.*)$", line)
        if found:
            values[found.group(1)] = found.group(2).strip().strip("\"'")
    return values


def notes(org: Org) -> list[tuple[Path, dict[str, str]]]:
    """Briefs and plans in an org's vault, briefs by priority then plans by name."""
    folder = org.vault / BRIEFS
    found = []
    for path in sorted(folder.glob("*.md")) if folder.is_dir() else []:
        try:
            meta = frontmatter(path.read_text(encoding="utf-8"))
        except OSError:
            continue
        if meta.get("type") in ("brief", "plan"):
            found.append((path, meta))

    def order(item):
        meta = item[1]
        priority = meta.get("priority", "9")
        return (meta["type"] != "brief", priority if priority.isdigit() else "9", item[0].stem)

    return sorted(found, key=order)


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)


def edit_diff(path: Path, before: str, after: str) -> str:
    lines = difflib.unified_diff(
        before.splitlines(), after.splitlines(), str(path.name), str(path.name), n=1, lineterm=""
    )
    lines = list(lines)[2:]  # The ---/+++ header names the file once, below.
    if len(lines) > DIFF_LINES:
        lines = [*lines[:DIFF_LINES], f"... {len(lines) - DIFF_LINES} more diff lines"]
    return "\n".join(lines)


def is_side(ctx) -> bool:
    """Side questions and shared workers must not consume the main conversation's edits."""
    from pcode.aside_guard import AsideGuard

    if getattr(getattr(ctx, "agent", None), "name", None) == "worker":
        return True
    capabilities = getattr(ctx, "capabilities", None) or {}
    return any(isinstance(c, AsideGuard) for c in capabilities.values())


class NoteFeedback:
    """Snapshots shared by broad memory feedback and active-plan-only feedback."""

    def __init__(self):
        self.seen: dict[Path, str] = {}

    def observe(self, path: Path) -> None:
        try:
            self.seen[path] = path.read_text(encoding="utf-8")
        except OSError:
            self.seen.pop(path, None)

    def edits(self, only: Path | None = None) -> list[str]:
        changed = []
        for path, before in list(self.seen.items()):
            if only is not None and path != only:
                continue
            try:
                after = path.read_text(encoding="utf-8")
            except OSError:
                continue
            if after != before:
                self.seen[path] = after
                changed.append(f"`{path}`:\n```diff\n{edit_diff(path, before, after)}\n```")
        return changed


# --- Vault setup ---------------------------------------------------------------------


def setup_vault(org: Org) -> tuple[list[str], list[str]]:
    """Write the vault's shared files that are missing. Returns (written, kept)."""
    files = {
        FORMAT_NOTE: FILES / "Brief format.md",
        "Home.md": FILES / "Home.md",
        "views/Briefs.base": FILES / "Briefs.base",
        f".obsidian/snippets/{SNIPPET}.css": FILES / f"{SNIPPET}.css",
    }
    written, kept = [], []
    for relative, source in files.items():
        target = org.vault / relative
        if target.exists():
            kept.append(relative)
        else:
            write_text(target, source.read_text(encoding="utf-8"))
            written.append(relative)
    (org.vault / BRIEFS).mkdir(parents=True, exist_ok=True)
    # Turn the snippet on. A running Obsidian may rewrite this file; then the
    # user enables it under Appearance > CSS snippets.
    appearance = org.vault / ".obsidian" / "appearance.json"
    try:
        data = json.loads(appearance.read_text())
    except OSError, ValueError:
        data = {}
    snippets = data.get("enabledCssSnippets") or []
    if SNIPPET not in snippets:
        data["enabledCssSnippets"] = [*snippets, SNIPPET]
        write_text(appearance, json.dumps(data, indent=2))
    return written, kept


def registered_in_basic_memory(vault: Path) -> bool | None:
    """Whether Basic Memory's config lists `vault` as a project; None when unreadable."""
    try:
        config = json.loads((Path.home() / ".basic-memory" / "config.json").read_text())
    except OSError, ValueError:
        return None
    projects = config.get("projects") or {}
    paths = (entry.get("path") if isinstance(entry, dict) else entry for entry in projects.values())
    return any(path and Path(path).expanduser().resolve() == vault for path in paths)


# --- Plans (used by the bundled plan_mode extension) -----------------------------------


@dataclass(frozen=True)
class PlanLocation:
    directory: Path
    project: str
    guidance: str

    def path_for(self, request: str) -> Path:
        words = " ".join(request.split("\n", 1)[0].split()[:8])
        title = safe_name(words, 70) or "plan"
        title = title[:1].upper() + title[1:]
        return self.directory / f"Plan — {self.project} — {title}.md"


def enabled() -> bool:
    from pcode.ext import name_list

    return NAME in name_list("extensions_on") - name_list("extensions_off")


def watches(workspace: Path) -> bool:
    """Whether this extension tracks note edits here: on, and an org covers `workspace`."""
    try:
        return enabled() and place_for(workspace) is not None
    except ValueError:  # setup() reports the broken config and registers nothing.
        return False


def plan_guidance(directory: Path) -> str:
    """Guidance also works for a saved absolute marker without current org configuration."""
    return (
        f"- This plan is a proposed brief: re-read `{directory.parent / FORMAT_NOTE}` before "
        "creating or revising it and follow its Plans section (type: plan, state: proposed, "
        "cssclasses: [proposed], the PROPOSED banner). Link an existing project brief via "
        f"`project:` if one in `{directory}` matches this workspace's `dirs`. "
        "If none exists, write a standalone plan; do not create a project brief."
    )


def plan_location(workspace: Path) -> PlanLocation:
    """The vault for `/plan --obsidian`, whether or not this extension is on.

    A workspace no org covers uses the shared `default` vault under `vault_root`,
    so planning in Obsidian needs no configuration. Raises ValueError with setup
    guidance when the config is broken or the vault isn't writable.
    """
    try:
        path = config_path()
        place = place_for(workspace)
        if place:
            vault = place.org.vault
            project = place.repo or (Path(place.dir).name if place.dir != "." else place.org.name)
        else:
            vault = (vault_root(read_config(path), path) / DEFAULT_VAULT).resolve()
            project = (project_checkout(workspace) or workspace.resolve()).name
        # Planning needs neither the dashboard nor Obsidian settings or sync.
        format_note = vault / FORMAT_NOTE
        if (format_note.exists() or format_note.is_symlink()) and not format_note.is_file():
            raise ValueError(f"{format_note} must be a file")
        briefs = vault / BRIEFS
        briefs.mkdir(parents=True, exist_ok=True)
        # Existing directories and format notes don't prove new plans can be written.
        with tempfile.NamedTemporaryFile(dir=briefs, prefix=".pcode-plan-") as probe:
            probe.write(b"plan write probe\n")
            probe.flush()
        if not format_note.exists():
            write_text(format_note, (FILES / FORMAT_NOTE).read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise ValueError(
            f"/plan --obsidian: {error}. Fix {config_path()} (orgs and vault_root) or make "
            "the vault writable, then retry (or use /plan --local)."
        ) from None
    return PlanLocation(
        vault / BRIEFS,
        safe_name(project, 40) or (place.org.name if place else DEFAULT_VAULT),
        plan_guidance(vault / BRIEFS),
    )


# --- Sessions ------------------------------------------------------------------------


def find_session(root: Path, selector: str):
    from pcode.sessions import list_sessions

    selector = selector.strip()
    if len(selector) < 8:
        raise ValueError("Give at least 8 characters of a session ID.")
    matches = [info for info in list_sessions(root) if info.id.startswith(selector)]
    if len(matches) != 1:
        raise ValueError(
            f"No session matches {selector}."
            if not matches
            else f"{selector} matches {len(matches)} sessions; give more characters."
        )
    return matches[0]


def session_place(orgs: list[Org], info) -> Place | None:
    # `project` is the main checkout pcode found when the session began; None
    # means the workspace was not in a repository, so there's no git to ask.
    checkout = Path(info.project) if info.project else None
    return locate(orgs, Path(info.workspace), checkout)


def digest(info, root: Path, max_chars: int = 16000) -> str:
    """A session's prompts, replies and tool calls, trimmed, with turn IDs for read_session."""
    from pcode.sessions import session_records

    turns: list[dict] = []
    # By run id: a side question's records land in the middle of a running turn's.
    by_run: dict[str, dict] = {}
    current: dict | None = None
    for record in session_records(info, root):
        kind = record.get("kind")
        run_id = record.get("run_id")
        if kind == "turn_started" and isinstance(record.get("prompt"), str):
            turn = {"id": run_id or "", "prompt": record["prompt"], "parts": []}
            turns.append(turn)
            if isinstance(run_id, str):
                by_run[run_id] = turn
            if not record.get("side"):
                current = turn
            continue
        turn = (by_run.get(run_id) if isinstance(run_id, str) else None) or current
        if turn is None:
            continue
        if kind == "Message" and isinstance(record.get("markdown"), str):
            turn["parts"].append(record["markdown"])
        elif kind == "ToolSummary":
            detail = record.get("command") or record.get("detail") or ""
            turn["parts"].append(f"[{record.get('name', 'tool')}] {str(detail)[:160]}")
    per_turn = max(600, max_chars // max(1, len(turns)))
    blocks = [
        f"Session {info.id} · {info.name or info.title or '(untitled)'} · {info.workspace} · "
        f"{info.created[:10]} to {info.updated[:10]} · {len(turns)} turns"
    ]
    for turn in turns:
        body = "\n".join(turn["parts"])
        prompt = turn["prompt"][: per_turn // 3]
        body = body[: per_turn - len(prompt)] + (" …" if len(body) > per_turn - len(prompt) else "")
        blocks.append(f"### Turn {turn['id']}\nUser: {prompt}\n{body}")
    return redact("\n\n".join(blocks))[:max_chars]


# --- The extension -------------------------------------------------------------------


def setup(pcode) -> None:
    try:
        place = place_for(pcode.workspace)
    except ValueError as error:
        pcode.ui.notify(f"basic_memory: {error}", "error")
        return
    if place is None:
        if not pcode.is_worker:
            pcode.register_command(
                "/brief",
                "Project briefs (needs an org in memory.toml)",
                lambda _: pcode.ui.notify(
                    f"No memory org covers {pcode.workspace}. Add one to {config_path()}:\n"
                    '[[org]]\nname = "personal"\nroot = "~/p"'
                ),
            )
        return
    org = place.org
    vault = org.vault
    briefs = vault / BRIEFS
    pcode.instructions(
        INSTRUCTIONS.format(org=org.name, briefs=briefs, dir=place.dir, format=vault / FORMAT_NOTE)
    )
    if pcode.is_worker:
        return
    root = pcode.session_dir
    current: dict = {"session": None}
    # Sync per session; None holds a choice made before the first message.
    sync: dict[str | None, bool] = {}
    feedback = NoteFeedback()

    def session_of(ctx) -> str | None:
        return base_conversation(getattr(ctx, "conversation_id", None)) or current["session"]

    def state_path(session: str) -> Path:
        return root / session / STATE_FILE

    def sync_on(session: str | None) -> bool:
        if session not in sync and session is not None:
            try:  # Saved by an earlier run of this session, before a reload.
                value = json.loads(state_path(session).read_text()).get("sync")
            except OSError, ValueError:
                value = None
            if value is not None:
                sync[session] = bool(value)
        return sync.get(session, sync.get(None, org.auto_sync))

    def set_sync(on: bool, session: str | None) -> None:
        sync[session] = on
        if session and state_path(session).parent.is_dir():  # Unsaved sessions have none.
            write_text(state_path(session), json.dumps({"sync": on}))

    def in_vault(path: Path) -> bool:
        return path.suffix == ".md" and (path == vault or vault in path.parents)

    @pcode.hooks.on.before_run
    async def remember_session(ctx):
        session = session_of(ctx)
        if is_side(ctx) or not session or session == current["session"]:
            return
        current["session"] = session
        feedback.seen.clear()  # Another conversation's notes are not this one's to diff.
        if None in sync:
            set_sync(sync.pop(None), session)

    @pcode.hooks.on.after_tool_execute
    async def note_seen(ctx, *, call, tool_def, args, result):
        if tool_def.name in NOTE_TOOLS and not is_side(ctx) and isinstance(args, dict):
            path = Path(str(args.get("path", ""))).expanduser()
            path = (path if path.is_absolute() else pcode.workspace / path).resolve()
            if in_vault(path):
                await asyncio.to_thread(feedback.observe, path)
        return result

    @pcode.hooks.on.before_model_request
    async def remind(ctx, request_context):
        # Once per user message, persisted in history so the cached prefix holds.
        # A retried request already carries it; mid-turn requests hold tool results.
        if is_side(ctx):
            return request_context
        request = run_request(ctx, request_context)
        parts = request.parts if isinstance(request, ModelRequest) else ()
        prompts = [p for p in parts if isinstance(p, UserPromptPart)]
        if (
            not prompts
            or any(isinstance(p, ToolReturnPart) for p in parts)
            or any(str(p.content).startswith(TAG) for p in prompts)
        ):
            return request_context
        lines = [SYNC] if sync_on(session_of(ctx)) else []
        changed = await asyncio.to_thread(feedback.edits)
        if changed:
            lines += [EDITED, *changed]
        if lines:
            parts.append(UserPromptPart(f"{TAG}\n" + "\n\n".join(lines) + "\n</memory>"))
        return request_context

    # -- /brief

    def listing() -> None:
        rows = []
        for path, meta in notes(org):
            if meta["type"] == "brief":
                rows.append(
                    f"P{meta.get('priority', '?')}  {path.stem}  [{meta.get('health', '?')}, "
                    f"reviewed {meta.get('reviewed', 'never')}]  {meta.get('tldr', '')}"
                )
            elif meta.get("state") in ("proposed", "approved"):
                rows.append(f"    plan [{meta['state']}]  {path.stem}  {meta.get('tldr', '')}")
        sync = "on" if sync_on(current["session"]) else "off"
        header = f"Briefs in {briefs} (sync {sync}; this directory is {place.dir}):"
        pcode.ui.notify(
            header + "\n" + "\n".join(rows)
            if rows
            else f"No briefs in {briefs} yet. /brief new NAME [focus] writes one."
        )

    def new(argument: str) -> str:
        try:
            words = shlex.split(argument)
        except ValueError as error:
            raise ValueError(f"/brief new: {error}") from None
        if not words:
            raise ValueError('Usage: /brief new NAME [focus], e.g. /brief new "Repo onboarding"')
        name = safe_name(words[0], 80)
        focus = " ".join(words[1:]).strip()
        if not name:
            raise ValueError("Give the brief a name.")
        path = briefs / f"{name}.md"
        if path.exists():
            raise ValueError(f"{path.name} exists; /brief update changes it.")
        if not (vault / FORMAT_NOTE).exists():
            written, _ = setup_vault(org)
            pcode.ui.notify(f"Set up {vault} first: {', '.join(written)}.")
        return (
            f"Write a new project brief, {path}, for the project {name!r}. Read "
            f"{vault / FORMAT_NOTE} first and follow it. The scope is this directory "
            f"(`{place.dir}`, org {org.name}) unless the focus says otherwise. Build it from "
            "evidence: survey the code (README, layout, main modules, recent git history) and "
            "find the sessions that worked here with search_sessions, reading the turns that "
            "matter. Write for someone who missed every session, list what you used in "
            "`sources`, and set `reviewed` to today." + (f"\n\nFocus: {focus}" if focus else "")
        )

    def update(argument: str) -> str:
        focus = argument.strip()
        return (
            f"Update the project briefs in {briefs} that this session materially changed (the "
            f"briefs whose `dirs` cover `{place.dir}`, plus any others the work touched), per "
            f"{vault / FORMAT_NOTE} (read it first). Change only what's now out of date, add a "
            "Log line, set `reviewed`, and name the briefs you changed. If nothing a newcomer "
            "would need has changed, say so and change nothing."
            + (f"\n\nFocus: {focus}" if focus else "")
        )

    def import_sessions(argument: str) -> str | None:
        from pcode.sessions import list_sessions

        orgs = load_orgs()
        selectors, _, focus = argument.partition("--")
        selectors = selectors.split()
        if not selectors:
            cited = " ".join(
                path.read_text(encoding="utf-8", errors="replace") for path, _ in notes(org)
            )
            rows = []
            for info in list_sessions(root):
                other = session_place(orgs, info)
                if info.turns == 0 or other is None or other.org != org:
                    continue
                if place.dir != "." and other.dir != place.dir:
                    continue
                mark = "in a brief" if info.id in cited else ""
                rows.append(
                    f"{info.id[:8]}  {info.updated[:10]}  {info.turns:>3} turns  "
                    f"{(info.name or info.title or '(untitled)')[:60]}  {mark}"
                )
                if len(rows) == 25:
                    break
            pcode.ui.notify(
                "Recent sessions here:\n"
                + "\n".join(rows)
                + "\n/brief import ID [ID ...] [-- focus]"
                if rows
                else "No saved sessions here."
            )
            return None
        chosen = sorted((find_session(root, s) for s in selectors), key=lambda i: i.updated)
        for info in chosen:
            other = session_place(orgs, info)
            if other is None or other.org != org:
                raise ValueError(f"Session {info.id[:8]} is not in the {org.name} org.")
        budget = max(4000, 48000 // len(chosen))
        digests = "\n\n---\n\n".join(digest(info, root, budget) for info in chosen)
        return (
            f"Fold these earlier sessions into the project briefs in {briefs}, per "
            f"{vault / FORMAT_NOTE} (read it first). Oldest first. For each session, decide "
            "which briefs it materially changes (often none or one, sometimes several) and "
            "update only those; read full turns with read_session(session_id, turn_id) where "
            "the digest is too thin, and add the session to `sources`. Don't create briefs; "
            "if a session belongs to a project with no brief, tell me instead. Name the briefs "
            "you changed."
            + (f"\n\nFocus: {focus.strip()}" if focus.strip() else "")
            + f"\n\nSession digests (trimmed; turn IDs work with read_session):\n\n{digests}"
        )

    pending: set[asyncio.Task] = set()

    async def register() -> None:
        """`bm project add`, which takes seconds to start, off the terminal's loop."""
        process = await asyncio.create_subprocess_exec(
            "bm",
            "project",
            "add",
            org.name,
            str(vault),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        output, _ = await process.communicate()
        if process.returncode:
            detail = output.decode(errors="replace").strip().splitlines()[-1:] or [""]
            pcode.ui.notify(f"bm project add failed: {detail[0]}", "warning")
        else:
            pcode.ui.notify(f"Registered {vault} with Basic Memory as {org.name!r}.")

    def vault_setup() -> None:
        written, kept = setup_vault(org)
        message = f"Vault {vault}: wrote {', '.join(written) or 'nothing'}"
        if kept:
            message += f"; kept {', '.join(kept)} (delete one and rerun for the current version)"
        if registered_in_basic_memory(vault) is False and shutil.which("bm"):
            pending.add(task := asyncio.get_running_loop().create_task(register()))
            task.add_done_callback(pending.discard)
            message += f". Registering it with Basic Memory as {org.name!r} in the background"
        pcode.ui.notify(message + ". Open the vault in Obsidian and pin Home.md.")

    def brief_command(argument: str) -> str | None:
        command, _, rest = argument.strip().partition(" ")
        if command == "":
            listing()
        elif command == "new":
            return new(rest)
        elif command == "update":
            return update(rest)
        elif command == "import":
            return import_sessions(rest)
        elif command == "sync" and rest.strip() in ("on", "off"):
            set_sync(rest.strip() == "on", current["session"])
            when = "" if current["session"] else " from the first message"
            pcode.ui.notify(f"Brief sync {rest.strip()} for this session{when}.")
        elif command == "sync" and rest.strip() in ("", "status"):
            pcode.ui.notify(f"Brief sync is {'on' if sync_on(current['session']) else 'off'}.")
        elif command == "setup":
            vault_setup()
        else:
            raise ValueError(
                "Usage: /brief [new NAME [focus] | update [focus] | import [ID ...] [-- focus] | "
                "sync on|off | setup]"
            )
        return None

    pcode.register_command(
        "/brief",
        "Project briefs: list, new, update, import sessions, sync, setup",
        brief_command,
        arguments=("new", "update", "import", "sync", "setup"),
        free_arguments=True,
        argument_descriptions={
            "new": 'NAME [focus]: write a brief (quote names with spaces: "Repo onboarding")',
            "update": "[focus]: update the briefs this session changed",
            "import": "[ID ...] [-- focus]: list sessions here, or fold them into briefs",
            "sync": "on|off: remind the model to keep briefs current this session",
            "setup": "Write the format note, dashboard and styling into the vault",
        },
    )
