import AppKit
import Foundation
import Testing
@testable import Ghostty

/// The native composer's host protocol: RPC framing, mirrored state, spawn output and keys.
struct PCodeComposerTests {
    private func wire(_ line: String) throws -> [String: Any] {
        try #require(try JSONSerialization.jsonObject(with: Data(line.utf8)) as? [String: Any])
    }

    // MARK: Framing

    @Test func helloNamesTheProtocol() throws {
        let line = PCodeHostRPCClient.helloLine()
        #expect(line.last == 0x0A)
        let obj = try #require(try JSONSerialization.jsonObject(with: line.dropLast()) as? [String: Any])
        #expect(obj["type"] as? String == "hello")
        #expect(obj["protocol"] as? Int == PCodeHostRPCClient.protocolVersion)
    }

    @Test func handshakeReadyOrError() {
        #expect(PCodeHostRPCClient.handshakeError(["type": "ready", "protocol": 2]) == nil)
        #expect(PCodeHostRPCClient.handshakeError(["type": "error", "message": "speaks protocol 3"]) == "speaks protocol 3")
        #expect(PCodeHostRPCClient.handshakeError(["type": "welcome"]) != nil)
    }

    @Test func encodesNotifyWithNullTag() throws {
        let data = try #require(PCodeHostRPCClient.encode(
            ["type": "notify", "method": "command", "args": ["/compact", NSNull()], "kwargs": [String: Any]()]))
        let obj = try #require(try JSONSerialization.jsonObject(with: data) as? [String: Any])
        #expect(obj["method"] as? String == "command")
        let args = try #require(obj["args"] as? [Any])
        #expect(args.first as? String == "/compact")
        #expect(args.last is NSNull)
    }

    @Test func parsesNotifyRequestAndReply() throws {
        guard case .notify(let method, let args, _)? = PCodeHostRPCClient.parse(try wire(
            #"{"type":"notify","method":"state","args":[{"busy":true}],"kwargs":{}}"#))
        else { Issue.record("not a notify"); return }
        #expect(method == "state")
        #expect((args.first as? [String: Any])?["busy"] as? Bool == true)

        guard case .request(let id, let name, let reqArgs, let kwargs)? = PCodeHostRPCClient.parse(try wire(
            #"{"type":"request","id":7,"method":"run_command","args":["/help"],"kwargs":{"idle":true,"tag":"t1"}}"#))
        else { Issue.record("not a request"); return }
        #expect(id == 7 && name == "run_command")
        #expect(reqArgs.first as? String == "/help")
        #expect(kwargs["tag"] as? String == "t1")

        guard case .reply(let replyID, let result, let error)? = PCodeHostRPCClient.parse(try wire(
            #"{"type":"reply","id":1,"result":null}"#))
        else { Issue.record("not a reply"); return }
        #expect(replyID == 1 && result == nil && error == nil)

        guard case .reply(_, _, let failure)? = PCodeHostRPCClient.parse(try wire(
            #"{"type":"reply","id":2,"error":{"type":"PermissionError","message":"no"}}"#))
        else { Issue.record("not a reply"); return }
        #expect(failure == "no")

        #expect(PCodeHostRPCClient.parse(try wire(#"{"type":"future"}"#)) == nil)
    }

    @Test func lineBufferSplitsAcrossReads() {
        var buffer = PCodeLineBuffer()
        let firstOK = buffer.append(ArraySlice(Array(#"{"a":1}"#.utf8) + [0x0A] + Array(#"{"b":"#.utf8)))
        let a = buffer.nextObject()
        let none = buffer.nextObject()
        let secondOK = buffer.append(ArraySlice(Array("2}\n\nnot json\n".utf8)))
        let b = buffer.nextObject()
        let end = buffer.nextObject()
        #expect(firstOK && secondOK)
        #expect(a?["a"] as? Int == 1)
        #expect(none == nil)
        #expect(b?["b"] as? Int == 2)
        #expect(end == nil)
    }

    // MARK: State

    @Test func foldsActivityAndSessionState() throws {
        var state = PCodeHostClientState()
        state.applyActivity(try wire(
            #"{"busy":true,"status":"Running shell · ls…","prompt_state":"running","prompt_kind":"user","job_count":2,"queued_prompts":["a","b"],"queued_modes":["steering","queue"]}"#))
        state.applySession(try wire(
            #"{"model":"claude:opus","effort":"high","context":" · 13.1k/600k","session_id":"s1","session_title":"Fix it","startup_pending":false,"startup_error":"","commands":[]}"#))
        #expect(state.busy && state.jobCount == 2)
        #expect(state.phase == "Running shell")
        #expect(state.pendingCounts == (1, 1))
        #expect(state.context == "13.1k/600k")
        #expect(state.sessionID == "s1" && state.sessionTitle == "Fix it" && state.model == "claude:opus")

        let status = state.composerStatus(sendMode: "queue", sendModeOnce: true)
        #expect(status.busy && status.phase == "Running shell")
        #expect(status.steeringPending == 1 && status.queued == 1 && status.jobs == 2)
        #expect(status.sendMode == "queue" && status.sendModeOnce)

        state.applyActivity(["busy": false, "prompt_state": "done", "status": ""])
        #expect(state.phase == "")
        #expect(state.pendingCounts == (0, 0))
    }

    @Test func phaseFollowsTheTerminalWording() {
        var state = PCodeHostClientState()
        state.promptState = "running"
        #expect(state.phase == "Thinking")
        state.status = "Waiting for model…"
        #expect(state.phase == "Waiting for model")
        state.promptKind = "command"
        state.prompt = "/compact"
        #expect(state.phase == "/compact")
        state.startupPending = true
        #expect(state.phase == "Starting")
    }

    @Test func countsSideQuestions() {
        var state = PCodeHostClientState()
        state.applyAsides([
            "1": ["status": "running", "read": false],
            "2": ["status": "done", "read": false],
            "3": ["status": "done", "read": true],
        ])
        #expect(state.asidesRunning == 1 && state.asidesReady == 1)
    }

    @Test func decodesPlanItems() {
        let plan = PCodePlanItem.decode([
            ["id": "a1", "content": "Add it", "status": "in_progress", "active_form": "Adding it", "size": "L"],
            ["id": "b2", "content": "Test it"],
        ])
        #expect(plan == [
            PCodePlanItem(id: "a1", content: "Add it", status: "in_progress", activeForm: "Adding it", size: "L"),
            PCodePlanItem(id: "b2", content: "Test it", status: "pending", activeForm: "", size: nil),
        ])
        #expect(PCodePlanItem.decode(nil).isEmpty)
    }

    // MARK: Spawning

    @Test func parsesSpawnOutput() {
        let out = "Last login: today\n{\"id\": \"ab12cd34\", \"socket\": \"/h/ab12cd34.sock\", \"log\": \"/h/ab12cd34.log\", \"pid\": 42, \"existing\": false}\n"
        #expect(PCodeComposerSession.parseSpawnOutput(out)
            == .host(id: "ab12cd34", socket: "/h/ab12cd34.sock", log: "/h/ab12cd34.log", pid: 42, existing: false))
        #expect(PCodeComposerSession.parseSpawnOutput(#"{"trust": {"key": "/r", "code": [".pcode/worktree-setup"]}}"#)
            == .trust(key: "/r", code: [".pcode/worktree-setup"]))
        #expect(PCodeComposerSession.parseSpawnOutput(#"{"error": "Workspace /x is not an existing directory."}"#)
            == .error("Workspace /x is not an existing directory."))
        #expect(PCodeComposerSession.parseSpawnOutput("", stderr: "pcode: command not found\n", status: 127)
            == .error("pcode: command not found"))
    }

    @Test func spawnArguments() {
        #expect(PCodeComposerSession.spawnArguments(resume: nil, trust: nil) == ["--host", "--spawn-only"])
        #expect(PCodeComposerSession.spawnArguments(resume: "s1", trust: "trust")
            == ["--host", "--spawn-only=trust", "-c", "s1"])
    }

    @Test func nativeComposerFlag() {
        #expect(!PCodeNativeComposer.enabled(environment: [:], preferences: [:]))
        #expect(PCodeNativeComposer.enabled(environment: [:], preferences: ["native_composer": "on"]))
        #expect(!PCodeNativeComposer.enabled(environment: ["PCODE_NATIVE_COMPOSER": "0"], preferences: ["native_composer": "on"]))
        #expect(PCodeNativeComposer.enabled(environment: ["PCODE_NATIVE_COMPOSER": "1"], preferences: [:]))
    }

    @Test func preferencesPathFollowsPcode() {
        #expect(PCodePreferences.url(environment: ["PCODE_CONFIG_DIR": "/c"]).path == "/c/preferences.json")
        #expect(PCodePreferences.url(environment: ["XDG_CONFIG_HOME": "/x"]).path == "/x/pcode/preferences.json")
    }

    // MARK: Sending

    @Test func shellAndTerminalCommands() {
        #expect(PCodeComposerSession.isShellCommand("!ls -la"))
        #expect(!PCodeComposerSession.isShellCommand("!"))
        #expect(!PCodeComposerSession.isShellCommand("!!"))
        #expect(!PCodeComposerSession.isShellCommand("hi !there"))
        let catalog = PCodeCommandCatalog()
        #expect(catalog.handling(for: "/help") == .terminal)
        #expect(catalog.handling(for: "/paste-image") == .native)
        #expect(catalog.handling(for: "/new write the docs") == .terminal)
        #expect(catalog.handling(for: "/new") == .session)
        #expect(catalog.handling(for: "/compact") == .session)
    }

    // MARK: Completion menu

    private func catalog() -> PCodeCommandCatalog {
        PCodeCommandCatalog(snapshot: .init(
            commands: [
                PCodeCommand("/compact", "Summarize the conversation"),
                PCodeCommand("/model", "Switch model", arguments: ["claude:opus", "openai:gpt"]),
            ],
            models: ["claude:opus"]))
    }

    @Test func commandRowsReplaceTheTypedWord() {
        let rows = PCodeComposerCompletion.commandRows(catalog(), text: "/com", caret: 4)
        #expect(rows.map(\.insertion).contains("/compact"))
        #expect(rows.map(\.insertion).contains("/commands") == false)  // An alias completes as its name.
        let compact = rows.first { $0.insertion == "/compact" }!
        #expect(compact.range == NSRange(location: 0, length: 4))
        #expect(compact.meta == "Summarize the conversation")
        #expect(PCodeComposerCompletion.apply(compact, to: "/com") == ("/compact", 8))
    }

    @Test func commandRowsCompleteArguments() {
        let rows = PCodeComposerCompletion.commandRows(catalog(), text: "/model cl", caret: 9)
        #expect(rows.map(\.insertion) == ["claude:opus"])
        #expect(rows[0].range == NSRange(location: 7, length: 2))
        #expect(PCodeComposerCompletion.apply(rows[0], to: "/model cl").text == "/model claude:opus")
    }

    @Test func noRowsForProseOrTextAfterTheCaret() {
        #expect(PCodeComposerCompletion.commandRows(catalog(), text: "hello", caret: 5).isEmpty)
        #expect(PCodeComposerCompletion.commandRows(catalog(), text: "/com x", caret: 4).isEmpty)
        #expect(PCodeComposerCompletion.commandRows(catalog(), text: "/com", caret: 99).isEmpty)
    }

    @Test func menuSelectionFollowsTheTerminal() {
        let menu = PCodeComposerCompletion(catalog: catalog(), files: nil)
        menu.update(text: "/", caret: 1)
        #expect(menu.isOpen && menu.selected == nil)
        // Nothing selected: Enter sends, Tab takes the first row.
        #expect(menu.chosen == nil)
        #expect(menu.offered == menu.rows.first)
        menu.move(1)
        #expect(menu.selected == 0 && menu.chosen == menu.rows[0])
        menu.move(-1)
        #expect(menu.selected == menu.rows.count - 1)  // Wraps.
        // Escape closes it until the text changes.
        menu.dismiss()
        #expect(!menu.isOpen)
        menu.update(text: "/", caret: 1)
        #expect(!menu.isOpen)
        menu.update(text: "/c", caret: 2)
        #expect(menu.isOpen && menu.selected == nil)
    }

    @Test func fileTokenNeedsAnIndex() {
        let menu = PCodeComposerCompletion(catalog: catalog(), files: nil)
        menu.update(text: "look at @ui", caret: 11)
        #expect(!menu.isOpen)
    }

    @Test func filePickerListDecodesAsAPickList() throws {
        let list = PCodeComposerSession.filePickerList(["src/a.py", "docs/b c.md", "README.md"], loaded: true)
        let data = try JSONSerialization.data(withJSONObject: list)
        let payload = try JSONDecoder().decode(PCodePicker.Payload.self, from: data)
        #expect(payload.items.map(\.id) == ["src/a.py", "docs/b c.md", "README.md"])
        #expect(payload.items[2].search == ["readme.md", "readme.md"])
        #expect(payload.items[0].search == ["a.py", "src/a.py"])
        #expect(payload.match == "fuzzy")
        #expect(PCodeFileIndex.Item(path: "docs/b c.md").insertion == "\"./docs/b c.md\" ")
    }

    @Test func localPickerHandsThePickBack() {
        let picker = PCodePicker()
        var picked: String?
        var closed = false
        picker.onClosed = { closed = true }
        picker.showLocal(PCodeComposerSession.filePickerList(["src/a.py", "src/b.py"], loaded: true)) { id, _ in
            picked = id
        }
        #expect(picker.rows.count == 2)
        picker.query = "B.PY"  // Case does not matter, as in pcode's pickers.
        picker.accept()
        #expect(picked == "src/b.py" && closed)
    }

    @Test func menuKeysOnlyWhileOpen() {
        let open = { (code: UInt16, chars: String?, mods: NSEvent.ModifierFlags) in
            PCodeComposerTextView.action(keyCode: code, characters: chars, modifiers: mods,
                                         onFirstLine: true, onLastLine: true, menuOpen: true)
        }
        #expect(open(48, "\t", []) == .menuAccept)
        #expect(open(37, "l", .control) == .menuAccept)
        #expect(open(126, nil, []) == .menuPrevious)
        #expect(open(35, "p", .control) == .menuPrevious)
        #expect(open(125, nil, []) == .menuNext)
        #expect(open(45, "n", .control) == .menuNext)
        #expect(open(53, "\u{1b}", []) == .menuClose)
        #expect(open(36, "\r", []) == .submit)  // The session decides: take a selection or send.
        #expect(action(126, nil, first: true) == .historyPrevious)  // Closed: history again.
        #expect(action(48, "\t") == nil)
        #expect(action(0, "a", [.command, .shift]) == .openFilePicker)
    }

    // MARK: History and drafts

    @Test func historyKeepsTheTextInProgressAndSavesDrafts() throws {
        let dir = FileManager.default.temporaryDirectory.appendingPathComponent("composer-drafts-\(UUID().uuidString)")
        defer { try? FileManager.default.removeItem(at: dir) }
        let history = PCodePromptHistory(entries: ["first", "second"], drafts: PCodeDraftStore(directory: dir))
        #expect(history.up(current: "typing") == "second")
        #expect(history.up(current: "second") == "first")
        #expect(history.down(current: "first") == "second")
        #expect(history.down(current: "second") == "typing")
        history.saveDrafts(current: "typing")
        let files = try FileManager.default.contentsOfDirectory(atPath: dir.path).filter { $0.hasSuffix(".txt") }
        #expect(files.count == 1)
        #expect(try String(contentsOf: dir.appendingPathComponent(files[0]), encoding: .utf8) == "typing")
        history.send("typing")
        #expect(try FileManager.default.contentsOfDirectory(atPath: dir.path).filter { $0.hasSuffix(".txt") }.isEmpty)
    }

    @Test func echoedPromptsReplaceLocalRows() {
        #expect(PCodeLiveTranscriptController.echoedPrompt(.turnStarted(prompt: "hi", isSide: false, runID: "")) == "hi")
        #expect(PCodeLiveTranscriptController.echoedPrompt(.turnStarted(prompt: "q", isSide: true, runID: "")) == nil)
        #expect(PCodeLiveTranscriptController.echoedPrompt(.user(text: "!ls")) == "!ls")
        #expect(PCodeLiveTranscriptController.echoedPrompt(.note("x")) == nil)
    }

    // MARK: Keys (`pcode.prompt_keys`)

    private func action(_ keyCode: UInt16, _ chars: String?, _ mods: NSEvent.ModifierFlags = [],
                        first: Bool = false, last: Bool = false) -> PCodeComposerTextView.Action? {
        PCodeComposerTextView.action(keyCode: keyCode, characters: chars, modifiers: mods, onFirstLine: first, onLastLine: last)
    }

    @Test func keysMatchTheTerminalEditor() {
        #expect(action(36, "\r") == .submit)
        #expect(action(76, "\r") == .submit)
        #expect(action(36, "\r", .shift) == .newline)
        #expect(action(36, "\r", .option) == .newline)
        #expect(action(36, "\r", .control) == .interruptSubmit)
        #expect(action(38, "j", .control) == .newline)
        #expect(action(8, "c", .control) == .clearOrCancel)
        #expect(action(1, "s", .control) == .cycleSendMode)
        #expect(action(8, "c", .command) == nil)  // Cmd+C copies.
        #expect(action(0, "a", .control) == nil)  // Emacs keys stay NSTextView's.
        #expect(action(126, nil, first: true) == .historyPrevious)
        #expect(action(126, nil, first: false) == nil)
        #expect(action(125, nil, last: true) == .historyNext)
    }

    // MARK: Review fixes

    @Test func echoesAnswerTheOldestSentStandInOnly() {
        let prompts = [
            PCodeLocalPrompt(id: "a", text: "again", sent: true),
            PCodeLocalPrompt(id: "b", text: "next", sent: true),
        ]
        #expect(PCodeLiveTranscriptController.echoAnswers("again", prompts) == 0)
        #expect(PCodeLiveTranscriptController.echoAnswers("next", prompts) == nil)  // Not by text alone.
        let unsent = [PCodeLocalPrompt(id: "a", text: "again", sent: false)]
        #expect(PCodeLiveTranscriptController.echoAnswers("again", unsent) == nil)
    }

    @Test func historyOnlyAccountsForTrailingSentStandIns() {
        let history: [PCodeHostEvent] = [
            .turnStarted(prompt: "again", isSide: false, runID: ""),
            .message(markdown: "ok"),
        ]
        // An older prompt with the same text does not take an unsent stand-in...
        #expect(PCodeLiveTranscriptController.promptsAlreadyRun(
            history: history, prompts: [PCodeLocalPrompt(id: "a", text: "again", sent: false)]) == 0)
        // ...but a sent one the host ran before this observer connected is accounted for.
        #expect(PCodeLiveTranscriptController.promptsAlreadyRun(
            history: history, prompts: [PCodeLocalPrompt(id: "a", text: "again", sent: true),
                                        PCodeLocalPrompt(id: "b", text: "more", sent: true)]) == 1)
        #expect(PCodeLiveTranscriptController.promptsAlreadyRun(
            history: history + [.user(text: "more")],
            prompts: [PCodeLocalPrompt(id: "a", text: "again", sent: true),
                      PCodeLocalPrompt(id: "b", text: "more", sent: true)]) == 2)
    }

    @Test func spawnAnswerIsACompleteLine() {
        #expect(!PCodeComposerSession.isAnswer(Data("Last login\n".utf8)))
        #expect(!PCodeComposerSession.isAnswer(Data(#"{"id": "ab12cd34""#.utf8)))  // Half a line.
        #expect(PCodeComposerSession.isAnswer(Data("motd\n{\"id\": \"ab12cd34\", \"socket\": \"/s\"}\n".utf8)))
        #expect(PCodeComposerSession.isAnswer(Data(#"{"error": "no"}"#.utf8 + [0x0A])))
        #expect(PCodeComposerSession.isAnswer(Data(#"{"trust": {}}"#.utf8 + [0x0A])))
    }

    @Test func spawnedChildRunsInItsOwnGroupAndAnswers() throws {
        let err = FileManager.default.temporaryDirectory.appendingPathComponent("spawn-test-\(UUID().uuidString)")
        defer { try? FileManager.default.removeItem(at: err) }
        let child = try #require(SpawnedChild(
            argv: ["/bin/sh", "-c", "echo '{\"id\": \"x\"}'; echo oops >&2; (sleep 30 &)"],
            environment: ["PATH": "/usr/bin:/bin"], directory: "/tmp", stderrPath: err.path))
        defer { kill(-child.pid, SIGKILL); child.closePipe() }
        #expect(getpgid(child.pid) == child.pid)
        var bytes = Data()
        var chunk = [UInt8](repeating: 0, count: 1024)
        let deadline = Date().addingTimeInterval(5)
        while !PCodeComposerSession.isAnswer(bytes), Date() < deadline {
            let n = read(child.stdout, &chunk, chunk.count)
            if n <= 0 { break }
            bytes.append(contentsOf: chunk[0..<n])
        }
        #expect(PCodeComposerSession.isAnswer(bytes))
    }

    @Test func savedHostPanesReopenOnlyLiveUnopenedHosts() {
        let saved = [
            PCodeWorkspace.SavedHostPane(hostID: "aaaa0001", directory: "/w", projectPath: "/w"),
            PCodeWorkspace.SavedHostPane(hostID: "aaaa0002", directory: "/w", projectPath: "/w"),
            PCodeWorkspace.SavedHostPane(hostID: "aaaa0003", directory: "/w", projectPath: "/w"),
        ]
        let live: [(id: String, pid: Int32, sessionID: String)] = [
            ("aaaa0001", 10, ""), ("aaaa0003", 12, "s3"),
        ]
        let reopen = PCodeWorkspace.reopenableHostPanes(saved, live: live, openSessions: [], nativeEnabled: true)
        #expect(reopen.map(\.pane.hostID) == ["aaaa0001", "aaaa0003"])
        #expect(reopen.map(\.sessionID) == [nil, "s3"])
        #expect(PCodeWorkspace.reopenableHostPanes(saved, live: live, openSessions: ["s3"], nativeEnabled: true)
            .map(\.pane.hostID) == ["aaaa0001"])
        #expect(PCodeWorkspace.reopenableHostPanes(saved, live: live, openSessions: [], nativeEnabled: false).isEmpty)
    }

    @Test func unencodableReplyIsAnError() {
        #expect(PCodeHostRPCClient.encode(["type": "reply", "id": 1, "result": Date()]) == nil)
    }
}
