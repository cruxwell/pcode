"""Where `/plan --obsidian` keeps a plan, and the user's edits to it.

Vaults are per org: each org is a directory root (`~/c`, `~/p`, ...) with its
own vault, configured in `obsidian.toml` in pcode's config directory, which is
never in a repository, so org names stay local. A workspace no org covers uses
the shared `default` vault under `vault_root`, so it works with no config.

`NoteFeedback` snapshots the active plan when the model reads or writes it and
sends the user's later edits back as a diff, so answers and `> [!comment]`
callouts written in Obsidian reach the model without being retyped.
"""

import difflib
import re
import tempfile
import tomllib
from dataclasses import dataclass
from pathlib import Path

CONFIG_NAME = "obsidian.toml"
# The file this config used to live in, while project briefs existed.
LEGACY_CONFIG_NAME = "memory.toml"
DEFAULT_VAULT_ROOT = "~/vaults"
DEFAULT_VAULT = "default"
PLANS = "plans"
NOTE_TOOLS = frozenset({"read_file", "write_file", "edit_file"})
DIFF_LINES = 80
EDITED = (
    "The plan changed since you last read or wrote it, most likely edited by the user. "
    "Treat answers and `> [!comment]` callouts as feedback: act on each, then fold the "
    "answer into the plan or remove the comment."
)


class LegacyConfig(ValueError):
    """Only the pre-rename config exists."""


@dataclass(frozen=True)
class Org:
    name: str
    root: Path
    vault: Path


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
    """The config as a table; a missing file is empty, a broken one raises ValueError."""
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        legacy = path.with_name(LEGACY_CONFIG_NAME)
        if legacy.exists():
            # Falling back to the shared default vault would put an org's plans
            # in the wrong vault, so make the rename explicit instead.
            raise LegacyConfig(f"rename {legacy} to {path.name}") from None
        return {}
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise ValueError(f"{path}: {error}") from None


def vault_root(data: dict, path: Path) -> Path:
    value = data.get("vault_root", DEFAULT_VAULT_ROOT)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{path}: vault_root must be a nonempty path string")
    return Path(value).expanduser()


def load_orgs(data: dict, path: Path) -> list[Org]:
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
            )
        )
    return orgs


def find_org(orgs: list[Org], path: Path) -> Org | None:
    """The org whose root holds `path`; the deepest root wins when roots nest."""
    path = path.resolve()
    matches = [org for org in orgs if path == org.root or org.root in path.parents]
    return max(matches, key=lambda org: len(org.root.parts), default=None)


def project_checkout(path: Path) -> Path | None:
    from pcode.worktree import project_checkout as checkout

    return checkout(path)


def project_name(org: Org | None, workspace: Path, checkout: Path | None) -> str:
    """The plan's project label: the repository, else the directory, else the org.

    A worktree counts as its main checkout, so every session in a repository
    names its plans alike.
    """
    base = (checkout or workspace).resolve()
    if org and base == org.root:
        return org.name
    return base.name


@dataclass(frozen=True)
class PlanLocation:
    directory: Path
    project: str

    def path_for(self, request: str) -> Path:
        words = " ".join(request.split("\n", 1)[0].split()[:8])
        title = safe_name(words, 70) or "plan"
        title = title[:1].upper() + title[1:]
        return self.directory / f"Plan — {self.project} — {title}.md"


def plan_location(workspace: Path) -> PlanLocation:
    """The vault folder for a new `/plan --obsidian` plan in `workspace`.

    Raises ValueError with setup guidance when the config is broken or the
    vault isn't writable: a silent fallback would put the plan somewhere else.
    """
    path = config_path()
    try:
        data = read_config(path)
        checkout = project_checkout(workspace)
        orgs = load_orgs(data, path)
        org = (find_org(orgs, checkout) if checkout else None) or find_org(orgs, workspace)
        vault = org.vault if org else (vault_root(data, path) / DEFAULT_VAULT).resolve()
        directory = vault / PLANS
        directory.mkdir(parents=True, exist_ok=True)
        # An existing directory doesn't prove new plans can be written.
        with tempfile.NamedTemporaryFile(dir=directory, prefix=".pcode-plan-") as probe:
            probe.write(b"plan write probe\n")
            probe.flush()
    except LegacyConfig as error:
        raise ValueError(f"/plan --obsidian: {error}, then retry.") from None
    except (OSError, ValueError) as error:
        raise ValueError(
            f"/plan --obsidian: {error}. Fix {path} (orgs and vault_root) or make "
            "the vault writable, then retry (or use /plan --local)."
        ) from None
    project = safe_name(project_name(org, workspace, checkout), 40)
    return PlanLocation(directory, project or (org.name if org else DEFAULT_VAULT))


def edit_diff(path: Path, before: str, after: str) -> str:
    lines = difflib.unified_diff(
        before.splitlines(), after.splitlines(), str(path.name), str(path.name), n=1, lineterm=""
    )
    lines = list(lines)[2:]  # The ---/+++ header names the file once, below.
    if len(lines) > DIFF_LINES:
        lines = [*lines[:DIFF_LINES], f"... {len(lines) - DIFF_LINES} more diff lines"]
    return "\n".join(lines)


class NoteFeedback:
    """The last text the model saw of each note, to diff the user's edits against."""

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
