"""A bug fix turn, shown as summaries and then with every command's output."""

from scene import Call, Think, launch

MODEL = "claude:claude-opus-5-5"
SIZE = (100, 30)
PREFERENCES = {"show_commands": "off"}

FIX = "subtotal - subtotal * order.discount / 100"

TURNS = {
    "discount": [
        [
            Think("The test expects 25 to mean a percentage. Check how total() uses it."),
            Call("read_file", path="acme/orders.py"),
            Call("grep", pattern="discount", path="."),
        ],
        [Call("shell", command="git log --oneline -3", purpose="checking recent history")],
        [
            "`total()` subtracts the discount as a flat amount, but the test passes a "
            "percentage. Fixing the formula:",
            Call(
                "edit_file",
                path="acme/orders.py",
                old_text="return subtotal - order.discount",
                new_text=f"return {FIX}",
            ),
        ],
        [Call("shell", command="grep -n 'return' acme/orders.py && git diff --stat")],
        [
            "Fixed. `total()` now treats `discount` as a percentage, so a 25% discount "
            "on a 40.00 order comes to 30.00, matching `test_total_applies_discount`."
        ],
    ],
}

STEPS = [
    ("type", "Orders with a 25% discount come out wrong. Can you find the bug?"),
    ("key", "Enter"),
    ("wait", "matching test_total"),
    ("shot", "scrollback-summary"),
    # Ctrl+G cycles off → activity-panel → scrollback; the second mirrors.
    ("key", "C-g"),
    ("wait", "Commands: activity-panel"),
    ("key", "C-g"),
    ("wait", "Initial commit"),
    ("shot", "scrollback-commands"),
]

if __name__ == "__main__":
    launch(TURNS, model=MODEL, preferences=PREFERENCES)
