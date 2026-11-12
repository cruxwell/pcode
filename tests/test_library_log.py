import logging
import os
import subprocess
import sys
from contextlib import contextmanager

from pcode import library_log

OURS = (library_log._LibraryLog, library_log._Discard)


@contextmanager
def bare_root():
    """The root logger with no handlers, as in a real process.

    pytest attaches its own for each test phase, which would keep Python's
    last-resort stderr handler quiet whether or not `install` did anything, and
    a test that ran `main()` earlier in this worker leaves a library log behind.
    """
    root = logging.getLogger()
    before = list(root.handlers)
    root.handlers[:] = []
    try:
        yield root
    finally:
        for handler in root.handlers:
            if isinstance(handler, OURS):
                handler.close()
        root.handlers[:] = before


def test_without_install_a_warning_reaches_stderr(capfd):
    with bare_root():
        logging.getLogger("some.sdk").warning("on the terminal")
    assert "on the terminal" in capfd.readouterr().err


def test_library_warning_goes_to_the_file_not_stderr(capfd):
    with bare_root():
        library_log.install()
        path = library_log.library_log_path()
        assert not path.exists()

        logging.getLogger("google_genai.models").warning("Tools at indices [%s] not AFC", "0, 1")
        logging.getLogger("google_genai.models").info("not kept")

    assert capfd.readouterr().err == ""
    text = path.read_text()
    assert "WARNING google_genai.models: Tools at indices [0, 1] not AFC" in text
    assert "not kept" not in text


def test_detail_a_library_was_asked_for_is_kept(capfd):
    sdk = logging.getLogger("verbose.sdk")
    with bare_root():
        library_log.install()
        sdk.setLevel(logging.DEBUG)
        try:
            sdk.debug("request sent")
        finally:
            sdk.setLevel(logging.NOTSET)

    assert capfd.readouterr().err == ""
    assert "DEBUG verbose.sdk: request sent" in library_log.library_log_path().read_text()


def test_install_twice_adds_one_handler():
    with bare_root() as root:
        library_log.install()
        library_log.install()
        assert len(root.handlers) == 1


def test_unwritable_state_directory_still_keeps_stderr_clean(capfd, monkeypatch, tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("")
    monkeypatch.setenv("XDG_STATE_HOME", str(blocker))
    with bare_root() as root:
        library_log.install()
        library_log.install()
        logging.getLogger("some.sdk").warning("dropped")
        assert [type(handler) for handler in root.handlers] == [library_log._Discard]

    assert capfd.readouterr().err == ""


def test_log_that_cannot_be_written_stays_silent(capfd):
    with bare_root():
        library_log.install()
        library_log.library_log_path().parent.rmdir()
        library_log.library_log_path().parent.write_text("")
        logging.getLogger("some.sdk").warning("dropped")

    assert capfd.readouterr().err == ""


def test_fastmcp_logs_to_the_file_not_its_own_stderr_handlers(tmp_path):
    """fastmcp configures logging on import, so this needs a fresh interpreter."""
    script = (
        "import logging\n"
        "from pcode import library_log\n"
        "library_log.install()\n"
        "import fastmcp\n"
        "logging.getLogger('fastmcp.client').warning('server went away')\n"
    )
    env = {**os.environ, "XDG_STATE_HOME": str(tmp_path)}
    env.pop("FASTMCP_LOG_ENABLED", None)
    done = subprocess.run(
        [sys.executable, "-c", script], env=env, capture_output=True, text=True, check=True
    )
    assert done.stderr == ""
    assert "fastmcp.client: server went away" in (tmp_path / "pcode" / "library.log").read_text()
