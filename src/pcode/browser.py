"""The conversation's browser: one real Chrome the bundled `browser` extension drives.

Lives outside the extension because `/reload` re-imports extension modules,
and the browser (with whatever the user logged in to) has to outlive that.
Never persisted: a fresh pcode starts with the browser off, and the cookies of
a session that was logged in leave with the process.

Two decisions differ from Harness's own `PlaywrightBrowser` capability:

- The session is owned here, not by the capability. That capability opens a
  browser per agent run and closes it when the run ends; pcode runs one agent
  run per turn, so under it the user would log in and lose the session at the
  end of the turn. One `PlaywrightBrowserSession` is handed to every run,
  parent or sub-agent, until `/browser off` or exit.
- The browser is the user's installed Chrome, attached over CDP, not
  Playwright's Chromium. Playwright launches its build with `--enable-automation`
  (`navigator.webdriver` is true) and no Google API keys, and Google refuses to
  sign in to that browser ("controlled through software automation"). Chrome
  started by us with only a debugging port and its own profile carries neither
  mark. When no Chrome is installed the session falls back to Playwright's
  Chromium, which works everywhere except such sign-in pages.
- The page opens in the browser's own default context rather than a fresh one
  (`SharedContextSession`). Harness isolates every run in a new context so a
  run never inherits a login; here inheriting is the point. On the Chrome we
  launch, the default context is the pcode profile on disk, so a login made
  once holds across pcode sessions. `/browser attach` (or
  `PCODE_BROWSER_CDP_URL`) joins the user's everyday Chrome instead, where the
  default context holds every account they are signed in to; that is the
  higher-risk choice and is never the default.
"""

from __future__ import annotations

import asyncio
import contextvars
import json
import os
import shutil
import socket
import subprocess
import sys
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

# A coding agent's usual target is its own dev server on localhost, which the
# Harness default (an SSRF guard for untrusted deployments) would refuse.
BLOCK_PRIVATE_ADDRESSES = False
# Every action returns page text; below the Harness default because a browsing
# turn is many actions and each lands in the conversation.
MAX_CONTENT_TOKENS = 2500
CHROME_START_SECONDS = 20
CHROME_CANDIDATES = (
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "google-chrome",
    "google-chrome-stable",
    "chromium",
    "chromium-browser",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
)


def chrome_executable() -> str | None:
    """The Chrome to launch: `PCODE_BROWSER_CHROME`, else the first installed candidate."""
    configured = os.environ.get("PCODE_BROWSER_CHROME", "").strip()
    candidates = (configured,) if configured else CHROME_CANDIDATES
    for candidate in candidates:
        if "/" in candidate:
            if Path(candidate).is_file():
                return candidate
        elif shutil.which(candidate):
            return candidate
    return None


def profile_dir() -> Path:
    """Chrome's profile for the agent, apart from the user's own so no login leaks in."""
    state = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local" / "state")
    return state / "pcode" / "chrome"


def _chrome_profile_dirs() -> list[Path]:
    """Where Chrome, Chromium, and Edge keep their debugging port files."""
    home = Path.home()
    return [
        home / "Library" / "Application Support" / "Google" / "Chrome",
        home / "Library" / "Application Support" / "Chromium",
        home / ".config" / "google-chrome",
        home / ".config" / "chromium",
        home / "Library" / "Application Support" / "Microsoft Edge",
        home / ".config" / "microsoft-edge",
    ]


def running_chrome_url() -> str | None:
    """The CDP endpoint of a user's Chromium-based browser with remote debugging on.

    `PCODE_BROWSER_CDP_URL` wins. Otherwise Chrome writes `DevToolsActivePort`
    (port, then the browser websocket path) into its profile when started with
    `--remote-debugging-port` or when `chrome://inspect/#remote-debugging` is
    toggled on; `PCODE_BROWSER_PORT_FILE` names that file elsewhere.
    """
    configured = os.environ.get("PCODE_BROWSER_CDP_URL", "").strip()
    if configured:
        return configured
    named = os.environ.get("PCODE_BROWSER_PORT_FILE", "").strip()
    files = [Path(named)] if named else [d / "DevToolsActivePort" for d in _chrome_profile_dirs()]
    for file in files:
        try:
            port, path = file.read_text().split()[:2]
        except OSError, ValueError:
            continue
        return f"ws://127.0.0.1:{int(port)}{path}"
    return None


REMOTE_DEBUGGING_PAGE = "chrome://inspect/#remote-debugging"


def show_remote_debugging_toggle() -> bool:
    """Open Chrome's remote-debugging switch in the user's own Chrome; False if none is installed.

    Chrome forwards a URL given to a second invocation to the running instance,
    or starts one on the default profile, so this lands in the everyday browser
    either way. On macOS that goes through `open -a`: a cold start from the
    binary itself drops a `chrome://` URL on the floor. Detached: the process
    is not ours to wait on.
    """
    executable = chrome_executable()
    if executable is None:
        return False
    command = [executable, REMOTE_DEBUGGING_PAGE]
    if sys.platform == "darwin" and ".app/Contents/MacOS/" in executable:
        bundle = Path(executable)
        while bundle.suffix != ".app":
            bundle = bundle.parent
        command = ["open", "-a", str(bundle), REMOTE_DEBUGGING_PAGE]
    _detach(command)
    return True


def _detach(command: list[str]) -> None:
    subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


# Marks the agent's tabs in the user's tab strip: the title gets a prefix, put
# back whenever the site changes its title. Per page, never on the context,
# which in attach mode holds the user's own tabs. Top frame only; idempotent,
# since it is both injected for future documents and run on the current one.
# Verified by hand in Chrome; the tests run on fakes, with no browser.
TAB_MARK = "\N{ROBOT FACE} "
_MARK_TAB = """(mark) => {
  if (window.top !== window || window.__pcodeTabMark) return;
  window.__pcodeTabMark = true;
  // Reading `document.title` trims it, so an empty page's mark reads back bare.
  const fix = () => {
    const title = document.title;
    if (title !== mark.trim() && !title.startsWith(mark)) document.title = mark + title;
  };
  new MutationObserver(fix).observe(document, {
    childList: true, subtree: true, characterData: true,
  });
  fix();
}"""


def _session_class():
    from pydantic_ai_harness.playwright import PlaywrightBrowserSession

    class SharedContextSession(PlaywrightBrowserSession):
        """A session whose page lives in the browser's default context.

        Overrides the private `_launch` of the pinned Harness revision: the
        only difference is `browser.contexts[0]` in place of `new_context()`
        when the browser has one, so the page shares that context's cookies.
        The guards Harness installs (route, websocket, page wiring) are kept.
        A browser reached over CDP is left running by Harness's own teardown,
        which only disconnects; the page opened here is closed first so no
        stray tab is left in the user's window.
        """

        async def _launch(self) -> None:
            assert self._driver_cm is not None
            if self._driver is None:
                self._driver = await self._driver_cm.__aenter__()
                self._driver_entered = True
            if self._browser is not None:
                stale = self._browser
                await self._bounded(stale.close())
                self._browser = None
            browser = await self._connect(self._driver)
            if browser is None:
                return
            self._browser = browser
            if browser.contexts:
                context = browser.contexts[0]
            else:  # pragma: no cover - a launched Chromium has none until asked
                context = await self._bounded(
                    browser.new_context(service_workers="block", accept_downloads=True)
                )
            self._context = context
            page = await self._bounded(context.new_page())
            if self.policy.enforced():
                await self._bounded(context.route("**/*", self._route_guard))
                await self._bounded(context.route_web_socket("**/*", self._websocket_guard))
            self._wire_page(page)
            self.pages.append(page)
            self.page = page
            # Show the new tab once, so in the user's own Chrome they watch the
            # agent's tab rather than whichever they had open. Never again
            # per action: switching away afterwards is the user's choice.
            try:
                await self._bounded(page.bring_to_front())
            except Exception:  # noqa: BLE001 - cosmetic; the page works without it.
                pass

        def _wire_page(self, page) -> None:
            """Harness's wiring, plus the title mark; every tab of ours passes here."""
            super()._wire_page(page)
            self._spawn(self._mark(page))

        async def _mark(self, page) -> None:
            # Injected for later navigations, then run on the document already
            # loaded (a popup arrives mid-load). Cosmetic: errors are dropped.
            try:
                await page.add_init_script(f"({_MARK_TAB})({json.dumps(TAB_MARK)})")
                await page.evaluate(_MARK_TAB, TAB_MARK)
            except Exception:  # noqa: BLE001 - the page may be gone or a fake.
                pass

        async def list_tabs(self) -> list[tuple[str, str]]:
            """Read browser targets over CDP, without evaluating possibly hung pages.

            A browser's debugging switch can expose only a WebSocket, with no
            HTTP `/json/list` endpoint. Reuse Playwright's connection instead.
            """
            await self.ensure_page()
            assert self._browser is not None
            cdp = await self._bounded(self._browser.new_browser_cdp_session())
            try:
                result = await self._bounded(cdp.send("Target.getTargets"))
            finally:
                await self._bounded(cdp.detach())
            return [
                (target.get("title", ""), target.get("url", ""))
                for target in result["targetInfos"]
                if target.get("type") == "page"
            ]

        async def __aexit__(self, exc_type, *rest) -> None:
            for page in list(self.pages):
                try:
                    await page.close()
                except Exception:  # noqa: BLE001 - the tab may already be gone.
                    pass
            await super().__aexit__(exc_type, *rest)

    return SharedContextSession


# In-page JavaScript for the two transfers Playwright has no API for.
_READ_IN_PAGE = """async ([url, limit]) => {
  const response = await fetch(url, {credentials: 'include'});
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
  const bytes = new Uint8Array(await response.arrayBuffer());
  if (bytes.length > limit) throw new Error(`${bytes.length} bytes is over the save limit`);
  let binary = '';
  for (let i = 0; i < bytes.length; i += 0x8000) {
    binary += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
  }
  return btoa(binary);
}"""
_DROP_FILES = """(target, files) => {
  const transfer = new DataTransfer();
  for (const file of files) {
    const bytes = Uint8Array.from(atob(file.data), (c) => c.charCodeAt(0));
    transfer.items.add(new File([bytes], file.name, {type: file.type}));
  }
  const box = target.getBoundingClientRect();
  const at = {clientX: box.left + box.width / 2, clientY: box.top + box.height / 2};
  for (const type of ['dragenter', 'dragover', 'drop', 'dragleave']) {
    target.dispatchEvent(new DragEvent(type, {
      bubbles: true, cancelable: true, dataTransfer: transfer, ...at,
    }));
  }
}"""
# Whether an <iframe> shows anything. Its client box excludes the default
# border, which gives even a width=0 height=0 frame a 4x4 bounding box.
_RENDERED_FRAME = """(e) => e.clientWidth > 0 && e.clientHeight > 0
  && getComputedStyle(e).visibility !== 'hidden'"""
# Set inside `get_text` and `execute_js`. A ContextVar, so another tool waiting
# on the operation lock in a different task still gets its result capped.
_WHOLE_OUTPUT = contextvars.ContextVar("pcode_browser_whole_output", default=False)
# A backstop for those whole results where no output limit spills them (an
# agent built without pcode's limits, or `tool_output_mode off`).
_MAX_WHOLE_CHARS = 1_000_000
# Files cross the CDP connection whole, and in-page as base64 strings.
_MAX_TRANSFER_BYTES = 100 * 1024 * 1024
_MAX_REDIRECTS = 10


def _toolset_class():
    """Harness's toolset, adjusted where its defaults lose data or mislead the model.

    - `get_text` and `execute_js` return their whole result. Harness caps every
      result at `MAX_CONTENT_TOKENS`, which is pcode's spill threshold too, so a
      large read was cut first and then spilled: the stored copy held only the
      cut text, and data pulled out of a page (JSON, base64) was lost. These two
      are deliberate reads; pcode's own output limits spill them whole. Page text
      returned beside an action stays capped.
    - The child-frame sweep skips frames that are not rendered. `innerText` of a
      document with no layout falls back to its raw text, scripts included, so
      every hidden helper frame (Google apps carry several) added kilobytes of
      inline JavaScript to each result.
    - Screenshots are taken in CSS pixels. On a HiDPI display the device-pixel
      image is twice the size of the coordinates `click('x,y')` takes, so a
      point read off the picture missed.
    - `download` and `upload` move files between disk and the logged-in page.
    """
    import base64
    import mimetypes

    from playwright.async_api import Error as PlaywrightError
    from pydantic_ai import BinaryContent, ToolReturn
    from pydantic_ai_harness.playwright import EgressRequest, PlaywrightBrowserToolset
    from pydantic_ai_harness.playwright._toolset import _truncate, _without_credentials

    whole = _WHOLE_OUTPUT

    class PcodeBrowserToolset(PlaywrightBrowserToolset):
        def _truncate_output(self, text: str) -> str:
            if whole.get():
                return _truncate(text, _MAX_WHOLE_CHARS)
            return super()._truncate_output(text)

        async def _frame_text(self, page, budget_ms: int) -> list[str]:
            texts: list[str] = []

            async def rendered(frame) -> bool:
                # A frame inside a hidden frame has no layout either, so every
                # ancestor up to the page must be showing.
                while frame.parent_frame is not None:
                    element = await frame.frame_element()
                    try:
                        if not await element.evaluate(_RENDERED_FRAME):
                            return False
                    finally:
                        await element.dispose()
                    frame = frame.parent_frame
                return True

            async def sweep() -> None:
                for frame in page.frames[1:]:
                    try:
                        if not await rendered(frame):
                            continue
                        text = await frame.inner_text("body", timeout=budget_ms)
                    except PlaywrightError:
                        continue
                    if text.strip():
                        texts.append(f"[frame {_without_credentials(frame.url)}]\n{text}")

            try:
                await asyncio.wait_for(sweep(), budget_ms / 1000)
            except TimeoutError:
                pass
            return texts

        async def get_text(self, selector: str | None = None, timeout_ms: int | None = None) -> str:
            """Extract text from the page or a specific element.

            The whole text comes back, not a page-sized excerpt; a large one is
            handled by the output limits like any other large result.

            Args:
                selector: CSS selector to read, or an `aria-ref=` handle from
                    `snapshot`. A CSS selector matches the main frame only; reading
                    inside an iframe needs the `aria-ref=` handle. Omit for the whole
                    page's visible text, which includes embedded frames.
                timeout_ms: Override the default timeout for this call, in milliseconds.
                    Must be greater than 0.

            Returns:
                The element's text, or the full page's visible text when no selector
                is given.
            """
            token = whole.set(True)
            try:
                return await super().get_text(selector, timeout_ms)
            finally:
                whole.reset(token)

        async def execute_js(self, script: str, timeout_ms: int | None = None) -> str:
            """Evaluate a JavaScript expression and return its result.

            The whole result comes back, not a page-sized excerpt; a large one
            is handled by the output limits like any other large result. To
            move a file, use `save_file` / `upload_file` instead of passing its
            bytes here.

            Args:
                script: JavaScript expression to evaluate, e.g. `document.title`.
                timeout_ms: Override the default timeout for this call, in milliseconds.
                    Must be greater than 0.

            Returns:
                A string result as-is, objects/arrays as JSON, `null`/`undefined` as
                `'undefined'`, or `JS error: ...` when evaluation raises.
            """
            token = whole.set(True)
            try:
                return await super().execute_js(script, timeout_ms)
            finally:
                whole.reset(token)

        async def screenshot(
            self, full_page: bool = False, timeout_ms: int | None = None
        ) -> Any:  # str | ToolReturn; the import is local, so not resolvable here.
            """Capture a screenshot of the current page.

            The image is in CSS pixels, so a point read off it is the `'x,y'`
            that `click` takes.

            Args:
                full_page: Capture the full scrollable page when `True`, else the
                    current viewport.
                timeout_ms: Override the default timeout for this call, in milliseconds.
                    Must be greater than 0.

            Returns:
                A short note with the page URL, and the PNG as image content so
                vision models can see it.
            """

            async def _screenshot(page, deadlines):
                png = await page.screenshot(
                    full_page=full_page, scale="css", timeout=deadlines.action
                )
                if (oversized := self._oversized_screenshot_error(png)) is not None:
                    return self._error(oversized)
                return ToolReturn(
                    f"Screenshot captured (CSS pixels). URL: {_without_credentials(page.url)}",
                    content=[BinaryContent(data=png, media_type="image/png")],
                )

            return await self._in_operation("screenshot", timeout_ms, _screenshot)

        async def download(
            self,
            destination: Path,
            url: str | None = None,
            selector: str | None = None,
            timeout_ms: int | None = None,
        ) -> str:
            """Write a file the browser can reach to `destination`; see `save_file`."""

            async def fetch(page, source: str, deadlines) -> bytes | str | None:
                """The body at `source` with the login's cookies, or why it was refused.

                Redirects are followed here, one hop at a time, so each one passes
                the egress policy: this client is not behind the page's route guard.
                None means the request could not be completed, not a refusal.
                """
                for _ in range(_MAX_REDIRECTS + 1):
                    reason = await self._session.decide(EgressRequest(url=source, kind="data"))
                    if reason is not None:
                        return f"Error: {reason}: {_without_credentials(source)}"
                    try:
                        response = await page.context.request.get(
                            source, max_redirects=0, timeout=deadlines.navigation
                        )
                    except PlaywrightError:
                        return None
                    try:
                        location = response.headers.get("location")
                        if 300 <= response.status < 400 and location:
                            source = urljoin(source, location)
                            continue
                        return await response.body() if response.ok else None
                    finally:
                        await response.dispose()
                return f"Error: more than {_MAX_REDIRECTS} redirects from {url}"

            async def _download(page, deadlines):
                source = urljoin(page.url, url) if url else page.url
                if selector is not None:
                    async with page.expect_download(timeout=deadlines.action) as started:
                        await page.click(selector, timeout=deadlines.action)
                    download = await started.value
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    try:
                        await download.save_as(destination)
                    except PlaywrightError:
                        # Chrome's default context, reached over CDP, keeps
                        # downloads out of Playwright's hands; fetch the same URL.
                        source = download.url
                    else:
                        size = destination.stat().st_size
                        how = f"the download {selector} started"
                        return f"Saved {size:,} bytes to {destination} from {how}."
                data = None
                if not source.startswith(("blob:", "data:")):
                    data = await fetch(page, source, deadlines)
                    if isinstance(data, str):
                        return self._error(data)
                if data is None:
                    # A blob: URL, or a link only the page can still read (an
                    # expired signed URL the tab already loaded).
                    encoded = await self._await_with_timeout(
                        page.evaluate(_READ_IN_PAGE, [source, _MAX_TRANSFER_BYTES]),
                        deadlines.navigation,
                    )
                    data = base64.b64decode(encoded)
                if len(data) > _MAX_TRANSFER_BYTES:
                    return self._error(f"Error: {len(data):,} bytes is over the save limit.")
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(data)
                how = _without_credentials(source)
                return f"Saved {len(data):,} bytes to {destination} from {how}."

            try:
                return await self._in_operation(
                    "save_file", timeout_ms, _download, governed_by_navigation=True
                )
            except OSError as error:
                return self._error(f"Error: could not write {destination}: {error}")

        async def upload(
            self,
            selector: str,
            files: list[Path],
            drop: bool = False,
            timeout_ms: int | None = None,
        ) -> str:
            """Give local files to the page; see `upload_file`."""
            missing = [str(path) for path in files if not path.is_file()]
            if missing:
                return await self._refuse(
                    "upload_file", timeout_ms, f"Error: no such file: {', '.join(missing)}"
                )
            if sum(path.stat().st_size for path in files) > _MAX_TRANSFER_BYTES:
                return await self._refuse(
                    "upload_file", timeout_ms, "Error: the files are over the upload limit."
                )

            async def _upload(page, deadlines):
                target = page.locator(selector).first
                names = ", ".join(path.name for path in files)
                if drop:
                    payload = [
                        {
                            "name": path.name,
                            "type": mimetypes.guess_type(path.name)[0]
                            or "application/octet-stream",
                            "data": base64.b64encode(path.read_bytes()).decode(),
                        }
                        for path in files
                    ]
                    await target.evaluate(_DROP_FILES, payload, timeout=deadlines.action)
                    return f"Dropped {names} on {selector}; check the page for the upload."
                is_input = await target.evaluate(
                    "e => e.tagName === 'INPUT' && e.type === 'file'", timeout=deadlines.action
                )
                if is_input:
                    await target.set_input_files(files, timeout=deadlines.action)
                else:
                    async with page.expect_file_chooser(timeout=deadlines.action) as chooser:
                        await target.click(timeout=deadlines.action)
                    await (await chooser.value).set_files(files, timeout=deadlines.action)
                return f"Gave {names} to {selector}; check the page for the upload."

            try:
                return await self._in_operation("upload_file", timeout_ms, _upload)
            except OSError as error:
                return self._error(f"Error: could not read a file to upload: {error}")

    return PcodeBrowserToolset


class BrowserState:
    """Whether the browser tools are offered, and the live session behind them."""

    def __init__(self) -> None:
        self.enabled = False
        self.attach = False
        self.session: Any = None
        self.toolset: Any = None
        self.cdp_url: str | None = None
        self.process: subprocess.Popen | None = None
        self._armed = False
        self._launch_lock: asyncio.Lock | None = None

    @property
    def attached(self) -> bool:
        """Joined a Chrome someone else started, which is theirs to close."""
        return self.attach

    def open(self):
        """Build (not launch) the session and toolset; idempotent.

        Raises `ValueError` in attach mode when no running Chrome is found.
        """
        if self.session is None:
            from pydantic_ai_harness.playwright import EgressPolicy

            if self.attach:
                self.cdp_url = running_chrome_url()
                if self.cdp_url is None:
                    raise ValueError(
                        "No running Chrome, Chromium, or Edge with remote debugging found. "
                        "Enable remote debugging in your browser "
                        "(chrome://inspect/#remote-debugging in Chrome), "
                        "or set PCODE_BROWSER_CDP_URL."
                    )
            elif chrome_executable():
                # The port is chosen now so the session can be built before the
                # browser exists; Chrome starts on the first tool call.
                self.cdp_url = f"http://127.0.0.1:{_free_port()}"
            else:
                self.cdp_url = None
            self.session = _session_class()(
                policy=EgressPolicy(block_private_addresses=BLOCK_PRIVATE_ADDRESSES),
                headless=False,
                cdp_url=self.cdp_url,
                auto_install_chromium=True,
            )
            self.toolset = _toolset_class()(
                session=self.session, max_content_tokens=MAX_CONTENT_TOKENS
            )
        return self.toolset

    async def arm(self) -> None:
        """Enter the session once, on the loop the tools run on.

        Entering is cheap and launches nothing. Entering twice would discard the
        driver handle, so it is guarded.
        """
        if self.session is not None and not self._armed:
            await self.session.__aenter__()
            self._armed = True

    async def ensure_chrome(self) -> str:
        """Start Chrome on the debugging port unless one is already answering there.

        Returns a line describing what happened, for a notice. Nothing to do
        when attaching to a foreign endpoint or falling back to Chromium.
        """
        if self.cdp_url is None or self.attached:
            return ""
        if self._launch_lock is None:
            self._launch_lock = asyncio.Lock()
        async with self._launch_lock:
            if self.process is not None and self.process.poll() is None:
                return ""
            if await self._answering():
                return ""
            executable = chrome_executable()
            if executable is None:  # pragma: no cover - open() would have chosen Chromium
                return ""
            port = self.cdp_url.rsplit(":", 1)[1]
            profile = profile_dir()
            profile.mkdir(parents=True, exist_ok=True)
            self.process = subprocess.Popen(
                [
                    executable,
                    f"--remote-debugging-port={port}",
                    f"--user-data-dir={profile}",
                    "--no-first-run",
                    "--no-default-browser-check",
                    "about:blank",
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            deadline = asyncio.get_running_loop().time() + CHROME_START_SECONDS
            while asyncio.get_running_loop().time() < deadline:
                if self.process.poll() is not None:
                    raise RuntimeError(
                        f"Chrome exited at startup (code {self.process.returncode})."
                    )
                if await self._answering():
                    return f"Started Chrome ({executable}) with profile {profile}."
                await asyncio.sleep(0.2)
            raise RuntimeError(
                f"Chrome did not open its debugging port within {CHROME_START_SECONDS}s."
            )

    async def _answering(self) -> bool:
        assert self.cdp_url is not None
        import httpx

        try:
            async with httpx.AsyncClient(timeout=1.0) as client:
                response = await client.get(self.cdp_url.rstrip("/") + "/json/version")
            return response.status_code == 200
        except httpx.HTTPError:
            return False

    async def close(self) -> None:
        """Disconnect, then quit the Chrome we started; safe when nothing was."""
        session, self.session, self.toolset = self.session, None, None
        armed, self._armed = self._armed, False
        process, self.process = self.process, None
        self.cdp_url = None
        self.attach = False
        try:
            if session is not None and armed:
                await session.__aexit__(None, None, None)
        finally:
            if process is not None and process.poll() is None:
                process.terminate()
                try:
                    await asyncio.to_thread(process.wait, 5)
                except subprocess.TimeoutExpired:
                    process.kill()

    async def list_tabs(self) -> list[tuple[str, str]]:
        """Every tab as (title, url), the user's included, without evaluating pages."""
        if self.session is None:
            return []
        return await self.session.list_tabs()

    @property
    def launched(self) -> bool:
        return self.session is not None and self.session.page is not None

    def describe(self) -> str:
        """One line for `/browser status`."""
        if not self.enabled:
            return "off"
        if self.cdp_url is None:
            how = "Playwright's Chromium (no Chrome found; Google sign-in will refuse it)"
        elif self.attached:
            how = f"attached to your browser at {self.cdp_url}"
        else:
            how = f"own Chrome on {self.cdp_url}, profile {profile_dir()}" + (
                "" if self.process is not None and self.process.poll() is None else " (not started)"
            )
        page = self.session.page if self.launched else None
        return f"on, {how}" + (f", at {page.url}" if page is not None else "")


STATE = BrowserState()
