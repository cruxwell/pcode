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
