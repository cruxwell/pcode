import AppKit
import Darwin
import Foundation
import SwiftUI

/// Short-lived protocol-2 host RPC helper for control intents (e.g., `stop_jobs`)
/// without calling `attach`, so it never affects `attached`, `_latest`, or `unseen`.
enum PCodeHostControlRPC {
    private static let queue = DispatchQueue(
        label: "com.pcode.host.control-rpc",
        qos: .userInitiated,
        attributes: .concurrent
    )

    static func stopJobs(_ jobIDs: [String]?, sessionID: String?, hostID: String?) {
        queue.async {
            guard let socketPath = PCodeSessionInfo.hostSocketPath(
                forSessionID: sessionID,
                hostID: hostID
            ), !socketPath.isEmpty else { return }
            _ = sendRequest(
                socketPath: socketPath,
                method: "stop_jobs",
                args: [jobIDs as Any]
            )
        }
    }

    static func stopJobs(_ jobIDs: [String]?, socketPath: String) {
        guard !socketPath.isEmpty else { return }
        queue.async {
            _ = sendRequest(
                socketPath: socketPath,
                method: "stop_jobs",
                args: [jobIDs as Any]
            )
        }
    }

    struct ControlError: LocalizedError {
        let message: String
        var errorDescription: String? { message }
    }

    static func manageWorker(
        action: String,
        workerID: String,
        text: String,
        sessionID: String?,
        hostID: String?,
        completion: @escaping @MainActor (Result<String, Error>) -> Void
    ) {
        queue.async {
            guard let socketPath = PCodeSessionInfo.hostSocketPath(
                forSessionID: sessionID,
                hostID: hostID
            ), !socketPath.isEmpty else {
                Task { @MainActor in
                    completion(.failure(ControlError(message: "Session host is not running.")))
                }
                return
            }
            let outcome = sendRequestWithReply(
                socketPath: socketPath,
                method: "manage_worker",
                args: [action, workerID, text]
            )
            Task { @MainActor in
                switch outcome {
                case .success(let value):
                    if let dict = value as? [String: Any] {
                        let ok = (dict["ok"] as? Bool) ?? false
                        let msg = (dict["message"] as? String) ?? (ok ? "Done." : "Request failed.")
                        completion(ok ? .success(msg) : .failure(ControlError(message: msg)))
                    } else {
                        completion(.failure(ControlError(message: "Unexpected reply from session host.")))
                    }
                case .failure(let error):
                    completion(.failure(error))
                }
            }
        }
    }

    static func navigateTree(
        nodeID: String?,
        edit: Bool,
        sessionID: String?,
        hostID: String?,
        completion: @escaping @MainActor (Result<String, Error>) -> Void
    ) {
        queue.async {
            guard let socketPath = PCodeSessionInfo.hostSocketPath(
                forSessionID: sessionID,
                hostID: hostID
            ), !socketPath.isEmpty else {
                Task { @MainActor in
                    completion(.failure(ControlError(message: "Session host is not running.")))
                }
                return
            }
            let outcome = sendRequestWithReply(
                socketPath: socketPath,
                method: "navigate_tree",
                args: [nodeID as Any, edit],
                timeoutSeconds: 5.0,
                maxReplyBytes: 1_048_576
            )
            Task { @MainActor in
                switch outcome {
                case .success(let value):
                    if let dict = value as? [String: Any] {
                        let ok = (dict["ok"] as? Bool) ?? false
                        if ok {
                            completion(.success((dict["draft"] as? String) ?? ""))
                        } else {
                            let msg = (dict["message"] as? String) ?? "Could not switch branch."
                            completion(.failure(ControlError(message: msg)))
                        }
                    } else {
                        completion(.failure(ControlError(message: "Unexpected reply from session host.")))
                    }
                case .failure(let error):
                    completion(.failure(error))
                }
            }
        }
    }

    @discardableResult
    static func sendRequest(
        socketPath: String,
        method: String,
        args: [Any],
        kwargs: [String: Any] = [:],
        timeoutSeconds: Double = 2.0
    ) -> Bool {
        if case .success = sendRequestWithReply(
            socketPath: socketPath,
            method: method,
            args: args,
            kwargs: kwargs,
            timeoutSeconds: timeoutSeconds
        ) {
            return true
        }
        return false
    }

    static func sendRequestWithReply(
        socketPath: String,
        method: String,
        args: [Any],
        kwargs: [String: Any] = [:],
        timeoutSeconds: Double = 2.0,
        maxReplyBytes: Int = 65_536
    ) -> Result<Any?, Error> {
        let clampedTimeout = max(0.2, timeoutSeconds)
        let deadline = CFAbsoluteTimeGetCurrent() + clampedTimeout

        let fd = Darwin.socket(AF_UNIX, SOCK_STREAM, 0)
        guard fd >= 0 else { return .failure(ControlError(message: "Failed to open socket.")) }
        defer { Darwin.close(fd) }

        var noSigPipe: Int32 = 1
        _ = Darwin.setsockopt(fd, SOL_SOCKET, SO_NOSIGPIPE, &noSigPipe, socklen_t(MemoryLayout<Int32>.size))

        var tv = timeval(
            tv_sec: Int(clampedTimeout),
            tv_usec: Int32((clampedTimeout - floor(clampedTimeout)) * 1_000_000)
        )
        _ = Darwin.setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &tv, socklen_t(MemoryLayout<timeval>.size))
        _ = Darwin.setsockopt(fd, SOL_SOCKET, SO_SNDTIMEO, &tv, socklen_t(MemoryLayout<timeval>.size))

        var addr = sockaddr_un()
        addr.sun_family = sa_family_t(AF_UNIX)
        let maxLen = MemoryLayout.size(ofValue: addr.sun_path)
        let bytes = Array(socketPath.utf8)
        guard bytes.count < maxLen else {
            return .failure(ControlError(message: "Host socket path is too long."))
        }
        withUnsafeMutableBytes(of: &addr.sun_path) { buf in
            for (idx, byte) in bytes.enumerated() {
                buf[idx] = byte
            }
            buf[bytes.count] = 0
        }

        let connectResult = withUnsafePointer(to: &addr) { ptr in
            ptr.withMemoryRebound(to: sockaddr.self, capacity: 1) { sockPtr in
                Darwin.connect(fd, sockPtr, socklen_t(MemoryLayout<sockaddr_un>.size))
            }
        }
        guard connectResult == 0 else {
            return .failure(ControlError(message: "Could not connect to session host."))
        }

        // Verify the peer socket belongs to our effective user.
        var peerUID: uid_t = 0
        var peerGID: gid_t = 0
        guard Darwin.getpeereid(fd, &peerUID, &peerGID) == 0, peerUID == Darwin.geteuid() else {
            return .failure(ControlError(message: "Host socket permission check failed."))
        }

        let hello = "{\"type\":\"hello\",\"protocol\":2}\n"
        guard writeAll(fd: fd, data: Data(hello.utf8)) else {
            return .failure(ControlError(message: "Failed to write handshake."))
        }

        var readBuffer = Data()
        guard let readyLine = readLine(fd: fd, buffer: &readBuffer, deadline: deadline),
              let readyObj = try? JSONSerialization.jsonObject(with: readyLine) as? [String: Any],
              readyObj["type"] as? String == "ready"
        else {
            return .failure(ControlError(message: "Session host did not respond to handshake."))
        }

        let sanitizedArgs: [Any] = args.map { value in
            if case Optional<Any>.none = value {
                return NSNull()
            }
            return value
        }
        let reqObj: [String: Any] = [
            "type": "request",
            "id": 1,
            "method": method,
            "args": sanitizedArgs,
            "kwargs": kwargs,
        ]
        guard JSONSerialization.isValidJSONObject(reqObj),
              var reqData = try? JSONSerialization.data(withJSONObject: reqObj)
        else { return .failure(ControlError(message: "Invalid RPC payload.")) }
        reqData.append(0x0A)
        guard writeAll(fd: fd, data: reqData) else {
            return .failure(ControlError(message: "Failed to send request to host."))
        }

        // Wait for reply and verify the host accepted the intent.
        guard let replyLine = readLine(
                fd: fd,
                buffer: &readBuffer,
                deadline: deadline,
                maxBytes: maxReplyBytes
              ),
              let replyObj = try? JSONSerialization.jsonObject(with: replyLine) as? [String: Any],
              replyObj["type"] as? String == "reply",
              (replyObj["id"] as? Int) == 1
        else {
            return .failure(ControlError(message: "No reply from session host."))
        }
        if let err = replyObj["error"], !(err is NSNull) {
            if let errDict = err as? [String: Any], let msg = errDict["message"] as? String {
                return .failure(ControlError(message: msg))
            }
            return .failure(ControlError(message: "\(err)"))
        }
        return .success(replyObj["result"])
    }

    private static func writeAll(fd: Int32, data: Data) -> Bool {
        data.withUnsafeBytes { raw in
            guard let base = raw.baseAddress else { return true }
            var sent = 0
            while sent < raw.count {
                let n = Darwin.write(fd, base.advanced(by: sent), raw.count - sent)
                if n <= 0 {
                    if errno == EINTR { continue }
                    return false
                }
                sent += n
            }
            return true
        }
    }

    private static func readLine(
        fd: Int32,
        buffer: inout Data,
        deadline: CFAbsoluteTime,
        maxBytes: Int = 65_536
    ) -> Data? {
        var chunk = [UInt8](repeating: 0, count: 4096)
        while true {
            if let nlIndex = buffer.firstIndex(of: 0x0A) {
                let line = buffer.prefix(upTo: nlIndex)
                buffer.removeSubrange(...nlIndex)
                return Data(line)
            }
            if buffer.count >= maxBytes { return nil }

            let remaining = deadline - CFAbsoluteTimeGetCurrent()
            if remaining <= 0 { return nil }

            var pfd = pollfd(fd: fd, events: Int16(POLLIN), revents: 0)
            let timeoutMs = Int32(min(Double(Int32.max), ceil(remaining * 1000.0)))
            let ready = Darwin.poll(&pfd, 1, timeoutMs)
            if ready < 0 {
                if errno == EINTR { continue }
                return nil
            }
            if ready == 0 { return nil }

            let n = Darwin.read(fd, &chunk, chunk.count)
            if n > 0 {
                buffer.append(contentsOf: chunk.prefix(n))
            } else if n < 0 && errno == EINTR {
                continue
            } else {
                break
            }
        }
        return nil
    }
}

/// Full-height right-side Session Inspector split (or slide-over drawer on narrow windows)
/// wrapping a session pane's main content (transcript, docked composer, and optional bottom shell).
struct PCodeInspectorSplit<MainContent: View>: View {
    @EnvironmentObject var ghostty: Ghostty.App
    @ObservedObject var pane: PCodeSessionPane
    @ObservedObject var inspectorState: PCodeInspectorState
    @ObservedObject var changesStore: PCodeSessionChangesStore
    @ObservedObject var agentsStore: PCodeInspectorAgentsStore
    @ObservedObject var commandsStore: PCodeInspectorCommandsStore
    @ObservedObject var treeStore: PCodeConversationTreeStore
    @ObservedObject private var typography = PCodeTypography.shared
    let mainContent: () -> MainContent

    @State private var dragStartFraction: CGFloat?
    @State private var dividerHovered = false

    init(
        pane: PCodeSessionPane,
        @ViewBuilder mainContent: @escaping () -> MainContent
    ) {
        self.pane = pane
        self.inspectorState = pane.inspectorState
        self.changesStore = pane.changesStore
        self.agentsStore = pane.agentsStore
        self.commandsStore = pane.commandsStore
        self.treeStore = pane.treeStore
        self.mainContent = mainContent
    }

    private var colorScheme: ColorScheme {
        NSColor(ghostty.config.backgroundColor).isLightColor ? .light : .dark
    }

    private var inspectorBackground: Color {
        ghostty.config.backgroundColor
    }

    private func actions(isOverlay: Bool) -> PCodeInspectorActions {
        let surface = pane.surface
        let composer = pane.composer
        let directory = changesStore.sessionDirectory ?? pane.directory
        let sessionID = pane.sessionID
        let hostID = surface?.pcodeHost
            ?? pane.hostID
            ?? pane.hostStatus?.hostID
            ?? pane.lastHostID
            ?? pane.launchHostID
        let state = inspectorState
        return PCodeInspectorActions(
            insertIntoComposer: { text in
                guard !text.isEmpty else { return }
                if state.isExpanded {
                    state.isExpanded = false
                }
                if isOverlay {
                    state.close()
                }
                if let surface {
                    surface.surfaceModel?.sendText(text)
                    Ghostty.moveFocus(to: surface)
                } else {
                    composer?.insert(text)
                }
            },
            openInEditor: { path, line in
                let resolved = Self.resolvePath(path, relativeTo: directory)
                PCodeExternalEditor.open(path: resolved, line: line)
            },
            copyToClipboard: { text in
                NSPasteboard.general.clearContents()
                NSPasteboard.general.setString(text, forType: .string)
            },
            stopJob: { jobID in
                PCodeHostControlRPC.stopJobs([jobID], sessionID: sessionID, hostID: hostID)
            },
            manageWorker: { action, workerID, text, completion in
                PCodeHostControlRPC.manageWorker(
                    action: action,
                    workerID: workerID,
                    text: text,
                    sessionID: sessionID,
                    hostID: hostID,
                    completion: completion
                )
            },
            scrollToTurn: { turnID in
                if state.isExpanded {
                    state.isExpanded = false
                }
                if isOverlay {
                    state.close()
                }
                pane.onScrollToTurn?(turnID)
            },
            navigateTree: { nodeID, edit, completion in
                PCodeHostControlRPC.navigateTree(
                    nodeID: nodeID,
                    edit: edit,
                    sessionID: sessionID,
                    hostID: hostID,
                    completion: completion
                )
            }
        )
    }

    static func overlayDrawerWidth(in totalWidth: CGFloat, fraction: CGFloat) -> CGFloat {
        let minWidth = min(totalWidth, PCodeInspectorState.minSplitWidth)
        let maxWidth = max(minWidth, totalWidth * 0.85)
        return min(maxWidth, max(minWidth, totalWidth * fraction))
    }

    private static func resolvePath(_ path: String, relativeTo directory: String) -> String {
        guard !path.isEmpty else { return path }
        if (path as NSString).isAbsolutePath {
            return path
        }
        return URL(fileURLWithPath: directory, isDirectory: true)
            .appendingPathComponent(path)
            .standardizedFileURL
            .path
    }

    var body: some View {
        GeometryReader { geometry in
            let totalWidth = max(1, geometry.size.width)
            let useOverlay = PCodeInspectorState.shouldUseOverlay(containerWidth: totalWidth)
            let isSplit = inspectorState.isOpen && !inspectorState.isExpanded && !useOverlay
            let isOverlay = inspectorState.isOpen && !inspectorState.isExpanded && useOverlay
            let isExpanded = inspectorState.isOpen && inspectorState.isExpanded
            let inspectorWidth = isSplit
                ? PCodeInspectorState.clampedWidth(in: totalWidth, fraction: inspectorState.splitFraction)
                : 0
            let drawerWidth = Self.overlayDrawerWidth(in: totalWidth, fraction: inspectorState.splitFraction)

            // Keep `mainContent()` at a single invariant structural position across all states
            // so opening/closing/expanding or crossing the overlay width threshold never tears
            // down the live transcript controller or Ghostty terminal surfaces.
            ZStack(alignment: .trailing) {
                HStack(spacing: 0) {
                    mainContent()
                        .frame(maxWidth: .infinity, maxHeight: .infinity)
                        .allowsHitTesting(!isExpanded)
                        .accessibilityHidden(isExpanded)

                    if isSplit {
                        Rectangle()
                            .fill(
                                dividerHovered || dragStartFraction != nil
                                    ? Color.accentColor.opacity(0.6)
                                    : ghostty.config.splitDividerColor
                            )
                            .frame(width: 1)

                        inspectorPanel(totalWidth: inspectorWidth, isOverlay: false)
                            .frame(width: inspectorWidth)
                            .frame(maxHeight: .infinity)
                    }
                }

                if isSplit {
                    resizeHandle(
                        totalWidth: totalWidth,
                        displayedWidth: inspectorWidth,
                        isOverlay: false
                    )
                    .frame(width: 11)
                    .offset(x: -(inspectorWidth - 5))
                    .zIndex(5)
                }

                if isOverlay {
                    Color.black.opacity(colorScheme == .dark ? 0.34 : 0.18)
                        .ignoresSafeArea()
                        .onTapGesture {
                            inspectorState.close()
                            pane.focusInput()
                        }

                    ZStack(alignment: .leading) {
                        inspectorPanel(totalWidth: drawerWidth, isOverlay: true)
                            .frame(width: drawerWidth)
                            .frame(maxHeight: .infinity)
                            .shadow(color: Color.black.opacity(0.28), radius: 14, x: -4, y: 0)

                        resizeHandle(
                            totalWidth: totalWidth,
                            displayedWidth: drawerWidth,
                            isOverlay: true
                        )
                        .frame(width: 11)
                        .offset(x: -5)
                    }
                    .frame(width: drawerWidth)
                    .frame(maxHeight: .infinity)
                }

                if isExpanded {
                    inspectorPanel(totalWidth: totalWidth, isOverlay: false)
                        .frame(maxWidth: .infinity, maxHeight: .infinity)
                }
            }
            .background(
                PCodeInspectorEscapeMonitor(enabled: isOverlay || isExpanded) {
                    if inspectorState.isExpanded {
                        inspectorState.isExpanded = false
                    } else {
                        inspectorState.close()
                    }
                    pane.focusInput()
                }
            )
        }
        .onAppear {
            syncChangesStoreGitPolling()
        }
        .onChange(of: inspectorState.diffScope) { _ in
            syncChangesStoreGitPolling()
        }
        .onChange(of: inspectorState.isOpen) { _ in
            syncChangesStoreGitPolling()
        }
        .onChange(of: inspectorState.activeTab) { _ in
            syncChangesStoreGitPolling()
        }
    }

    private func syncChangesStoreGitPolling() {
        let shouldPollGit = inspectorState.isOpen
            && inspectorState.activeTab == .changes
            && (inspectorState.diffScope == .workingTree || inspectorState.diffScope == .branch)
        if shouldPollGit {
            changesStore.setActiveScope(inspectorState.diffScope, triggerRefresh: true)
        } else {
            changesStore.setActiveScope(.session, triggerRefresh: false)
            changesStore.cancelPendingGitRefresh()
        }
    }

    private func resizeHandle(totalWidth: CGFloat, displayedWidth: CGFloat, isOverlay: Bool) -> some View {
        PCodeResizeHandle(
            orientation: .horizontal,
            onHover: { dividerHovered = $0 },
            onDoubleClick: { inspectorState.toggleExpanded() },
            onDragStart: {
                dragStartFraction = displayedWidth / max(1, totalWidth)
            },
            onDragChanged: { delta in
                let base = dragStartFraction ?? (displayedWidth / max(1, totalWidth))
                let nextFraction = base - (delta / max(1, totalWidth))
                let maxFrac: CGFloat = isOverlay ? 0.85 : PCodeInspectorState.maxSplitFraction
                inspectorState.splitFraction = min(maxFrac, max(PCodeInspectorState.minSplitFraction, nextFraction))
            },
            onDragEnded: {
                dragStartFraction = nil
            }
        )
    }

    private func inspectorPanel(totalWidth: CGFloat, isOverlay: Bool) -> some View {
        VStack(spacing: 0) {
            inspectorHeader(totalWidth: totalWidth, isOverlay: isOverlay)

            Rectangle()
                .fill(ghostty.config.splitDividerColor.opacity(0.75))
                .frame(height: 1)

            inspectorBody(isOverlay: isOverlay)
                .frame(maxWidth: .infinity, maxHeight: .infinity)
        }
        .background(inspectorBackground)
        .environment(\.colorScheme, colorScheme)
    }

    private func inspectorHeader(totalWidth: CGFloat, isOverlay: Bool) -> some View {
        let isCompact = totalWidth < 430
        return HStack(spacing: isCompact ? 4 : 6) {
            HStack(spacing: isCompact ? 1 : 2) {
                tabButton(.changes, badge: changesBadgeCount, pulsing: changesHasStreaming, compact: isCompact)
                tabButton(.agents, badge: agentsBadgeCount, pulsing: agentsStore.activeCount > 0, compact: isCompact)
                tabButton(.commands, badge: commandsBadgeCount, pulsing: commandsStore.runningCount > 0, compact: isCompact)
                tabButton(.tree, badge: treeBadgeCount, pulsing: false, compact: isCompact)
            }
            .padding(2)
            .background(
                RoundedRectangle(cornerRadius: 6, style: .continuous)
                    .fill(Color.primary.opacity(colorScheme == .dark ? 0.08 : 0.06))
            )

            Spacer(minLength: 2)

            Button {
                inspectorState.toggleExpanded()
            } label: {
                Image(systemName: inspectorState.isExpanded
                    ? "arrow.down.right.and.arrow.up.left"
                    : "arrow.up.left.and.arrow.down.right")
                    .font(.system(size: 11, weight: .medium))
                    .foregroundStyle(.secondary)
                    .frame(width: isCompact ? 20 : 22, height: 22)
                    .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .help(inspectorState.isExpanded ? "Restore split (⌘⇧F)" : "Expand full width (⌘⇧F)")

            Button {
                inspectorState.close()
            } label: {
                Image(systemName: "xmark")
                    .font(.system(size: 11, weight: .medium))
                    .foregroundStyle(.secondary)
                    .frame(width: isCompact ? 20 : 22, height: 22)
                    .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .help(isOverlay ? "Close inspector (Esc / ⌘I)" : "Close inspector (⌘I)")
        }
        .padding(.horizontal, isCompact ? 6 : 10)
        .padding(.vertical, 5)
    }

    private var changesBadgeCount: Int {
        let count = changesStore.files(for: inspectorState.diffScope).count
        return count > 0 ? count : changesStore.sessionFiles.count
    }

    private var changesHasStreaming: Bool {
        changesStore.sessionFiles.contains(where: \.isStreaming)
    }

    private var agentsBadgeCount: Int {
        agentsStore.activeCount > 0 ? agentsStore.activeCount : agentsStore.agents.count
    }

    private var commandsBadgeCount: Int {
        commandsStore.runningCount > 0 ? commandsStore.runningCount : commandsStore.commands.count
    }

    private var treeBadgeCount: Int {
        treeStore.tree.turnCount
    }

    private func tabButton(_ tab: PCodeInspectorTab, badge: Int, pulsing: Bool, compact: Bool = false) -> some View {
        let selected = inspectorState.activeTab == tab
        let showBadge = badge > 0 && (!compact || selected)
        return Button {
            inspectorState.activeTab = tab
        } label: {
            HStack(spacing: compact ? 2 : 3) {
                Text(tab.title)
                    .font(.system(size: compact ? 10.5 : 11, weight: selected ? .semibold : .medium))
                    .lineLimit(1)
                    .fixedSize(horizontal: true, vertical: false)
                if pulsing {
                    Circle()
                        .fill(Color.accentColor)
                        .frame(width: 5, height: 5)
                }
                if showBadge {
                    Text("\(badge)")
                        .font(.system(size: 9.5, weight: .medium, design: .monospaced))
                        .foregroundStyle(selected ? .primary : .secondary)
                        .lineLimit(1)
                        .fixedSize(horizontal: true, vertical: false)
                        .padding(.horizontal, compact ? 3.5 : 4)
                        .padding(.vertical, 0.5)
                        .background(
                            Capsule()
                                .fill(Color.primary.opacity(selected ? 0.14 : 0.08))
                        )
                }
            }
            .foregroundStyle(selected ? .primary : .secondary)
            .padding(.horizontal, compact ? 4.5 : 6)
            .padding(.vertical, 3.5)
            .background(
                RoundedRectangle(cornerRadius: 4.5, style: .continuous)
                    .fill(selected ? Color.primary.opacity(colorScheme == .dark ? 0.14 : 0.10) : .clear)
            )
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
    }

    @ViewBuilder
    private func inspectorBody(isOverlay: Bool) -> some View {
        let resolvedActions = actions(isOverlay: isOverlay)
        switch inspectorState.activeTab {
        case .changes:
            let diffs: [PCodeFileDiff] = {
                if inspectorState.diffScope == .session {
                    switch inspectorState.sessionDiffViewMode {
                    case .stream:
                        return changesStore.sessionStreamEdits
                    case .files:
                        return Array(changesStore.sessionFiles.reversed())
                    }
                }
                return changesStore.files(for: inspectorState.diffScope)
            }()
            let singleEditLabel: String? = {
                if case .singleEdit(let callID) = inspectorState.diffScope,
                   let single = changesStore.singleEdit(for: callID) {
                    return single.basename
                }
                return nil
            }()
            PCodeChangesInspectorView(
                state: inspectorState,
                diffs: diffs,
                actions: resolvedActions,
                singleEditLabel: singleEditLabel,
                isLoading: changesStore.isLoadingGit && (inspectorState.diffScope == .workingTree || inspectorState.diffScope == .branch),
                codeFont: typography.snapshot.diffCodeNSFont(),
                surfaceBackground: inspectorBackground,
                foregroundColor: NSColor(ghostty.config.foregroundColor),
                onScopeChange: { scope in
                    changesStore.setActiveScope(scope, triggerRefresh: true)
                },
                onRefresh: {
                    let scope = inspectorState.diffScope
                    if scope == .workingTree || scope == .branch {
                        Task { await changesStore.refreshGit(for: scope) }
                    }
                }
            )
        case .agents:
            PCodeInspectorAgentsView(
                state: inspectorState,
                store: agentsStore,
                actions: resolvedActions
            )
        case .commands:
            PCodeInspectorCommandsView(
                state: inspectorState,
                store: commandsStore,
                actions: resolvedActions,
                codeFont: typography.snapshot.codeNSFont(scale: typography.snapshot.codeBlockScale),
                foregroundColor: NSColor(ghostty.config.foregroundColor)
            )
        case .tree:
            PCodeInspectorTreeView(
                state: inspectorState,
                store: treeStore,
                actions: resolvedActions
            )
        }
    }
}

/// Window-scoped local key monitor that dismisses narrow-overlay or expanded Inspector on `Esc`
/// even when focus stays in the Ghostty composer or an AppKit transcript/text view.
private struct PCodeInspectorEscapeMonitor: NSViewRepresentable {
    let enabled: Bool
    let onEscape: () -> Void

    func makeNSView(context: Context) -> MonitorView {
        let view = MonitorView()
        view.enabled = enabled
        view.onEscape = onEscape
        return view
    }

    func updateNSView(_ view: MonitorView, context: Context) {
        view.enabled = enabled
        view.onEscape = onEscape
    }

    static func dismantleNSView(_ view: MonitorView, coordinator: ()) {
        view.teardown()
    }

    final class MonitorView: NSView {
        var enabled = false {
            didSet { syncMonitor() }
        }
        var onEscape: (() -> Void)?
        private var monitor: Any?

        deinit {
            teardown()
        }

        override func viewDidMoveToWindow() {
            super.viewDidMoveToWindow()
            syncMonitor()
        }

        func teardown() {
            if let monitor {
                NSEvent.removeMonitor(monitor)
                self.monitor = nil
            }
        }

        private func syncMonitor() {
            guard enabled, window != nil else {
                teardown()
                return
            }
            guard monitor == nil else { return }
            monitor = NSEvent.addLocalMonitorForEvents(matching: .keyDown) { [weak self] event in
                guard let self,
                      self.enabled,
                      let window = self.window,
                      event.window === window,
                      event.keyCode == 53, // Escape
                      event.modifierFlags.intersection([.command, .option, .control, .shift]).isEmpty
                else {
                    return event
                }
                // Let active text-input fields (e.g. agent message draft or search bar) handle Escape first.
                if let responder = window.firstResponder as? NSTextView, responder.isEditable {
                    return event
                }
                self.onEscape?()
                return nil
            }
        }
    }
}

/// Compact `+N -M` stat pill shown on an open session's sidebar row when it has edits.
struct PCodeSidebarSessionChangeStats: View {
    @ObservedObject var changesStore: PCodeSessionChangesStore

    var body: some View {
        let stats = changesStore.sessionStats
        if !changesStore.sessionFiles.isEmpty && (stats.added > 0 || stats.removed > 0) {
            HStack(spacing: 3) {
                if stats.added > 0 {
                    Text("+\(stats.added)")
                        .foregroundStyle(Color.green.opacity(0.85))
                }
                if stats.removed > 0 {
                    Text("-\(stats.removed)")
                        .foregroundStyle(Color.red.opacity(0.85))
                }
            }
            .font(.system(size: 10, weight: .medium, design: .monospaced))
            .fixedSize()
        }
    }
}

/// Compact nested list of files edited by an open session, shown under its sidebar row.
struct PCodeSidebarFileChangesList: View {
    @ObservedObject var changesStore: PCodeSessionChangesStore
    @ObservedObject var inspectorState: PCodeInspectorState
    let isSelectedPane: Bool
    let onSelectFile: (String) -> Void

    static let initialVisibleFiles = 5
    static let pageSize = 15

    @State private var visibleLimit: Int = Self.initialVisibleFiles

    private var activeFilePath: String? {
        guard isSelectedPane, inspectorState.isOpen, inspectorState.activeTab == .changes else {
            return nil
        }
        return inspectorState.focusedFilePath
    }

    var body: some View {
        let slice = changesStore.sidebarFiles(limit: visibleLimit)
        let isExpanded = visibleLimit > Self.initialVisibleFiles && changesStore.sessionFiles.count > Self.initialVisibleFiles
        if !slice.visible.isEmpty {
            let duplicateBasenames = Self.duplicateBasenames(in: slice.visible)
            VStack(alignment: .leading, spacing: 1) {
                ForEach(slice.visible) { file in
                    fileRow(file, showParent: duplicateBasenames.contains(file.basename))
                }
                if slice.overflowCount > 0 || isExpanded {
                    HStack(spacing: 10) {
                        if slice.overflowCount > 0 {
                            let remaining = slice.overflowCount
                            let next = min(remaining, Self.pageSize)
                            Button {
                                visibleLimit += Self.pageSize
                            } label: {
                                Text(next == remaining ? "Show \(remaining) more…" : "Show \(next) more of \(remaining)…")
                                    .font(.system(size: 10.5))
                                    .foregroundStyle(.secondary)
                            }
                            .buttonStyle(.plain)
                        }
                        if isExpanded {
                            Button {
                                visibleLimit = Self.initialVisibleFiles
                            } label: {
                                Text("Show less")
                                    .font(.system(size: 10.5))
                                    .foregroundStyle(.tertiary)
                            }
                            .buttonStyle(.plain)
                        }
                        Spacer(minLength: 4)
                    }
                    .padding(.leading, 26)
                    .padding(.trailing, 8)
                    .padding(.vertical, 2.5)
                }
            }
            .padding(.bottom, 3)
        }
    }

    private func fileRow(_ file: PCodeFileDiff, showParent: Bool) -> some View {
        let isFocused = activeFilePath == file.path
        return Button {
            onSelectFile(file.path)
        } label: {
            HStack(spacing: 5) {
                Text(file.kind.badge)
                    .font(.system(size: 9.5, weight: .bold, design: .monospaced))
                    .foregroundStyle(badgeColor(for: file.kind))
                    .frame(width: 10, alignment: .center)

                if file.isStreaming {
                    Circle()
                        .fill(Color.accentColor)
                        .frame(width: 4.5, height: 4.5)
                }

                Text(file.basename)
                    .font(.system(size: 11, weight: isFocused ? .medium : .regular))
                    .foregroundStyle(isFocused ? .primary : .secondary)
                    .lineLimit(1)
                    .truncationMode(.middle)

                if showParent && !file.parentDirectory.isEmpty {
                    Text(file.parentDirectory)
                        .font(.system(size: 10))
                        .foregroundStyle(.tertiary)
                        .lineLimit(1)
                        .truncationMode(.head)
                }

                Spacer(minLength: 4)

                HStack(spacing: 3) {
                    if file.added > 0 {
                        Text("+\(file.added)")
                            .foregroundStyle(Color.green.opacity(0.85))
                    }
                    if file.removed > 0 {
                        Text("-\(file.removed)")
                            .foregroundStyle(Color.red.opacity(0.85))
                    }
                }
                .font(.system(size: 10, weight: .medium, design: .monospaced))
            }
            .padding(.leading, 26)
            .padding(.trailing, 8)
            .padding(.vertical, 2.5)
            .background(
                RoundedRectangle(cornerRadius: 4, style: .continuous)
                    .fill(isFocused ? Color.accentColor.opacity(0.14) : .clear)
                    .padding(.leading, 20)
                    .padding(.trailing, 4)
            )
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .help(file.path)
    }

    private func badgeColor(for kind: PCodeFileChangeKind) -> Color {
        switch kind {
        case .added: return .green
        case .modified: return .orange
        case .deleted: return .red
        case .renamed: return .blue
        }
    }

    private static func duplicateBasenames(in files: [PCodeFileDiff]) -> Set<String> {
        var seen: Set<String> = []
        var dupes: Set<String> = []
        for file in files {
            if !seen.insert(file.basename).inserted {
                dupes.insert(file.basename)
            }
        }
        return dupes
    }
}
