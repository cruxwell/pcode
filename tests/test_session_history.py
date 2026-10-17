"""Session recall is scoped, read-only, bounded, and survives compaction."""

import asyncio
import json
import subprocess
from collections import OrderedDict
from types import SimpleNamespace

import pytest
from pydantic_ai import Agent, ModelRetry
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import FunctionModel

from pcode.ext import BUNDLED_DIR, Extension, ExtensionUI, load_extension, user_extension_dir
from pcode.history import CHUNK_CHARS, History, keyword_ranking, read_turns
from pcode.history_embeddings import CACHE_NAME, semantic_ranking
from pcode.sessions import SavedSession, SessionInfo


@pytest.fixture(autouse=True)
def no_embeddings(monkeypatch):
    monkeypatch.delenv("PCODE_HISTORY_EMBEDDING_MODEL", raising=False)
    monkeypatch.setattr("pcode.history_cursor._continuations", OrderedDict())


@pytest.fixture
def root(tmp_path):
    return tmp_path / "sessions"


def save(root, workspace, identity="session-a", records=None, **metadata):
    metadata.setdefault("created", "2026-01-01T00:00:00Z")
    metadata.setdefault("updated", metadata["created"])
    info = SessionInfo(
        id=identity,
        model="test",
        workspace=str(workspace),
        packages={},
        **metadata,
    )
    directory = root / identity
    directory.mkdir(parents=True)
    (directory / "session.json").write_text(info.model_dump_json())
    (directory / "transcript.jsonl").write_text(
        "".join(json.dumps(record) + "\n" for record in (records or turn()))
    )
    return info


def turn(
    prompt="Fix flicker", response="Suspend the editor during redraws.", identity="run-a", **start
):
    return [
        {"kind": "turn_started", "run_id": identity, "prompt": prompt, **start},
        {"kind": "Message", "markdown": response},
        {"kind": "turn_completed"},
    ]


def extension(workspace, root):
    result = load_extension(
        Extension("session_history", BUNDLED_DIR / "session_history.py", "bundled"),
        workspace,
        ExtensionUI(),
        session_dir=root,
    )
    assert result.loaded, result.error
    return result


def tools(workspace, root):
    result = extension(workspace, root)
    return result.capabilities[0].get_toolset().tools


def call(tool, **kwargs):
    return asyncio.run(tool.function(SimpleNamespace(conversation_id="session-a"), **kwargs))


def test_load_is_lazy_and_registers_two_tools(tmp_path, root):
    registered = tools(tmp_path, root)
    assert set(registered) == {"search_sessions", "read_session"}
    assert not root.exists()
    assert registered["search_sessions"].takes_ctx


def test_tools_search_and_read_with_custom_root(tmp_path, root):
    save(root, tmp_path)
    registered = tools(tmp_path, root)
    result = call(registered["search_sessions"], query="redraws", scope="session")
    assert result["mode"] == "keyword"
    (group,) = result["results"]
    assert group["session_id"] == "session-a"
    assert group["current"]
    assert result["sessions_searched"] == result["sessions_in_scope"] == 1
    (hit,) = group["turns"]
    assert hit["turn_id"] == "run-a"
    assert hit["status"] == "completed"
    assert hit["branch"] == "active"
    assert "redraws" in hit["excerpt"]
    read = call(registered["read_session"], session_id="session-a", turn_id="run-a")
    assert "Suspend the editor" in read["text"]
    assert read["next_offset"] is None
    assert not (root / CACHE_NAME).exists()


def test_empty_scope_is_complete_and_distinct_from_a_pending_page(tmp_path, root):
    result = call(tools(tmp_path, root)["search_sessions"], query="absent")
    assert result["scan_complete"] and result["next_cursor"] is None
    assert result["sessions_in_scope"] == 0 and not result["results"]
    assert "No saved history" in result["note"]


def test_real_agent_context_scopes_to_current_session(tmp_path, root):
    save(root, tmp_path)
    save(root, tmp_path, "other")
    calls = []

    def respond(messages, info):
        returns = [
            part for msg in messages for part in msg.parts if isinstance(part, ToolReturnPart)
        ]
        if not returns:
            return ModelResponse(
                parts=[ToolCallPart("search_sessions", {"query": "flicker", "scope": "session"})]
            )
        calls.append(returns[-1].content)
        return ModelResponse(parts=[TextPart("Found it.")])

    agent = Agent(FunctionModel(respond), capabilities=extension(tmp_path, root).capabilities)
    agent.run_sync("Recall", conversation_id="session-a")
    assert [r["session_id"] for r in calls[0]["results"]] == ["session-a"]


def test_scope_and_read_fail_closed(tmp_path, root):
    workspace = tmp_path / "repo"
    save(root, workspace)
    save(root, tmp_path / "other", "other")
    history = History(workspace, root, "session-a")
    assert len(history.sessions("project")) == 1
    assert len(history.sessions("workspace")) == 1
    assert len(history.sessions("all")) == 2
    assert len(History(workspace, root).sessions("session")) == 0
    with pytest.raises(ValueError, match="not found in scope"):
        history.read("other", "run-a")
    with pytest.raises(ValueError, match="not found in scope"):
        history.read("../other", "run-a", "all")
    assert history.read("other", "run-a", "all")["session_id"] == "other"


def git(path, *args):
    return subprocess.run(
        ["git", "-C", str(path), *args], check=True, capture_output=True, text=True
    )


def test_project_scope_tracks_live_and_deleted_worktrees(tmp_path, root):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init")
    git(
        repo,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.invalid",
        "commit",
        "--allow-empty",
        "-m",
        "initial",
    )
    linked = repo / ".worktrees" / "old"
    git(repo, "worktree", "add", "-b", "feature", str(linked))
    saved = SavedSession.create("test", linked, root=root)
    assert saved.info.project == str(repo)
    saved.close()
    save(root, linked, "legacy")
    save(root, tmp_path / "external-deleted", "external", project=str(repo))
    assert len(History(repo, root).sessions("project")) == 3
    assert len(History(linked, root).sessions("project")) == 3
    assert len(History(repo, root).sessions("workspace")) == 0
    git(repo, "worktree", "remove", str(linked))
    assert len(History(repo, root).sessions("project")) == 3


def test_compaction_preserves_recall_and_ancestor_context(tmp_path, root):
    records = (
        turn("Original decision", "Use a bounded queue.")
        + [
            {
                "kind": "compaction_checkpoint",
                "node_id": "compact",
                "parent_id": "run-a",
                "messages": [],
            },
        ]
        + turn("After compaction", "Continue.", "run-b", parent_id="compact")
    )
    info = save(root, tmp_path, records=records)
    turns = read_turns(info, root)
    assert turns["run-a"].active
    assert turns["run-b"].parent == "run-a"
    registered = tools(tmp_path, root)
    result = call(registered["search_sessions"], query="bounded queue", scope="session")
    assert result["results"][0]["turns"][0]["turn_id"] == "run-a"
    read = call(registered["read_session"], session_id=info.id, turn_id="run-b", scope="session")
    assert "bounded queue" in read["context"][0]["text"]


def test_inactive_branch_and_failed_turn_are_labelled(tmp_path, root):
    records = (
        turn()
        + turn("Abandoned", "Do not ship", "abandoned", parent_id="run-a")
        + [
            {"kind": "tree_selected", "node_id": "run-a"},
            {"kind": "turn_started", "run_id": "failed", "parent_id": "run-a", "prompt": "Retry"},
            {"kind": "turn_failed"},
        ]
    )
    save(root, tmp_path, records=records)
    history = History(tmp_path, root)
    assert history.read("session-a", "abandoned")["branch"] == "inactive"
    read = history.read("session-a", "failed", context_turns=3)
    assert read["status"] == "failed"
    assert [c["turn_id"] for c in read["context"]] == ["run-a"]


def test_searches_all_text_blocks_and_tool_summaries_not_raw_results(tmp_path, root):
    records = turn()[:-1] + [
        {
            "kind": "ToolSummary",
            "name": "shell",
            "detail": "pytest",
            "command": "pytest -q",
            "result": "private-output",
        },
        {"kind": "ToolFinished", "result": "private-output"},
        {"kind": "Message", "markdown": "Final"},
        {"kind": "turn_completed"},
    ]
    save(root, tmp_path, records=records)
    chunks = History(tmp_path, root).chunks("project").chunks
    assert keyword_ranking(chunks, "redraws")
    assert keyword_ranking(chunks, "pytest")
    assert not keyword_ranking(chunks, "private-output")


def test_live_locked_session_read_without_open_or_resume(tmp_path, root):
    saved = SavedSession.create("test", tmp_path, root=root)
    try:
        saved.append("turn_started", run_id="live", prompt="Earlier detail")
        saved.append("Message", markdown="Durable response")
        history = History(tmp_path, root, saved.info.id)
        assert history.read(saved.info.id, "live", "session")["text"].endswith("Durable response")
        saved.append("Message", markdown="New detail")
        assert "New detail" in history.read(saved.info.id, "live")["text"]
    finally:
        saved.close()


def test_torn_journal_legacy_ids_unreadable_and_symlinks(tmp_path, root):
    info = save(root, tmp_path, records=[{"kind": "turn_started", "prompt": "legacy"}])
    with (root / info.id / "transcript.jsonl").open("a") as stream:
        stream.write('not json\n{"torn":')
    assert History(tmp_path, root).read(info.id, "turn-1")["text"] == "User: legacy"
    other = save(root, tmp_path, "other")
    (root / other.id / "transcript.jsonl").unlink()
    (root / other.id / "transcript.jsonl").symlink_to(root / info.id / "transcript.jsonl")
    (root / "alias").symlink_to(root / info.id, target_is_directory=True)
    scan = History(tmp_path, root).chunks("project")
    chunks, warnings = scan.chunks, scan.warnings
    assert len(chunks) == 1
    assert "Skipped unreadable session other." in warnings
    assert any("Unfinished final record" in warning for warning in warnings)
    assert not scan.scan_complete


def test_redaction_before_return_and_pagination(tmp_path, root, monkeypatch):
    secret = "synthetic-secret-value"
    monkeypatch.setenv("TEST_API_KEY", secret)
    response = f"{secret} " + "a" * (CHUNK_CHARS * 2) + " unique-tail"
    save(root, tmp_path, records=turn(response=response))
    registered = tools(tmp_path, root)
    (group,) = call(registered["search_sessions"], query="unique-tail")["results"]
    hits = group["turns"]
    assert len(hits) == 1
    assert hits[0]["offset"] > CHUNK_CHARS
    first = call(registered["read_session"], session_id="session-a", turn_id="run-a", max_chars=40)
    assert secret not in str(first)
    assert "[redacted]" in first["text"]
    second = call(
        registered["read_session"],
        session_id="session-a",
        turn_id="run-a",
        offset=first["next_offset"],
    )
    assert second["offset"] == 40


@pytest.mark.parametrize(
    "kwargs", [{"query": ""}, {"query": "x", "limit": 0}, {"query": "x" * 1001}]
)
def test_invalid_search_arguments_retry(tmp_path, root, kwargs):
    with pytest.raises(ModelRetry):
        call(tools(tmp_path, root)["search_sessions"], **kwargs)


def test_invalid_read_arguments_retry(tmp_path, root):
    save(root, tmp_path)
    with pytest.raises(ModelRetry):
        call(
            tools(tmp_path, root)["read_session"],
            session_id="session-a",
            turn_id="run-a",
            max_chars=20000,
        )


class FakeEmbedder:
    def __init__(self):
        self.documents = []
        self.queries = []

    async def embed_documents(self, documents):
        self.documents.extend(documents)
        return SimpleNamespace(embeddings=[[1.0, 0.0] for _ in documents])

    async def embed_query(self, query):
        self.queries.append(query)
        return SimpleNamespace(embeddings=[[1.0, 0.0]])


def test_embedding_cache_incremental_model_specific_and_private(tmp_path, root):
    save(root, tmp_path)
    chunks = History(tmp_path, root).chunks("project").chunks
    embedder = FakeEmbedder()
    for _ in range(2):
        ranking, warnings = asyncio.run(
            semantic_ranking(chunks, "flashing", "test:model", root, embedder=embedder)
        )
        assert ranking == [0]
        assert not warnings
    assert len(embedder.documents) == 1
    assert len(embedder.queries) == 2
    assert (root / CACHE_NAME).stat().st_mode & 0o777 == 0o600
    assert b"Suspend the editor" not in (root / CACHE_NAME).read_bytes()
    asyncio.run(semantic_ranking(chunks, "flashing", "test:other", root, embedder=embedder))
    assert len(embedder.documents) == 2
    chunks[0].text += " Changed"
    asyncio.run(semantic_ranking(chunks, "flashing", "test:model", root, embedder=embedder))
    assert len(embedder.documents) == 3


def test_hybrid_opt_in_redacts_and_keeps_scope(tmp_path, root, monkeypatch):
    save(root, tmp_path, records=turn(response="Synthetic password=hiddenvalue flicker"))
    save(root, tmp_path / "other", "other", records=turn(response="outside-project"))
    embedder = FakeEmbedder()
    monkeypatch.setenv("PCODE_HISTORY_EMBEDDING_MODEL", "test:model")
    monkeypatch.setattr("pydantic_ai.Embedder", lambda model: embedder)
    registered = tools(tmp_path, root)
    result = call(registered["search_sessions"], query="flashing")
    assert result["mode"] == "hybrid"
    assert result["results"]
    assert "hiddenvalue" not in str(embedder.documents)
    assert "outside-project" not in str(embedder.documents)
    call(registered["search_sessions"], query="flicker", semantic=False)
    assert len(embedder.queries) == 1


def test_provider_failure_falls_back_without_echoing_error(tmp_path, root, monkeypatch):
    save(root, tmp_path)
    monkeypatch.setenv("PCODE_HISTORY_EMBEDDING_MODEL", "test:model")

    def fail(model):
        raise ValueError("sensitive provider response")

    monkeypatch.setattr("pydantic_ai.Embedder", fail)
    result = call(tools(tmp_path, root)["search_sessions"], query="flicker")
    assert result["mode"] == "keyword"
    assert result["results"]
    assert "sensitive" not in str(result)
    assert "ValueError" in result["warnings"][0]


def test_embedding_batch_budget_and_deleted_sessions_not_returned(tmp_path, root, monkeypatch):
    save(root, tmp_path, records=turn(response="x" * CHUNK_CHARS * 3))
    monkeypatch.setattr("pcode.history_embeddings.MAX_NEW_CHUNKS", 1)
    chunks = History(tmp_path, root).chunks("project").chunks
    embedder = FakeEmbedder()
    ranking, warnings = asyncio.run(
        semantic_ranking(chunks, "query", "test:model", root, embedder=embedder)
    )
    assert len(ranking) == 1
    assert "partial" in warnings[0]
    assert len(embedder.documents) == 1
    (root / "session-a" / "session.json").unlink()
    assert History(tmp_path, root).chunks("project").chunks == []


def test_app_passes_session_dir_on_load_and_reload(tmp_path, root):
    from pcode.app import PreviewApp

    app = PreviewApp(workspace=tmp_path, session_dir=root)
    for _ in range(2):
        loaded = app.controller._load_extensions()
        (selected,) = [e for e in loaded.extensions if e.name == "session_history"]
        assert selected.loaded, selected.error
        save_path = root / "session-a"
        if not save_path.exists():
            save(root, tmp_path)
        registered = selected.capabilities[0].get_toolset().tools
        assert call(registered["search_sessions"], query="flicker")["results"]


def test_environment_session_root_and_explicit_override(tmp_path, root, monkeypatch):
    from pcode.ext import ExtensionAPI

    monkeypatch.setenv("PCODE_SESSION_DIR", str(root))
    assert ExtensionAPI("test", tmp_path, ExtensionUI()).session_dir == root
    explicit = tmp_path / "explicit"
    assert (
        ExtensionAPI("test", tmp_path, ExtensionUI(), session_dir=explicit).session_dir == explicit
    )


def test_explicit_session_root_matches_storage_path_semantics(tmp_path, monkeypatch):
    from pathlib import Path

    from pcode.ext import ExtensionAPI

    monkeypatch.chdir(tmp_path)
    # Explicit roots are literal Paths in SavedSession; only session_root expands env values.
    literal = Path("~") / "sessions"
    saved = SavedSession.create("test", tmp_path, literal)
    try:
        api = ExtensionAPI("test", tmp_path, ExtensionUI(), session_dir=literal)
        assert api.session_dir == saved.directory.parent.resolve()
    finally:
        saved.close()


def test_scan_budget_reports_partial_results(tmp_path, root, monkeypatch):
    save(root, tmp_path, records=turn(response="x" * CHUNK_CHARS * 3))
    monkeypatch.setattr("pcode.history.MAX_CHUNKS", 1)
    scan = History(tmp_path, root).chunks("project")
    chunks, warnings = scan.chunks, scan.warnings
    assert len(chunks) == 1
    assert "limited" in warnings[0]


def test_scan_byte_budget_stops_before_decoding_oversized_record(tmp_path, root, monkeypatch):
    from pcode.sessions import SessionReadBudget, session_records

    records = turn() + [{"kind": "Message", "markdown": "x" * 10000}]
    info = save(root, tmp_path, records=records)
    prefix_size = sum(len((json.dumps(r) + "\n").encode()) for r in records[:3])
    monkeypatch.setattr("pcode.history.MAX_SCAN_BYTES", prefix_size)
    scan = History(tmp_path, root).chunks("project")
    chunks, warnings = scan.chunks, scan.warnings
    assert not chunks  # Defer hits until ancestry and turn text are fully resolved.
    assert scan.next_cursor and not scan.scan_complete
    assert "byte budget" in warnings[0]
    budget = SessionReadBudget(prefix_size)
    assert len(list(session_records(info, root, budget=budget))) == 3
    assert budget.remaining == 0 and budget.exhausted
    read = History(tmp_path, root).read("session-a", "run-a")
    assert read["warnings"] and read["next_cursor"]
    assert not read["scan_complete"]
    assert not read["text"]


def test_semantic_shortlist_keeps_distinct_turns(tmp_path, root):
    save(root, tmp_path, records=turn(response="x" * CHUNK_CHARS * 55) + turn(identity="run-b"))
    chunks = History(tmp_path, root).chunks("project").chunks
    # Simulate the long turn ranking first, so a chunk-level top-50 would hide run-b.
    chunks.sort(key=lambda chunk: chunk.turn.id)
    ranking, _ = asyncio.run(
        semantic_ranking(chunks, "question", "test:model", root, embedder=FakeEmbedder())
    )
    assert [chunks[index].turn.id for index in ranking] == ["run-a", "run-b"]


def test_consumed_steering_is_journaled_and_recallable(tmp_path, root):
    from pcode.controller import Steer
    from pcode.live import AgentRuntime

    saved = SavedSession.create("test", tmp_path, root)
    sent_at = "2026-10-16T12:00:00+00:00"
    pending = [Steer("Use a bounded queue instead", sent_at)]
    requests = []

    async def respond(messages, info):
        requests.append(messages)
        yield "Acknowledged."

    async def run():
        runtime = AgentRuntime(Agent(FunctionModel(stream_function=respond)), saved)
        runtime.take_steering = lambda: [pending.pop()] if pending else []
        try:
            _ = [event async for event in runtime.stream("Implement the worker")]
            records = list(saved.records())
            start = next(r for r in records if r["kind"] == "turn_started")
            steering = next(r for r in records if r["kind"] == "steering")
            assert steering["run_id"] == start["run_id"]
            # Sent and delivered times both kept: the gap is the wait on tools.
            assert steering["sent_at"] == sent_at and steering["time"] != sent_at
            # The model gets plain text, not the stamped string.
            [part] = [
                part
                for part in requests[0][-1].parts
                if getattr(part, "content", None) == "Use a bounded queue instead"
            ]
            assert type(part.content) is str
            saved.append(
                "compaction_checkpoint",
                node_id="compact",
                parent_id=start["run_id"],
                messages=[],
                plan=[],
                before=100,
                after=10,
            )
            history = History(tmp_path, root, saved.info.id)
            chunks = history.chunks("session").chunks
            hits = keyword_ranking(chunks, "bounded queue")
            assert hits
            read = history.read(saved.info.id, chunks[hits[0]].turn.id, "session")
            assert "User (steering): Use a bounded queue instead" in read["text"]
        finally:
            runtime.close()

    asyncio.run(run())


def test_bm25_ranking_prefers_rare_terms_and_short_documents(tmp_path, root):
    records = (
        turn("Common", "editor " * 200 + "layout", "many-common")
        + turn("Rare", "The editor flicker came from redraws.", "rare-term")
        + turn("Phrase", "Fix the flicker in the editor.", "phrase")
        + turn("Nothing", "Unrelated database migration.", "none")
    )
    save(root, tmp_path, records=records)
    chunks = History(tmp_path, root).chunks("project").chunks
    by_turn = {chunk.turn.id: index for index, chunk in enumerate(chunks)}
    ranking = keyword_ranking(chunks, "editor flicker")
    assert [chunks[i].turn.id for i in ranking][0] == "rare-term"
    assert by_turn["none"] not in ranking
    # Repeating a common term does not beat the document holding the rare one.
    assert ranking.index(by_turn["many-common"]) > ranking.index(by_turn["phrase"])
    # Punctuation and case are ignored; an empty or symbol-only query matches nothing.
    assert keyword_ranking(chunks, "FLICKER.") == keyword_ranking(chunks, "flicker")
    assert keyword_ranking(chunks, "!!!") == []


def test_current_turn_excluded_and_current_session_flagged(tmp_path, root):
    records = turn("Earlier work", "We chose a bounded queue.") + [
        {"kind": "turn_started", "run_id": "live", "prompt": "bounded queue question"}
    ]
    save(root, tmp_path, records=records)
    registered = tools(tmp_path, root)
    search = registered["search_sessions"].function
    context = SimpleNamespace(conversation_id="session-a", run_id="live")
    (group,) = asyncio.run(search(context, query="bounded queue", scope="session"))["results"]
    assert group["current"]
    assert [t["turn_id"] for t in group["turns"]] == ["run-a"]
    # Without the in-flight run ID the prompt itself still comes back.
    again = call(registered["search_sessions"], query="bounded queue", scope="session")
    (group,) = again["results"]
    assert "live" in [t["turn_id"] for t in group["turns"]]


def test_in_flight_turn_returns_once_auto_compaction_drops_it_from_context(tmp_path, root):
    records = [
        {"kind": "turn_started", "run_id": "live", "prompt": "Long task"},
        {"kind": "Message", "markdown": "Early finding: the queue must stay bounded."},
        {"kind": "auto_compacted", "run_id": "live", "before": 100, "after": 10},
        {"kind": "Message", "markdown": "Continuing."},
    ]
    save(root, tmp_path, records=records)
    search = tools(tmp_path, root)["search_sessions"].function
    context = SimpleNamespace(conversation_id="session-a", run_id="live")
    (group,) = asyncio.run(search(context, query="bounded queue", scope="session"))["results"]
    (hit,) = group["turns"]
    assert hit["turn_id"] == "live"
    assert hit["current_turn"]


def test_results_group_by_session_and_spread_the_limit(tmp_path, root):
    many = [record for index in range(5) for record in turn("Flicker", "flicker", f"run-{index}")]
    save(root, tmp_path, records=many, identity="wordy")
    save(root, tmp_path, "sparse", records=turn("Other", "flicker once"))
    result = call(tools(tmp_path, root)["search_sessions"], query="flicker", limit=4)
    assert [g["session_id"] for g in result["results"]] == ["wordy", "sparse"]
    assert [len(g["turns"]) for g in result["results"]] == [3, 1]
    assert not any(g["current"] for g in result["results"])


def test_excerpt_prefers_prose_over_tool_summaries(tmp_path, root):
    records = turn()[:-1] + [
        {"kind": "ToolSummary", "name": "shell", "command": "grep -rn strict_tools src"},
        {"kind": "Message", "markdown": "Done - strict_tools is now the default."},
        {"kind": "turn_completed"},
    ]
    save(root, tmp_path, records=records)
    (group,) = call(tools(tmp_path, root)["search_sessions"], query="strict_tools")["results"]
    assert "Done - strict_tools" in group["turns"][0]["excerpt"]


def test_excerpt_anchors_on_the_densest_match_and_carries_the_conclusion(tmp_path, root):
    body = (
        "Mentioned strict tools once in passing.\n\n"
        + "filler " * 300
        + "\n\nThe strict tools failure was a schema mismatch in tools we send.\n\n"
        + "filler " * 300
    )
    records = [
        {"kind": "turn_started", "run_id": "run-a", "prompt": "Investigate"},
        {"kind": "Message", "markdown": body},
        {"kind": "Message", "markdown": "Shipped: strict tools is the default now."},
        {"kind": "turn_completed"},
    ]
    save(root, tmp_path, records=records)
    (group,) = call(tools(tmp_path, root)["search_sessions"], query="strict tools failure")[
        "results"
    ]
    hit = group["turns"][0]
    assert "schema mismatch" in hit["excerpt"]
    assert "once in passing" not in hit["excerpt"]
    assert hit["conclusion"] == "Shipped: strict tools is the default now."


def test_partial_scan_reports_session_coverage(tmp_path, root, monkeypatch):
    save(root, tmp_path, records=turn())
    save(root, tmp_path, "older", records=turn())
    monkeypatch.setattr("pcode.history.MAX_SCAN_BYTES", 10)
    result = call(tools(tmp_path, root)["search_sessions"], query="flicker")
    assert (result["sessions_searched"], result["sessions_in_scope"]) == (0, 2)
    assert "inside session" in result["warnings"][-1]
    assert result["sessions_partial"] == 1
    assert not result["scan_complete"]
    assert "empty page does not establish absence" in result["note"]
    assert result["next_cursor"] not in {None, "session-a", "older"}


def test_cursor_continues_into_sessions_the_budget_did_not_reach(tmp_path, root, monkeypatch):
    save(
        root,
        tmp_path,
        records=turn("Recent", "Recent work on the editor."),
        updated="2026-02-01T00:00:00Z",
    )
    save(root, tmp_path, "older", records=turn("Older", "The editor flicker fix we shipped."))
    # One session per scan, so reaching the older one requires the cursor.
    monkeypatch.setattr(
        "pcode.history.MAX_SCAN_BYTES",
        sum(len(json.dumps(record)) + 1 for record in turn()) + 20,
    )
    registered = tools(tmp_path, root)
    first = call(registered["search_sessions"], query="flicker fix")
    assert not [g for g in first["results"] if g["session_id"] == "older"]
    second = call(registered["search_sessions"], query="flicker fix", after=first["next_cursor"])
    (group,) = second["results"]
    assert group["session_id"] == "older"
    assert second["next_cursor"] is None
    with pytest.raises(ModelRetry, match="Unknown cursor"):
        call(registered["search_sessions"], query="flicker", after="no-such-session")


def collect_pages(workspace, root, scope="project"):
    """Use a fresh History per call, just like the extension does."""
    after, seen, pages = None, set(), []
    for _ in range(1000):
        scan = History(workspace, root, "session-a").chunks(scope, after=after)
        pages.append(scan)
        if scan.next_cursor is None:
            return pages
        assert not scan.scan_complete
        assert scan.next_cursor not in seen
        seen.add(scan.next_cursor)
        after = scan.next_cursor
    pytest.fail("pagination did not terminate")


@pytest.mark.parametrize("byte_limit", [97, 997, 100_000])
@pytest.mark.parametrize("chunk_limit", [1, 2, 10_000])
def test_pagination_covers_every_chunk_and_preserves_ancestry(
    tmp_path, root, monkeypatch, byte_limit, chunk_limit
):
    records = (
        turn("First", "x" * (CHUNK_CHARS * 2) + " only-tail-match")
        + turn("Abandoned", "Do not ship.", "abandoned", parent_id="run-a")
        + [
            {"kind": "tree_selected", "node_id": "run-a"},
            {"kind": "compaction_checkpoint", "node_id": "compact", "parent_id": "run-a"},
        ]
        + turn("Final", "Shipped.", "final", parent_id="compact")
        + [{"kind": "Message", "run_id": "run-a", "markdown": "Delayed original response."}]
    )
    save(root, tmp_path, records=records)
    save(root, tmp_path, "older", records=turn("Older", "Older evidence."), updated="2025")

    def signature(chunk):
        return (
            chunk.session.id,
            chunk.turn.id,
            chunk.offset,
            chunk.text,
            chunk.turn.branch,
            chunk.turn.parent,
            chunk.turn.status,
        )

    expected = [signature(c) for c in History(tmp_path, root).chunks("project").chunks]
    monkeypatch.setattr("pcode.history.MAX_SCAN_BYTES", byte_limit)
    monkeypatch.setattr("pcode.history.MAX_CHUNKS", chunk_limit)
    pages = collect_pages(tmp_path, root)
    assert all(len(page.chunks) <= chunk_limit for page in pages)
    assert [signature(c) for page in pages for c in page.chunks] == expected
    assert pages[-1].scan_complete
    assert pages[-1].sessions_searched == pages[-1].sessions_in_scope == 2
    assert all(page.sessions_searched < 2 for page in pages[:-1])


def test_tail_only_hit_is_found_and_read_across_byte_pages(tmp_path, root, monkeypatch):
    records = turn("Beginning", "No answer here.")
    records += [{"kind": "TextDelta", "content": "discarded delta " * 50} for _ in range(3)]
    records += turn("Later", "The answer is tail-only-evidence.", "later", parent_id="run-a")
    save(root, tmp_path, records=records)
    monkeypatch.setattr("pcode.history.MAX_SCAN_BYTES", 100)
    registered = tools(tmp_path, root)
    result, after = None, ""
    for _ in range(100):
        result = call(registered["search_sessions"], query="tail-only-evidence", after=after)
        if not result["next_cursor"]:
            break
        assert not result["results"]  # No misleading partial turn text or branch labels.
        assert "absence" in result["note"]
        after = result["next_cursor"]
    assert result["scan_complete"]
    (group,) = result["results"]
    (hit,) = group["turns"]
    assert hit["turn_id"] == "later"
    after = ""
    for _ in range(100):
        read = call(
            registered["read_session"],
            session_id=group["session_id"],
            turn_id=hit["turn_id"],
            after=after,
        )
        if not read["next_cursor"]:
            break
        assert not read["scan_complete"] and not read["text"]
        after = read["next_cursor"]
    assert read["scan_complete"]
    assert "tail-only-evidence" in read["text"]
    assert read["context"][0]["turn_id"] == "run-a"


def test_byte_pages_do_not_rescan_prefixes_and_redact_after_assembly(tmp_path, root, monkeypatch):
    from pcode.history_cursor import JournalReader

    info = save(root, tmp_path, records=turn(response="évidence password=synthetic-secret " * 10))
    path = root / info.id / "transcript.jsonl"
    # Force actual multibyte characters into the journal, not JSON escape sequences.
    path.write_text(path.read_text().replace("\\u00e9", "é"))
    consumed = []
    records = JournalReader.records

    def measured(self, budget):
        before, allowed = self.offset, budget.remaining
        yield from records(self, budget)
        consumed.append(self.offset - before)
        assert 0 <= consumed[-1] <= allowed

    monkeypatch.setattr(JournalReader, "records", measured)
    monkeypatch.setattr("pcode.history.MAX_SCAN_BYTES", 17)
    pages = collect_pages(tmp_path, root)
    assert sum(consumed) == path.stat().st_size
    assert len(consumed) > 2
    text = "".join(c.text for page in pages for c in page.chunks)
    assert "synthetic-secret" not in text
    assert "évidence" in text and "[redacted]" in text


def test_chunk_limit_does_not_mark_a_partial_session_complete(tmp_path, root, monkeypatch):
    save(root, tmp_path, records=turn("Only match is early", "Older decision.", "early") + turn())
    monkeypatch.setattr("pcode.history.MAX_CHUNKS", 1)
    first, last = collect_pages(tmp_path, root)
    assert first.sessions_searched == 0
    assert first.sessions_partial == 1
    assert first.next_cursor and not first.scan_complete
    assert last.chunks[0].turn.id == "early"
    assert last.sessions_searched == 1 and last.scan_complete
    assert History(tmp_path, root).read("session-a", "early")["text"].startswith("User: Only match")


def test_complete_final_record_without_newline_remains_searchable(tmp_path, root, monkeypatch):
    save(root, tmp_path)
    path = root / "session-a" / "transcript.jsonl"
    path.write_bytes(path.read_bytes().rstrip(b"\n"))
    monkeypatch.setattr("pcode.history.MAX_SCAN_BYTES", 17)
    pages = collect_pages(tmp_path, root)
    assert pages[-1].scan_complete
    (chunk,) = [chunk for page in pages for chunk in page.chunks]
    assert chunk.turn.status == "completed" and chunk.turn.branch == "active"


def test_exact_byte_and_chunk_limits_need_no_extra_page(tmp_path, root, monkeypatch):
    save(root, tmp_path)
    size = (root / "session-a" / "transcript.jsonl").stat().st_size
    monkeypatch.setattr("pcode.history.MAX_SCAN_BYTES", size)
    monkeypatch.setattr("pcode.history.MAX_CHUNKS", 1)
    (page,) = collect_pages(tmp_path, root)
    assert len(page.chunks) == 1 and page.scan_complete


def test_continuations_freeze_session_order_and_journal_end(tmp_path, root, monkeypatch):
    save(root, tmp_path, records=turn("Original", "Original evidence."), updated="2026")
    save(root, tmp_path, "older", records=turn("Older", "Old evidence."), updated="2025")
    monkeypatch.setattr("pcode.history.MAX_SCAN_BYTES", 50)
    history = History(tmp_path, root, "session-a")
    first = history.chunks("project")
    # Appends are outside the first session's captured snapshot; reordering the
    # metadata must not make the second session vanish behind a session-ID cursor.
    path = root / "session-a" / "transcript.jsonl"
    with path.open("a") as stream:
        stream.write("".join(json.dumps(r) + "\n" for r in turn("Appended", "New", "new")))
    metadata = root / "older" / "session.json"
    info = json.loads(metadata.read_text())
    info["updated"] = "2027"
    metadata.write_text(json.dumps(info))
    pages, after = [first], first.next_cursor
    for _ in range(50):
        page = History(tmp_path, root, "session-a").chunks("project", after=after)
        pages.append(page)
        if page.next_cursor is None:
            break
        after = page.next_cursor
    assert pages[-1].scan_complete
    assert [c.session.id for p in pages for c in p.chunks] == ["session-a", "older"]
    assert all(c.turn.id != "new" for p in pages for c in p.chunks)
    assert any(c.turn.id == "new" for p in collect_pages(tmp_path, root) for c in p.chunks)


@pytest.mark.parametrize("change", ["truncate", "replace", "delete", "symlink"])
def test_changed_journal_never_resumes_at_stale_offset(tmp_path, root, monkeypatch, change):
    save(root, tmp_path)
    monkeypatch.setattr("pcode.history.MAX_SCAN_BYTES", 10)
    history = History(tmp_path, root)
    first = history.chunks("project")
    path = root / "session-a" / "transcript.jsonl"
    if change == "truncate":
        path.write_text("")
    elif change == "replace":
        other = path.with_suffix(".new")
        other.write_text(path.read_text())
        other.replace(path)
    elif change == "delete":
        (path.parent / "session.json").unlink()
    else:
        other = tmp_path / "other.jsonl"
        path.rename(other)
        path.symlink_to(other)
    if change == "symlink":
        scan = history.chunks("project", after=first.next_cursor)
        assert scan.sessions_unreadable == 1 and not scan.scan_complete and not scan.chunks
    else:
        with pytest.raises(ValueError, match="restart without after"):
            history.chunks("project", after=first.next_cursor)


def test_cursors_are_bound_single_use_and_expire(tmp_path, root, monkeypatch):
    save(root, tmp_path)
    monkeypatch.setattr("pcode.history.MAX_SCAN_BYTES", 10)
    now = [100.0]
    monkeypatch.setattr("pcode.history_cursor.time.monotonic", lambda: now[0])
    history = History(tmp_path, root)
    first = history.chunks("project")
    with pytest.raises(ValueError, match="does not match"):
        history.chunks("all", after=first.next_cursor)
    with pytest.raises(ValueError, match="does not match"):
        History(tmp_path / "other", root).chunks("project", after=first.next_cursor)
    with pytest.raises(ValueError, match="does not match"):
        history.read("session-a", "run-a", after=first.next_cursor)
    second = history.chunks("project", after=first.next_cursor)
    with pytest.raises(ValueError, match="Unknown cursor"):
        history.chunks("project", after=first.next_cursor)
    now[0] += 1801
    with pytest.raises(ValueError, match="expired"):
        history.chunks("project", after=second.next_cursor)


def test_cursor_retention_is_bounded(tmp_path, root, monkeypatch):
    save(root, tmp_path)
    monkeypatch.setattr("pcode.history.MAX_SCAN_BYTES", 10)
    monkeypatch.setattr("pcode.history_cursor.MAX_CURSORS", 2)
    history = History(tmp_path, root)
    first = history.chunks("project")
    second = history.chunks("project")
    third = history.chunks("project")
    with pytest.raises(ValueError, match="expired"):
        history.chunks("project", after=first.next_cursor)
    assert history.chunks("project", after=second.next_cursor).next_cursor
    assert history.chunks("project", after=third.next_cursor).next_cursor


@pytest.mark.parametrize("boundary", ["inside", "between"])
@pytest.mark.parametrize("change", ["scope", "invalid"])
def test_unvisited_session_is_revalidated_after_continuation(
    tmp_path, root, monkeypatch, boundary, change
):
    save(root, tmp_path, updated="2026")
    save(root, tmp_path, "older", updated="2025")
    size = (root / "session-a" / "transcript.jsonl").stat().st_size
    monkeypatch.setattr("pcode.history.MAX_SCAN_BYTES", size if boundary == "between" else size - 1)
    history = History(tmp_path, root)
    first = history.chunks("project")
    metadata = root / "older" / "session.json"
    if change == "scope":
        info = json.loads(metadata.read_text())
        info["workspace"] = str(tmp_path / "elsewhere")
        metadata.write_text(json.dumps(info))
    else:
        metadata.write_text("invalid")
    with pytest.raises(ValueError, match="no longer in scope"):
        history.chunks("project", after=first.next_cursor)


def test_legacy_attribution_and_compacted_current_turn_survive_byte_pages(
    tmp_path, root, monkeypatch
):
    records = [
        {"kind": "turn_started", "prompt": "Legacy request"},
        {"kind": "Message", "markdown": "Legacy reply"},
        {"kind": "turn_completed"},
        {"kind": "turn_started", "run_id": "live", "prompt": "Current task"},
        {"kind": "tree_selected", "node_id": "turn-1"},
        {"kind": "steering", "prompt": "Steered current task"},
        {"kind": "auto_compacted", "run_id": "live"},
        {"kind": "Message", "markdown": "Current reply"},
    ]
    save(root, tmp_path, records=records)
    monkeypatch.setattr("pcode.history.MAX_SCAN_BYTES", 13)
    search = tools(tmp_path, root)["search_sessions"].function
    context = SimpleNamespace(conversation_id="session-a", run_id="live")
    after = ""
    for _ in range(100):
        result = asyncio.run(search(context, query="Steered", scope="session", after=after))
        if not result["next_cursor"]:
            break
        after = result["next_cursor"]
    assert result["scan_complete"]
    (hit,) = result["results"][0]["turns"]
    assert hit["turn_id"] == "live" and hit["current_turn"]
    assert hit["branch"] == "inactive"
    assert "User (steering): Steered" in hit["excerpt"]


def test_read_scan_continuation_precedes_redacted_text_pagination(tmp_path, root, monkeypatch):
    save(root, tmp_path, records=turn(response="password=synthetic-secret " + "evidence " * 30))
    expected = History(tmp_path, root).read("session-a", "run-a")["text"]
    monkeypatch.setattr("pcode.history.MAX_SCAN_BYTES", 43)
    offset, text = 0, ""
    for _ in range(50):
        after = None
        for _ in range(50):
            result = History(tmp_path, root).read(
                "session-a", "run-a", offset=offset, max_chars=37, after=after
            )
            if not result["next_cursor"]:
                break
            after = result["next_cursor"]
        assert result["scan_complete"]
        text += result["text"]
        if result["next_offset"] is None:
            break
        offset = result["next_offset"]
    assert text == expected
    assert "synthetic-secret" not in text


def test_missing_turn_is_only_rejected_after_read_scan_finishes(tmp_path, root, monkeypatch):
    save(root, tmp_path)
    monkeypatch.setattr("pcode.history.MAX_SCAN_BYTES", 10)
    history = History(tmp_path, root)
    result = history.read("session-a", "absent")
    assert not result["scan_complete"] and result["next_cursor"]
    for _ in range(100):
        try:
            result = history.read("session-a", "absent", after=result["next_cursor"])
        except ValueError as error:
            assert "Turn not found" in str(error)
            break
    else:
        pytest.fail("missing turn scan did not terminate")


def test_snapshot_compaction_leaves_search_intact(tmp_path, root):
    # `pcode --sessions --compact` prunes the step store, never the transcript.
    from pcode.sessions import compact_snapshots

    saved = SavedSession.create("test", tmp_path, root=root)
    saved.append("turn_started", run_id="run-a", prompt="Fix flicker")
    saved.append("Message", markdown="Suspend the editor during redraws.")
    saved.close()
    compact_snapshots(root / saved.info.id)
    (group,) = call(tools(tmp_path, root)["search_sessions"], query="redraws")["results"]
    assert group["turns"][0]["turn_id"] == "run-a"


def test_user_override_disables_extension(tmp_path, root):
    from pcode.ext import load_extensions

    directory = user_extension_dir()
    directory.mkdir(parents=True)
    (directory / "session_history.py").write_text("def setup(pcode):\n    pass\n")
    loaded = load_extensions(tmp_path, session_dir=root)
    (selected,) = [e for e in loaded.extensions if e.name == "session_history"]
    assert selected.scope == "user"
    assert not selected.capabilities
