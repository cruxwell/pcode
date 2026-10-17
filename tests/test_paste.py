import asyncio

from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from pcode.commands import CommandRegistry
from pcode.ui import create_prompt

BIG = "line one\r\n" + "x" * 300


def test_large_paste_goes_into_the_editor_verbatim():
    async def run():
        with create_pipe_input() as pipe:
            prompt = create_prompt(CommandRegistry(), input=pipe, output=DummyOutput())

            async def feed():
                pipe.send_text("explain \x1b[200~" + BIG + "\x1b[201~")
                while "x" * 300 not in prompt.default_buffer.text:
                    await asyncio.sleep(0.01)
                assert prompt.default_buffer.text == "explain " + BIG.replace("\r\n", "\n")
                pipe.send_text("\r")

            task = asyncio.ensure_future(feed())
            try:
                return await asyncio.wait_for(prompt.prompt_async(), timeout=5)
            finally:
                task.cancel()

    assert asyncio.run(run()) == "explain " + BIG.replace("\r\n", "\n")


def paste_into_prompt(pasted: str) -> tuple[str, list]:
    """What the editor holds after typing and pasting, with image paste wired up."""
    asked = []

    async def run():
        with create_pipe_input() as pipe:
            prompt = create_prompt(
                CommandRegistry(),
                input=pipe,
                output=DummyOutput(),
                on_paste_image=lambda: asked.append(True),
            )

            async def feed():
                pipe.send_text("look \x1b[200~" + pasted + "\x1b[201~" + "!")
                while not prompt.default_buffer.text.endswith("!"):
                    await asyncio.sleep(0.01)
                pipe.send_text("\r")

            task = asyncio.ensure_future(feed())
            try:
                return await asyncio.wait_for(prompt.prompt_async(), timeout=5)
            finally:
                task.cancel()

    return asyncio.run(run()), asked


def test_text_pastes_as_before_when_image_paste_is_wired():
    assert paste_into_prompt(BIG) == ("look " + BIG.replace("\r\n", "\n") + "!", [])


def test_pasted_whitespace_is_text_not_an_image_request():
    assert paste_into_prompt("  \n") == ("look   \n!", [])


def test_an_empty_paste_asks_for_the_clipboard_image():
    # What kitty and Ghostty send for Cmd+V when the clipboard holds only an image.
    assert paste_into_prompt("") == ("look !", [True])
