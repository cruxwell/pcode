"""Tree guides describe the visible task/delegate/tool hierarchy, not hidden rows."""

from functools import partial

import pytest
from rich.cells import cell_len

from pcode.runtime import ToolStarted
from pcode.tool_panel import ToolCall, ToolHistory, panel_fragments, task_panel_rows


@pytest.fixture
def task_tree(monkeypatch):
    monkeypatch.setattr("pcode.tool_panel.monotonic", lambda: 10.0)
    monkeypatch.setattr("pcode.tool_panel.ToolCall", partial(ToolCall, started=10.0))
    history = ToolHistory()
    history.record(
        ToolStarted(
            "delegate_task", "", "worker", agent="worker", task="Fix it", activity="Working"
        )
    )
    history.record_plan(
        "worker",
        [
            {"content": "Read the code", "status": "in_progress"},
            {"content": "Test the fix", "status": "pending"},
        ],
    )
    history.record(ToolStarted("read_file", "child.py", "worker:read", parent_call_id="worker"))
    history.record(ToolStarted("run_command", "check", "check", command="make check"))
    history.record(ToolStarted("grep", "status row", "status"))
    items = [
        {"content": "Inspect", "status": "completed"},
        {"content": "Implement", "status": "in_progress"},
        {"content": "Validate", "status": "pending"},
    ]
    return items, history


def test_only_delegates_and_their_plans_nest_under_the_active_task(task_tree):
    items, history = task_tree
    rows = task_panel_rows(items, history, 10, "*")
    # Plain calls, the parent's and the sub-agent's, stay on the status row.
    assert rows == [
        ("class:plan.completed", "✔ Inspect"),
        ("class:plan.in_progress", "* Implement"),
        ("class:plan.agent,agent.hue.0", "└── » Worker · 0.0s · Working · Fix it"),
        ("class:plan.in_progress,agent.hue.0", "    ├── * Read the code"),
        ("class:plan.pending,agent.hue.0", "    └── ○ Test the fix"),
        ("class:plan.pending", "○ Validate"),
    ]


@pytest.mark.parametrize("budget", range(11))
def test_clipped_tree_keeps_ancestors_and_ends_at_the_last_visible_sibling(task_tree, budget):
    items, history = task_tree
    rows = task_panel_rows(items, history, budget, "*")
    assert len(rows) <= budget
    text = [text for _, text in rows]
    assert not any("make check" in line or "child.py" in line for line in text)
    if budget == 0:
        assert rows == []
    elif budget == 1:
        assert text == ["* Implement"]
    else:
        assert "* Implement" in text
        assert "└── » Worker · 0.0s · Working · Fix it" in text
        if budget == 3:
            assert text[-1] == "    └── * Read the code"
        if budget >= 4:
            assert ["    ├── * Read the code", "    └── ○ Test the fix"] == [
                line for line in text if line.startswith("    ")
            ]


def test_parallel_delegates_have_separate_branches(task_tree):
    _, history = task_tree
    history.record(ToolStarted("delegate_task", "", "reviewer", agent="reviewer", task="Review"))
    history.record_plan("reviewer", [{"content": "Check diff", "status": "pending"}])
    history.record(ToolStarted("read_file", "diff", "reviewer:read", parent_call_id="reviewer"))
    text = [text for _, text in history.rows(10, nested=True)]
    assert text == [
        "├── » Worker · 0.0s · Working · Fix it",
        "│   ├── ↺ Read the code",
        "│   └── ○ Test the fix",
        "└── » Reviewer · 0.0s · Starting · Review",
        "    └── ○ Check diff",
    ]


def test_without_an_active_task_only_the_delegate_children_have_guides(task_tree):
    items, history = task_tree
    items[1]["status"] = "completed"
    rows = task_panel_rows(items, history, 10, "*")
    assert [text for _, text in rows] == [
        "✔ Inspect",
        "✔ Implement",
        "○ Validate",
        "» Worker · 0.0s · Working · Fix it",
        "├── * Read the code",
        "└── ○ Test the fix",
    ]
    assert task_panel_rows([], history, 10, "*") == rows[3:]


@pytest.mark.parametrize("width", [1, 4, 8, 12, 40, 80])
def test_nested_guides_and_wide_text_share_the_terminal_cell_budget(task_tree, width):
    items, history = task_tree
    history.plans["worker"][0]["content"] = "界e\u0301🙂\n\x1b[2J" * 10
    rows = task_panel_rows(items, history, 10, "*")
    fragments = panel_fragments(rows, width)
    lines = "".join(text for _, text in fragments).splitlines()
    assert len(lines) == len(rows)
    assert all(cell_len(line) <= width for line in lines)
    assert "\x1b" not in "".join(lines)
