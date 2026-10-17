"""The parts of the session controller, tested without a terminal."""

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

from pcode.controller import PromptQueue, SessionController
from pcode.ui import Activity


def panel():
    return SimpleNamespace(queued_prompts=[], queued_modes=[], queued=0)


def test_queue_keeps_the_panel_in_step():
    async def run():
        activity = panel()
        prompts = PromptQueue(activity)
        prompts.put("one", "queue")
        prompts.put("two", "steering")
        prompts.put("again", "resend", first=True)
        assert activity.queued_prompts == ["again", "one", "two"]
        assert activity.queued_modes == ["resend", "queue", "steering"]
        assert activity.queued == len(prompts) == 3
        item = await prompts.get()
        assert item[:4] == (0, "again", "resend", None) and prompts.current(item)
        prompts.taken()
        assert activity.queued_prompts == ["one", "two"] and activity.queued == 2

    asyncio.run(run())


def test_a_queued_messages_owner_hears_when_it_is_dropped_and_keeps_its_place():
    class Owner:
        def __init__(self):
            self.heard = []

        def started(self):
            self.heard.append("started")

        def dropped(self):
            self.heard.append("dropped")

    async def run():
        prompts = PromptQueue(panel())
        mine, other = Owner(), Owner()
        prompts.put("mine", "queue", owner=mine)
        prompts.put("steer me", "steering")
        prompts.put("first", "resend", first=True, owner=other)
        # Reordering keeps each item's owner with it.
        assert prompts.take_steering() == ["steer me"]
        for owner in (other, mine):
            assert (await prompts.get())[3] is owner
            prompts.taken()
        prompts.put("mine", "queue", owner=mine)
        assert prompts.clear() == 1
        assert mine.heard == ["dropped"] and other.heard == []

    asyncio.run(run())


def test_steering_is_taken_out_of_line_and_the_rest_keep_their_order():
    async def run():
        activity = panel()
        prompts = PromptQueue(activity)
        prompts.put("queued first", "queue")
        prompts.put("steer me", "steering")
        prompts.put("queued second", "queue")
        assert prompts.take_steering() == ["steer me"]
        assert activity.queued_prompts == ["queued first", "queued second"]
        assert activity.queued == 2
        assert [(await prompts.get())[1] for _ in range(2)] == ["queued first", "queued second"]

    asyncio.run(run())


def test_taken_steering_says_when_it_was_sent():
    prompts = PromptQueue(panel())
    before = datetime.now(timezone.utc)
    prompts.put("steer me", "steering")
    after = datetime.now(timezone.utc)
    [steer] = prompts.take_steering()
    assert steer == "steer me"
    assert before <= datetime.fromisoformat(steer.sent_at) <= after


def test_clear_starts_a_generation_that_makes_fetched_items_stale():
    async def run():
        activity = panel()
        prompts = PromptQueue(activity)
        prompts.put("fetched before the clear", "queue")
        item = await prompts.get()
        prompts.put("dropped", "queue")
        assert prompts.clear() == 2
        assert not prompts.current(item)
        assert activity.queued_prompts == [] and activity.queued == 0
        prompts.put("after", "steering")
        # Steering from before a clear is never delivered.
        assert prompts.take_steering() == ["after"]

    asyncio.run(run())


class Session:
    """A controller over a stand-in job registry, shown on a view that keeps what it was told."""

    def __init__(self, asides: int = 0) -> None:
        self.shown: list[tuple[str, str]] = []
        self.policies: list[str] = []
        self.released = 0
        self.redraws = 0
        self.activity = Activity()
        jobs = self

        class Jobs:
            @property
            def cancel_policy(self):
                return jobs.policies[-1] if jobs.policies else "detach"

            @cancel_policy.setter
            def cancel_policy(self, policy):
                jobs.policies.append(policy)

            def release_waits(self):
                jobs.release()

        self.controller = SessionController(self, self.activity, SimpleNamespace(jobs=Jobs()))
        self.controller.asides.cancel = lambda: asides

    def release(self) -> None:
        self.released += 1

    def redraw(self) -> None:
        self.redraws += 1

    def user(self, text: str) -> None:
        self.shown.append(("user", text))

    def note(self, text: str) -> None:
        self.shown.append(("note", text))

    def warning(self, text: str) -> None:
        self.shown.append(("warning", text))

    def cancelled(self) -> None:
        self.shown.append(("cancelled", ""))


async def turn(controller: SessionController) -> asyncio.Event:
    """Start a stand-in turn that runs until cancelled or released."""
    release = asyncio.Event()
    controller.live_task = asyncio.create_task(release.wait())
    await asyncio.sleep(0)
    return release


def test_interrupt_cancels_the_turn_but_keeps_its_own_message():
    async def run():
        session = Session()
        controller = session.controller
        await turn(controller)
        controller.submit("waiting behind the turn", "queue")
        controller.submit("do this instead", "interrupt")
        # The shell wait is abandoned, not killed: the model is being redirected.
        assert session.policies == ["detach"]
        assert controller.live_task.cancelling()
        assert ("note", "Cleared 1 queued message(s).") in session.shown
        assert session.activity.queued_prompts == ["do this instead"]
        await asyncio.gather(controller.live_task, return_exceptions=True)
        controller.live_task = None
        controller.turn_ended(False)
        assert session.activity.queued_prompts == ["do this instead"]
        assert session.activity.busy and not controller.interrupt_pending

    asyncio.run(run())


def test_a_failed_turn_drops_what_was_queued_behind_it():
    async def run():
        session = Session()
        controller = session.controller
        controller.submit("next", "queue")
        controller.turn_ended(False)
        assert session.activity.queued_prompts == [] and not session.activity.busy
        controller.submit("next", "queue")
        controller.turn_ended(True)
        assert session.activity.queued_prompts == ["next"] and session.activity.busy

    asyncio.run(run())


def test_steering_releases_a_shell_wait_only_while_a_turn_runs():
    async def run():
        session = Session()
        controller = session.controller
        controller.submit("idle, so it just queues", "steering")
        assert session.released == 0
        release = await turn(controller)
        controller.submit("look at this", "steering")
        assert session.released == 1
        assert controller.take_steering() == ["idle, so it just queues", "look at this"]
        assert session.shown == [("user", "idle, so it just queues"), ("user", "look at this")]
        assert session.redraws == 1 and session.activity.prompt == "look at this"
        release.set()

    asyncio.run(run())


def test_steering_peek_respects_modes_mcp_ordering_and_queue_resets():
    session = Session()
    controller = session.controller
    controller.prompts.put("queued", "queue")
    controller.prompts.put("interrupt", "interrupt")
    assert not controller.has_steering()
    controller.prompts.put("before enable", "steering")
    assert controller.has_steering()
    controller.hold_later_steering()
    controller.pending_mcp = 1
    controller.prompts.put("after enable", "steering")
    assert controller.has_steering()
    assert controller.has_steering()  # Peeking doesn't consume or render anything.
    assert controller.prompts.steering() == 2
    assert session.shown == [] and session.redraws == 0
    assert controller.take_steering() == ["before enable"]
    assert not controller.has_steering()
    assert controller.take_steering() == []
    controller.pending_mcp = 0
    assert controller.has_steering()
    assert controller.take_steering() == ["after enable"]
    assert not controller.has_steering()
    assert controller.steering_before_mcp is None

    controller.prompts.put("discarded", "steering")
    assert controller.has_steering()
    controller.clear_queue()
    assert not controller.has_steering()
    controller.prompts.put("fresh generation", "steering")
    assert controller.has_steering()
    assert controller.take_steering() == ["fresh generation"]
    assert not controller.has_steering()


def test_cancel_stops_work_then_side_questions_then_just_says_so():
    async def run():
        session = Session()
        controller = session.controller
        await turn(controller)
        controller.submit("queued", "queue")
        controller.cancel()
        assert session.policies == ["stop"] and controller.live_task.cancelling()
        assert session.shown == [("note", "Cleared 1 queued message(s).")]
        await asyncio.gather(controller.live_task, return_exceptions=True)

        session = Session(asides=2)
        session.controller.cancel()
        assert session.shown == [("note", "Stopped 2 side question(s).")]

        session = Session()
        session.activity.busy = True
        session.controller.cancel()
        assert session.shown == [("cancelled", "")] and not session.activity.busy

    asyncio.run(run())


def test_model_commands_hold_the_session_busy_until_they_start():
    async def run():
        session = Session()
        controller = session.controller
        controller.command("/compact keep the plan", tag="popup")
        controller.command("/mcp enable docs")
        assert session.activity.busy and not controller.command_idle.is_set()
        generation, text, idle, tag = await controller.commands.get()
        assert (generation, text, idle, tag) == (0, "/compact keep the plan", True, "popup")
        controller.command_started(text)
        # The MCP command behind it still holds the session.
        assert session.activity.busy
        controller.clear_queue()
        assert session.shown == [("warning", "Pending MCP enable command cancelled.")]
        assert not controller.commands_pending
        await controller.commands.get()
        controller.command_finished()
        assert controller.command_idle.is_set()
        controller.refresh_busy()
        assert not session.activity.busy

    asyncio.run(run())


def test_the_terminal_owns_exactly_the_commands_it_reserves():
    """Skills and extension commands may not take a terminal command's name."""
    from pcode.app import PreviewApp
    from pcode.controller import TERMINAL_COMMANDS

    app = PreviewApp()
    owned = {command.name for command in app.controller.registry.commands}
    terminal = {
        name
        for command in app.registry.commands
        if command.name not in owned
        for name in (command.name, *command.aliases)
    }
    assert terminal == TERMINAL_COMMANDS
