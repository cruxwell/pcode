import AppKit
import Foundation
import SwiftUI

/// What the native composer does with a terminal command (`TERMINAL_COMMANDS` in
/// `src/pcode/controller.py`) and with the host's popup requests (`choose_model`,
/// `browse_jobs`, `read_asides`), which pcode's terminal editor used to draw.
///
/// `route(for:)` is the pure decision; `run(_:context:)` carries it out through a
/// `Context`, which `PCodeHostRequests` builds (`Context.live(session:client:)`) and
/// tests replace with closures. The audit behind each route is the "Native command
/// routes" table in dev/native-app-plan.md (3b).
enum PCodeNativeCommands {
    /// Workspace and pane actions the router asks for.
    enum Action: Equatable {
        /// Close the pane (`/quit`, `/exit`, `/stop`): closing a native pane stops its host.
        case closePane
        /// `/paste-image`: the clipboard's image, even beside text.
        case pasteImage
        /// Open the Session Inspector on a tab.
        case inspector(PCodeInspectorTab)
        /// Pcode.app's Settings window (`/config`).
        case settings
        /// A searchable list of saved sessions to open (`/resume`).
        case resume
        /// A running session host: by id (or prefix), or nil for a list of them (`/attach`).
        case attach(String?)
        /// A new session in this pane's folder, sent `prompt` (`/new PROMPT`).
        case newSession(prompt: String)
        /// Restart this pane's host on the pcode installed now (`/restart`).
        case restartHost
        /// The `/btw` drawer on the latest side thread (`read_asides`).
        case btw
    }

    /// Where a terminal command goes.
    enum Route: Equatable {
        case action(Action)
        /// `/help`, `/commands`: the grouped command list.
        case help
        /// `/status`: the Session info sheet.
        case sessionInfo
        /// `/links`: URLs from the conversation, in a picker that opens one.
        case links
        /// `/copy`: a response, or a code block from one, to copy.
        case copy
        /// `/config list|diff|get|set|unset ...`: `pcode config` run for its output.
        case config([String])
        /// `/show-thinking [mode]`: nil cycles.
        case thinking(String?)
        /// `/group-tools [on|off]`: nil toggles.
        case groupTools(Bool?)
        /// No native equivalent: shown as a toast with the reason.
        case unsupported(String)
        /// A malformed argument: shown as a toast.
        case usage(String)
        /// Nothing to do (`/redraw`: the native transcript lays itself out).
        case ignore
    }

    /// One conversation message, for `/links` and `/copy`.
    struct Message: Equatable {
        enum Role: String { case user, assistant }
        let role: Role
        let text: String
    }

    /// A sheet or picker to put over the window.
    enum Presentation {
        /// A SwiftUI view; it is handed a `dismiss` closure to close itself.
        case sheet(title: String, content: (_ dismiss: @escaping () -> Void) -> AnyView)
        /// A picker already given its list (`PCodePicker.showLocal`).
        case picker(title: String, PCodePicker)
    }

    /// What the router needs from the pane, its host connection and the workspace.
    struct Context {
        /// The session's folder.
        var workspace: String
        var hostID: String?
        /// The latest mirrored session state (model, effort, context, ids, thinking mode).
        var state: () -> PCodeHostClientState
        /// The command catalog (built-ins plus the host's), for `/help`.
        var catalog: () -> PCodeCommandCatalog
        /// The conversation's messages, oldest first, for `/links` and `/copy`.
        var transcript: () -> [Message]
        /// A host call that answers (`peek`, `query`); the completion runs on the main queue.
        var request: (_ method: String, _ args: [Any], _ completion: @escaping (_ result: Any?, _ error: String?) -> Void) -> Void
        /// A host intent (`stop_jobs`, `watch_job`, `set_thinking`).
        var notify: (_ method: String, _ args: [Any]) -> Void
        var toast: (String) -> Void
        var present: (Presentation) -> Void
        var perform: (Action) -> Void
    }

    // MARK: Routing

    /// The route for a command line, or nil when it is not a terminal command
    /// (the caller sends it to the host). Mirrors `runs_in_terminal`.
    static func route(for text: String) -> Route? {
        let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        let parts = trimmed.split(maxSplits: 1, whereSeparator: \.isWhitespace)
        guard let first = parts.first else { return nil }
        let name = String(first)
        let argument = parts.count > 1 ? String(parts[1]).trimmingCharacters(in: .whitespacesAndNewlines) : ""
        switch name {
        case "/new":
            // Bare `/new` resets the conversation, which is the session's.
            return argument.isEmpty ? nil : .action(.newSession(prompt: argument))
        case "/help", "/commands":
            return .help
        case "/config":
            return argument.isEmpty ? .action(.settings) : .config(shellWords(argument))
        case "/bind", "/unbind":
            return .unsupported("Keybindings are the terminal editor's; the native composer uses macOS text editing keys.")
        case "/quit", "/exit", "/stop":
            return argument.isEmpty ? .action(.closePane) : .usage("Usage: \(name)")
        case "/detach":
            return .unsupported("A native pane's host runs as long as the pane: switch panes to leave it running, or close it to stop it.")
        case "/status":
            return argument.isEmpty ? .sessionInfo : .usage("Usage: /status")
        case "/tools":
            switch argument {
            case "edits": return .action(.inspector(.changes))
            case "", "failed": return .action(.inspector(.commands))
            default: return .usage("Usage: /tools [failed|edits]")
            }
        case "/diffs":
            return .action(.inspector(.changes))
        case "/links":
            return argument.isEmpty ? .links : .usage("Usage: /links")
        case "/copy":
            return argument.isEmpty ? .copy : .usage("Usage: /copy")
        case "/paste-image":
            return argument.isEmpty ? .action(.pasteImage) : .usage("Usage: /paste-image")
        case "/tree":
            return .action(.inspector(.tree))
        case "/agents":
            return .action(.inspector(.agents))
        case "/resume":
            return .action(.resume)
        case "/attach", "/switch":
            if argument == "-" {
                return .unsupported("/attach - isn't native yet: pick the previous session in the sidebar.")
            }
            return .action(.attach(argument.isEmpty ? nil : argument))
        case "/fork":
            return .unsupported("/fork isn't in Pcode.app yet: run it in a terminal pcode.")
        case "/restart":
            switch argument {
            case "": return .action(.restartHost)
            case "all": return .unsupported("/restart all isn't native: Restart Pcode (in the app menu) restarts every session.")
            default: return .usage("Usage: /restart [all]")
            }
        case "/show-activity", "/show-tasks", "/autohide-tasks":
            return .unsupported("Pcode.app has no activity panel; the task list comes with the composer's task strip.")
        case "/show-edits":
            return .unsupported("Pcode.app always shows edits, folded in the transcript and in full in the Changes tab.")
        case "/show-commands":
            return .unsupported("Pcode.app keeps shell output in the Commands tab of the Session Inspector.")
        case "/show-thinking":
            let mode = argument == "status-line" ? "activity-panel" : argument  // RENAMED_VALUES
            if mode.isEmpty { return .thinking(nil) }
            return thinkingModes.contains(mode) ? .thinking(mode) : .usage("Usage: /show-thinking [\(thinkingModes.joined(separator: "|"))]")
        case "/group-tools":
            switch argument {
            case "": return .groupTools(nil)
            case "on", "off": return .groupTools(argument == "on")
            default: return .usage("Usage: /group-tools [on|off]")
            }
        case "/theme", "/syntax", "/theme-preview":
            return .unsupported("Pcode.app follows macOS appearance and its Ghostty theme; \(name) styles the terminal editor only.")
        case "/redraw":
            return .ignore
        default:
            return nil
        }
    }

    /// `THINKING_MODES` (`pcode.preferences`).
    static let thinkingModes = ["off", "activity-panel", "scrollback"]

    /// The mode bare `/show-thinking` moves to. The app shows thinking or not, so
    /// it cycles off and scrollback, as pcode's editor does inside the app.
    static func nextThinkingMode(after current: String) -> String {
        current == "off" ? "scrollback" : "off"
    }

    /// `shlex.split`, enough for `/config set KEY VALUE`: whitespace-separated words,
    /// single or double quotes grouping, backslash escaping outside single quotes.
    static func shellWords(_ text: String) -> [String] {
        var words: [String] = []
        var current = ""
        var inWord = false
        var quote: Character?
        var escaped = false
        for character in text {
            if escaped {
                current.append(character)
                escaped = false
                continue
            }
            if let open = quote {
                if character == open {
                    quote = nil
                } else if character == "\\" && open == "\"" {
                    escaped = true
                } else {
                    current.append(character)
                }
                continue
            }
            switch character {
            case "\\":
                escaped = true
                inWord = true
            case "'", "\"":
                quote = character
                inWord = true
            case _ where character.isWhitespace:
                if inWord { words.append(current) }
                current = ""
                inWord = false
            default:
                current.append(character)
                inWord = true
            }
        }
        if inWord { words.append(current) }
        return words
    }

    // MARK: Running

    /// Run a terminal command natively. False when it isn't one (send it to the host).
    @discardableResult
    static func run(_ text: String, context: Context) -> Bool {
        guard let route = route(for: text) else { return false }
        switch route {
        case .action(let action):
            context.perform(action)
        case .help:
            showHelp(context: context)
        case .sessionInfo:
            showSessionInfo(context: context)
        case .links:
            showLinks(context: context)
        case .copy:
            showCopy(context: context)
        case .config(let arguments):
            runConfig(arguments, context: context)
        case .thinking(let mode):
            let next = mode ?? nextThinkingMode(after: context.state().thinkingMode)
            // The host tells the transcript (`thinking_mode`) and asks the provider
            // for what the mode needs; the preference keeps it for the next session.
            context.notify("set_thinking", [next])
            savePreference("show_thinking", next)
            context.toast("Thinking: \(next).")
        case .groupTools(let wanted):
            let preferences = PCodeTranscriptPreferences.shared
            preferences.groupTools = wanted ?? !preferences.groupTools
            context.toast("Group tools: \(preferences.groupTools ? "on" : "off").")
        case .unsupported(let reason), .usage(let reason):
            context.toast(reason)
        case .ignore:
            break
        }
        return true
    }

    /// Save one key in pcode's `preferences.json`, keeping the rest.
    static func savePreference(_ key: String, _ value: String, url: URL = PCodePreferences.url()) {
        var preferences: [String: Any] = [:]
        if let data = try? Data(contentsOf: url),
           let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
            preferences = object
        }
        preferences[key] = value
        guard let data = try? JSONSerialization.data(withJSONObject: preferences, options: [.prettyPrinted, .sortedKeys]) else { return }
        try? FileManager.default.createDirectory(at: url.deletingLastPathComponent(), withIntermediateDirectories: true)
        try? data.write(to: url, options: .atomic)
    }

    // MARK: Host requests

    /// `choose_model(values, providers, current)`: the model picker. `completion`
    /// gets the chosen id, or nil when the picker closes without one, exactly once.
    /// Returns the picker so the caller can `cancel()` it if the pane closes first.
    @discardableResult
    static func chooseModel(
        values: [String], providers: [String], current: String?, context: Context,
        completion: @escaping (String?) -> Void
    ) -> PCodePicker {
        let picker = PCodePicker()
        var answered = false
        let finish: (String?) -> Void = { choice in
            guard !answered else { return }
            answered = true
            completion(choice)
        }
        // A pick calls `onPick` then `onClosed`; a cancel only `onClosed`.
        picker.onClosed = { finish(nil) }
        picker.showLocal(modelList(values, providers: providers, current: current)) { id, _ in finish(id) }
        // A list the picker couldn't read would leave nothing to cancel: answer now.
        guard picker.payload != nil else {
            finish(nil)
            return picker
        }
        context.present(.picker(title: "Choose model", picker))
        return picker
    }

    /// `browse_jobs()`: the Jobs sheet. The host's command doesn't wait on it.
    static func browseJobs(context: Context) {
        let model = PCodeJobsModel(context: context)
        model.refresh()
        context.present(.sheet(title: "Jobs") { dismiss in
            AnyView(PCodeJobsSheet(model: model, dismiss: dismiss))
        })
    }

    /// `read_asides(tag)`: open the `/btw` drawer. The host's command doesn't wait on it.
    static func readAsides(context: Context) {
        context.perform(.btw)
    }

    // MARK: Sheets and lists

    static func showHelp(context: Context) {
        let groups = helpGroups(context.catalog())
        context.present(.sheet(title: "Commands") { dismiss in
            AnyView(PCodeHelpSheet(groups: groups, dismiss: dismiss))
        })
    }

    static func showSessionInfo(context: Context) {
        let model = PCodeSessionInfoModel(rows: sessionRows(state: context.state(), hostID: context.hostID))
        // The host's own overview (`session_overview`: model, workspace, session,
        // context usage...) replaces the local rows once it answers.
        context.request("query", ["session_overview"]) { result, _ in
            let overview = overviewRows(result)
            guard !overview.isEmpty else { return }
            model.rows = hostRows(hostID: context.hostID) + overview
        }
        context.present(.sheet(title: "Session") { dismiss in
            AnyView(PCodeSessionInfoSheet(model: model, dismiss: dismiss))
        })
    }

    static func showLinks(context: Context) {
        let links = conversationLinks(context.transcript())
        guard !links.isEmpty else { return context.toast("No links in this conversation yet.") }
        let picker = PCodePicker()
        picker.showLocal(linkList(links)) { id, _ in
            guard let id, let url = URL(string: id) else { return }
            // `file://` links (with a line, `#L12` or `:12`) open as pcode's do: text in the editor.
            let opened = url.isFileURL ? PCodeExternalEditor.open(fileURL: url) : NSWorkspace.shared.open(url)
            if !opened { context.toast("Could not open \(id).") }
        }
        context.present(.picker(title: "Links", picker))
    }

    static func showCopy(context: Context) {
        let responses = context.transcript().filter { $0.role == .assistant }.map(\.text)
        guard !responses.isEmpty else { return context.toast("No responses to copy yet.") }
        let (list, texts) = copyList(responses)
        let picker = PCodePicker()
        picker.showLocal(list) { id, _ in
            guard let id, let text = texts[id] else { return }
            NSPasteboard.general.clearContents()
            NSPasteboard.general.setString(text, forType: .string)
            context.toast("Copied.")
        }
        context.present(.picker(title: "Copy", picker))
    }

    /// `/config ARGS`: `pcode config ARGS` (`pcode.config.configure`), its output in a sheet.
    static func runConfig(_ arguments: [String], context: Context) {
        let directory = context.workspace
        DispatchQueue.global(qos: .userInitiated).async {
            let result = runPcode(["config"] + arguments, directory: directory)
            DispatchQueue.main.async {
                let text = result.output.trimmingCharacters(in: .whitespacesAndNewlines)
                if result.status == 0, !text.contains("\n"), text.count < 120 {
                    return context.toast(text.isEmpty ? "Done." : text)
                }
                context.present(.sheet(title: "/config " + arguments.joined(separator: " ")) { dismiss in
                    AnyView(PCodeTextSheet(text: text.isEmpty ? "pcode config failed." : text, dismiss: dismiss))
                })
            }
        }
    }

    /// Run `pcode ARGS` as the workspace launches it (login-shell env, direnv) and
    /// collect stdout and stderr. For short commands only: past `timeout` it is stopped.
    static func runPcode(_ arguments: [String], directory: String, timeout: TimeInterval = 20) -> (status: Int32, output: String) {
        PCodeWorkspaceMode.awaitLoginShellCache(timeout: 3)
        let launch = PCodeWorkspaceMode.launchConfig(arguments, directory: directory)
        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/bin/sh")
        process.arguments = ["-c", launch.command]
        var environment = ProcessInfo.processInfo.environment.filter { key, _ in
            !key.hasPrefix("GHOSTTY_") && !["PCODE_EDITOR_ONLY", "PCODE_DOCK", "PCODE_NATIVE_TRANSCRIPT", "TERM_PROGRAM"].contains(key)
        }
        environment.merge(launch.env) { _, new in new }
        environment["PCODE_HUSH_LOGIN"] = "1"
        process.environment = environment
        process.currentDirectoryURL = URL(fileURLWithPath: directory)
        let pipe = Pipe()
        process.standardOutput = pipe
        process.standardError = pipe
        process.standardInput = FileHandle.nullDevice
        let exited = DispatchSemaphore(value: 0)
        process.terminationHandler = { _ in exited.signal() }
        let output = OutputBuffer()
        pipe.fileHandleForReading.readabilityHandler = { handle in output.append(handle.availableData) }
        do { try process.run() } catch { return (-1, "Could not run pcode: \(error.localizedDescription)") }
        defer { pipe.fileHandleForReading.readabilityHandler = nil }
        if exited.wait(timeout: .now() + timeout) == .timedOut {
            process.terminate()
            return (-1, "pcode \(arguments.joined(separator: " ")) took over \(Int(timeout)) seconds and was stopped.")
        }
        // Not to EOF (a child the login shell left behind can hold the pipe open):
        // a moment for the handler to drain what pcode wrote before it exited.
        usleep(100_000)
        return (process.terminationStatus, output.text)
    }

    /// Output collected from a pipe's handler thread.
    private final class OutputBuffer: @unchecked Sendable {
        private let lock = NSLock()
        private var data = Data()

        func append(_ chunk: Data) {
            lock.lock()
            data.append(chunk)
            lock.unlock()
        }

        var text: String {
            lock.lock()
            defer { lock.unlock() }
            return String(decoding: data, as: UTF8.self)
        }
    }
}
