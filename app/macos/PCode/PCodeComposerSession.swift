import AppKit
import Combine
import Foundation

/// The `native_composer` preference (`pcode config set native_composer on`), read when a
/// session pane opens. `PCODE_NATIVE_COMPOSER=1|0` overrides it.
enum PCodeNativeComposer {
    static var enabled: Bool {
        enabled(environment: ProcessInfo.processInfo.environment, preferences: PCodePreferences.load())
    }

    static func enabled(environment: [String: String], preferences: [String: Any]) -> Bool {
        if let env = environment["PCODE_NATIVE_COMPOSER"], !env.isEmpty {
            return !["0", "off", "false", "no"].contains(env.lowercased())
        }
        return (preferences["native_composer"] as? String) == "on"
    }
}

/// pcode's user preferences file, found as `pcode.preferences.config_dir` finds it.
enum PCodePreferences {
    static func url(environment: [String: String] = ProcessInfo.processInfo.environment) -> URL {
        if let dir = environment["PCODE_CONFIG_DIR"], !dir.isEmpty {
            return URL(fileURLWithPath: (dir as NSString).expandingTildeInPath).appendingPathComponent("preferences.json")
        }
        let base = environment["XDG_CONFIG_HOME"].flatMap { $0.isEmpty ? nil : ($0 as NSString).expandingTildeInPath }
            ?? (NSHomeDirectory() as NSString).appendingPathComponent(".config")
        return URL(fileURLWithPath: base).appendingPathComponent("pcode/preferences.json")
    }

    static func load() -> [String: Any] {
        guard let data = try? Data(contentsOf: url()),
              let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else { return [:] }
        return obj
    }
}

/// One native-composer pane's connection to its session host, and everything the composer
/// shows of it.
///
/// The host is started headless (`pcode --host --spawn-only`, `pcode.app_spawn`) from the
/// same login-shell environment a pane's terminal would get, then this attaches as a full
/// client (`PCodeHostRPCClient`). Typing never waits for any of that: what is sent before
/// the host attaches waits in `outbox`, its prompts already shown in the transcript
/// (`PCodeLiveTranscriptController.showLocalPrompt`), and goes out in order on attach.
///
/// The transcript keeps its own read-only observer connection (`transcript`), as for
/// terminal panes: it already handles history, catch-up and reconnects.
final class PCodeComposerSession: ObservableObject {
    enum Connection: Equatable {
        /// Spawning the host, or waiting for its socket.
        case starting
        /// The repository ships launch-time code nobody has trusted yet: ask first.
        case needsTrust(key: String, code: [String])
        case attached
        /// The host went away; waiting for it (or its `/restart` successor) to come back.
        case reconnecting(String)
        case failed(String)
    }

    /// Sent before the host attached, delivered in order once it has.
    enum Outgoing: Equatable {
        case submit(text: String, mode: String)
        case command(String)
    }

    @Published private(set) var connection: Connection = .starting {
        didSet { if connection != oldValue { updatePhaseClock() } }
    }
    @Published private(set) var state = PCodeHostClientState()
    @Published private(set) var outbox: [Outgoing] = []
    /// The saved send mode (`send_mode`) and a one-send override from the toggle.
    @Published private(set) var sendMode: String
    @Published private(set) var sendModeOnce: String?
    /// The host this pane talks to, once spawned or found.
    @Published private(set) var hostID: String?
    @Published private(set) var toast: String?
    /// When the busy phase the strip shows began, for its elapsed clock.
    @Published private(set) var phaseStartedAt: Date?

    /// The latest raw `session_state` (commands, arguments, models, skills...).
    private(set) var sessionState: [String: Any] = [:]
    /// Called with each `session_state`: where the command catalog snapshot hooks in
    /// (`PCodeCommandCatalog.Snapshot(sessionState:)`, `PCodeCatalogStore.save`).
    var onSessionState: (([String: Any]) -> Void)?
    /// The session or host id changed: (session id, or nil before turn 1; host id).
    var onSessionReported: ((String?, String?) -> Void)?
    /// `/quit`, or the host ended the session (`session_ended`): close the pane.
    var onCloseRequested: (() -> Void)?

    /// The pane transcript's controller, owned here so prompts sent before attach show in it.
    let transcript = PCodeLiveTranscriptController()
    /// The composer's text view and its scroll view, made on first mount and kept here so
    /// the draft survives pane switches.
    lazy var editor: PCodeComposerEditorView = {
        let editor = PCodeComposerEditorView()
        editorMade = true
        editor.textView.onAction = { [weak self] action in self?.perform(action) ?? false }
        editor.textView.onWindowChange = { [weak self] in self?.focusIfWanted() }
        wireInput(editor.textView)
        return editor
    }()
    /// The text view, once a pane has mounted it (nil before, without making one).
    var textView: PCodeComposerTextView? { editorMade ? editor.textView : nil }
    private var editorMade = false
    /// Focus was asked for before the text view was in a window.
    private var wantsFocus = false

    let directory: String

    // Input (`PCodeComposerSession+Input.swift`)

    /// The `/` and `@` menu. Its catalog starts from the saved snapshot so it works
    /// on the first frame, and the host's live `session_state` replaces it.
    let completion: PCodeComposerCompletion
    let catalogStore: PCodeCatalogStore
    /// Up/Down recall and draft files, shared with the terminal editor's `drafts.py`.
    let history: PCodePromptHistory
    /// The `@` file picker while it is open.
    @Published var filePicker: PCodePicker?
    var draftTimer: Timer?
    /// The text the last draft save saw, so an idle composer writes nothing.
    var lastDraftText = ""
    /// Set while the text is replaced in code (history), which must not open the menu.
    var settingText = false

    private(set) var resumeSessionID: String?
    /// The id the first host is started with (`PCODE_HOST_ID`); relaunches pick new ones.
    let launchHostID: String

    private var client: PCodeHostRPCClient?
    private var socketPath: String?
    private var hostPID: Int32?
    private var asides: [String: [String: Any]] = [:]
    private var successor: String?
    private var connectDeadline = Date.distantFuture
    private var spawnAttempts = 0
    private var stopped = false
    private var toastWork: DispatchWorkItem?
    private var lastStatus: PCodeComposerStatus?

    static let maxRelaunchAttempts = PCodeWorkspace.maxHostRelaunchAttempts
    static let sendModes = ["steering", "queue", "interrupt"]

    init(
        directory: String, resumeSessionID: String?, launchHostID: String,
        catalogStore: PCodeCatalogStore = PCodeCatalogStore(),
        history: PCodePromptHistory? = nil
    ) {
        self.directory = directory
        self.resumeSessionID = resumeSessionID
        self.launchHostID = launchHostID
        self.catalogStore = catalogStore
        self.history = history ?? PCodePromptHistory(drafts: PCodeDraftStore())
        completion = PCodeComposerCompletion(
            catalog: catalogStore.catalog(workspace: directory),
            files: PCodeFileIndex.shared(for: directory))
        let saved = PCodePreferences.load()["send_mode"] as? String
        sendMode = saved.flatMap { Self.sendModes.contains($0) ? $0 : nil } ?? "steering"
    }

    // MARK: Lifecycle

    func start() {
        guard !stopped else { return }
        spawn(trust: nil, hostID: launchHostID)
    }

    /// The pane closed. The workspace stops the host (`PCodeWorkspace.stopHost`).
    func stop() {
        saveDrafts()
        draftTimer?.invalidate()
        draftTimer = nil
        filePicker?.cancel()
        stopped = true
        client?.close()
        client = nil
    }

    func answerTrust(_ trusted: Bool) {
        guard case .needsTrust = connection else { return }
        spawn(trust: trusted ? "trust" : "skip", hostID: hostID ?? launchHostID)
    }

    /// The transcript's host never showed up, or the user pressed Reopen.
    func requestRelaunch(isManual: Bool) -> PCodeLiveTranscriptController.RelaunchOutcome {
        switch connection {
        case .starting, .needsTrust, .attached:
            if !isManual { return .keepWaiting }
        case .reconnecting:
            if !isManual { return .keepWaiting }
        case .failed:
            break
        }
        relaunch(manual: isManual)
        return .keepWaiting
    }

    private func relaunch(manual: Bool) {
        if !manual, spawnAttempts > Self.maxRelaunchAttempts {
            return
        }
        client?.close()
        client = nil
        if !state.sessionID.isEmpty { resumeSessionID = state.sessionID }
        spawn(trust: nil, hostID: PCodeWorkspace.newHostID())
    }

    // MARK: Spawning

    enum SpawnResult: Equatable {
        case host(id: String, socket: String, log: String, pid: Int32, existing: Bool)
        case trust(key: String, code: [String])
        case error(String)
    }

    /// What `pcode --host --spawn-only` printed: its last JSON line (a login shell may
    /// print other things first).
    static func parseSpawnOutput(_ output: String, stderr: String = "", status: Int32 = 0) -> SpawnResult {
        let objects = output.split(whereSeparator: \.isNewline).reversed().lazy.compactMap { line in
            (try? JSONSerialization.jsonObject(with: Data(line.utf8))) as? [String: Any]
        }
        guard let obj = objects.first else {
            let detail = stderr.trimmingCharacters(in: .whitespacesAndNewlines)
            if detail.isEmpty { return .error("pcode exited (\(status)) without starting a session host.") }
            return .error(String(detail.suffix(600)))
        }
        if let error = obj["error"] as? String { return .error(error) }
        if let trust = obj["trust"] as? [String: Any] {
            return .trust(key: (trust["key"] as? String) ?? "", code: (trust["code"] as? [String]) ?? [])
        }
        guard let id = obj["id"] as? String, let socket = obj["socket"] as? String else {
            return .error("pcode did not say where its session host is.")
        }
        return .host(
            id: id, socket: socket, log: (obj["log"] as? String) ?? "",
            pid: Int32((obj["pid"] as? Int) ?? 0), existing: (obj["existing"] as? Bool) ?? false)
    }

    /// The `pcode` arguments for spawning this pane's host.
    static func spawnArguments(resume: String?, trust: String?) -> [String] {
        ["--host", trust.map { "--spawn-only=\($0)" } ?? "--spawn-only"] + (resume.map { ["-c", $0] } ?? [])
    }

    private func spawn(trust: String?, hostID: String) {
        connection = .starting
        spawnAttempts += 1
        // Reported before the host exists, so closing the pane now still stops it.
        self.hostID = hostID
        onSessionReported?(resumeSessionID, hostID)
        let args = Self.spawnArguments(resume: resumeSessionID, trust: trust)
        let directory = self.directory
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            // Panes made right after launch take the direct command, not a login shell each.
            PCodeWorkspaceMode.awaitLoginShellCache(timeout: 3)
            let launch = PCodeWorkspaceMode.launchConfig(args, directory: directory)
            let result = Self.runSpawn(command: launch.command, env: launch.env, directory: directory, hostID: hostID)
            DispatchQueue.main.async { self?.spawned(result) }
        }
    }

    private static func runSpawn(command: String, env: [String: String], directory: String, hostID: String) -> SpawnResult {
        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/bin/sh")
        process.arguments = ["-c", command]
        process.currentDirectoryURL = URL(fileURLWithPath: directory)
        var environment = ProcessInfo.processInfo.environment.filter { key, _ in
            !key.hasPrefix("GHOSTTY_") && !["PCODE_EDITOR_ONLY", "PCODE_DOCK", "PCODE_NATIVE_TRANSCRIPT", "TERM_PROGRAM"].contains(key)
        }
        environment.merge(env) { _, new in new }
        environment["PCODE_HOST_ID"] = hostID
        environment["PCODE_HUSH_LOGIN"] = "1"
        process.environment = environment
        let stdout = Pipe()
        let stderrURL = FileManager.default.temporaryDirectory.appendingPathComponent("pcode-spawn-\(hostID).err")
        FileManager.default.createFile(atPath: stderrURL.path, contents: nil)
        defer { try? FileManager.default.removeItem(at: stderrURL) }
        process.standardOutput = stdout
        process.standardError = (try? FileHandle(forWritingTo: stderrURL)) ?? FileHandle.nullDevice
        process.standardInput = FileHandle.nullDevice
        do {
            try process.run()
        } catch {
            return .error("Could not run pcode: \(error.localizedDescription)")
        }
        let timeout = DispatchWorkItem { if process.isRunning { process.terminate() } }
        DispatchQueue.global(qos: .utility).asyncAfter(deadline: .now() + 60, execute: timeout)
        let data = stdout.fileHandleForReading.readDataToEndOfFile()
        process.waitUntilExit()
        timeout.cancel()
        let errText = (try? String(contentsOf: stderrURL, encoding: .utf8)) ?? ""
        return parseSpawnOutput(String(decoding: data, as: UTF8.self), stderr: errText, status: process.terminationStatus)
    }

    private func spawned(_ result: SpawnResult) {
        guard !stopped else { return }
        switch result {
        case .error(let message):
            connection = .failed(message)
        case .trust(let key, let code):
            connection = .needsTrust(key: key, code: code)
        case .host(let id, let socket, _, let pid, _):
            hostID = id
            socketPath = socket
            hostPID = pid > 0 ? pid : nil
            onSessionReported?(resumeSessionID, id)
            // Worktree setup can make a host slow to open its socket.
            connectDeadline = Date().addingTimeInterval(120)
            connect()
        }
    }

    // MARK: Connecting

    private func connect() {
        guard !stopped, let socketPath else { return }
        guard FileManager.default.fileExists(atPath: socketPath) else { return retryConnect() }
        let client = PCodeHostRPCClient()
        self.client = client
        client.onAttached = { [weak self, weak client] welcome in
            guard let self, let client, self.client === client else { return }
            self.attached(welcome)
        }
        client.onNotify = { [weak self, weak client] method, args, kwargs in
            guard let self, let client, self.client === client else { return }
            self.receive(method, args, kwargs)
        }
        client.onRequest = { [weak self, weak client] id, method, args, kwargs in
            guard let self, let client, self.client === client else { return }
            PCodeHostRequests.handle(id: id, method: method, args: args, kwargs: kwargs, session: self, client: client)
        }
        client.onClosed = { [weak self, weak client] error in
            guard let self, let client, self.client === client else { return }
            self.client = nil
            self.connectionLost(error)
        }
        client.connect(socketPath: socketPath)
    }

    private func retryConnect() {
        let alive = hostPID.map { kill($0, 0) == 0 } ?? true
        guard alive, Date() < connectDeadline else {
            if case .reconnecting = connection {
                connection = .failed("The session host stopped.")
            } else {
                let tail = PCodeSessionInfo.hostLogTail(hostID: hostID)
                connection = .failed(tail ?? "The session host didn't start.")
            }
            return
        }
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.1) { [weak self] in self?.connect() }
    }

    private func connectionLost(_ error: String?) {
        guard !stopped else { return }
        guard connection == .attached else {
            // Still booting (or refused us), or not back yet: keep trying until the deadline.
            return retryConnect()
        }
        var next = state
        next.busy = false
        next.promptState = ""
        setState(next)
        if let successor {
            // `/restart`: the host handed the session to a successor; follow it.
            self.successor = nil
            hostID = successor
            socketPath = ((socketPath ?? "") as NSString).deletingLastPathComponent + "/\(successor).sock"
            hostPID = nil
            onSessionReported?(state.sessionID.isEmpty ? nil : state.sessionID, successor)
        }
        connection = .reconnecting(error ?? "The session host stopped.")
        connectDeadline = Date().addingTimeInterval(10)
        updatePhaseClock()
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.3) { [weak self] in self?.connect() }
    }

    private func attached(_ welcome: [String: Any]) {
        connection = .attached
        spawnAttempts = 0
        if let session = welcome["session"] as? [String: Any] { applySessionState(session) }
        var next = state
        if let activity = welcome["activity"] as? [String: Any] { next.applyActivity(activity) }
        next.plan = PCodePlanItem.decode(welcome["plan"])
        asides = [:]
        for aside in (welcome["asides"] as? [[String: Any]]) ?? [] {
            if let id = aside["id"] as? String { asides[id] = aside }
        }
        next.applyAsides(asides)
        setState(next)
        // The buffered calls: only the session-state ones matter here; the transcript
        // draws the rest from its own observer welcome.
        for call in (welcome["calls"] as? [[Any]]) ?? [] {
            guard call.count >= 2, let method = call[0] as? String else { continue }
            if ["state", "session_state", "plan_changed", "aside_changed"].contains(method) {
                receive(method, (call[1] as? [Any]) ?? [], (call.count > 2 ? call[2] as? [String: Any] : nil) ?? [:])
            }
        }
        flushOutbox()
    }

    private func flushOutbox() {
        guard let client, connection == .attached else { return }
        let sending = outbox
        outbox.removeAll()
        for item in sending {
            switch item {
            case .submit(let text, let mode): client.notify("submit", [text, mode])
            case .command(let text): client.notify("command", [text, NSNull()])
            }
        }
    }

    // MARK: What the host sends

    func receive(_ method: String, _ args: [Any], _ kwargs: [String: Any]) {
        switch method {
        case "state":
            guard let changes = args.first as? [String: Any] else { return }
            var next = state
            next.applyActivity(changes)
            setState(next)
            if let notice = changes["notice"] as? String, !notice.isEmpty { showToast(notice) }
        case "session_state":
            guard let session = args.first as? [String: Any] else { return }
            applySessionState(session)
        case "plan_changed":
            var next = state
            next.plan = PCodePlanItem.decode(args.first)
            setState(next)
        case "aside_changed", "aside_answered":
            guard let aside = args.first as? [String: Any], let id = aside["id"] as? String else { return }
            asides[id] = aside
            var next = state
            next.applyAsides(asides)
            setState(next)
        case "conversation_reset":
            var next = state
            next.sessionID = ""
            next.plan = []
            setState(next)
            onSessionReported?(nil, hostID)
        case "host_closed":
            successor = (args.first as? String).flatMap { $0.isEmpty ? nil : $0 }
        case "session_ended":
            onCloseRequested?()
        default:
            break  // Transcript calls: the observer connection draws them.
        }
    }

    private func applySessionState(_ session: [String: Any]) {
        sessionState = session
        let before = state.sessionID
        var next = state
        next.applySession(session)
        setState(next)
        catalogChanged(session)
        onSessionState?(session)
        if next.sessionID != before, !next.sessionID.isEmpty {
            onSessionReported?(next.sessionID, hostID)
        }
    }

    private func setState(_ next: PCodeHostClientState) {
        guard next != state else { return }
        state = next
        updatePhaseClock()
    }

    // MARK: What the user does

    /// Enter: send `raw` the way the terminal editor would. Returns whether it was taken
    /// (the composer clears itself then).
    @discardableResult
    func submit(_ raw: String, interrupt: Bool = false) -> Bool {
        let text = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty else { return false }
        if text.hasPrefix("/") {
            switch completion.catalog.handling(for: text) {
            case .session: send(.command(text))
            case .terminal, .native: PCodeHostRequests.runTerminalCommand(text, session: self)
            }
            return true
        }
        let mode: String
        if Self.isShellCommand(text) {
            mode = "shell"
        } else {
            mode = interrupt ? "interrupt" : nextSendMode
            if sendModeOnce != nil { sendModeOnce = nil }
        }
        send(.submit(text: text, mode: mode))
        return true
    }

    /// `pcode.shell_mode.shell_command`: `!cmd`, but not `!` alone or `!!`.
    static func isShellCommand(_ text: String) -> Bool {
        let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard trimmed.hasPrefix("!") else { return false }
        let command = trimmed.dropFirst().trimmingCharacters(in: .whitespaces)
        return !command.isEmpty && !command.hasPrefix("!")
    }

    private func send(_ item: Outgoing) {
        if let client, connection == .attached {
            switch item {
            case .submit(let text, let mode):
                client.notify("submit", [text, mode])
                // As `RemoteController.submit`: Ctrl+C right after Enter cancels it.
                var next = state
                next.busy = true
                setState(next)
            case .command(let text):
                client.notify("command", [text, NSNull()])
            }
            return
        }
        outbox.append(item)
        switch item {
        case .submit(let text, _): transcript.showLocalPrompt(text)
        case .command(let text):
            let name = text.split(separator: " ").first.map(String.init) ?? text
            showToast("\(name) runs once the session has started.")
        }
        updatePhaseClock()
    }

    /// Whether Ctrl+C on an empty composer has something to interrupt.
    var isBusy: Bool { state.busy || !outbox.isEmpty }

    /// Ctrl+C with nothing typed: stop the turn and clear the queue.
    func cancel() {
        if let client, connection == .attached {
            client.notify("cancel")
            return
        }
        guard !outbox.isEmpty else { return }
        let count = outbox.count
        outbox.removeAll()
        transcript.dropLocalPrompts()
        showToast("Cleared \(count) queued message\(count == 1 ? "" : "s").")
        updatePhaseClock()
    }

    /// The mode the next prompt goes with: a toggle pick, else the saved default.
    var nextSendMode: String { sendModeOnce ?? sendMode }

    /// Ctrl+S or a click on the strip's pill: the next send only, as `cycle_send_mode`.
    func cycleSendMode() {
        let index = Self.sendModes.firstIndex(of: nextSendMode) ?? 0
        let mode = Self.sendModes[(index + 1) % Self.sendModes.count]
        sendModeOnce = mode == sendMode ? nil : mode
        updatePhaseClock()
    }

    // MARK: Status strip

    var composerStatus: PCodeComposerStatus {
        let base = state.composerStatus(sendMode: nextSendMode, sendModeOnce: sendModeOnce != nil)
        guard connection != .attached else { return base }
        let queued = outbox.count
        return PCodeComposerStatus(
            busy: true,
            phase: Self.phaseLabel(connection),
            sendMode: base.sendMode,
            sendModeOnce: base.sendModeOnce,
            starting: true,
            steeringPending: 0,
            queued: queued,
            jobs: base.jobs,
            btwRunning: base.btwRunning,
            btwReady: base.btwReady,
            cacheNote: "")
    }

    static func phaseLabel(_ connection: Connection) -> String {
        switch connection {
        case .starting: return "Starting"
        case .needsTrust: return "Waiting for trust"
        case .attached: return ""
        case .reconnecting: return "Reconnecting"
        case .failed: return "Session host failed"
        }
    }

    private func updatePhaseClock() {
        let status = composerStatus
        let wasActive = lastStatus.map { $0.busy || $0.starting } ?? false
        if status.busy || status.starting {
            if !wasActive || lastStatus?.phase != status.phase { phaseStartedAt = Date() }
        } else if phaseStartedAt != nil {
            phaseStartedAt = nil
        }
        lastStatus = status
    }

    func showToast(_ text: String, seconds: TimeInterval = 3) {
        toastWork?.cancel()
        toast = text
        let work = DispatchWorkItem { [weak self] in self?.toast = nil }
        toastWork = work
        DispatchQueue.main.asyncAfter(deadline: .now() + seconds, execute: work)
    }

    /// Give the composer the keyboard, now or as soon as it is mounted.
    func focus() {
        wantsFocus = true
        focusIfWanted()
    }

    private func focusIfWanted() {
        guard wantsFocus, editorMade, let window = editor.textView.window else { return }
        wantsFocus = false
        window.makeFirstResponder(editor.textView)
    }

    // MARK: Keys

    private func perform(_ action: PCodeComposerTextView.Action) -> Bool {
        let textView = editor.textView
        switch action {
        case .submit:
            // First Enter takes a selected completion; the next one sends.
            if let row = completion.chosen {
                take(row)
                return true
            }
            let text = textView.string
            if submit(text) {
                history.send(text)
                clearDraft()
            }
            return true
        case .interruptSubmit:
            let text = textView.string
            if !text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty, submit(text, interrupt: true) {
                history.send(text)
                clearDraft()
            }
            return true
        case .clearOrCancel:
            // As the terminal editor: never discard a draft and interrupt in one keypress.
            if !textView.string.isEmpty {
                // `drafts.edited`: changed from the entry it was recalled from, so ↑ finds it.
                let unsent = history.edited(current: textView.string)[history.index] != nil
                history.clear(current: textView.string)
                clearDraft()
                let discarded = unsent ? "Input discarded. ↑ brings it back." : "Input discarded."
                showToast(isBusy ? "\(discarded) Ctrl+C again interrupts." : discarded)
            } else if isBusy {
                cancel()
            }
            return true
        case .cycleSendMode:
            cycleSendMode()
            return true
        case .newline:
            return false
        case .historyPrevious, .historyNext:
            return recall(action == .historyPrevious ? -1 : 1)
        case .menuPrevious, .menuNext:
            completion.move(action == .menuPrevious ? -1 : 1)
            return true
        case .menuAccept:
            if let row = completion.offered { take(row) }
            return true
        case .menuClose:
            completion.dismiss()
            return true
        case .openFilePicker:
            openFilePicker()
            return true
        }
    }

    func clearDraft() {
        let textView = editor.textView
        textView.string = ""
        textView.didChangeText()
        editor.reportHeight()
    }

    /// Put `text` into the draft at the caret (the inspector's "insert into composer").
    func insert(_ text: String) {
        let textView = editor.textView
        textView.insertText(text, replacementRange: textView.selectedRange())
        focus()
    }
}
