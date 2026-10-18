"""Session recall is scoped, read-only, indexed, and survives compaction."""

import asyncio
import json
import subprocess
from types import SimpleNamespace

import pytest
from pydantic_ai import Agent, ModelRetry
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import FunctionModel

from pcode.ext import BUNDLED_DIR, Extension, ExtensionUI, load_extension, user_extension_dir
from pcode.history import CHUNK_CHARS, History, read_turns
from pcode.history_embeddings import CACHE_NAME, semantic_ranking
from pcode.history_index import index_name
from pcode.sessions import SavedSession, SessionInfo


@pytest.fixture(autouse=True)
def no_embeddings(monkeypatch):
    monkeypatch.delenv("PCODE_HISTORY_EMBEDDING_MODEL", raising=False)


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
    if "query" in kwargs:
        kwargs["queries"] = [kwargs.pop("query")]
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
    assert result["scan_complete"] and not result["warnings"]
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
                parts=[
                    ToolCallPart("search_sessions", {"queries": ["flicker"], "scope": "session"})
                ]
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
    _, turns, _ = read_turns(info, root)
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
    chunks = History(tmp_path, root).chunks("project")[0]
    assert chunks
    history = History(tmp_path, root)
    assert history.search("project", ["redraws"])[0]
    assert history.search("project", ["pytest"])[0]
    assert not history.search("project", ["private-output"])[0]


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
    chunks, coverage = History(tmp_path, root).chunks("project")
    assert len(chunks) == 1
    assert "Skipped unreadable session other." in coverage.warnings
    assert any("Unfinished final record" in warning for warning in coverage.warnings)
    assert not coverage.complete


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
    chunks = History(tmp_path, root).chunks("project")[0]
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
    chunks = History(tmp_path, root).chunks("project")[0]
    embedder = FakeEmbedder()
    ranking, warnings = asyncio.run(
        semantic_ranking(chunks, "query", "test:model", root, embedder=embedder)
    )
    assert len(ranking) == 1
    assert "partial" in warnings[0]
    assert len(embedder.documents) == 1
    (root / "session-a" / "session.json").unlink()
    assert History(tmp_path, root).chunks("project")[0] == []


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


def test_semantic_shortlist_keeps_distinct_turns(tmp_path, root):
    save(root, tmp_path, records=turn(response="x" * CHUNK_CHARS * 55) + turn(identity="run-b"))
    chunks = History(tmp_path, root).chunks("project")[0]
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
            chunks = history.chunks("session")[0]
            assert chunks
            hits, _ = history.search("session", ["bounded queue"])
            read = history.read(saved.info.id, hits[0].turn.id, "session")
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
    history = History(tmp_path, root)

    def ranking(query):
        return [chunk.turn.id for chunk in history.search("project", [query])[0]]

    ranked = ranking("editor flicker")
    assert ranked[0] == "rare-term"
    assert "none" not in ranked
    # Repeating a common term does not beat the document holding the rare one.
    assert ranked.index("many-common") > ranked.index("phrase")
    # Punctuation and case are ignored; an empty or symbol-only query matches nothing.
    assert ranking("FLICKER.") == ranking("flicker")
    assert ranking("!!!") == []


def test_current_turn_excluded_and_current_session_flagged(tmp_path, root):
    records = turn("Earlier work", "We chose a bounded queue.") + [
        {"kind": "turn_started", "run_id": "live", "prompt": "bounded queue question"}
    ]
    save(root, tmp_path, records=records)
    registered = tools(tmp_path, root)
    search = registered["search_sessions"].function
    context = SimpleNamespace(conversation_id="session-a", run_id="live")
    (group,) = asyncio.run(search(context, queries=["bounded queue"], scope="session"))["results"]
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
    (group,) = asyncio.run(search(context, queries=["bounded queue"], scope="session"))["results"]
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


def test_index_is_private_reused_refreshed_and_pruned(tmp_path, root, monkeypatch):
    import pcode.history

    parsed = []
    original = pcode.history.read_turns

    def counting(info, directory):
        parsed.append(info.id)
        return original(info, directory)

    monkeypatch.setattr("pcode.history.read_turns", counting)
    save(root, tmp_path)
    save(root, tmp_path, "other", records=turn(response="Unrelated"))
    history = History(tmp_path, root)
    assert history.search("project", ["redraws"])[0]
    assert sorted(parsed) == ["other", "session-a"]
    assert (root / index_name()).stat().st_mode & 0o777 == 0o600
    # An unchanged transcript is not parsed again.
    assert history.search("project", ["redraws"])[0]
    assert len(parsed) == 2
    # An appended one is, and its new text is searchable.
    with (root / "session-a" / "transcript.jsonl").open("a") as stream:
        stream.write(json.dumps({"kind": "Message", "run_id": "run-a", "markdown": "zebra"}) + "\n")
    assert history.search("project", ["zebra"])[0]
    assert parsed[2:] == ["session-a"]
    # A deleted session's rows go on the next refresh, whatever its scope.
    (root / "session-a" / "session.json").unlink()
    History(tmp_path / "elsewhere", root).search("workspace", ["anything"])
    assert b"zebra" not in (root / index_name()).read_bytes()


def test_out_of_time_refresh_reports_pending_sessions(tmp_path, root, monkeypatch):
    save(root, tmp_path)
    monkeypatch.setattr("pcode.history.REFRESH_SECONDS", -1.0)
    registered = tools(tmp_path, root)
    result = call(registered["search_sessions"], query="redraws")
    assert not result["results"] and not result["scan_complete"]
    assert result["sessions_pending"] == 1 and result["sessions_searched"] == 0
    assert "still being built" in result["warnings"][0]
    # Reading a session always indexes it, however long that takes.
    read = call(registered["read_session"], session_id="session-a", turn_id="run-a")
    assert "redraws" in read["text"]
    # A changed session that has an older copy is still searched while pending.
    with (root / "session-a" / "transcript.jsonl").open("a") as stream:
        stream.write(json.dumps({"kind": "Message", "run_id": "run-a", "markdown": "late"}) + "\n")
    result = call(registered["search_sessions"], query="redraws")
    assert result["results"] and result["sessions_pending"] == 1
    monkeypatch.setattr("pcode.history.REFRESH_SECONDS", 30.0)
    result = call(registered["search_sessions"], query="late")
    assert result["results"] and result["scan_complete"]


def test_corrupt_index_is_rebuilt_and_stale_versions_swept(tmp_path, root):
    import os

    save(root, tmp_path)
    root.mkdir(exist_ok=True)
    (root / index_name()).write_bytes(b"not a database" * 100)
    stale = root / ".history-index-v0-old.sqlite3"
    recent = root / ".history-index-v2-new.sqlite3"
    stale.write_bytes(b"x")
    recent.write_bytes(b"x")
    os.utime(stale, (0, 0))
    assert History(tmp_path, root).search("project", ["redraws"])[0]
    # Another version's index in use stays; one untouched for a week goes.
    assert recent.exists() and not stale.exists()


def test_index_errors_become_retries(tmp_path, root, monkeypatch):
    import sqlite3

    save(root, tmp_path)

    def locked(self):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr("pcode.history_index.HistoryIndex._open", locked)
    registered = tools(tmp_path, root)
    with pytest.raises(ModelRetry, match="OperationalError"):
        call(registered["search_sessions"], query="redraws")
    with pytest.raises(ModelRetry, match="OperationalError"):
        call(registered["read_session"], session_id="session-a", turn_id="run-a")


def test_semantic_failure_does_not_mark_keyword_coverage_incomplete(tmp_path, root, monkeypatch):
    save(root, tmp_path)
    monkeypatch.setenv("PCODE_HISTORY_EMBEDDING_MODEL", "test:model")

    def fail(model):
        raise ValueError("down")

    monkeypatch.setattr("pydantic_ai.Embedder", fail)
    result = call(tools(tmp_path, root)["search_sessions"], query="redraws")
    assert result["scan_complete"] and result["warnings"]


def test_hits_carry_their_chunk_not_the_whole_turn(tmp_path, root):
    save(root, tmp_path, records=turn(response="needle " * CHUNK_CHARS))
    hits, _ = History(tmp_path, root).search("project", ["needle"])
    assert len(hits) > 3
    assert all(not hit.turn.text and len(hit.text) <= CHUNK_CHARS for hit in hits)


def test_several_queries_fuse_and_agreement_ranks_first(tmp_path, root):
    records = (
        turn("One", "The flicker came back.", "only-first")
        + turn("Two", "The redraw is slow.", "only-second")
        + turn("Both", "Flicker during each redraw.", "both")
    )
    save(root, tmp_path, records=records)
    registered = tools(tmp_path, root)
    result = call(registered["search_sessions"], queries=["flicker", "redraw"])
    (group,) = result["results"]
    ids = [hit["turn_id"] for hit in group["turns"]]
    assert ids[0] == "both" and set(ids) == {"both", "only-first", "only-second"}
    with pytest.raises(ModelRetry):
        call(registered["search_sessions"], queries=["a"] * 6)
    with pytest.raises(ModelRetry):
        call(registered["search_sessions"], queries=["  "])


def test_failure_messages_are_searchable(tmp_path, root):
    records = [
        {"kind": "turn_started", "run_id": "run-a", "prompt": "Use the drive"},
        {
            "kind": "ToolSummary",
            "name": "mcp_gdrive_read",
            "failed": True,
            "error": "OAuthFlowError: refresh token expired",
        },
        {
            "kind": "turn_failed",
            "run_id": "run-a",
            "error": {"type": "ModelHTTPError", "status": 400, "provider_message": "out of usage"},
        },
    ]
    save(root, tmp_path, records=records)
    history = History(tmp_path, root)
    (tool,) = history.search("project", ["OAuthFlowError"])[0]
    assert "[failed: OAuthFlowError: refresh token expired]" in tool.text
    (failed,) = history.search("project", ["out of usage"])[0]
    assert "Turn failed: ModelHTTPError 400: out of usage" in failed.text
    assert failed.turn.status == "failed"


def test_results_carry_the_session_title(tmp_path, root):
    save(root, tmp_path, title="Editor flicker fix")
    (group,) = call(tools(tmp_path, root)["search_sessions"], query="redraws")["results"]
    assert group["title"] == "Editor flicker fix"


def test_overview_lists_turns_with_prompts_conclusions_and_failures(tmp_path, root, monkeypatch):
    records = (
        turn("Fix flicker\n\nwith details", "Suspend the editor.", "run-a")
        + [
            {"kind": "turn_started", "run_id": "run-b", "prompt": "Ship it"},
            {"kind": "turn_failed", "run_id": "run-b", "error": {"type": "Boom", "message": "x"}},
        ]
        + turn("Third", "Done.", "run-c")
    )
    save(root, tmp_path, "0d59e869-0bce-4594-a658-3e15a5954410", records=records, title="Flicker")
    monkeypatch.setattr("pcode.history.OVERVIEW_TURNS", 2)
    read = tools(tmp_path, root)["read_session"].function
    context = SimpleNamespace(conversation_id="other")
    first = asyncio.run(read(context, session_id="0d59e869"))
    assert first["session_id"].startswith("0d59e869") and first["title"] == "Flicker"
    assert first["turn_count"] == 3 and first["next_offset"] == 2
    a, b = first["turns"]
    assert a["prompt"] == "User: Fix flicker\n\nwith details"
    assert a["conclusion"] == "Suspend the editor."
    assert b["status"] == "failed" and b["failure"] == "Boom: x" and "conclusion" not in b
    rest = asyncio.run(read(context, session_id="0d59e869", offset=2))
    assert [t["turn_id"] for t in rest["turns"]] == ["run-c"] and rest["next_offset"] is None


def test_session_prefixes_must_be_long_and_unique(tmp_path, root):
    save(root, tmp_path, "abcdef12-one")
    save(root, tmp_path, "abcdef12-two")
    history = History(tmp_path, root)
    with pytest.raises(ValueError, match="2 sessions"):
        history.read("abcdef12")
    with pytest.raises(ValueError, match="at least 8"):
        history.read("abcdef1")
    assert history.read("abcdef12-o", "run-a")["session_id"] == "abcdef12-one"


def test_overview_ignores_quoted_failures_and_pages_by_size(tmp_path, root):
    quoted = "Seen before:\nTurn failed: not this one"
    records = [record for i in range(30) for record in turn(f"Task {i}", quoted, f"run-{i}")]
    save(root, tmp_path, records=records, name="secret ghp_" + "a" * 36)
    history = History(tmp_path, root)
    page = history.read("session-a", max_chars=2000)
    assert not any("failure" in entry for entry in page["turns"])
    assert 1 < len(page["turns"]) < 30 and page["next_offset"] == len(page["turns"])
    assert "ghp_" not in page["title"]
    # One turn always fits, however small the budget.
    assert len(history.read("session-a", max_chars=1)["turns"]) == 1
