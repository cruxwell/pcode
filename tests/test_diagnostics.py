import asyncio
import json

import pytest
from pydantic_ai import Agent
from pydantic_ai.exceptions import ModelAPIError
from pydantic_ai.models.function import FunctionModel

from pcode.diagnostics import REPORT_CHARS, error_details, error_report
from pcode.live import AgentRuntime
from pcode.sessions import SavedSession


def test_connection_cause_is_saved_and_redacted(tmp_path, monkeypatch):
    monkeypatch.setenv("EXAMPLE_TOKEN", "sensitive-environment-value")

    async def model(messages, info):
        try:
            try:
                raise OSError(
                    "Connection refused: http://proxy-user:proxy-pass@localhost:8080 "
                    "Bearer sensitive-environment-value; refresh_token=hidden"
                )
            except OSError as error:
                raise ConnectionError("Proxy connection failed") from error
        except ConnectionError as error:
            raise ModelAPIError("test:local", "Connection error.") from error
        yield "unreachable"

    saved = SavedSession.create("test:local", tmp_path, tmp_path / "sessions")
    runtime = AgentRuntime(Agent(FunctionModel(stream_function=model)), saved)

    async def run():
        with pytest.raises(ModelAPIError):
            _ = [event async for event in runtime.stream("hello")]

    try:
        asyncio.run(run())
        records = [
            json.loads(line)
            for line in (saved.directory / "transcript.jsonl").read_text().splitlines()
        ]
        detail = next(r["error"] for r in records if r["kind"] == "turn_failed")
        assert detail["type"] == "ModelAPIError"
        assert detail["message"] == "Connection error."
        assert detail["cause"]["type"] == "ConnectionError"
        assert detail["cause"]["cause"]["type"] == "OSError"
        assert "Connection refused" in detail["cause"]["cause"]["message"]
        assert "localhost:8080" in detail["cause"]["cause"]["message"]
        for secret in ("proxy-user", "proxy-pass", "sensitive-environment-value", "hidden"):
            assert secret not in json.dumps(detail)
    finally:
        runtime.close()


def failing_runtime(tmp_path, error: BaseException):
    """A runtime whose model always fails inside a named frame."""

    async def model(messages, info):
        raise error
        yield "unreachable"

    saved = SavedSession.create("test:local", tmp_path, tmp_path / "sessions")
    return AgentRuntime(Agent(FunctionModel(stream_function=model)), saved), saved


def drive(runtime, expected: type[BaseException]) -> None:
    async def run():
        with pytest.raises(expected):
            _ = [event async for event in runtime.stream("hello")]

    asyncio.run(run())


def test_failed_turn_saves_a_redacted_traceback(tmp_path, monkeypatch):
    """A type and a message name the symptom; only the frames name the line."""
    monkeypatch.setenv("EXAMPLE_TOKEN", "sensitive-environment-value")
    runtime, saved = failing_runtime(
        tmp_path, AttributeError("'NoneType' object has no attribute 'usage'")
    )
    try:
        drive(runtime, AttributeError)
        report = (saved.directory / "errors.log").read_text()
    finally:
        runtime.close()

    assert "AttributeError: 'NoneType' object has no attribute 'usage'" in report
    # The frames are the point: without them the transcript already suffices.
    assert 'File "' in report and "in model" in report
    assert "sensitive-environment-value" not in report
    assert (saved.directory / "errors.log").stat().st_mode & 0o777 == 0o600


def test_cancelled_turn_saves_no_traceback(tmp_path):
    """Stopping on purpose is not a defect; only failures are worth frames."""
    runtime, saved = failing_runtime(tmp_path, asyncio.CancelledError())
    try:
        drive(runtime, asyncio.CancelledError)
    finally:
        runtime.close()
    assert not (saved.directory / "errors.log").exists()


def test_reports_are_bounded_and_keep_the_innermost_frames():
    error = RuntimeError("x" * (REPORT_CHARS * 2))
    error.__cause__ = OSError("the original failure")
    report = error_report(error)
    assert len(report) <= REPORT_CHARS + len("[earlier frames omitted]\n")
    assert report.startswith("[earlier frames omitted]")
    assert report.rstrip().endswith("x" * 20)


def test_implicit_context_and_explicit_cause_precedence():
    error = RuntimeError("wrapper")
    error.__context__ = OSError("DNS lookup failed")
    assert error_details(error)["context"]["message"] == "DNS lookup failed"
    error.__cause__ = TimeoutError("Connection timed out")
    details = error_details(error)
    assert details["cause"]["type"] == "TimeoutError"
    assert "context" not in details
    error.__cause__ = None
    assert "context" not in error_details(error)  # Equivalent to `raise ... from None`.


def test_grouped_causes_cycles_and_depth_are_bounded():
    error = RuntimeError("wrapper")
    error.__cause__ = ExceptionGroup("transport", [OSError("TLS verification failed")])
    assert error_details(error)["cause"]["message"] == "TLS verification failed"
    error.__cause__ = error
    assert error_details(error)["cause"]["truncated"] is True
    for _ in range(30):
        wrapper = RuntimeError("another wrapper")
        wrapper.__cause__ = error
        error = wrapper
    detail = error_details(error)
    for _ in range(16):
        detail = detail["cause"]
    assert detail["truncated"] is True


@pytest.mark.parametrize(
    ("transport", "expected"),
    [
        ("RemoteProtocolError", "closed or returned an incomplete/invalid response"),
        ("ReadTimeout", "timed out"),
        ("ConnectError", "Could not communicate"),
    ],
)
def test_wrapped_transport_failure_has_actionable_safe_message(transport, expected):
    import httpx2
    from openai import APIConnectionError

    from pcode.live import error_message

    request = httpx2.Request("POST", "https://user:secret@example.com/responses")
    cause = getattr(httpx2, transport)("sensitive transport body", request=request)
    sdk_error = APIConnectionError(request=request)
    sdk_error.__cause__ = cause
    error = ModelAPIError("test:local", "Connection error.")
    error.__cause__ = sdk_error

    message = error_message(error)
    assert expected in message
    assert "saved session diagnostics" in message
    for secret in ("sensitive", "secret", "example.com"):
        assert secret not in message
    assert error_message(ExceptionGroup("wrapped", [error])) == message


def test_transport_classification_respects_suppressed_context_and_cycles():
    import httpx2

    from pcode.live import error_message

    error = ModelAPIError("test:local", "private body")
    error.__context__ = httpx2.RemoteProtocolError("private transport body")
    error.__suppress_context__ = True
    assert "Run failed (ModelAPIError)" in error_message(error)
    error.__cause__ = error
    assert "Run failed (ModelAPIError)" in error_message(error)


def test_stale_install_names_a_merge_or_reinstall_since_startup(tmp_path, monkeypatch):
    import sysconfig

    import pcode
    from pcode.diagnostics import stale_install

    monkeypatch.setattr(pcode, "STARTED", float("inf"))
    assert stale_install() is None
    monkeypatch.setattr(pcode, "STARTED", 0.0)
    assert "source changed" in stale_install()
    # A reinstall on another Python deletes this interpreter's site-packages.
    monkeypatch.setattr(sysconfig, "get_paths", lambda: {"purelib": str(tmp_path / "gone")})
    assert "reinstalled" in stale_install()


def test_failed_slash_command_blames_the_command_not_the_provider(tmp_path, monkeypatch):
    from io import StringIO

    from rich.console import Console

    import pcode
    from pcode.app import PreviewApp

    monkeypatch.setattr(pcode, "STARTED", 0.0)
    saved = SavedSession.create("test:local", tmp_path, tmp_path / "sessions")
    try:
        runtime = AgentRuntime(Agent(FunctionModel(lambda *_: None)), session=saved)
        output = StringIO()
        console = Console(file=output, width=500)
        app = PreviewApp(model="test:local", runtime=runtime, console=console)
        app.controller.command_failed("/diffs", KeyError("popup_mouse"))
        rendered = StringIO()
        console = Console(file=rendered, width=200, theme=app.transcript.rich_theme)
        for objects, end, _ in app.transcript.replay():
            console.print(*objects, end=end)
        shown = rendered.getvalue()
        assert "popup_mouse" in (saved.directory / "errors.log").read_text()
    finally:
        saved.close()
    assert "/diffs failed (KeyError)." in shown
    assert "Check the model string" not in shown
    assert "source changed since this session started" in shown
    # Notes print once rather than replaying.
    assert "errors.log" in output.getvalue()


def test_error_report_survives_its_own_module_disappearing(monkeypatch):
    """A removed session worktree takes `pcode.live` with it, mid-session."""
    import sys

    from pcode.error_report import error_message

    class Deleted:
        """`pcode.live` is no longer on disk; nothing can import it again."""

        def find_spec(self, name, path=None, target=None):
            if name == "pcode.live":
                raise ModuleNotFoundError(f"No module named {name!r}", name=name)
            return None

    monkeypatch.delitem(sys.modules, "pcode.live", raising=False)
    monkeypatch.setattr(sys, "meta_path", [Deleted(), *sys.meta_path])

    message = error_message(KeyError("popup_mouse"), unexpected="/diffs failed")
    assert message.startswith("/diffs failed (KeyError), and describing it failed")
    assert "Restart pcode" in message
    # Sanitizing is what failed, so the error's own text stays out of the message.
    assert "popup_mouse" not in message


def test_error_report_delegates_while_the_package_is_intact():
    from pcode.error_report import error_message as reported
    from pcode.live import error_message as described

    error = ModelAPIError("test:local", "Connection error.")
    assert reported(error) == described(error)
    assert reported(error, unexpected="/diffs failed") == "/diffs failed (ModelAPIError)."
