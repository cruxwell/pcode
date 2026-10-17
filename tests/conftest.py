import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import leaked_processes
import pytest

RUN_KEY = pytest.StashKey[str | None]()

# Rich forces terminal output for any non-empty FORCE_COLOR, "0" included, which
# pads and truncates rendered text the suite compares exactly. Dropped at import,
# before any module builds a Console, and so for subprocesses too.
os.environ.pop("FORCE_COLOR", None)

# Private tmux servers are parented to init, so a pytest that dies without
# running fixture teardown (SIGKILL, a timeout, an abandoned CI runner) strands
# the server plus its ~170MB Python child forever. Sweep the previous run's
# corpses before starting a new one.
TMUX_SOCKET_PREFIXES = ("pcode-test-", "pcode-bench-")
# A single tmux test lives for seconds; this is only long enough to never touch
# a concurrent run's sockets.
STALE_TMUX_SERVER_SECONDS = 15 * 60
# A starting server binds its socket a moment before it listens, so a socket
# that refuses connections may belong to a concurrent run that is just booting.
# Unlinking it then would strand that server, so dead sockets wait this long.
DEAD_TMUX_SOCKET_SECONDS = 60

# The real-tmux regressions are 64 of ~1580 tests but three quarters of the
# suite's wall clock: each boots a private tmux server plus a pcode process and
# then polls the pane. They stay in the suite (plain PTYs miss the frame bugs
# they catch), but the edit/test loop should not pay for them on every run.
TMUX_OPT_IN_ENV = "PCODE_TEST_TMUX"
TRANSPORT_ENV = "PCODE_TEST_TRANSPORT"

# `-n auto` means a worker per core, and every worker boots its own pcode
# processes, PTYs and session hosts. Two or three agent sessions running the
# suite at once then took the whole machine down with memory pressure, so auto
# stops at a few workers per run. PYTEST_XDIST_AUTO_NUM_WORKERS picks another.
AUTO_WORKER_CAP = 4


def pytest_xdist_auto_num_workers(config):
    if os.environ.get("PYTEST_XDIST_AUTO_NUM_WORKERS"):
        return None  # xdist's own implementation reads it
    return min(AUTO_WORKER_CAP, os.cpu_count() or 1)


def pytest_addoption(parser):
    parser.addoption(
        "--tmux",
        action="store_true",
        default=False,
        help=f"Run the slow real-tmux regressions (also {TMUX_OPT_IN_ENV}=1).",
    )
    parser.addoption(
        "--transport",
        choices=("in-process", "socket"),
        default=os.environ.get(TRANSPORT_ENV, "in-process"),
        help=f"Where app-level tests run the session (also {TRANSPORT_ENV}); see "
        "tests/socket_transport.py.",
    )


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "in_process(reason): looks inside the in-process session; skipped with --transport socket",
    )
    # Only the controller: a worker reaping its run would stop its siblings.
    # Tagging here, before xdist spawns workers, puts the tag in theirs too.
    if not hasattr(config, "workerinput"):
        reaped = leaked_processes.reap_dead_runs()
        if reaped:
            lines = "".join(f"\n  {line}" for line in reaped)
            print(f"\nstopped {len(reaped)} processes from dead runs:{lines}", file=sys.stderr)
        config.stash[RUN_KEY] = leaked_processes.tag_run()


def pytest_unconfigure(config):
    identity = config.stash.get(RUN_KEY, None)
    if identity is None:
        return
    # Reap while still holding the lock, so no other run takes this one for dead.
    leaked = leaked_processes.reap_run(identity)
    leaked_processes.end_run(identity)
    if leaked:
        lines = "".join(f"\n  {line}" for line in leaked)
        print(f"\nstopped {len(leaked)} processes this test run leaked:{lines}", file=sys.stderr)


def pytest_collection_modifyitems(config, items):
    """Mark the real-tmux tests, and skip them unless this run asked for them.

    Naming a tmux path on the command line counts as asking, so
    `pytest tests/test_tmux.py -k ...` still runs rather than skipping everything.
    """
    tmux = pytest.mark.tmux
    for item in items:
        if "tmux" in item.path.name:
            item.add_marker(tmux)
    if config.getoption("--transport") == "socket":
        for item in items:
            if marker := item.get_closest_marker("in_process"):
                reason = marker.args[0] if marker.args else "in-process session only"
                item.add_marker(pytest.mark.skip(reason=f"in-process only: {reason}"))
    if (
        config.getoption("--tmux")
        or os.environ.get(TMUX_OPT_IN_ENV) == "1"
        or any("tmux" in argument for argument in config.args)
        or config.option.markexpr.strip() == "tmux"
    ):
        return
    skip = pytest.mark.skip(reason=f"real-tmux regression: pass --tmux or {TMUX_OPT_IN_ENV}=1")
    for item in items:
        if item.get_closest_marker("tmux"):
            item.add_marker(skip)


def tmux_socket_dir() -> Path:
    """Where tmux keeps per-user sockets (`-L` names resolve inside this)."""
    return Path(os.environ.get("TMUX_TMPDIR", "/tmp")) / f"tmux-{os.getuid()}"


def sweep_tmux_servers(*, max_age_seconds: float) -> int:
    """Kill leaked test servers, and unlink sockets with no server behind them.

    Only servers older than ``max_age_seconds`` are killed, and only sockets
    older than DEAD_TMUX_SOCKET_SECONDS unlinked, so a concurrent pytest
    process, in this worktree or another, keeps its own servers.
    """
    directory = tmux_socket_dir()
    if shutil.which("tmux") is None or not directory.is_dir():
        return 0
    reaped = 0
    now = time.time()
    for socket in directory.iterdir():
        if not socket.name.startswith(TMUX_SOCKET_PREFIXES):
            continue
        base = ["tmux", "-L", socket.name, "-f", "/dev/null"]
        alive = subprocess.run([*base, "list-sessions"], capture_output=True).returncode == 0
        try:
            age = now - socket.stat().st_mtime
        except OSError:
            continue
        if age <= (max_age_seconds if alive else DEAD_TMUX_SOCKET_SECONDS):
            continue
        if alive:
            subprocess.run([*base, "kill-server"], capture_output=True)
            reaped += 1
        socket.unlink(missing_ok=True)
    return reaped


@pytest.fixture(scope="session", autouse=True)
def reap_leaked_tmux_servers():
    """Bound the damage from any earlier run that never reached its teardown."""
    sweep_tmux_servers(max_age_seconds=STALE_TMUX_SERVER_SECONDS)
    yield
    # This run's own servers are gone by now; clear dead files earlier runs left.
    sweep_tmux_servers(max_age_seconds=float("inf"))


@pytest.fixture(autouse=True)
def isolated_preferences(monkeypatch, tmp_path, request):
    """Tests must neither consume nor overwrite the user's saved defaults."""
    monkeypatch.delenv("PCODE_CONFIG_DIR", raising=False)
    monkeypatch.delenv("PCODE_CODEX_CREDENTIALS_FILE", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.delenv("PCODE_MCP_CONFIG", raising=False)
    # An always-on profile default plus a real PCODE_PROFILE_DIR would otherwise
    # have the suite writing captures into the developer's own directory.
    monkeypatch.delenv("PCODE_PROFILE_DIR", raising=False)
    # A clean release build would otherwise ask PyPI from every startup test.
    monkeypatch.setenv("PCODE_NO_UPDATE_CHECK", "1")
    # skill_dirs defaults to ~/.agents/skills, so a developer's own skills would
    # otherwise register as commands in every app the suite builds.
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    # Diffs render with Rich whether or not the machine has delta installed;
    # tests of delta itself put it back.
    monkeypatch.setattr("pcode.delta.find_delta", lambda: None)
    # Only COLORTERM, which tests set themselves, may decide true color: not
    # the developer's terminal, nor the tmux client the suite happens to run in.
    monkeypatch.delenv("TERM_PROGRAM", raising=False)
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.delenv("PROMPT_TOOLKIT_COLOR_DEPTH", raising=False)
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.setattr("pcode.terminal_color._tmux_rgb", lambda pane: False)
    monkeypatch.setattr("pcode.terminal_color._setting", "auto")
    monkeypatch.setattr("pcode.glyphs._active", False)
    # The CLI fixes the project overlay root once per process; tests that run
    # main() would otherwise leak this checkout's .pcode/preferences.json into
    # every later test.
    from pcode import preferences

    monkeypatch.setattr(preferences, "_project_root", None)
    # main() sets the root again from the cwd, which is this checkout. Its
    # committed overlay turns worktrees on, so every in-process main() would
    # check out a real .worktrees/<session> here. Overlay tests use tmp repos.
    checkout = Path(__file__).resolve().parents[1]
    set_root = preferences.set_project_root

    def set_project_root(path):
        own = path is not None and path.resolve() == checkout
        set_root(None if own else path)

    monkeypatch.setattr(preferences, "set_project_root", set_project_root)
    from dataclasses import replace as replace_setting

    # Grouping is the shipped default, but most transcript tests assert on the
    # per-call summary lines; tests/test_group_tools.py turns grouping on itself.
    monkeypatch.setitem(
        preferences.SETTINGS,
        "group_tools",
        replace_setting(preferences.SETTINGS["group_tools"], default="off"),
    )
    # Existing UI tests exercise Ctrl chords. Keep the real default available
    # to regressions requesting shipped_key_prefix, without adding a saved
    # preference to tests that assert on the exact persistence payload.
    if "shipped_key_prefix" not in request.fixturenames:
        monkeypatch.setitem(
            preferences.SETTINGS,
            "key_prefix",
            replace_setting(preferences.SETTINGS["key_prefix"], default="ctrl"),
        )
    # Hints are on by default, but most tests assert on the exact footer and
    # task heading; tests/test_hints.py turns them on itself.
    monkeypatch.setitem(
        preferences.SETTINGS,
        "show_hints",
        replace_setting(preferences.SETTINGS["show_hints"], default="off"),
    )
    # Most layout tests find the boxes by their border glyphs, and whether
    # shading applies depends on the developer's terminal (COLORTERM); shaded
    # rendering has its own tests in tests/test_frame_shades.py.
    monkeypatch.setitem(
        preferences.SETTINGS,
        "frame_style",
        replace_setting(preferences.SETTINGS["frame_style"], default="borders"),
    )
    # The shipped thresholds would hide the task widget on the small screens
    # most tests draw; tests/test_task_size_autohide.py covers them itself.
    for name in ("tasks_min_rows", "tasks_min_columns"):
        monkeypatch.setitem(
            preferences.SETTINGS,
            name,
            replace_setting(preferences.SETTINGS[name], default="0"),
        )
    # Both are on by default. Off here, so a test's first turn makes no extra
    # model request and no terminal title escape lands in captured output;
    # tests/test_session_naming.py turns them on itself.
    for name in ("session_naming", "terminal_title"):
        monkeypatch.setitem(
            preferences.SETTINGS,
            name,
            replace_setting(preferences.SETTINGS[name], default="off"),
        )


@pytest.fixture
def shipped_key_prefix():
    """Opt out of the suite's legacy chord default without replacing the real one."""


@pytest.fixture(autouse=True)
def session_transport(request, monkeypatch):
    """With `--transport socket`, in-process sessions run in a host instead."""
    if request.config.getoption("--transport") != "socket":
        yield
        return
    import tempfile

    from socket_transport import install

    # macOS caps a Unix socket path at 104 bytes, and pytest's tmp_path is longer.
    directory = Path(tempfile.mkdtemp(prefix="pch-", dir="/tmp"))
    monkeypatch.setenv("PCODE_HOST_DIR", str(directory))
    install(monkeypatch)
    yield
    shutil.rmtree(directory, ignore_errors=True)


@pytest.fixture(autouse=True)
def isolated_terminal_background(monkeypatch):
    """No test inherits a background another test's OSC 11 reply recorded.

    `frame_style shaded` mixes from it, so a leftover report would flip a
    later test's boxes from borders to shades depending on worker order.
    """
    from pcode import theme

    monkeypatch.setattr(theme, "_background", None)


@pytest.fixture(autouse=True)
def isolated_jobs():
    """Shell jobs are process-wide on purpose; tests must not inherit them.

    Resetting gives each test job ids from `j1` and stops anything a previous
    test leaked, which a registry designed to outlive its run would otherwise
    keep alive for the whole session.
    """
    from pcode.jobs import registry

    registry().reset()
    yield
    registry().reset()


@pytest.fixture(autouse=True)
def isolated_context_catalog(monkeypatch, tmp_path):
    """Never fetch real metadata or read user caches in ordinary unit tests.

    Adapter tests construct their own ContextCatalog with mocked transports.
    """
    from unittest.mock import AsyncMock

    from pcode import model_metadata

    catalog = model_metadata.ContextCatalog(tmp_path / "metadata.json")
    monkeypatch.setattr(catalog, "refresh", AsyncMock())
    monkeypatch.setattr(model_metadata, "catalog", catalog)
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.delenv("PCODE_CONTEXT_WINDOW", raising=False)
