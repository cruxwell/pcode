from dataclasses import FrozenInstanceError

import pytest

from pcode.conversation_tree import ConversationTree, TurnNode
from pcode.copy_ui import (
    Answer,
    answers,
    last_response,
)


def test_answers_follow_active_branch_newest_first():
    tree = ConversationTree()
    for node in [
        TurnNode("root", None, "first question", response="first answer"),
        TurnNode("old", "root", "abandoned question", response="abandoned answer"),
        TurnNode("new", "root", "current question", response="current answer"),
        TurnNode("compact", "new", "focus", response="summary", kind="compaction"),
        TurnNode("empty", "compact", "unfinished", response=" \n\t"),
    ]:
        tree.nodes[node.id] = node
    tree.active = "empty"
    assert answers(tree) == [
        Answer("current question", "current answer"),
        Answer("first question", "first answer"),
    ]
    assert last_response(tree) == "current answer"
    tree.active = "old"
    assert answers(tree)[0] == Answer("abandoned question", "abandoned answer")
    tree.active = None
    assert answers(tree) == []


def test_answers_are_cleaned_before_search_display_and_pick():
    secret = "synthetic-answer-text"
    tree = ConversationTree()
    tree.nodes["one"] = TurnNode(
        "one", None, f"\nquestion {secret}\n", response=f"\nanswer {secret}\x00\n"
    )
    tree.active = "one"
    answer = answers(tree)[0]
    assert answer == Answer(f"question {secret}", f"answer {secret}")
    assert last_response(tree) == tree.nodes["one"].response  # Legacy API stays raw.
    with pytest.raises(FrozenInstanceError):
        answer.text = "changed"
    # Directly supplied choices are cleaned the same way.
    assert Answer("a\x00b", "c") == Answer("ab", "c")


def test_answers_skip_text_that_is_blank_after_cleaning():
    tree = ConversationTree()
    tree.nodes["one"] = TurnNode("one", None, "question", response="\x00\x1b")
    tree.active = "one"
    assert answers(tree) == []
