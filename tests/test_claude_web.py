"""The `claude:` CLI's web tool results, as the server tool blocks pcode records."""

import logging
import warnings

from anthropic._models import construct_type
from anthropic.types.beta import BetaRawMessageStreamEvent

# Private, and moved before: this test exists to pin how Pydantic AI maps them.
from pydantic_ai.models.anthropic._native_tools import (
    map_web_fetch_tool_result_block,
    map_web_search_tool_result_block,
)

from pcode.claude_sdk.web import result_block

SEARCH = (
    'Web search results for query: "pcode"\n\n'
    'Links: [{"title":"pcode docs","url":"https://pcode.example/docs"},'
    '{"title":"pcode repo","url":"https://pcode.example/repo"}]\n\n'
    "pcode is a coding agent.\n\n\n"
    "REMINDER: You MUST include the sources above in your response to the user."
)


def parsed(block: dict) -> object:
    """The block's content as Pydantic AI records it, failing on serializer warnings."""
    event = {"type": "content_block_start", "index": 0, "content_block": block}
    item = construct_type(type_=BetaRawMessageStreamEvent, value=event).content_block
    mapper = (
        map_web_search_tool_result_block
        if block["type"] == "web_search_tool_result"
        else map_web_fetch_tool_result_block
    )
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        return mapper(item, "claude").content


def test_search_keeps_hits_and_the_summary_without_the_cli_reminder():
    content = parsed(result_block("WebSearch", "t1", {"query": "pcode"}, SEARCH, False))
    assert [hit["url"] for hit in content] == [
        "https://pcode.example/docs",
        "https://pcode.example/repo",
    ]
    assert content[0]["summary"] == "pcode is a coding agent."
    assert "summary" not in content[1]


def test_search_in_an_unknown_format_is_logged_and_its_text_kept(caplog):
    text = 'Web search results for query: "pcode"\n\nSomething new: pcode is an agent.'
    with caplog.at_level(logging.WARNING, logger="pcode.claude_sdk.web"):
        content = parsed(result_block("WebSearch", "t1", {}, text, False))
    assert "no parseable Links line" in caplog.text
    assert content == [
        {
            "type": "web_search_result",
            "title": "",
            "url": "",
            "encrypted_content": "",
            "page_age": None,
            "summary": "Something new: pcode is an agent.",
        }
    ]


def test_an_empty_search_is_not_a_format_change(caplog):
    text = 'Web search results for query: "zzz"\n\nLinks: []\n\nNo results found.'
    with caplog.at_level(logging.WARNING, logger="pcode.claude_sdk.web"):
        content = parsed(result_block("WebSearch", "t1", {}, text, False))
    assert not caplog.text
    assert [(hit["url"], hit["summary"]) for hit in content] == [("", "No results found.")]


def test_failures_use_a_valid_code_and_keep_the_whole_message():
    for name, server in (("WebSearch", "web_search"), ("WebFetch", "web_fetch")):
        content = parsed(result_block(name, "t1", {}, "Fetch failed\nstatus 503", True))
        assert content == {
            "type": f"{server}_tool_result_error",
            "error_code": "unavailable",
            "message": "Fetch failed\nstatus 503",
        }


def test_fetch_records_when_it_arrived():
    content = parsed(result_block("WebFetch", "t1", {"url": "https://u"}, "hello", False))
    assert content["url"] == "https://u" and content["retrieved_at"]
    assert content["content"]["source"]["data"] == "hello"
