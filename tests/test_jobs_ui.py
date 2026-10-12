"""The /jobs browser: rows, the followed log, and its stop and watch shortcuts."""

import shutil
import time
from types import SimpleNamespace

from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput
from test_jobs_command import app_with_jobs, command, until_finished

from pcode.jobs_ui import JobBrowser, details


def press(browser, key):
    browser.app.key_bindings.get_bindings_for_keys((key,))[-1].handler(
        SimpleNamespace(app=browser.app)
    )


def until_logged(jobs, job, text):
    deadline = time.monotonic() + 10
    while text not in jobs.read_output(job)[0]:
        if time.monotonic() > deadline:
            raise AssertionError(f"{job.id} never printed {text!r}")
        time.sleep(0.02)


def test_browser_lists_running_first_follows_the_log_and_stops_and_watches(tmp_path):
    app, jobs = app_with_jobs()
    done = jobs.launch(command("print('all done')"), cwd=tmp_path)
    until_finished(jobs, done)
    live = jobs.launch(
        command("import time; print('serving on 8000', flush=True); time.sleep(60)"),
        cwd=tmp_path,
        background=True,
        purpose="serving the docs",
    )
    until_logged(jobs, live, "serving on 8000")

    with create_pipe_input() as pipe:
        browser = JobBrowser(
            jobs,
            stop=lambda job: app.controller.stop_jobs([job.id]),
            watch=lambda job: app.controller.watch_job(job.id if job else None),
            watched=lambda: app.activity.watched_job,
            input=pipe,
            output=DummyOutput(),
        )
        rows = browser.list.text.splitlines()
        assert rows[0].startswith(f"⟳ {live.id} · running · ")
        assert rows[0].endswith("serving the docs")
        assert rows[1].startswith(f"✓ {done.id} · exit 0 · ")
        assert browser.selected == live.id
        shown = browser.detail.text(120)
        assert "serving the docs" in shown and "serving on 8000" in shown
        assert "time.sleep(60)" in shown and "background" in shown

        press(browser, "c-w")
        assert app.activity.watched_job == live.id
        assert browser.list.text.splitlines()[0].endswith(" · watching")
        assert "watching in the preview" in browser.detail.text(120)
        press(browser, "c-w")
        assert app.activity.watched_job == ""

        press(browser, "c-k")
        assert not live.running and live.outcome() == "stopped"
        assert browser.notice == f"stopped {live.id}"
        # The stop was reported in scrollback, so the idle watcher stays quiet.
        assert app.controller.report_finished_jobs() == []

        # Stopped, it sorts after the older finished job; the selection follows it.
        assert browser.list.text.splitlines()[1].startswith(f"✗ {live.id} · stopped")
        assert browser.selected == live.id
        browser.list.buffer.cursor_up()
        assert browser.selected == done.id
        assert "all done" in browser.detail.text(120)
        press(browser, "c-k")
        assert browser.notice == "nothing running to stop"
        press(browser, "c-w")
        assert browser.notice == "only a running job can be watched"
        assert app.activity.watched_job == ""


def test_details_show_the_log_and_say_when_it_is_gone(tmp_path):
    _, jobs = app_with_jobs()
    job = jobs.launch(command("print('token=sk-0123456789ab')"), cwd=tmp_path)
    until_finished(jobs, job)
    shown = " ".join(str(block) for block in details(jobs, job, code_theme="ansi_dark"))
    assert "sk-0123456789ab" in shown
    shutil.rmtree(job.directory)
    shown = " ".join(str(block) for block in details(jobs, job, code_theme="ansi_dark"))
    assert "The log has been removed." in shown
