import AppKit
import Foundation

/// The `Context` for a real pane: its composer session, the host connection, and
/// the workspace window it lives in. `PCodeHostRequests` builds one per command.
extension PCodeNativeCommands.Context {
    static func live(session: PCodeComposerSession, client: PCodeHostRPCClient?) -> Self {
        let workspace = PCodeWorkspaceController.existing?.workspace
        let pane = workspace?.panes.first { $0.composer === session }
        let present: (PCodeNativeCommands.Presentation) -> Void = { presentation in
            PCodeNativeSheetPresenter.present(
                presentation, in: session.textView?.window ?? workspace?.window, ghostty: workspace?.ghostty)
        }
        let toast: (String) -> Void = { session.showToast($0) }
        return Self(
            workspace: session.directory,
            hostID: session.hostID,
            state: { session.state },
            catalog: {
                PCodeCommandCatalog(snapshot: PCodeCommandCatalog.Snapshot(sessionState: session.sessionState) ?? .init())
            },
            transcript: { [weak pane] in
                PCodeNativeCommands.messages(pane?.transcriptScrollView?.transcriptDocumentView.items ?? [])
            },
            request: { method, args, completion in
                guard let client else { return completion(nil, "Not connected to the session host yet.") }
                client.request(method, args) { result, error in
                    DispatchQueue.main.async { completion(result, error) }
                }
            },
            notify: { method, args in client?.notify(method, args) },
            toast: toast,
            present: present,
            perform: { action in
                PCodeNativeCommands.perform(
                    action, session: session, pane: pane, workspace: workspace, present: present, toast: toast)
            })
    }
}

extension PCodeNativeCommands {
    /// The conversation's prompts and finished answers, from the native transcript's rows.
    static func messages(_ items: [PCodeTranscriptItem]) -> [Message] {
        items.compactMap { item in
            switch item.payload {
            case .prompt(_, let text, let isSide) where !isSide:
                return Message(role: .user, text: text)
            case .answerActions(let markdown):
                return Message(role: .assistant, text: markdown)
            // An answer ending in prose carries its copy text on that row instead
            // (`finishStreamingTurn`), which is how most answers end.
            case .prose(_, _, let copyMarkdown?, true):
                return Message(role: .assistant, text: copyMarkdown)
            default:
                return nil
            }
        }
    }

    static func perform(
        _ action: Action, session: PCodeComposerSession, pane: PCodeSessionPane?, workspace: PCodeWorkspace?,
        present: @escaping (Presentation) -> Void, toast: @escaping (String) -> Void
    ) {
        switch action {
        case .closePane:
            session.onCloseRequested?()
        case .pasteImage:
            if !session.pasteImage(from: .general) { toast("The clipboard holds no image.") }
        case .inspector(let tab):
            guard let pane else { return }
            MainActor.assumeIsolated {
                pane.inspectorState.activeTab = tab
                pane.inspectorState.isOpen = true
            }
        case .settings:
            MainActor.assumeIsolated { PCodeSettingsController.shared.show() }
        case .resume:
            guard let workspace else { return }
            let sessions = workspace.history
            guard !sessions.isEmpty else { return toast("No saved sessions yet.") }
            let picker = PCodePicker()
            picker.showLocal(sessionList(sessions, current: session.state.sessionID)) { [weak workspace] id, _ in
                guard let workspace, let info = sessions.first(where: { $0.id == id }) else { return }
                workspace.open(info)
            }
            present(.picker(title: "Sessions", picker))
        case .attach(let wanted):
            guard let workspace else { return }
            let hosted = workspace.projectGroups.flatMap(\.hostedSessions)
            if let wanted {
                switch attachTarget(wanted, among: hosted.map { (host: $0.host.hostID, session: $0.info.id) }) {
                case .found(let index): workspace.open(hosted[index].info)
                case .none: toast("No running session \(wanted).")
                case .ambiguous: toast("\(wanted) matches several running sessions: use more characters.")
                }
                return
            }
            guard !hosted.isEmpty else { return toast("No other session is running in the background.") }
            let picker = PCodePicker()
            picker.showLocal(hostList(hosted.map { (id: $0.info.id, title: $0.title, folder: $0.info.directoryName, state: $0.host.state) })) { [weak workspace] id, _ in
                guard let workspace, let match = hosted.first(where: { $0.info.id == id }) else { return }
                workspace.open(match.info)
            }
            present(.picker(title: "Running sessions", picker))
        case .newSession(let prompt):
            guard let workspace else { return }
            workspace.newSession(directory: pane?.projectPath ?? session.directory)
            // The new pane's composer queues it until its host is up.
            guard let composer = workspace.selectedPane?.composer, composer !== session else {
                return toast("Started a session; send the prompt there.")
            }
            _ = composer.submit(prompt)
        case .restartHost:
            _ = session.requestRelaunch(isManual: true)
        case .btw:
            session.openBtw()
        }
    }

    enum AttachTarget: Equatable {
        case found(Int)
        case none
        case ambiguous
    }

    /// `/attach HOST`, as `find_host` matches: an exact host or session id, else a
    /// prefix of exactly one of them.
    static func attachTarget(_ wanted: String, among hosts: [(host: String, session: String)]) -> AttachTarget {
        if let exact = hosts.firstIndex(where: { $0.host == wanted || $0.session == wanted }) { return .found(exact) }
        let matches = hosts.indices.filter { hosts[$0].host.hasPrefix(wanted) || hosts[$0].session.hasPrefix(wanted) }
        switch matches.count {
        case 0: return .none
        case 1: return .found(matches[0])
        default: return .ambiguous
        }
    }

    /// The `/resume` picker: saved sessions, newest first as `history` keeps them.
    static func sessionList(_ sessions: [PCodeSessionInfo], current: String) -> [String: Any] {
        let items = sessions.map { info in
            pickItem(
                id: info.id, label: info.label, detail: info.directoryName,
                marker: info.id == current ? "current" : "",
                search: [info.label, info.directoryName, info.id])
        }
        return pickList(title: "Sessions", empty: "No matching sessions.", items: items, enterLabel: "Open")
    }

    /// The bare `/attach` picker: sessions running in background hosts.
    static func hostList(_ hosts: [(id: String, title: String, folder: String, state: String)]) -> [String: Any] {
        let items = hosts.map { host in
            pickItem(
                id: host.id, label: host.title, detail: "\(host.folder) · \(host.state)",
                search: [host.title, host.folder, host.id])
        }
        return pickList(title: "Running sessions", empty: "No matching sessions.", items: items, enterLabel: "Open")
    }
}
