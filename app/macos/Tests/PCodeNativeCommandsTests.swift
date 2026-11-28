import AppKit
import Foundation
import Testing
@testable import Ghostty

/// Which native route each terminal command takes, and the lists the routes show.
struct PCodeNativeCommandsTests {
    typealias R = PCodeNativeCommands.Route

    /// Every name in `TERMINAL_COMMANDS` (`src/pcode/controller.py`).
    static let terminalCommands = [
        "/help", "/commands", "/config", "/bind", "/unbind", "/quit", "/exit", "/status", "/tools",
        "/diffs", "/links", "/copy", "/paste-image", "/tree", "/agents", "/resume", "/attach", "/switch",
        "/fork", "/restart", "/stop", "/detach", "/show-activity", "/show-tasks", "/autohide-tasks",
        "/show-thinking", "/show-edits", "/show-commands", "/group-tools", "/theme", "/syntax",
        "/theme-preview", "/redraw",
    ]

    @Test func everyTerminalCommandHasARoute() {
        for name in Self.terminalCommands {
            #expect(PCodeNativeCommands.route(for: name) != nil, "\(name) has no route")
        }
        // ...and the catalog's terminal list is the same set.
        let catalog = Set(PCodeCommandCatalog.terminalCommands.flatMap { [$0.name] + $0.aliases })
        #expect(catalog == Set(Self.terminalCommands))
    }

    @Test func sessionCommandsGoToTheHost() {
        for line in ["/model", "/compact now", "/btw what?", "/new", "/new   ", "hello", "", "/unknown"] {
            #expect(PCodeNativeCommands.route(for: line) == nil, "\(line)")
        }
    }

    @Test func routes() {
        let cases: [(String, R)] = [
            ("/help", .help),
            ("/commands", .help),
            ("/status", .sessionInfo),
            ("/links", .links),
            ("/copy", .copy),
            ("/quit", .action(.closePane)),
            ("/exit", .action(.closePane)),
            ("/stop", .action(.closePane)),
            ("/paste-image", .action(.pasteImage)),
            ("/diffs", .action(.inspector(.changes))),
            ("/tools", .action(.inspector(.commands))),
            ("/tools failed", .action(.inspector(.commands))),
            ("/tools edits", .action(.inspector(.changes))),
            ("/tree", .action(.inspector(.tree))),
            ("/agents", .action(.inspector(.agents))),
            ("/resume", .action(.resume)),
            ("/attach", .action(.attach(nil))),
            ("/switch ab12cd34", .action(.attach("ab12cd34"))),
            ("/restart", .action(.restartHost)),
            ("/config", .action(.settings)),
            ("/config set theme dark", .config(["set", "theme", "dark"])),
            ("/config set prompt 'a b'", .config(["set", "prompt", "a b"])),
            ("/new fix the tests", .action(.newSession(prompt: "fix the tests"))),
            ("  /new   two  words ", .action(.newSession(prompt: "two  words"))),
            ("/show-thinking", .thinking(nil)),
            ("/show-thinking scrollback", .thinking("scrollback")),
            ("/show-thinking status-line", .thinking("activity-panel")),
            ("/group-tools", .groupTools(nil)),
            ("/group-tools off", .groupTools(false)),
            ("/redraw", .ignore),
        ]
        for (line, route) in cases {
            #expect(PCodeNativeCommands.route(for: line) == route, "\(line)")
        }
    }

    @Test func unsupportedAndUsage() {
        for line in ["/bind", "/unbind x", "/detach", "/fork", "/fork do it", "/restart all", "/attach -",
                     "/show-activity", "/show-tasks on", "/autohide-tasks", "/show-edits", "/show-commands",
                     "/theme dark", "/syntax monokai", "/theme-preview"] {
            guard case .unsupported = PCodeNativeCommands.route(for: line) else {
                Issue.record("\(line) should be unsupported")
                continue
            }
        }
        for line in ["/status now", "/links x", "/tools all", "/restart now", "/group-tools maybe",
                     "/show-thinking loud", "/quit now"] {
            guard case .usage = PCodeNativeCommands.route(for: line) else {
                Issue.record("\(line) should be a usage error")
                continue
            }
        }
    }

    @Test func thinkingCyclesOffAndScrollback() {
        #expect(PCodeNativeCommands.nextThinkingMode(after: "off") == "scrollback")
        #expect(PCodeNativeCommands.nextThinkingMode(after: "scrollback") == "off")
        #expect(PCodeNativeCommands.nextThinkingMode(after: "activity-panel") == "off")
        #expect(PCodeNativeCommands.nextThinkingMode(after: "") == "off")
    }

    @Test func shellWords() {
        #expect(PCodeNativeCommands.shellWords("set  key value") == ["set", "key", "value"])
        #expect(PCodeNativeCommands.shellWords(#"set k "a \"b\" c""#) == ["set", "k", #"a "b" c"#])
        #expect(PCodeNativeCommands.shellWords("set k 'x y' ''") == ["set", "k", "x y", ""])
        #expect(PCodeNativeCommands.shellWords(#"a\ b"#) == ["a b"])
        #expect(PCodeNativeCommands.shellWords("   ") == [])
    }

    // MARK: Running through a context

    final class Recorder {
        var actions: [PCodeNativeCommands.Action] = []
        var toasts: [String] = []
        var notified: [(String, [Any])] = []
        var presented: [String] = []
    }

    static func context(_ recorder: Recorder, state: PCodeHostClientState = .init()) -> PCodeNativeCommands.Context {
        PCodeNativeCommands.Context(
            workspace: "/tmp/w", hostID: "ab12cd34",
            state: { state },
            catalog: { PCodeCommandCatalog() },
            transcript: { [] },
            request: { _, _, completion in completion(nil, "offline") },
            notify: { method, args in recorder.notified.append((method, args)) },
            toast: { recorder.toasts.append($0) },
            present: { presentation in
                switch presentation {
                case .sheet(let title, _): recorder.presented.append(title)
                case .picker(let title, _): recorder.presented.append(title)
                }
            },
            perform: { recorder.actions.append($0) })
    }

    @Test func runDispatches() {
        let recorder = Recorder()
        let context = Self.context(recorder)
        #expect(!PCodeNativeCommands.run("/model x", context: context))
        #expect(PCodeNativeCommands.run("/tree", context: context))
        #expect(PCodeNativeCommands.run("/quit", context: context))
        #expect(recorder.actions == [.inspector(.tree), .closePane])
        #expect(PCodeNativeCommands.run("/help", context: context))
        #expect(PCodeNativeCommands.run("/status", context: context))
        #expect(recorder.presented == ["Commands", "Session"])
        #expect(PCodeNativeCommands.run("/fork", context: context))
        #expect(PCodeNativeCommands.run("/links", context: context))  // nothing to list
        #expect(recorder.toasts.count == 2)
        #expect(recorder.toasts.last == "No links in this conversation yet.")
    }

    @Test func readAsidesOpensTheDrawer() {
        let recorder = Recorder()
        PCodeNativeCommands.readAsides(context: Self.context(recorder))
        #expect(recorder.actions == [.btw])
    }

    // MARK: Lists

    @Test func modelListMirrorsModelUI() throws {
        let list = PCodeNativeCommands.modelList(
            ["anthropic:claude-opus-5", "openai:gpt-6"], providers: ["anthropic", "myproxy"], current: "openai:gpt-6")
        let items = try #require(list["items"] as? [[String: Any]])
        #expect(items.map { $0["label"] as? String } == ["Anthropic · claude-opus-5", "OpenAI · gpt-6"])
        #expect(items.map { $0["marker"] as? String } == ["", "current"])
        #expect((items[0]["search"] as? [String]) == ["anthropic:claude-opus-5", "anthropic claude-opus-5"])
        #expect(list["selected"] as? String == "openai:gpt-6")
        let custom = try #require(list["custom"] as? String)
        let anchored = try #require(PCodePickMatch.anchored(custom))
        #expect(PCodePickMatch.fullMatch(anchored, "myproxy:some/model-1"))
        #expect(PCodePickMatch.fullMatch(anchored, "anthropic:claude-x"))
        #expect(!PCodePickMatch.fullMatch(anchored, "nope:model"))
        // The picker decodes it.
        let picker = PCodePicker()
        var picked: String??
        picker.showLocal(list) { id, _ in picked = id }
        #expect(picker.payload?.items.count == 2)
        picker.cancel()
        #expect(picked == nil)
    }

    @Test func linksAsPcodeFindsThem() {
        let links = PCodeNativeCommands.extractLinks(
            "See [the docs](https://example.com/docs). Also https://example.com/a), and (https://w.org/A_(b)).",
            source: "assistant")
        #expect(links.map(\.url) == ["https://example.com/docs", "https://example.com/a", "https://w.org/A_(b)"])
        #expect(links.first?.label == "the docs")

        let messages: [PCodeNativeCommands.Message] = [
            .init(role: .user, text: "look at [home](https://h.io) and https://x.io"),
            .init(role: .assistant, text: "Sure: https://h.io again"),
        ]
        let found = PCodeNativeCommands.conversationLinks(messages)
        // Latest position wins, the earlier label stays.
        #expect(found.map(\.url) == ["https://x.io", "https://h.io"])
        #expect(found.last?.label == "home")
        #expect(found.last?.source == "assistant")
        let items = PCodeNativeCommands.linkList(found)["items"] as? [[String: Any]]
        #expect(items?.first?["id"] as? String == "https://h.io")
        #expect(items?.first?["label"] as? String == "home — https://h.io")
    }

    @Test func copyListNestsCodeBlocks() throws {
        let (list, texts) = PCodeNativeCommands.copyList([
            "first answer",
            "Here:\n```swift\nlet a = 1\nlet b = 2\n```\nand\n~~~\nraw\n~~~",
        ])
        let items = try #require(list["items"] as? [[String: Any]])
        #expect(items.map { $0["id"] as? String } == ["r1", "r1c0", "r1c1", "r0"])
        #expect(items.map { $0["depth"] as? Int } == [0, 1, 1, 0])
        #expect(texts["r1c0"] == "let a = 1\nlet b = 2")
        #expect(texts["r1c1"] == "raw")
        #expect(texts["r0"] == "first answer")
    }

    @Test func helpGroupsKeepCatalogOrder() {
        let catalog = PCodeCommandCatalog(snapshot: .init(commands: [
            PCodeCommand("/model", "Switch model", group: "Model"),
            PCodeCommand("/review", "Skill", group: "Skills"),
        ]))
        let groups = PCodeNativeCommands.helpGroups(catalog)
        #expect(groups.map(\.name) == ["App", "Inspect", "Session", "Display", "Model", "Skills"])
        #expect(groups.first?.commands.first?.name == "/help")
        #expect(groups.flatMap(\.commands).count == catalog.commands.count)
    }

    @Test func sessionRows() {
        var state = PCodeHostClientState()
        state.model = "anthropic:claude"
        state.sessionID = "s1"
        let rows = PCodeNativeCommands.sessionRows(state: state, hostID: "ab12cd34")
        #expect(rows.map(\.label) == ["Host", "Model", "Session"])
        let overview = PCodeNativeCommands.overviewRows([["Model", "x"], ["Context", "12%"], ["bad"], 3])
        #expect(overview == [.init(label: "Model", value: "x"), .init(label: "Context", value: "12%")])
    }

    @Test func jobsFromPeek() {
        let peek: [String: Any] = ["jobs": [
            ["id": "j1", "command": "make", "purpose": "building", "adopted": false],
            ["command": "no id"],
        ]]
        #expect(PCodeNativeCommands.jobs(fromPeek: peek) == [.init(id: "j1", command: "make", purpose: "building")])
        #expect(PCodeNativeCommands.jobs(fromPeek: nil).isEmpty)
    }

    @Test func savePreferenceKeepsOtherKeys() throws {
        let dir = FileManager.default.temporaryDirectory.appendingPathComponent("pcode-prefs-\(UUID().uuidString)")
        defer { try? FileManager.default.removeItem(at: dir) }
        let url = dir.appendingPathComponent("preferences.json")
        try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        try Data(#"{"theme": "dark"}"#.utf8).write(to: url)
        PCodeNativeCommands.savePreference("show_thinking", "off", url: url)
        let saved = try #require(try JSONSerialization.jsonObject(with: Data(contentsOf: url)) as? [String: String])
        #expect(saved == ["theme": "dark", "show_thinking": "off"])
    }

    @Test func messagesFromTranscriptRows() {
        let rows = [
            PCodeTranscriptItem(id: "p", payload: .prompt(label: "›", text: "hi", isSide: false), spacingAfter: 0),
            PCodeTranscriptItem(id: "s", payload: .prompt(label: "btw", text: "side", isSide: true), spacingAfter: 0),
            PCodeTranscriptItem(id: "a", payload: .answerActions(copyMarkdown: "hello"), spacingAfter: 0),
        ]
        #expect(PCodeNativeCommands.messages(rows) == [.init(role: .user, text: "hi"), .init(role: .assistant, text: "hello")])
    }
}
