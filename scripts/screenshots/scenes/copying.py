"""/copy on a code block: the conversation pane highlights exactly what Enter copies."""

from scene import launch

MODEL = "claude:claude-opus-5-5"
SIZE = (110, 30)

TURNS = {
    "How do I run": [
        [
            "Run the order tests on their own:\n\n"
            "```sh\nuv run pytest tests/test_orders.py -q\n```\n\n"
            "Add `-x` to stop at the first failure."
        ],
    ],
    "discount": [
        [
            "A discount is a flat amount, as the docstring says:\n\n"
            "> `discount` is subtracted from the subtotal, not applied as a percentage.\n\n"
            "So a 25 means 25.00 off."
        ],
    ],
}

STEPS = [
    ("type", "How do I run just the order tests?"),
    ("key", "Enter"),
    ("wait", "first failure"),
    ("type", "Is the discount a percentage?"),
    ("key", "Enter"),
    ("wait", "25.00 off"),
    ("type", "/copy"),
    ("key", "Enter"),
    ("wait", "highlighted text"),
    # Up from the newest response, past the prompt that asked it, to the older
    # answer's code block.
    ("key", "Up"),
    ("key", "Up"),
    ("shot", "copy", "/copy"),
]

if __name__ == "__main__":
    launch(TURNS, model=MODEL)
