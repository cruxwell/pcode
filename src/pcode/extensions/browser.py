"""A real Chrome the model can drive, with the user logging in by hand.

Off by default and per conversation: `/browser launch` or `/browser attach`
adds Harness's eighteen Playwright tools plus `browser_open`,
`browser_tabs`, `save_file` and `upload_file`, and a `browser` sub-agent
that runs multi-step flows without the page text landing in the parent's
context. `/browser off` closes it and
removes the tools.

The browser is the user's own Chrome, started with a debugging port and a
profile of its own that keeps logins between pcode sessions; `/browser attach`
joins the Chrome the user already has open instead (see `pcode.browser`).
The window is visible on purpose: a sign-in page is left to the user, who logs
in there and says so in the next message, and the user sees what the model
does with that session afterwards. There is no sandbox around it beyond the address bar: a
page the model reads can tell it to do things with the user's login, which is
the trade-off of turning this on.

The state itself lives in `pcode.browser`, since `/reload` re-imports this file.
Copy this file to `~/.config/pcode/extensions/browser.py` to change defaults;
an empty `setup` removes the feature.
"""

import asyncio
from pathlib import Path

from pcode import sandbox
from pcode.browser import REMOTE_DEBUGGING_PAGE, STATE, show_remote_debugging_toggle

DELEGATE_INSTRUCTIONS = (
    "A `browser` sub-agent shares the same browser window and login. Delegate to it "
    "when a browsing task takes several steps and you only need the outcome (find a "
    "value, verify a flow works, reproduce a bug), so page text stays out of this "
    "conversation. Drive the browser yourself for a single check or a screenshot, "
    "and whenever a page needs the user to sign in."
)

LOGIN_INSTRUCTIONS = {
    "attach": (
        "This is the user's own Chrome: every site they are signed in to is already "
        "signed in here, so just `navigate`."
    ),
    "own": (
        "The browser profile persists between conversations, so a site the user logged "
        "in to before is still logged in; just `navigate`."
    ),
}

SIGN_IN_INSTRUCTIONS = (
    "When a page turns out to be a sign-in page, never type credentials. Call "
    "`browser_open` so the window is in front, then end your turn asking the user to "
    "log in there and tell you when they are done; continue from the same page when "
    "they do."
)

FILE_INSTRUCTIONS = (
    "Move files with `save_file` (a URL, the current tab, or a download button, saved "
    "with the login's cookies) and `upload_file` (a file input, a button that opens "
    "a file picker, or `drop=True` for a drag-and-drop zone) rather than passing "
    "bytes through `execute_js` or a local server. `get_text` and `execute_js` return "
    "their whole result, which the output limits store or trim like any large one. "
    "Screenshots are in CSS pixels, the coordinates `click('x,y')` takes."
)

SUBAGENT_INSTRUCTIONS = (
    "You drive a real browser through the tools available to you and report back "
    "concisely: what you found, what worked, what failed, and the final URL. Do not "
    "narrate each step. If a page needs a login that is not present, stop and say so "
    "rather than guessing credentials; the parent will ask the user."
)


def _spawn(coroutine) -> None:
    """Run a coroutine on the terminal's loop, or inline when there is none (tests)."""
    try:
        asyncio.get_running_loop().create_task(coroutine)
    except RuntimeError:
        asyncio.run(coroutine)


async def _start(pcode) -> None:
    """Have Chrome up and the session armed, reporting a launch to the user."""
    await STATE.arm()
    if note := await STATE.ensure_chrome():
        pcode.ui.notify(note)


async def _replace_lost_page(session, page) -> bool:
    """Give the session a live tab after the user closed `page` or the browser.

    Harness keeps pointing at the last page it had once that page closes, so a
    later call would fail on it forever. Returns False when `page` is still
    open, so an unrelated failure is reported rather than abandoning a live tab.
    """
    from playwright.async_api import Error as PlaywrightError

    browser = session._browser
    connected = browser is not None and browser.is_connected()
    if connected and not page.is_closed():
        return False
    if connected and session._context is not None:
        try:
            # Same context, so the login survives; the Chromium fallback would
            # lose its cookies to a reconnect.
            await session.open_tab()
            return True
        except PlaywrightError:
            pass  # The context went with the window; reconnect below.
    # The connection is gone: forget its pages so the next navigate reconnects.
    session.page = None
    session.pages = []
    return True


def _capability(pcode, toolset):
    """The browser tools, the open and login tools, and their guidance, sharing one session."""
    from playwright.async_api import Error as PlaywrightError
    from pydantic_ai.capabilities import Capability
    from pydantic_ai_harness.playwright import BrowserUnavailableError, PlaywrightBrowser

    browser_tools = set(toolset.tools) | {
        "browser_open",
        "browser_tabs",
        "save_file",
        "upload_file",
    }

    workspace = Path(pcode.workspace)

    def local(path: str) -> Path:
        # Resolved as the file tools and the sandbox resolve it, so the sandbox
        # judges the file these tools touch.
        return sandbox.tool_target(path, workspace)

    def refusal(targets: list[Path], *, write: bool) -> str | None:
        """Why a target is off limits even with the sandbox off, or None.

        A page can ask for these: a download written into `.git/hooks` or
        pcode's config runs as code, and an upload of a credential file
        hands it to the page.
        """
        try:
            policy = sandbox.Policy.build(sandbox.base_roots(workspace))
        except ValueError as error:
            return f"Error: the sandbox policy is invalid ({error})."
        for target in targets:
            if write and policy.guards(target):
                return f"Error: {target} is in a protected location."
            if not write and not policy.can_read(target):
                return f"Error: {target} holds credentials and cannot be uploaded."
        return None

    async def save_file(
        path: str,
        url: str | None = None,
        selector: str | None = None,
        timeout_ms: int | None = None,
    ) -> str:
        """Save a file from the browser to disk, using the browser's logins.

        With neither `url` nor `selector`, saves what the active tab shows (a
        PDF, an image, a JSON response). With `url`, fetches it with the
        browser's cookies; a `blob:` URL is read inside the page. With
        `selector`, clicks it and saves the download it starts. The saved file
        is then on disk for the file and shell tools, or for `upload_file`.

        Args:
            path: Where to write the file, absolute or relative to the workspace
                (`~` is not expanded). Parent directories are created.
            url: The file's URL. Omit to save the active tab.
            selector: A link or button that starts a download, as a CSS selector
                or an `aria-ref=` handle. Takes the place of `url`.
            timeout_ms: Override the default timeout for this call, in milliseconds.
        """
        url = url or None
        if url and selector:
            return "Error: pass `url` or `selector`, not both."
        target = local(path)
        if reason := refusal([target], write=True):
            return reason
        return await toolset.download(target, url, selector, timeout_ms)

    async def upload_file(
        selector: str,
        paths: list[str],
        drop: bool = False,
        timeout_ms: int | None = None,
    ) -> str:
        """Give local files to the page, as a user picking them would.

        `selector` may be an `<input type=file>` (hidden ones work), or a button
        or menu item that opens the file picker (Google Drive's "File upload").
        With `drop=True` the files are dropped onto the element instead, for
        drag-and-drop zones with no picker. Check the page afterwards.

        Args:
            selector: CSS selector or `aria-ref=` handle of the input, the button
                that opens the picker, or the drop zone.
            paths: Files to give, absolute or relative to the workspace
                (`~` is not expanded).
            drop: Drop the files on the element rather than pick them.
            timeout_ms: Override the default timeout for this call, in milliseconds.
        """
        targets = [local(p) for p in paths]
        if reason := refusal(targets, write=False):
            return reason
        return await toolset.upload(selector, targets, drop, timeout_ms)

    async def browser_tabs() -> str:
        """List every tab open in the browser, including the user's own, as title and URL.

        The `tabs` tool covers only tabs this session opened; this one also shows
        what the user already has open, which tells you which sites and
        accounts they use (their mail, their issue tracker) so you can open the
        right URL in your own tab. Read only: act through `navigate`.
        """
        await _start(pcode)
        try:
            tabs = await STATE.list_tabs()
        except PlaywrightError, BrowserUnavailableError:
            STATE.session.launch_error = None  # Allow another attempt after a failed launch.
            return (
                "Could not list browser tabs: the browser is unavailable or did not respond. "
                "Check /browser status and remote debugging; if needed, use /browser off "
                "then /browser attach to reconnect."
            )
        mine = {page.url for page in STATE.session.pages}
        lines = []
        for title, url in tabs:
            ours = " (yours)" if url in mine else ""
            lines.append(f"- {title or '(untitled)'}{ours}: {url}")
        return "\n".join(lines) or "No tabs are open."

    async def browser_open() -> str:
        """Bring the browser window to the front, opening it first if needed.

        The current page stays as it is. Use it to show the user a page that
        needs them, such as a sign-in page, before asking them to act there.
        """
        await _start(pcode)
        session = STATE.session
        for _ in range(2):
            if session.page is None:
                result = await toolset.navigate("about:blank")
                if session.page is None:
                    return f"The browser could not be opened: {result}"
            page = session.page
            try:
                await page.bring_to_front()
            except PlaywrightError as error:
                if not await _replace_lost_page(session, page):
                    return f"Could not bring the browser to the front: {error}"
                continue
            return f"The browser window is in front, showing {page.url}."
        return (
            "Could not bring the browser to the front: its window keeps closing. Check "
            "/browser status; if needed, use /browser off then /browser launch or attach."
        )

    class Browser(Capability):
        async def before_tool_execute(self, ctx, *, call, tool_def, args):
            # Chrome starts on the first browser tool, for the parent and the
            # sub-agent alike, on the loop the tools run on.
            if tool_def.name in browser_tools:
                await _start(pcode)
            return args

    guidance = PlaywrightBrowser(
        headless=False, block_private_addresses=False, max_content_tokens=2500
    ).get_instructions()(None)
    return Browser(
        id="browser",
        toolsets=[toolset],
        tools=[browser_open, browser_tabs, save_file, upload_file],
        instructions="\n".join(
            (
                guidance,
                FILE_INSTRUCTIONS,
                LOGIN_INSTRUCTIONS["attach" if STATE.attach else "own"],
                SIGN_IN_INSTRUCTIONS,
            )
        ),
    )


def _subagent(pcode, toolset):
    from pydantic_ai import Agent

    return Agent(
        name="browser",
        description=(
            "Drive the shared browser window through a multi-step task and report the "
            "outcome; the login the user made is already there"
        ),
        instructions=SUBAGENT_INSTRUCTIONS,
        capabilities=[_capability(pcode, toolset)],
    )


def setup(pcode) -> None:
    def turn_on() -> None:
        pcode.ui.request_reload()  # Waits out a running turn; refuses with no live session.
        STATE.enabled = True
        pcode.ui.notify(
            "Browser tools on for this conversation. Pages the model reads can act on "
            "whatever you log in to there."
        )

    def browser(argument: str) -> None:
        argument = argument.strip() or "status"
        if argument == "launch":
            if STATE.session is not None:
                raise ValueError("A browser is already open; /browser off first.")
            turn_on()
            STATE.open()
            _spawn(_launch())
        elif argument == "attach":
            if STATE.session is not None:
                raise ValueError("A browser is already open; /browser off first.")
            STATE.attach = True
            try:
                STATE.open()
            except ValueError:
                STATE.attach = False
                if show_remote_debugging_toggle():
                    pcode.ui.notify(
                        f"Opened {REMOTE_DEBUGGING_PAGE} in your Chrome. Turn the switch on, "
                        "then run /browser attach again."
                    )
                    return
                raise
            if not STATE.enabled:
                try:
                    turn_on()
                except ValueError:
                    # Refused (no live session): leave nothing half-open behind.
                    _spawn(STATE.close())
                    raise
            pcode.ui.notify(
                f"Joining your browser at {STATE.cdp_url}: the model can act as every account "
                "you are signed in to there. A new tab opens on first use.",
                "warning",
            )
        elif argument == "off":
            if not STATE.enabled:
                raise ValueError("The browser is already off.")
            pcode.ui.request_reload()
            # The reload's `setup` closes it: a running turn keeps the tools until then.
            STATE.enabled = False
            pcode.ui.notify("Browser off; it closes as its tools leave.")
        else:
            pcode.ui.notify(f"Browser {STATE.describe()}. /browser launch|attach|off.")

    async def _launch() -> None:
        try:
            await _start(pcode)
            await STATE.toolset.navigate("about:blank")
        except Exception as error:  # noqa: BLE001 - a failed launch is a notice, not a crash.
            pcode.ui.notify(f"Browser launch failed: {error}", "error")

    pcode.register_command(
        "/browser",
        "A real browser the model can drive; `launch` opens Chrome, `attach` joins yours",
        browser,
        arguments=("launch", "attach", "off", "status"),
        argument_descriptions={
            "launch": "Open pcode's own Chrome window, with its own logins",
            "attach": "Join Chrome, Chromium, or Edge, logins included; needs remote debugging on",
            "off": "Close the browser and remove the tools",
            "status": "Show which browser is in use and where it is",
        },
    )
    # Workers borrow the session browser, not ownership of its process/connection.
    if not pcode.is_worker:
        pcode.on_close(STATE.close)
    if not STATE.enabled:
        if not pcode.is_worker and STATE.session is not None:
            _spawn(STATE.close())  # `/browser off`, now that no agent holds the tools.
        return
    toolset = STATE.open()
    pcode.add_capability(_capability(pcode, toolset))
    pcode.instructions(DELEGATE_INSTRUCTIONS)
    pcode.subagent(_subagent(pcode, toolset), timeout_seconds=900)
