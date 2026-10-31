"""The bundled browser extension: off by default, one session across runs, a sub-agent.

No Chromium here. The launch is Harness's; what pcode adds is the lifecycle
around it, and a fake page stands in for the launched browser.
"""

import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from pydantic_ai.models.function import DeltaToolCall, FunctionModel

import pcode.browser as browser_state
from pcode.agent import create_agent
from pcode.browser import BrowserState
from pcode.ext import ExtensionUI, load_extensions


@pytest.fixture(autouse=True)
def fresh_state(monkeypatch, tmp_path):
    monkeypatch.setattr(browser_state, "STATE", BrowserState())
    monkeypatch.setattr("pcode.extensions.browser.STATE", browser_state.STATE, raising=False)
    # A Chrome that exists but is never started: the session is built for a
    # port nothing answers, and tests that would launch stub `ensure_chrome`.
    fake_chrome = tmp_path / "fake-chrome"
    fake_chrome.touch()
    monkeypatch.setenv("PCODE_BROWSER_CHROME", str(fake_chrome))
    monkeypatch.delenv("PCODE_BROWSER_CDP_URL", raising=False)
    monkeypatch.delenv("PCODE_BROWSER_PORT_FILE", raising=False)
    yield browser_state.STATE


def browser_extension(workspace, ui=None):
    loaded = load_extensions(workspace, ui)
    (extension,) = [e for e in loaded.extensions if e.name == "browser"]
    assert extension.loaded, extension.error
    return loaded, extension


def test_off_by_default_contributes_only_the_command(tmp_path):
    loaded, extension = browser_extension(tmp_path)
    assert extension.capabilities == []
    assert extension.subagents == []
    assert [c.name for c in extension.commands] == ["/browser"]
    assert extension.summary() == "/browser"
    assert loaded.subagents == []
    assert len(extension.closers) == 1


@pytest.mark.parametrize("enabled", [False, True])
def test_rebound_worker_does_not_close_parent_browser(tmp_path, fresh_state, monkeypatch, enabled):
    from unittest.mock import AsyncMock

    fresh_state.enabled = enabled
    close = AsyncMock()
    monkeypatch.setattr(fresh_state, "close", close)
    loaded, extension = browser_extension(tmp_path)
    child = tmp_path / "child"
    child.mkdir()
    assert extension.closers == [close]

    async def scenario():
        async with loaded.capabilities.for_workspace(child):
            close.assert_not_awaited()
        close.assert_not_awaited()
        await loaded.close()
        close.assert_awaited_once_with()

    asyncio.run(scenario())


def test_on_adds_the_tools_and_a_subagent_sharing_one_session(tmp_path, fresh_state):
    fresh_state.enabled = True
    loaded, extension = browser_extension(tmp_path)
    assert extension.summary() == "22 tools, @browser, /browser"
    assert [c.id for c in extension.capabilities] == ["browser", "ext.browser"]
    capability = extension.capabilities[0]
    (delegate,) = loaded.subagents
    assert delegate.resolved_name == "browser"
    # Parent and child drive the same toolset, so the child sees the parent's login.
    child = next(c for c in delegate.agent.root_capability.capabilities if c.id == "browser")
    assert child.get_toolset().toolsets[1] is fresh_state.toolset
    assert "browser_open" in capability.get_toolset().toolsets[0].tools
    assert fresh_state.cdp_url.startswith("http://127.0.0.1:")


def test_command_toggles_state_and_requests_a_reload(tmp_path, fresh_state, monkeypatch):
    reloads = []
    notices = []
    ui = ExtensionUI(lambda text, level: notices.append(level), lambda: reloads.append(True))
    _, extension = browser_extension(tmp_path, ui)
    (command,) = extension.commands

    async def nothing():
        return ""

    monkeypatch.setattr(fresh_state, "arm", nothing)
    monkeypatch.setattr(fresh_state, "ensure_chrome", nothing)
    command.handler("")
    assert notices == ["info"]
    with pytest.raises(ValueError, match="already off"):
        command.handler("off")
    command.handler("launch")
    assert fresh_state.enabled and reloads == [True]
    with pytest.raises(ValueError, match="already open"):
        command.handler("launch")
    command.handler("off")
    assert not fresh_state.enabled and reloads == [True, True]
    # A running turn may still be using it; the reload's `setup` closes it.
    assert fresh_state.session is not None
    browser_extension(tmp_path, ui)
    assert fresh_state.session is None


def test_launch_turns_on_and_starts_chrome(tmp_path, fresh_state, monkeypatch):
    reloads = []
    notices = []
    ui = ExtensionUI(lambda text, level: notices.append((level, text)), lambda: reloads.append(1))
    _, extension = browser_extension(tmp_path, ui)
    started = []

    async def ensure_chrome():
        started.append(fresh_state.cdp_url)
        return "Started Chrome."

    async def navigate(url):
        return url

    monkeypatch.setattr(BrowserState, "ensure_chrome", staticmethod(ensure_chrome))
    monkeypatch.setattr(fresh_state, "arm", ensure_chrome)
    extension.commands[0].handler("launch")
    assert fresh_state.enabled and reloads == [1]
    # `open()` ran, so the session exists before the reload re-imports the extension.
    assert started and fresh_state.session is not None
    assert ("info", "Started Chrome.") in notices
    # The port nothing answers on surfaces as a notice, not a crash.
    assert notices[-1][0] == "error"


def test_a_refused_reload_leaves_state_untouched(tmp_path, fresh_state):
    def refuse():
        raise ValueError("busy")

    _, extension = browser_extension(tmp_path, ExtensionUI(None, refuse))
    with pytest.raises(ValueError, match="busy"):
        extension.commands[0].handler("launch")
    assert not fresh_state.enabled and fresh_state.session is None


def test_a_refused_attach_leaves_nothing_open(tmp_path, fresh_state, monkeypatch):
    monkeypatch.setenv("PCODE_BROWSER_CDP_URL", "ws://127.0.0.1:9333/devtools/browser/x")

    def refuse():
        raise ValueError("no session")

    _, extension = browser_extension(tmp_path, ExtensionUI(None, refuse))
    with pytest.raises(ValueError, match="no session"):
        extension.commands[0].handler("attach")
    assert not fresh_state.enabled and fresh_state.session is None
    assert not fresh_state.attach


def test_delegate_task_lists_the_browser_agent(tmp_path, monkeypatch, fresh_state):
    monkeypatch.delenv("EXA_API_KEY", raising=False)
    fresh_state.enabled = True
    loaded, _ = browser_extension(tmp_path)
    seen = []

    async def respond(messages, info):
        seen.append(info)
        yield "ok"

    agent = create_agent("test", tmp_path, loaded.capabilities, loaded.subagents)
    agent.run_sync("hi", model=FunctionModel(stream_function=respond))
    assert "- browser:" in seen[0].instructions
    names = {tool.name for tool in seen[0].function_tools}
    assert {"delegate_task", "browser_open", "navigate", "snapshot"} <= names


def test_browser_open_fronts_the_current_page(tmp_path, fresh_state, monkeypatch):
    """A sign-in page the user must act on is shown, not replaced with about:blank."""
    fresh_state.enabled = True
    _, extension = browser_extension(tmp_path)
    open_tool = extension.capabilities[0].get_toolset().toolsets[0].tools["browser_open"]
    page = SimpleNamespace(url="https://x/login", fronted=False)

    async def bring_to_front():
        page.fronted = True

    page.bring_to_front = bring_to_front
    navigated = []

    async def navigate(url):
        navigated.append(url)
        fresh_state.session.page = page
        return "navigated"

    async def nothing():
        return ""

    monkeypatch.setattr(fresh_state.toolset, "navigate", navigate)
    monkeypatch.setattr(fresh_state, "arm", nothing)
    monkeypatch.setattr(fresh_state, "ensure_chrome", nothing)
    assert (
        asyncio.run(open_tool.function())
        == "The browser window is in front, showing https://x/login."
    )
    assert navigated == ["about:blank"] and page.fronted
    # With a page already open nothing is navigated.
    assert asyncio.run(open_tool.function()).endswith("https://x/login.")
    assert navigated == ["about:blank"]


def _page(url, *, closed=False, error=None):
    return SimpleNamespace(
        url=url, is_closed=lambda: closed, bring_to_front=AsyncMock(side_effect=error)
    )


@pytest.fixture
def open_browser(tmp_path, fresh_state, monkeypatch):
    """`browser_open` over a session whose browser, context and navigate are fakes."""
    fresh_state.enabled = True
    _, extension = browser_extension(tmp_path)
    tool = extension.capabilities[0].get_toolset().toolsets[0].tools["browser_open"]
    session = fresh_state.session
    session._browser = SimpleNamespace(is_connected=lambda: True)
    session._context = object()
    navigate = AsyncMock(return_value="navigated")
    monkeypatch.setattr(fresh_state.toolset, "navigate", navigate)
    monkeypatch.setattr(fresh_state, "arm", AsyncMock())
    monkeypatch.setattr(fresh_state, "ensure_chrome", AsyncMock(return_value=""))
    return SimpleNamespace(
        run=lambda: asyncio.run(tool.function()), session=session, navigate=navigate
    )


def test_browser_open_replaces_a_closed_tab_in_the_same_context(open_browser, monkeypatch):
    """A tab the user closed is replaced by a new tab, keeping the login; no reconnect."""
    from playwright.async_api import Error as PlaywrightError

    session = open_browser.session
    closed = _page(
        "https://x/old", closed=True, error=PlaywrightError("Target page has been closed")
    )
    fresh = _page("about:blank")

    async def open_tab():
        session.page = fresh
        return fresh

    monkeypatch.setattr(session, "open_tab", open_tab)
    session.page, session.pages = closed, [closed]
    assert open_browser.run().endswith("showing about:blank.")
    closed.bring_to_front.assert_awaited_once()
    fresh.bring_to_front.assert_awaited_once()
    open_browser.navigate.assert_not_awaited()


def test_browser_open_reconnects_when_the_browser_is_gone(open_browser):
    """A dropped connection forgets its pages so navigate reconnects with a fresh tab."""
    from playwright.async_api import Error as PlaywrightError

    session = open_browser.session
    session._browser = SimpleNamespace(is_connected=lambda: False)
    stale = _page("https://x/old", error=PlaywrightError("Browser has been closed"))
    fresh = _page("about:blank")

    async def navigate(url):
        assert session.pages == []
        session.page = fresh
        return "navigated"

    open_browser.navigate.side_effect = navigate
    session.page, session.pages = stale, [stale]
    assert open_browser.run().endswith("showing about:blank.")
    open_browser.navigate.assert_awaited_once_with("about:blank")


def test_browser_open_reports_a_failure_on_a_live_tab(open_browser):
    """An error on a tab that is still open is reported, and the tab is kept."""
    from playwright.async_api import Error as PlaywrightError

    session = open_browser.session
    live = _page("https://x/login", error=PlaywrightError("Protocol error"))
    session.page, session.pages = live, [live]
    assert open_browser.run() == "Could not bring the browser to the front: Protocol error"
    assert session.page is live and session.pages == [live]


def test_browser_open_surfaces_why_navigate_failed(open_browser):
    open_browser.navigate.return_value = "Browser unavailable: no Chrome on port 9222"
    assert open_browser.run() == (
        "The browser could not be opened: Browser unavailable: no Chrome on port 9222"
    )


def test_browser_open_gives_up_on_a_browser_that_keeps_closing(open_browser):
    from playwright.async_api import Error as PlaywrightError

    session = open_browser.session
    session._browser = SimpleNamespace(is_connected=lambda: False)

    async def navigate(url):
        session.page = _page(url, error=PlaywrightError("closed"))
        return "navigated"

    open_browser.navigate.side_effect = navigate
    assert open_browser.run().startswith("Could not bring the browser to the front: its window")
    assert open_browser.navigate.await_count == 2
    assert session.page is None


def test_browser_tabs_lists_the_users_tabs_and_marks_ours(tmp_path, fresh_state, monkeypatch):
    """Tabs come from Chrome's target list, never from the pages, so a hung tab cannot stall it."""
    fresh_state.enabled = True
    _, extension = browser_extension(tmp_path)
    tabs = extension.capabilities[0].get_toolset().toolsets[0].tools["browser_tabs"]
    fresh_state.session.pages = []

    async def nothing():
        return ""

    async def list_tabs():
        # The first query connects lazily and opens our tab.
        fresh_state.session.pages.append(SimpleNamespace(url="about:blank"))
        return [
            ("Inbox", "https://mail.example.com/inbox"),
            ("", "https://x/"),
            ("", "about:blank"),
        ]

    monkeypatch.setattr(fresh_state, "arm", nothing)
    monkeypatch.setattr(fresh_state, "ensure_chrome", nothing)
    monkeypatch.setattr(fresh_state, "list_tabs", list_tabs)
    assert asyncio.run(tabs.function()).splitlines() == [
        "- Inbox: https://mail.example.com/inbox",
        "- (untitled): https://x/",
        "- (untitled) (yours): about:blank",
    ]


@pytest.fixture
def cdp_session(fresh_state, monkeypatch):
    monkeypatch.setenv("PCODE_BROWSER_CDP_URL", "ws://127.0.0.1:9333/devtools/browser/abc")
    fresh_state.attach = True
    fresh_state.open()
    cdp = SimpleNamespace(
        send=AsyncMock(return_value={"targetInfos": []}),
        detach=AsyncMock(),
    )
    browser = SimpleNamespace(new_browser_cdp_session=AsyncMock(return_value=cdp))

    async def ensure_page():
        fresh_state.session._browser = browser

    monkeypatch.setattr(fresh_state.session, "ensure_page", AsyncMock(side_effect=ensure_page))
    return cdp, browser


def test_list_tabs_uses_cdp_without_http_discovery(fresh_state, monkeypatch, cdp_session):
    import httpx

    cdp, browser = cdp_session
    # Edge's debugging switch returns an empty 404 here, not a JSON target list.
    http = Mock(return_value=httpx.Response(404, content=b""))
    real = httpx.AsyncClient
    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(http), **kw)
    )
    cdp.send.return_value = {
        "targetInfos": [
            {"type": "page", "title": "Inbox", "url": "https://mail.example.com/"},
            {"type": "service_worker", "title": "sw", "url": "https://mail.example.com/sw.js"},
            {"type": "page"},
        ]
    }
    assert asyncio.run(fresh_state.list_tabs()) == [
        ("Inbox", "https://mail.example.com/"),
        ("", ""),
    ]
    fresh_state.session.ensure_page.assert_awaited_once_with()
    browser.new_browser_cdp_session.assert_awaited_once_with()
    cdp.send.assert_awaited_once_with("Target.getTargets")
    cdp.detach.assert_awaited_once_with()
    http.assert_not_called()


def test_list_tabs_is_empty_without_a_session(fresh_state):
    assert asyncio.run(fresh_state.list_tabs()) == []


@pytest.mark.parametrize("stage", ["connect", "send", "detach"])
@pytest.mark.parametrize("failure", ["error", "timeout"])
def test_list_tabs_bounds_cdp_operations_and_detaches(fresh_state, cdp_session, stage, failure):
    from playwright.async_api import Error as PlaywrightError
    from playwright.async_api import TimeoutError as PlaywrightTimeoutError

    cdp, browser = cdp_session
    operation = {
        "connect": browser.new_browser_cdp_session,
        "send": cdp.send,
        "detach": cdp.detach,
    }[stage]
    if failure == "error":
        operation.side_effect = PlaywrightError("disconnected")
        error = PlaywrightError
    else:

        async def stalled(*args):
            await asyncio.Event().wait()

        operation.side_effect = stalled
        fresh_state.session._launch_timeout_ms = 10
        error = PlaywrightTimeoutError
    with pytest.raises(error):
        asyncio.run(fresh_state.list_tabs())
    if stage == "connect":
        cdp.send.assert_not_awaited()
        cdp.detach.assert_not_awaited()
    else:
        cdp.detach.assert_awaited_once_with()


@pytest.mark.parametrize("failure", ["disconnected", "unavailable"])
def test_browser_tabs_failure_is_a_tool_result_not_a_failed_turn(
    tmp_path, fresh_state, monkeypatch, failure
):
    from playwright.async_api import Error as PlaywrightError
    from pydantic_ai.messages import ToolReturnPart
    from pydantic_ai_harness.playwright import BrowserUnavailableError

    fresh_state.enabled = True
    loaded, _ = browser_extension(tmp_path)
    error = (
        PlaywrightError("disconnected")
        if failure == "disconnected"
        else BrowserUnavailableError("unavailable")
    )
    monkeypatch.setattr(fresh_state, "arm", AsyncMock())
    monkeypatch.setattr(fresh_state, "ensure_chrome", AsyncMock(return_value=""))
    monkeypatch.setattr(fresh_state, "list_tabs", AsyncMock(side_effect=error))
    fresh_state.session.launch_error = "previous failure"
    results = []

    async def respond(messages, info):
        if len(messages) == 1:
            yield {0: DeltaToolCall(name="browser_tabs", json_args="{}")}
        else:
            results.extend(
                part.content for part in messages[-1].parts if isinstance(part, ToolReturnPart)
            )
            yield "Browser unavailable; reconnect it."

    agent = create_agent("test", tmp_path, loaded.capabilities, loaded.subagents)
    result = agent.run_sync("List my browser tabs", model=FunctionModel(stream_function=respond))
    assert result.output == "Browser unavailable; reconnect it."
    assert len(results) == 1
    assert "Could not list browser tabs" in results[0]
    assert "/browser attach" in results[0]
    assert fresh_state.session.launch_error is None


def test_guidance_says_logins_persist_per_mode(tmp_path, fresh_state, monkeypatch):
    fresh_state.enabled = True
    _, extension = browser_extension(tmp_path)
    text = str(extension.capabilities[0].get_instructions())
    assert "persists between conversations" in text

    asyncio.run(fresh_state.close())
    monkeypatch.setenv("PCODE_BROWSER_CDP_URL", "ws://127.0.0.1:1/devtools/browser/x")
    fresh_state.enabled = fresh_state.attach = True
    _, extension = browser_extension(tmp_path)
    assert "user's own Chrome" in str(extension.capabilities[0].get_instructions())


def test_close_is_safe_before_launch_and_drops_the_session(fresh_state):
    fresh_state.open()
    assert fresh_state.session is not None and not fresh_state.launched
    asyncio.run(fresh_state.close())
    assert fresh_state.session is None and fresh_state.toolset is None
    assert fresh_state.cdp_url is None


def test_without_chrome_the_session_falls_back_to_chromium(fresh_state, monkeypatch):
    monkeypatch.setenv("PCODE_BROWSER_CHROME", "")
    monkeypatch.setattr(browser_state, "CHROME_CANDIDATES", ("/nonexistent/chrome",))
    fresh_state.enabled = True
    fresh_state.open()
    assert fresh_state.cdp_url is None
    assert "no Chrome found" in fresh_state.describe()
    assert asyncio.run(fresh_state.ensure_chrome()) == ""


def test_attach_joins_a_running_chrome_and_never_launches(fresh_state, monkeypatch):
    monkeypatch.setenv("PCODE_BROWSER_CDP_URL", "http://127.0.0.1:9222")
    fresh_state.enabled = True
    fresh_state.attach = True
    fresh_state.open()
    assert fresh_state.attached and fresh_state.cdp_url == "http://127.0.0.1:9222"
    assert asyncio.run(fresh_state.ensure_chrome()) == ""
    assert fresh_state.process is None
    assert "attached to your browser" in fresh_state.describe()
    asyncio.run(fresh_state.close())
    assert not fresh_state.attach


def test_attach_reads_chromes_port_file(fresh_state, monkeypatch, tmp_path):
    from pcode.browser import running_chrome_url

    port_file = tmp_path / "DevToolsActivePort"
    monkeypatch.setenv("PCODE_BROWSER_PORT_FILE", str(port_file))
    assert running_chrome_url() is None
    port_file.write_text("9333\n/devtools/browser/abc\n")
    assert running_chrome_url() == "ws://127.0.0.1:9333/devtools/browser/abc"


@pytest.mark.parametrize(
    "profile",
    [
        "Library/Application Support/Google/Chrome",
        "Library/Application Support/Chromium",
        ".config/google-chrome",
        ".config/chromium",
        "Library/Application Support/Microsoft Edge",
        ".config/microsoft-edge",
    ],
)
def test_attach_discovers_browser_profiles(tmp_path, monkeypatch, profile):
    monkeypatch.setattr(browser_state.Path, "home", lambda: tmp_path)
    directory = tmp_path / profile
    directory.mkdir(parents=True)
    (directory / "DevToolsActivePort").write_text("9333\n/devtools/browser/abc\n")
    assert browser_state.running_chrome_url() == "ws://127.0.0.1:9333/devtools/browser/abc"


def test_explicit_endpoint_and_port_file_override_discovery(tmp_path, monkeypatch):
    monkeypatch.setattr(browser_state.Path, "home", lambda: tmp_path)
    directory = tmp_path / "Library/Application Support/Microsoft Edge"
    directory.mkdir(parents=True)
    (directory / "DevToolsActivePort").write_text("9333\n/devtools/browser/edge\n")
    port_file = tmp_path / "custom-port"
    port_file.write_text("9444\n/devtools/browser/custom\n")
    monkeypatch.setenv("PCODE_BROWSER_PORT_FILE", str(port_file))
    assert browser_state.running_chrome_url() == "ws://127.0.0.1:9444/devtools/browser/custom"
    monkeypatch.setenv("PCODE_BROWSER_CDP_URL", "http://127.0.0.1:9555")
    assert browser_state.running_chrome_url() == "http://127.0.0.1:9555"


def test_attach_command_fails_cleanly_without_any_chrome(tmp_path, fresh_state, monkeypatch):
    monkeypatch.setenv("PCODE_BROWSER_PORT_FILE", str(tmp_path / "missing"))
    monkeypatch.setenv("PCODE_BROWSER_CHROME", "/nonexistent/chrome")
    reloads = []
    _, extension = browser_extension(tmp_path, ExtensionUI(None, lambda: reloads.append(1)))
    with pytest.raises(ValueError, match="No running Chrome"):
        extension.commands[0].handler("attach")
    assert not fresh_state.attach and not fresh_state.enabled and reloads == []


def test_attach_opens_the_debugging_switch_when_chrome_is_not_listening(
    tmp_path, fresh_state, monkeypatch
):
    monkeypatch.setenv("PCODE_BROWSER_PORT_FILE", str(tmp_path / "missing"))
    launched = []
    monkeypatch.setattr(browser_state, "_detach", launched.append)
    app = tmp_path / "Google Chrome.app" / "Contents" / "MacOS"
    app.mkdir(parents=True)
    (app / "Google Chrome").touch()
    monkeypatch.setenv("PCODE_BROWSER_CHROME", str(app / "Google Chrome"))
    monkeypatch.setattr(browser_state.sys, "platform", "darwin")
    notices = []
    _, extension = browser_extension(tmp_path, ExtensionUI(lambda text, _: notices.append(text)))
    extension.commands[0].handler("attach")
    page = "chrome://inspect/#remote-debugging"
    assert launched == [["open", "-a", str(tmp_path / "Google Chrome.app"), page]]
    assert "Turn the switch on" in notices[-1]
    assert not fresh_state.attach and not fresh_state.enabled

    monkeypatch.setattr(browser_state.sys, "platform", "linux")
    linux = tmp_path / "google-chrome"
    linux.touch()
    monkeypatch.setenv("PCODE_BROWSER_CHROME", str(linux))
    extension.commands[0].handler("attach")
    assert launched[-1] == [str(linux), page]


def test_command_arguments_are_described_for_completion(tmp_path):
    _, extension = browser_extension(tmp_path)
    (command,) = extension.commands
    assert set(command.argument_descriptions) == set(command.arguments)
    assert "remote debugging" in command.argument_descriptions["attach"]


def test_attach_command_turns_on_with_a_warning(tmp_path, fresh_state, monkeypatch):
    monkeypatch.setenv("PCODE_BROWSER_CDP_URL", "ws://127.0.0.1:9333/devtools/browser/x")
    reloads, notices = [], []
    ui = ExtensionUI(lambda text, level: notices.append(level), lambda: reloads.append(1))
    _, extension = browser_extension(tmp_path, ui)
    extension.commands[0].handler("attach")
    assert fresh_state.attach and fresh_state.enabled and reloads == [1]
    assert notices[-1] == "warning"
    with pytest.raises(ValueError, match="already open"):
        extension.commands[0].handler("attach")


def test_session_shares_the_browsers_default_context(fresh_state):
    """The page joins `contexts[0]` so cookies persist with the profile, and is wired."""
    fresh_state.open()
    session = fresh_state.session
    calls = []

    class Page:
        url = "about:blank"

        def on(self, *args):
            calls.append(("on", args[0]))

        async def bring_to_front(self):
            calls.append("page.front")

        async def close(self):
            calls.append("page.close")

    class Context:
        pages = []

        async def new_page(self):
            return Page()

        async def route(self, pattern, handler):
            calls.append(("route", pattern))

        async def route_web_socket(self, pattern, handler):
            calls.append(("ws", pattern))

    class Browser:
        contexts = [Context()]

        async def new_context(self, **kwargs):  # pragma: no cover
            raise AssertionError("must reuse the default context")

        async def close(self):
            calls.append("browser.close")

    async def scenario():
        session._driver_cm = SimpleNamespace(__aexit__=_aexit)
        session._driver = object()
        session._driver_entered = True
        session._connect = _connect
        await session._launch()
        assert session._context is Browser.contexts[0]
        assert session.page is not None and session.pages == [session.page]
        await session.__aexit__(None, None, None)

    async def _connect(driver):
        return Browser()

    async def _aexit(*args):
        calls.append("driver.exit")

    asyncio.run(scenario())
    # Open egress plus reachable localhost means no route guard; the page events
    # Harness listens to are still wired, and the tab is closed before disconnecting.
    assert ("on", "dialog") in calls
    assert calls.count("page.front") == 1  # shown once, on creation
    assert calls[-3:] == ["page.close", "browser.close", "driver.exit"]


def test_chrome_that_never_answers_is_reported(fresh_state, monkeypatch, tmp_path):
    """A binary that exits at once yields a clear error rather than a hang."""
    fake = tmp_path / "chrome"
    fake.write_text("#!/bin/sh\nexit 3\n")
    fake.chmod(0o755)
    monkeypatch.setenv("PCODE_BROWSER_CHROME", str(fake))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    fresh_state.enabled = True
    fresh_state.open()
    with pytest.raises(RuntimeError, match="exited at startup"):
        asyncio.run(fresh_state.ensure_chrome())
    assert (tmp_path / "state" / "pcode" / "chrome").is_dir()


def test_extension_closers_run_on_exit():
    from pcode.ext import Extension, LoadedExtensions

    closed = []

    async def ok():
        closed.append("ok")

    async def bad():
        raise RuntimeError("nope")

    first = Extension("first", None, "user")
    first.closers = [bad, ok]
    asyncio.run(LoadedExtensions([first]).close())
    assert closed == ["ok"]


def test_subagent_reaches_delegate_task(tmp_path, monkeypatch):
    """The generic plumbing: any extension can add a delegate beside the worker."""
    from pydantic_ai import Agent

    from pcode.ext import ExtensionAPI

    monkeypatch.delenv("EXA_API_KEY", raising=False)
    api = ExtensionAPI("mine", tmp_path, ExtensionUI())
    api.subagent(Agent(name="helper", description="Helps out", instructions="Help."))
    (delegate,) = api.subagents
    calls = 0

    async def respond(messages, info):
        nonlocal calls
        calls += 1
        if calls == 1:
            yield {
                0: DeltaToolCall(
                    name="delegate_task", json_args='{"agent_name": "helper", "task": "go"}'
                )
            }
            return
        yield "done"

    agent = create_agent("test", tmp_path, subagents=[delegate])
    assert agent.run_sync("hi", model=FunctionModel(stream_function=respond)).output == "done"
    assert calls == 3  # parent, child, parent


def _toolset_on(fresh_state, page):
    """The pcode toolset driving `page`, with no browser behind it."""
    toolset = fresh_state.open()
    fresh_state.session.ensure_page = AsyncMock(return_value=page)
    fresh_state.session.page = page
    return toolset


def _fake_page(**overrides):
    page = SimpleNamespace(**{"url": "https://x/doc", "frames": [], **overrides})
    page.frames = page.frames or [page]  # The main frame alone: no child frames.
    return page


def test_reads_come_back_whole_while_action_text_stays_capped(fresh_state):
    """A capped read was cut to the spill threshold, so the spilled copy lost data."""
    long = "x" * 50_000
    page = _fake_page(
        evaluate=AsyncMock(return_value=long), inner_text=AsyncMock(return_value=long)
    )
    toolset = _toolset_on(fresh_state, page)

    async def scenario():
        assert await toolset.execute_js("big()") == long
        assert await toolset.get_text("#data") == long
        assert len(await toolset.get_text()) == len(long)

    asyncio.run(scenario())
    capped = toolset._truncate_output(long)
    assert len(capped) <= browser_state.MAX_CONTENT_TOKENS * 4


def test_frame_sweep_skips_frames_that_are_not_rendered(fresh_state):
    """A hidden frame's innerText is its raw text, inline scripts and all."""

    main = SimpleNamespace(parent_frame=None)
    elements = []

    def frame(url, rendered, text, parent=main):
        element = SimpleNamespace(evaluate=AsyncMock(return_value=rendered), dispose=AsyncMock())
        elements.append(element)
        return SimpleNamespace(
            url=url,
            parent_frame=parent,
            frame_element=AsyncMock(return_value=element),
            inner_text=AsyncMock(return_value=text),
        )

    hidden = frame("https://x/hidden", False, "window.WIZ_global_data = {...}")
    # Shown in its own box, but inside the hidden frame, so not laid out either.
    nested = frame("https://x/nested", True, "var junk = 1;", parent=hidden)
    shown = frame("https://x/shown", True, "checkout")
    page = _fake_page(frames=[main, hidden, nested, shown])
    toolset = _toolset_on(fresh_state, page)
    texts = asyncio.run(toolset._frame_text(page, 1000))
    assert texts == ["[frame https://x/shown]\ncheckout"]
    hidden.inner_text.assert_not_awaited()
    nested.inner_text.assert_not_awaited()
    # Every handle is released: a page that never navigates would pile them up.
    assert all(e.dispose.await_count == e.evaluate.await_count for e in elements)


def test_screenshots_are_taken_in_css_pixels(fresh_state):
    """On HiDPI a device-pixel image is twice the coordinates `click('x,y')` takes."""
    page = _fake_page(screenshot=AsyncMock(return_value=b"\x89PNG"))
    toolset = _toolset_on(fresh_state, page)
    result = asyncio.run(toolset.screenshot())
    assert page.screenshot.await_args.kwargs["scale"] == "css"
    assert result.content[0].data == b"\x89PNG"


def _response(status, body=b"", location=None):
    return SimpleNamespace(
        status=status,
        ok=200 <= status < 300,
        headers={"location": location} if location else {},
        body=AsyncMock(return_value=body),
        dispose=AsyncMock(),
    )


def test_save_file_uses_the_logins_cookies_then_falls_back_to_the_page(fresh_state, tmp_path):
    import base64

    responses = [_response(200, b"%PDF-1.4"), _response(403)]
    request = SimpleNamespace(get=AsyncMock(side_effect=responses))
    page = _fake_page(
        context=SimpleNamespace(request=request),
        evaluate=AsyncMock(return_value=base64.b64encode(b"from the tab").decode()),
    )
    toolset = _toolset_on(fresh_state, page)

    async def scenario():
        fetched = await toolset.download(tmp_path / "a" / "lab.pdf", "https://x/lab.pdf")
        assert fetched.startswith("Saved 8 bytes")
        # The signed link expired, but the tab still holds what it loaded.
        fallback = await toolset.download(tmp_path / "tab.pdf")
        assert fallback.startswith("Saved 12 bytes")

    asyncio.run(scenario())
    assert (tmp_path / "a" / "lab.pdf").read_bytes() == b"%PDF-1.4"
    assert (tmp_path / "tab.pdf").read_bytes() == b"from the tab"
    assert request.get.await_args_list[1].args[0] == "https://x/doc"


def test_upload_file_fills_an_input_or_answers_the_picker(fresh_state, tmp_path):
    from contextlib import asynccontextmanager

    upload = tmp_path / "labs.pdf"
    upload.write_bytes(b"%PDF")
    chooser = SimpleNamespace(set_files=AsyncMock())

    def locator(is_input):
        target = SimpleNamespace(
            evaluate=AsyncMock(return_value=is_input),
            set_input_files=AsyncMock(),
            click=AsyncMock(),
        )
        return SimpleNamespace(first=target)

    @asynccontextmanager
    async def expect_file_chooser(timeout):
        async def value():
            return chooser

        yield SimpleNamespace(value=value())

    targets = {"#input": locator(True), "#button": locator(False)}
    page = _fake_page(locator=targets.__getitem__, expect_file_chooser=expect_file_chooser)
    toolset = _toolset_on(fresh_state, page)

    async def scenario():
        assert (await toolset.upload("#input", [upload])).startswith("Gave labs.pdf")
        assert (await toolset.upload("#button", [upload])).startswith("Gave labs.pdf")
        missing = await toolset.upload("#input", [tmp_path / "gone.pdf"])
        assert missing.startswith("Error: no such file")

    asyncio.run(scenario())
    targets["#input"].first.set_input_files.assert_awaited_once()
    targets["#button"].first.click.assert_awaited_once()
    chooser.set_files.assert_awaited_once()


def test_file_tools_resolve_paths_against_the_workspace(tmp_path, fresh_state):
    fresh_state.enabled = True
    _, extension = browser_extension(tmp_path)
    tools = extension.capabilities[0].get_toolset().toolsets[0].tools
    fresh_state.toolset.download = AsyncMock(return_value="saved")
    result = asyncio.run(tools["save_file"].function("out/lab.pdf", url="https://x/a"))
    assert result == "saved"
    assert fresh_state.toolset.download.await_args.args[0] == tmp_path / "out" / "lab.pdf"
    both = asyncio.run(tools["save_file"].function("a", url="https://x", selector="#b"))
    assert both.startswith("Error")
    assert "save_file" in str(extension.capabilities[0].get_instructions())


def test_save_file_checks_every_redirect_against_the_egress_policy(fresh_state, tmp_path):
    """The request client is not behind the page's route guard, so each hop is checked."""
    request = SimpleNamespace(
        get=AsyncMock(side_effect=[_response(302, location="https://evil/steal")])
    )
    page = _fake_page(context=SimpleNamespace(request=request))
    toolset = _toolset_on(fresh_state, page)

    async def decide(egress):
        return "refused host" if "evil" in egress.url else None

    fresh_state.session.decide = decide
    result = asyncio.run(toolset.download(tmp_path / "f.pdf", "/report.pdf"))
    assert result == "Error: refused host: https://evil/steal"
    # A relative URL is the page's: resolved against it before anything is fetched.
    assert request.get.await_args.args[0] == "https://x/report.pdf"
    assert request.get.await_args.kwargs["max_redirects"] == 0
    assert not (tmp_path / "f.pdf").exists()


def test_save_file_fetches_a_download_playwright_cannot_save(fresh_state, tmp_path):
    """Over CDP, Chrome's default context keeps downloads from Playwright."""
    from contextlib import asynccontextmanager

    from playwright.async_api import Error as PlaywrightError

    download = SimpleNamespace(
        url="https://x/export.csv", save_as=AsyncMock(side_effect=PlaywrightError("no"))
    )

    @asynccontextmanager
    async def expect_download(timeout):
        async def value():
            return download

        yield SimpleNamespace(value=value())

    request = SimpleNamespace(get=AsyncMock(return_value=_response(200, b"a,b")))
    page = _fake_page(
        context=SimpleNamespace(request=request),
        expect_download=expect_download,
        click=AsyncMock(),
    )
    toolset = _toolset_on(fresh_state, page)
    result = asyncio.run(toolset.download(tmp_path / "out.csv", selector="#export"))
    assert result.startswith("Saved 3 bytes")
    page.click.assert_awaited_once()
    assert request.get.await_args.args[0] == "https://x/export.csv"


def test_upload_file_drops_files_on_a_zone(fresh_state, tmp_path):
    import base64

    upload = tmp_path / "labs.pdf"
    upload.write_bytes(b"%PDF")
    target = SimpleNamespace(evaluate=AsyncMock())
    page = _fake_page(locator=lambda selector: SimpleNamespace(first=target))
    toolset = _toolset_on(fresh_state, page)
    result = asyncio.run(toolset.upload("#zone", [upload], drop=True))
    assert result.startswith("Dropped labs.pdf")
    script, payload = target.evaluate.await_args.args
    assert "DataTransfer" in script
    assert payload == [
        {"name": "labs.pdf", "type": "application/pdf", "data": base64.b64encode(b"%PDF").decode()}
    ]


def test_file_tools_refuse_protected_and_credential_paths(tmp_path, fresh_state):
    """Even with the sandbox off: a page can ask for either."""
    from pcode import sandbox

    fresh_state.enabled = True
    _, extension = browser_extension(tmp_path)
    tools = extension.capabilities[0].get_toolset().toolsets[0].tools
    fresh_state.toolset.download = AsyncMock()
    fresh_state.toolset.upload = AsyncMock()
    hook = asyncio.run(tools["save_file"].function(".git/hooks/pre-commit", url="https://x"))
    assert "protected location" in hook
    key = str(sandbox.real(Path.home()) / ".ssh" / "id_ed25519")
    sent = asyncio.run(tools["upload_file"].function("#f", [key]))
    assert "holds credentials" in sent
    fresh_state.toolset.download.assert_not_awaited()
    fresh_state.toolset.upload.assert_not_awaited()


def test_harness_browser_internals_still_exist():
    """`PcodeBrowserToolset` overrides and calls these; a rename must fail here, loudly."""
    import inspect

    from pydantic_ai_harness.playwright import PlaywrightBrowserToolset
    from pydantic_ai_harness.playwright import _toolset as harness

    assert callable(harness._truncate) and callable(harness._without_credentials)
    for name in (
        "_truncate_output",
        "_frame_text",
        "screenshot",
        "_in_operation",
        "_refuse",
        "_error",
        "_await_with_timeout",
        "_oversized_screenshot_error",
    ):
        assert callable(getattr(PlaywrightBrowserToolset, name)), name
    assert list(inspect.signature(PlaywrightBrowserToolset._frame_text).parameters) == [
        "self",
        "page",
        "budget_ms",
    ]
    assert (
        "governed_by_navigation"
        in inspect.signature(PlaywrightBrowserToolset._in_operation).parameters
    )
