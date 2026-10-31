"""The tab-title mark, run in a real Chrome: the parts a fake page cannot show.

Skipped where no Chrome or Playwright Chromium can be launched.
"""

import asyncio
import json

import pytest

from pcode.browser import _MARK_TAB, TAB_MARK

PAGES = {
    "plain": ("<title>Foo</title>", "🤖 Foo"),
    # A <head> script hands the observer a moment before <title> is parsed.
    "script first": ("<head><script>1</script><title>Foo</title></head>", "🤖 Foo"),
    "no title": ("<p>hi</p>", ""),
    "frame title ignored": (
        "<title>Top</title><iframe srcdoc='<title>Inner</title>'></iframe>",
        "🤖 Top",
    ),
}


async def _launch(playwright):
    for options in ({"channel": "chrome"}, {}):
        try:
            return await playwright.chromium.launch(headless=True, **options)
        except Exception:  # noqa: BLE001 - try the next browser, else skip.
            continue
    pytest.skip("no Chrome or Playwright Chromium to launch")


def test_mark_in_a_real_browser():
    from playwright.async_api import async_playwright

    async def scenario():
        async with async_playwright() as playwright:
            browser = await _launch(playwright)
            context = await browser.new_context()
            page, other = await context.new_page(), await context.new_page()
            await page.add_init_script(f"({_MARK_TAB})({json.dumps(TAB_MARK)})")
            await page.evaluate(_MARK_TAB, TAB_MARK)
            await page.evaluate(_MARK_TAB, TAB_MARK)  # Idempotent.
            for name, (html, title) in PAGES.items():
                await page.goto(f"data:text/html,{html}")
                assert await page.title() == title, name
                count = await page.evaluate("document.querySelectorAll('title').length")
                assert count == html.count("<title>") - html.count("srcdoc"), name

            await page.goto("data:text/html,<title>Foo</title>")
            await page.evaluate("document.title = 'Changed'")
            await page.wait_for_function("document.title === '🤖 Changed'")
            await page.evaluate(
                "document.querySelector('title').remove();"
                "const t = document.createElement('title');"
                "t.textContent = 'New'; document.head.append(t)"
            )
            await page.wait_for_function("document.title === '🤖 New'")

            # A site that enforces its own title wins rather than looping forever.
            await page.evaluate(
                "new MutationObserver(() => { if (document.title !== 'Mine')"
                " document.title = 'Mine' }).observe(document.head,"
                " {childList: true, subtree: true, characterData: true});"
                "document.title = 'Mine'"
            )
            await page.wait_for_function("document.title === 'Mine'")
            assert await page.evaluate("1 + 1") == 2  # The tab still answers.

            await other.goto("data:text/html,<title>Foo</title>")
            assert await other.title() == "Foo"  # Only the marked page.
            await browser.close()

    asyncio.run(scenario())
