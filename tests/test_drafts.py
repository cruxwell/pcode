"""Unsent editor text survives a reset, and a session that ends."""

import asyncio
import os

from test_bindings_app import running_app, wait_for

from pcode import drafts
from pcode.drafts import Drafts

UP, DOWN, CTRL_C, CTRL_ENTER = "\x1b[A", "\x1b[B", "\x03", "\x1b[13;5u"


def saved() -> list[str]:
    return sorted(path.read_text() for path in drafts.default_dir().glob("*"))


async def recalled(view, text):
    """Press ↑ until the editor shows ``text``; the history refills after a reset."""
    buffer = view.session.default_buffer

    async def step():
        await view.press(UP)
        return buffer.text == text

    async with asyncio.timeout(5):
        while not await step():
            await asyncio.sleep(0.01)


def test_ctrl_c_keeps_the_discarded_draft_in_history_and_on_disk():
    async def run():
        async with running_app() as view:
            buffer = view.session.default_buffer
            await view.press("a long thought")
            await view.press(CTRL_C)
            assert buffer.text == ""
            assert view.session.history.get_strings() == ["a long thought"]
            assert saved() == ["a long thought"]
            await recalled(view, "a long thought")
            # Recalled and discarded again, it is not a second entry.
            await view.press(CTRL_C)
            assert view.session.history.get_strings() == ["a long thought"]

    asyncio.run(run())


def test_an_edited_recalled_prompt_survives_sending_something_else():
    async def run():
        async with running_app() as view:
            buffer = view.session.default_buffer
            await view.press("first\r")
            await wait_for(lambda: view.runtime.calls == ["first"])
            await recalled(view, "first")
            await view.press(" edited")
            # ↓ steps off it to the empty new prompt: the text looks gone.
            await view.press(DOWN)
            assert buffer.text == ""
            await view.press("second\r")
            assert view.session.history.get_strings() == ["first", "first edited", "second"]
            assert saved() == ["first edited"]

    asyncio.run(run())


def test_a_draft_is_saved_as_it_grows_and_forgotten_once_sent():
    async def run():
        async with running_app() as view:
            await view.press("typing")
            view.session.drafts.save()
            assert saved() == ["typing"]
            await view.press(" more")
            view.session.drafts.save()
            assert saved() == ["typing more"]
            [path] = drafts.default_dir().iterdir()
            assert path.stat().st_mode & 0o777 == 0o600
            await view.press(" still\r")
            await wait_for(lambda: view.runtime.calls == ["typing more still"])
            assert saved() == []
            assert view.session.history.get_strings() == ["typing more still"]

    asyncio.run(run())


def test_an_unchanged_recalled_prompt_is_not_a_draft():
    async def run():
        async with running_app() as view:
            await view.press("first\r")
            await wait_for(lambda: view.runtime.calls == ["first"])
            await recalled(view, "first")
            view.session.drafts.save()
            assert saved() == []

    asyncio.run(run())


def test_the_next_session_recalls_saved_drafts_and_sending_one_deletes_it():
    root = drafts.default_dir()
    root.mkdir(parents=True)
    for age, text in enumerate(["newer", "older"]):
        path = root / f"{text}.txt"
        path.write_text(text)
        os.utime(path, (1000 - age, 1000 - age))

    async def run():
        async with running_app() as view:
            assert view.session.history.get_strings() == ["older", "newer"]
            await recalled(view, "newer")
            await view.press("\r")
            await wait_for(lambda: view.runtime.calls == ["newer"])
            assert saved() == ["older"]

    asyncio.run(run())


def test_loading_deletes_all_but_the_newest_drafts(tmp_path, monkeypatch):
    monkeypatch.setattr(drafts, "KEEP", 2)
    for age, text in enumerate(["c", "b", "a"]):
        path = tmp_path / f"{text}.txt"
        path.write_text(text)
        os.utime(path, (1000 - age, 1000 - age))
    (tmp_path / "blank.txt").write_text(" \n")
    os.utime(tmp_path / "blank.txt", (2000, 2000))

    assert Drafts(tmp_path).load() == ["c"]
    assert sorted(path.name for path in tmp_path.iterdir()) == ["blank.txt", "c.txt"]
    assert Drafts(tmp_path / "missing").load() == []
    assert Drafts().load() == []


def test_a_draft_that_cannot_be_written_leaves_the_editor_alone(tmp_path):
    async def run():
        async with running_app() as view:
            # A file where the directory should be: every write fails.
            blocked = tmp_path / "blocked"
            blocked.write_text("")
            view.session.drafts.root = blocked / "drafts"
            await view.press("kept anyway")
            view.session.drafts.save()
            await view.press(CTRL_C)
            assert view.session.history.get_strings() == ["kept anyway"]

    asyncio.run(run())


def test_ctrl_enter_keeps_and_forgets_drafts_like_enter():
    async def run():
        async with running_app() as view:
            await view.press("first\r")
            await wait_for(lambda: view.runtime.calls == ["first"])
            await recalled(view, "first")
            await view.press(" edited")
            await view.press(DOWN)
            await view.press("interrupting")
            view.session.drafts.save()
            assert saved() == ["first edited", "interrupting"]
            await view.press(" now" + CTRL_ENTER)
            assert view.session.history.get_strings() == [
                "first",
                "first edited",
                "interrupting now",
            ]
            assert saved() == ["first edited"]

    asyncio.run(run())


def test_a_recalled_draft_finished_and_sent_leaves_no_file():
    root = drafts.default_dir()
    root.mkdir(parents=True)
    (root / "recovered.txt").write_text("recovered")

    async def run():
        async with running_app() as view:
            await recalled(view, "recovered")
            await view.press(" and finished")
            view.session.drafts.save()
            assert saved() == ["recovered", "recovered and finished"]
            await view.press("\r")
            await wait_for(lambda: view.runtime.calls == ["recovered and finished"])
            assert saved() == []

    asyncio.run(run())


def test_the_same_text_discarded_twice_is_one_file():
    async def run():
        async with running_app() as view:
            await view.press("same")
            await view.press(CTRL_C)
            await view.press("same")
            view.session.drafts.save()
            assert saved() == ["same"]
            await view.press("\r")
            await wait_for(lambda: view.runtime.calls == ["same"])
            assert saved() == []

    asyncio.run(run())


def test_loading_merges_duplicates_and_clears_abandoned_writes(tmp_path):
    for age, name in enumerate(["new.txt", "old.txt", "dead.tmp"]):
        (tmp_path / name).write_text("twice")
        os.utime(tmp_path / name, (1000 - age, 1000 - age))
    (tmp_path / "live.tmp").write_text("being written")

    assert Drafts(tmp_path).load() == ["twice"]
    assert sorted(path.name for path in tmp_path.iterdir()) == ["live.tmp", "new.txt"]


def test_a_draft_whose_file_another_session_deleted_is_saved_again():
    async def run():
        async with running_app() as view:
            await view.press("still here")
            view.session.drafts.save()
            [path] = drafts.default_dir().iterdir()
            path.unlink()
            view.session.drafts.save()
            assert saved() == ["still here"]

    asyncio.run(run())
