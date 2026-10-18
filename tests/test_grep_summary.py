"""A `grep` too large to show inline comes back as match counts per file.

Through the toolset Coder registers, so what is asserted is what the model gets.
"""

import asyncio
import os
import re

import pytest
from pydantic_ai.workspaces import LocalWorkspaceBackend
from pydantic_ai_harness.filesystem import FileSystem

from pcode import filesystem
from pcode.agent import create_coder
from pcode.filesystem import grep_summary
from pcode.preferences import save_preferences
from pcode.tool_display import result_detail


def grep(workspace, pattern, **options):
    files = next(c for c in create_coder(workspace).capabilities if isinstance(c, FileSystem))
    toolset = files.get_toolset()
    return asyncio.run(toolset.grep(pattern, workspace=LocalWorkspaceBackend(workspace), **options))


def shown(result):
    return result_detail("grep", {"pattern": "needle"}, result, "success")[0]


@pytest.fixture
def tree(tmp_path):
    (tmp_path / "hot.py").write_text("".join(f"needle {i}\n" for i in range(60)))
    for i in range(4):
        (tmp_path / f"cold-{i}.py").write_text("needle\nhay\nhay\n")
    return tmp_path


def test_a_result_within_the_limit_is_returned_line_by_line(tree):
    result = grep(tree, "needle 1")
    assert result.splitlines()[0] == "hot.py:2:needle 1"


def test_a_result_over_the_limit_lists_counts_per_file_most_first(tree):
    save_preferences(tool_output_threshold="500")
    result = grep(tree, "needle")
    lines = result.splitlines()
    assert lines[0].startswith("64 matching lines in 5 files: too many to show")
    assert "500-character result limit" in lines[0]
    assert lines[1:6] == [
        "60  hot.py",
        " 1  cold-0.py",
        " 1  cold-1.py",
        " 1  cold-2.py",
        " 1  cold-3.py",
    ]
    assert lines[-1].startswith("Narrow the search and grep again:")
    assert "`context`" not in lines[-1]
    assert shown(result).endswith(" → 64 matches in 5 files · summarized")


def test_context_lines_are_not_counted_as_matches(tree):
    save_preferences(tool_output_threshold="500")
    result = grep(tree, "needle", context=2)
    assert result.startswith("64 matching lines in 5 files")
    assert "a smaller `context`" in result


def test_a_few_long_matches_are_left_to_the_output_limits(tmp_path):
    # Narrowing cannot shrink them, and the lines say where they are.
    (tmp_path / "min.js").write_text(("needle" + "x" * 3000 + "\n") * 3)
    save_preferences(tool_output_threshold="500")
    assert grep(tmp_path, "needle").startswith("min.js:1:needle")


def test_the_listed_files_fit_under_the_limit(tmp_path, monkeypatch):
    monkeypatch.setattr(filesystem, "GREP_SUMMARY_MIN_MATCHES", 0)
    for i in range(80):
        (tmp_path / f"{'deep/' * 5}file-{i:02}.py").parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / f"{'deep/' * 5}file-{i:02}.py").write_text("needle\n")
    save_preferences(tool_output_threshold="800")
    result = grep(tmp_path, "needle")
    assert len(result) < 800
    assert re.fullmatch(r"… \d+ more files with \d+ matches", result.splitlines()[-2])


def test_files_past_the_listed_ones_are_folded_into_one_line(tree, monkeypatch):
    monkeypatch.setattr(filesystem, "GREP_SUMMARY_FILES", 2)
    save_preferences(tool_output_threshold="500")
    lines = grep(tree, "needle").splitlines()
    assert lines[1:4] == ["60  hot.py", " 1  cold-0.py", "… 3 more files with 3 matches"]


def test_a_capped_scan_says_its_counts_are_partial(tmp_path):
    # Harness stops at 1,000 lines, in path order.
    (tmp_path / "a.py").write_text("needle\n" * 1100)
    (tmp_path / "b.py").write_text("needle\n")
    result = grep(tmp_path, "needle")
    assert result.startswith(
        "At least 1,000 matching lines in 1 file "
        "(the search stopped after 1,000 lines, in path order)"
    )
    assert "[... truncated" not in result
    assert shown(result).endswith("1,000+ matches in 1 file · summarized")


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads any file")
def test_unreadable_paths_are_still_reported(tree):
    locked = tree / "locked.py"
    locked.write_text("needle\n")
    locked.chmod(0)
    try:
        save_preferences(tool_output_threshold="500")
        result = grep(tree, "needle")
    finally:
        locked.chmod(0o644)
    assert result.startswith("64 matching lines")
    assert result.splitlines()[-1].startswith("[1 path could not be read")


def test_reduction_off_keeps_the_whole_result(tree):
    save_preferences(tool_output_mode="off", tool_output_threshold="200")
    assert len(grep(tree, "needle").splitlines()) == 64


def test_ordinary_lines_that_look_like_a_summary_are_not_one():
    text = "12 matching lines in 3 files.md:1:needle\n"
    assert shown(text).endswith("1 matches in 1 files")
    summary = grep_summary([("a.py", True)] * 3, complete=True, limit=100, context=0, notes=[])
    assert shown(summary).endswith("3 matches in 1 file · summarized")
