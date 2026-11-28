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
        #expect(PCodeHostRequests.runsInTerminal("/help"))
        #expect(PCodeHostRequests.runsInTerminal("/new write the docs"))
        #expect(!PCodeHostRequests.runsInTerminal("/new"))
        #expect(!PCodeHostRequests.runsInTerminal("/compact"))
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
}
