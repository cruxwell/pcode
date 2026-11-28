import Foundation
import Testing
@testable import Ghostty

/// The native `/` menu must complete as pcode's `SlashCompleter` does.
struct PCodeCommandCatalogTests {
    /// A trimmed `session_state` as the host sends it.
    static let sessionState = """
    {
      "model": "anthropic:claude",
      "workspace": "/tmp/w",
      "commands": [
        {"name": "/model", "description": "Switch model", "arguments": [], "aliases": [],
         "free_arguments": true, "group": "Model", "argument_descriptions": {},
         "completer": "model_completions", "models": false},
        {"name": "/btw", "description": "Ask a side question", "arguments": [], "aliases": ["/aside"],
         "free_arguments": true, "group": "Inspect", "argument_descriptions": {},
         "completer": "aside_completions", "models": true},
        {"name": "/compact", "description": "Summarize history", "arguments": ["now", "never"],
         "aliases": [], "free_arguments": false, "group": "History",
         "argument_descriptions": {"now": "Compact right away"}, "completer": null, "models": false},
        {"name": "/mcp", "description": "MCP servers", "arguments": [], "aliases": [],
         "free_arguments": true, "group": "Tools", "argument_descriptions": {}, "completer": null, "models": false},
        {"name": "/help", "description": "shadowed by the terminal's"},
        {"name": "/review", "description": "Skill: review code", "group": "Skills"}
      ],
      "arguments": {"/mcp": ["github", "linear"]},
      "skills": ["/review"],
      "models": ["anthropic:claude-opus", "openai:gpt-5", "anthropic:claude-haiku"]
    }
    """

    private func catalog() throws -> PCodeCommandCatalog {
        PCodeCommandCatalog(snapshot: try .init(sessionStateJSON: Data(Self.sessionState.utf8)))
    }

    private func texts(_ completions: [PCodeCommandCatalog.Completion]) -> [String] {
        completions.map(\.text)
    }

    // MARK: Decoding

    @Test func decodesSessionState() throws {
        let snapshot = try PCodeCommandCatalog.Snapshot(sessionStateJSON: Data(Self.sessionState.utf8))
        #expect(snapshot.commands.count == 6)
        #expect(snapshot.commands[2].argumentDescriptions == ["now": "Compact right away"])
        #expect(snapshot.commands[1].aliases == ["/aside"])
        #expect(snapshot.commands[1].models)
        #expect(snapshot.commands[0].freeArguments)
        #expect(snapshot.commands[5].group == "Skills")
        #expect(snapshot.commands[5].handling == .session)
        #expect(snapshot.arguments == ["/mcp": ["github", "linear"]])
        #expect(snapshot.skills == ["/review"])
        #expect(snapshot.models.count == 3)
    }

    @Test func decodesAParsedDictionary() throws {
        let object = try JSONSerialization.jsonObject(with: Data(Self.sessionState.utf8)) as! [String: Any]
        let snapshot = try #require(PCodeCommandCatalog.Snapshot(sessionState: object))
        #expect(snapshot.commands.map(\.name).contains("/compact"))
        #expect(PCodeCommandCatalog.Snapshot(sessionState: ["model": "x"]) == nil)
    }

    @Test func terminalCommandsWinAndComeFirst() throws {
        let catalog = try catalog()
        #expect(catalog.commands.first?.name == "/help")
        #expect(catalog.commands.filter { $0.name == "/help" }.count == 1)
        #expect(catalog.find("/help")?.handling == .terminal)
        #expect(catalog.find("/switch")?.name == "/attach")
        #expect(catalog.find("/diffs")?.handling == .native)
        #expect(catalog.find("/review")?.handling == .session)
    }

    @Test func terminalListMatchesPythonsSet() {
        // TERMINAL_COMMANDS in src/pcode/controller.py.
        let expected: Set<String> = [
            "/help", "/commands", "/config", "/bind", "/unbind", "/quit", "/exit", "/status", "/tools",
            "/diffs", "/links", "/copy", "/paste-image", "/tree", "/agents", "/resume", "/attach", "/switch",
            "/fork", "/restart", "/stop", "/detach", "/show-activity", "/show-tasks", "/autohide-tasks",
            "/show-thinking", "/show-edits", "/show-commands", "/group-tools", "/theme", "/syntax",
            "/theme-preview", "/redraw",
        ]
        let names = Set(PCodeCommandCatalog.terminalCommands.flatMap { [$0.name] + $0.aliases })
        #expect(names == expected)
    }

    // MARK: Completion

    @Test func namePrefixIncludesAliases() throws {
        let catalog = try catalog()
        #expect(texts(catalog.completions(before: "/co")) == ["/help", "/config", "/copy", "/compact"])
        #expect(texts(catalog.completions(before: "/as")) == ["/btw"])
        let first = try #require(catalog.completions(before: "/di").first)
        #expect(first == .init(text: "/diffs", replaceLength: 3, display: "/diffs",
                               meta: "Git diff of this worktree's branch, or of files edited this session"))
    }

    @Test func bareSlashListsEverything() throws {
        let catalog = try catalog()
        #expect(catalog.completions(before: "/").count == catalog.commands.count)
    }

    @Test func fixedArgumentsWithDescriptions() throws {
        let catalog = try catalog()
        let completions = catalog.completions(before: "/compact n")
        #expect(completions == [
            .init(text: "now", replaceLength: 1, display: "now", meta: "Compact right away"),
            .init(text: "never", replaceLength: 1, display: "never", meta: ""),
        ])
        #expect(texts(catalog.completions(before: "/theme ")) == ["dark", "light", "auto"])
        #expect(catalog.completions(before: "/theme d ").isEmpty)
    }

    @Test func dynamicArgumentsReplaceFixedOnes() throws {
        #expect(texts(try catalog().completions(before: "/mcp li")) == ["linear"])
    }

    @Test func aliasFindsArguments() throws {
        #expect(texts(try catalog().completions(before: "/exit ")).isEmpty)
        #expect(texts(try catalog().completions(before: "/switch ")).isEmpty)
    }

    @Test func onlyOneLineWithNothingAfterTheCaret() throws {
        let catalog = try catalog()
        #expect(catalog.completions(before: "/co", after: "x").isEmpty)
        #expect(catalog.completions(before: "hi\n/co").isEmpty)
        #expect(catalog.completions(before: "/co", after: "\nmore").isEmpty)
        #expect(catalog.completions(before: "co").isEmpty)
        #expect(catalog.completions(before: "/nope ").isEmpty)
    }

    @Test func commandsWithTheirOwnCompleterAreSkipped() throws {
        #expect(try catalog().completions(before: "/model op").isEmpty)
    }

    @Test func modelWordsComplete() throws {
        let catalog = try catalog()
        let completions = catalog.completions(before: "/btw $claude")
        #expect(texts(completions) == ["$anthropic:claude-opus", "$anthropic:claude-haiku"])
        #expect(completions.first?.replaceLength == 7)
        #expect(completions.first?.display == "anthropic:claude-opus")
        // In a prompt too, and by alias.
        #expect(texts(catalog.completions(before: "explain this $gpt")) == ["$openai:gpt-5"])
        #expect(texts(catalog.completions(before: "/aside $GPT", after: " rest")) == ["$openai:gpt-5"])
        // Not in a shell line or another command, nor with text glued after the caret.
        #expect(catalog.completions(before: "!echo $gpt").isEmpty)
        #expect(catalog.completions(before: "/compact $gpt").isEmpty)
        #expect(catalog.completions(before: "hi $gpt", after: "x").isEmpty)
        // An effort word is pcode's; nothing here, and no `/` menu either.
        #expect(catalog.completions(before: "/btw +hi").isEmpty)
    }

    // MARK: Snapshots

    @Test func snapshotRoundTripsAndFallsBack() throws {
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent("pcode-catalog-\(UUID().uuidString)")
        defer { try? FileManager.default.removeItem(at: directory) }
        let store = PCodeCatalogStore(directory: directory)
        #expect(store.load(workspace: "/tmp/a") == nil)
        #expect(store.catalog(workspace: "/tmp/a").commands == PCodeCommandCatalog.terminalCommands)

        let snapshot = try PCodeCommandCatalog.Snapshot(sessionStateJSON: Data(Self.sessionState.utf8))
        #expect(store.save(snapshot, workspace: "/tmp/a"))
        let loaded = try #require(store.load(workspace: "/tmp/a/"))
        #expect(loaded.commands == snapshot.commands)
        #expect(loaded.arguments == snapshot.arguments)
        #expect(loaded.models == snapshot.models)
        #expect(loaded.skills.isEmpty)

        // Another workspace gets the last one saved anywhere, until its own exists.
        #expect(store.load(workspace: "/tmp/b")?.commands == snapshot.commands)
        var other = snapshot
        other.commands = [PCodeCommand("/only-b", "b")]
        store.save(other, workspace: "/tmp/b")
        #expect(store.load(workspace: "/tmp/b")?.commands.map(\.name) == ["/only-b"])
        #expect(store.load(workspace: "/tmp/a")?.commands == snapshot.commands)
        #expect(store.load(workspace: "/tmp/new")?.commands.map(\.name) == ["/only-b"])
    }

    @Test func fileNameIsAStableHash() {
        let name = PCodeCatalogStore.fileName(for: "/tmp/a")
        #expect(name.count == 64 + 5)
        #expect(name == PCodeCatalogStore.fileName(for: "/tmp/a/"))
        #expect(name != PCodeCatalogStore.fileName(for: "/tmp/b"))
    }
}
