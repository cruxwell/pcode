"""A turn mid-run: the thought, its tool calls and the plan in the activity panel."""

from scene import Call, Think, launch

MODEL = "claude:claude-opus-5-5"
SIZE = (100, 30)

STEPS_TEXT = [
    ("read", "Find where the discount is applied", "Finding where the discount is applied"),
    ("fix", "Apply the discount as a percentage", "Applying the discount as a percentage"),
    ("run", "Run the test suite", "Running the test suite"),
    ("docs", "Note the fix in the README", "Noting the fix in the README"),
]


def plan(done: int):
    """The whole plan with `done` steps completed and the next one in progress."""

    def status(index):
        return "completed" if index < done else "in_progress" if index == done else "pending"

    items = [
        {"id": key, "content": text, "active_form": active, "status": status(index)}
        for index, (key, text, active) in enumerate(STEPS_TEXT)
    ]
    return Call("write_plan", items=items)


FIX_OLD = "    return subtotal - order.discount\n"
FIX_NEW = "    return subtotal * (1 - order.discount / 100)\n"
# Long enough to hold the turn on the running step while the shots are taken.
SUITE = "python3 -m unittest discover tests; sleep 60"

TURNS = {
    "discount": [
        [plan(0)],
        [Call("read_file", path="acme/orders.py")],
        [plan(1)],
        [
            Think(
                "total() subtracts order.discount as a flat amount, but the field holds "
                "a percentage. Multiply the subtotal instead."
            ),
            Call("edit_file", path="acme/orders.py", old_text=FIX_OLD, new_text=FIX_NEW),
        ],
        [plan(2)],
        [
            Think(
                "Before running the suite, make sure nothing else reads discount as a "
                "flat amount; then the tests should confirm a 25% discount on 40.00 "
                "now totals 30.00."
            ),
            Call("shell", command="grep -rn discount acme", purpose="finding other uses"),
            Call("shell", command=SUITE, purpose="running the test suite"),
        ],
    ],
}

STEPS = [
    ("type", "Orders with a 25% discount come out wrong. Fix it, with a test."),
    ("key", "Enter"),
    ("wait", "unittest discover"),
    # The status row holds its last phase for a moment before naming the call.
    ("sleep", 3),
    ("shot", "activity", "activity panel"),
    # Ctrl+O hides the whole panel; the status row stays.
    ("key", "C-o"),
    ("sleep", 1),
    ("shot", "activity-hidden", "activity panel hidden"),
]

if __name__ == "__main__":
    launch(TURNS, model=MODEL)
