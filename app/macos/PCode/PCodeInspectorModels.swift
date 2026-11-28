import AppKit
import Foundation
import SwiftUI

/// Tabs available in the right-hand Session Inspector.
enum PCodeInspectorTab: String, CaseIterable, Identifiable, Equatable, Hashable, Sendable {
    case changes
    case files
    case agents
    case commands
    case tree

    var id: String { rawValue }

    var title: String {
        switch self {
        case .changes: return "Changes"
        case .files: return "Files"
        case .agents: return "Agents"
        case .commands: return "Commands"
        case .tree: return "Tree"
        }
    }

    var systemImage: String {
        switch self {
        case .changes: return "doc.text.magnifyingglass"
        case .files: return "doc.text"
        case .agents: return "person.2"
        case .commands: return "terminal"
        case .tree: return "point.3.connected.trianglepath.dotted"
        }
    }
}

/// Which set of diffs the Changes tab displays.
enum PCodeDiffScope: Equatable, Hashable, Sendable {
    /// Edits created by this session (default), ordered by most recently modified.
    case session
    /// Uncommitted git working tree + untracked files in the session directory.
    case workingTree
    /// Full branch diff (merge-base...HEAD + working tree) when on a feature branch.
    case branch
    /// One specific edit clicked from the transcript.
    case singleEdit(callID: String)

    var displayTitle: String {
        switch self {
        case .session: return "This Session"
        case .workingTree: return "Working Tree"
        case .branch: return "Branch"
        case .singleEdit: return "Single Edit"
        }
    }
}

/// Visual layout for diff hunks.
enum PCodeDiffLayout: String, CaseIterable, Equatable, Hashable, Sendable {
    case unified
    case sideBySide
}

/// How `This Session` diffs are grouped in the Inspector Changes tab.
enum PCodeSessionDiffViewMode: String, CaseIterable, Equatable, Hashable, Sendable {
    /// Append-only chronological stream of edits, oldest to newest (bottom-pinned).
    case stream
    /// Combined per-file view for reviewing everything this session touched.
    case files

    var title: String {
        switch self {
        case .stream: return "Stream"
        case .files: return "Files"
        }
    }
}

/// File-level change status shown in diff headers and sidebar rows.
enum PCodeFileChangeKind: String, Equatable, Hashable, Sendable {
    case added
    case modified
    case deleted
    case renamed

    var badge: String {
        switch self {
        case .added: return "A"
        case .modified: return "M"
        case .deleted: return "D"
        case .renamed: return "R"
        }
    }

    init(operation: String) {
        switch operation.lowercased() {
        case "create", "write", "add", "added", "new":
            self = .added
        case "delete", "deleted", "remove", "removed":
            self = .deleted
        case "rename", "renamed":
            self = .renamed
        default:
            self = .modified
        }
    }
}

/// A UTF-16 character range within a diff line that changed relative to its counterpart.
struct PCodeIntralineSpan: Equatable, Hashable, Sendable {
    let range: Range<Int>
    let emphasized: Bool

    init(range: Range<Int>, emphasized: Bool = true) {
        self.range = range
        self.emphasized = emphasized
    }
}

/// One line inside a diff hunk.
struct PCodeDiffLine: Identifiable, Equatable, Hashable, Sendable {
    enum Kind: String, Equatable, Hashable, Sendable {
        case context
        case added
        case deleted
        case meta
    }

    let id: String
    let kind: Kind
    let oldLineNumber: Int?
    let newLineNumber: Int?
    /// Line text without the leading `+`, `-`, or space marker.
    let text: String
    let intralineSpans: [PCodeIntralineSpan]

    init(
        id: String,
        kind: Kind,
        oldLineNumber: Int? = nil,
        newLineNumber: Int? = nil,
        text: String,
        intralineSpans: [PCodeIntralineSpan] = []
    ) {
        self.id = id
        self.kind = kind
        self.oldLineNumber = oldLineNumber
        self.newLineNumber = newLineNumber
        self.text = text
        self.intralineSpans = intralineSpans
    }

    /// Preferred line number for editor jumps and `@path:L...` references.
    var referenceLineNumber: Int? {
        newLineNumber ?? oldLineNumber
    }
}

/// One unified diff hunk (`@@ -oldStart,oldCount +newStart,newCount @@`).
struct PCodeDiffHunk: Identifiable, Equatable, Hashable, Sendable {
    let id: String
    let header: String
    let oldStart: Int
    let oldCount: Int
    let newStart: Int
    let newCount: Int
    let lines: [PCodeDiffLine]

    init(
        id: String,
        header: String,
        oldStart: Int,
        oldCount: Int,
        newStart: Int,
        newCount: Int,
        lines: [PCodeDiffLine]
    ) {
        self.id = id
        self.header = header
        self.oldStart = oldStart
        self.oldCount = oldCount
        self.newStart = newStart
        self.newCount = newCount
        self.lines = lines
    }
}

/// Aggregated diff for a single file.
struct PCodeFileDiff: Identifiable, Equatable, Hashable, Sendable {
    let id: String
    /// Display path (relative to session directory when inside it).
    let path: String
    /// Resolved absolute path on disk when available (for Open in Editor & context expansion).
    let absolutePath: String?
    let kind: PCodeFileChangeKind
    let added: Int
    let removed: Int
    let hunks: [PCodeDiffHunk]
    let rawPatch: String
    let lastModifiedAt: Date
    let editCount: Int
    /// True while an `editPreview` is in flight before `editCompleted` settles.
    let isStreaming: Bool
    let truncated: Bool
    /// Tool call IDs that contributed to this file diff in session scope.
    let callIDs: [String]
    /// Syntax language identifier for `PCodeSyntaxHighlighter`.
    let language: String?

    init(
        id: String? = nil,
        path: String,
        absolutePath: String? = nil,
        kind: PCodeFileChangeKind = .modified,
        added: Int = 0,
        removed: Int = 0,
        hunks: [PCodeDiffHunk] = [],
        rawPatch: String = "",
        lastModifiedAt: Date = Date(),
        editCount: Int = 1,
        isStreaming: Bool = false,
        truncated: Bool = false,
        callIDs: [String] = [],
        language: String? = nil
    ) {
        self.id = id ?? path
        self.path = path
        self.absolutePath = absolutePath
        self.kind = kind
        self.added = added
        self.removed = removed
        self.hunks = hunks
        self.rawPatch = rawPatch
        self.lastModifiedAt = lastModifiedAt
        self.editCount = editCount
        self.isStreaming = isStreaming
        self.truncated = truncated
        self.callIDs = callIDs
        self.language = language ?? Self.inferLanguage(for: path)
    }

    var basename: String {
        (path as NSString).lastPathComponent
    }

    var parentDirectory: String {
        let dir = (path as NSString).deletingLastPathComponent
        return dir == "." ? "" : dir
    }

    /// First changed line number in the file, useful for jumping when opening in an external editor.
    var firstChangedLine: Int? {
        for hunk in hunks {
            for line in hunk.lines where line.kind == .added || line.kind == .deleted {
                if let number = line.referenceLineNumber {
                    return number
                }
            }
            if hunk.newStart > 0 { return hunk.newStart }
        }
        return nil
    }

    static func inferLanguage(for path: String) -> String? {
        let ext = (path as NSString).pathExtension.lowercased()
        guard !ext.isEmpty else { return nil }
        switch ext {
        case "swift": return "swift"
        case "py", "pyi": return "python"
        case "ts", "mts", "cts": return "typescript"
        case "tsx": return "tsx"
        case "js", "mjs", "cjs": return "javascript"
        case "jsx": return "jsx"
        case "rs": return "rust"
        case "go": return "go"
        case "zig": return "zig"
        case "c", "h": return "c"
        case "cc", "cpp", "cxx", "hpp", "mm": return "cpp"
        case "m": return "objectivec"
        case "rb": return "ruby"
        case "sh", "bash", "zsh": return "bash"
        case "json", "jsonc": return "json"
        case "yaml", "yml": return "yaml"
        case "toml": return "toml"
        case "md", "markdown": return "markdown"
        case "html", "htm": return "html"
        case "css": return "css"
        case "sql": return "sql"
        default: return ext
        }
    }
}

/// A user-selected line range inside a diff, formatted for `@path:L...` composer injection.
struct PCodeDiffSelection: Equatable, Hashable, Sendable {
    let path: String
    let startLine: Int
    let endLine: Int

    init(path: String, startLine: Int, endLine: Int) {
        self.path = path
        self.startLine = min(startLine, endLine)
        self.endLine = max(startLine, endLine)
    }

    var promptReference: String {
        if startLine == endLine {
            return "@\(path):L\(startLine)"
        }
        return "@\(path):L\(startLine)-L\(endLine)"
    }
}

/// One delegated sub-agent displayed in the Inspector's Agents tab.
struct PCodeInspectorAgentItem: Identifiable, Equatable, Sendable {
    enum Status: String, Equatable, Hashable, Sendable {
        case running
        case waiting
        case completed
        case failed
        case cancelled
    }

    let id: String
    let parentAgentID: String?
    let workerID: String?
    let role: String
    let detail: String
    let status: Status
    let startedAt: Date?
    let elapsedSeconds: Double?
    let nestedCalls: [PCodePeekToolCall]
    let reportMarkdown: String
    let pendingQuestion: String?
    let error: String?

    init(
        id: String,
        parentAgentID: String? = nil,
        workerID: String? = nil,
        role: String,
        detail: String = "",
        status: Status = .running,
        startedAt: Date? = nil,
        elapsedSeconds: Double? = nil,
        nestedCalls: [PCodePeekToolCall] = [],
        reportMarkdown: String = "",
        pendingQuestion: String? = nil,
        error: String? = nil
    ) {
        self.id = id
        self.parentAgentID = parentAgentID
        self.workerID = workerID
        self.role = role
        self.detail = detail
        self.status = status
        self.startedAt = startedAt
        self.elapsedSeconds = elapsedSeconds
        self.nestedCalls = nestedCalls
        self.reportMarkdown = reportMarkdown
        self.pendingQuestion = pendingQuestion
        self.error = error
    }

    var isActive: Bool { status == .running || status == .waiting }
}

/// One shell command or background job displayed in the Inspector's Commands tab.
struct PCodeInspectorCommandItem: Identifiable, Equatable, Sendable {
    enum Status: String, Equatable, Hashable, Sendable {
        case running
        case succeeded
        case failed
        case exitedNonZero
    }

    let id: String
    let command: String
    let detail: String
    let output: String
    let status: Status
    let isBackgroundJob: Bool
    let exitCode: Int?
    let startedAt: Date?
    let elapsedSeconds: Double?

    init(
        id: String,
        command: String,
        detail: String = "",
        output: String = "",
        status: Status = .running,
        isBackgroundJob: Bool = false,
        exitCode: Int? = nil,
        startedAt: Date? = nil,
        elapsedSeconds: Double? = nil
    ) {
        self.id = id
        self.command = command
        self.detail = detail
        self.output = output
        self.status = status
        self.isBackgroundJob = isBackgroundJob
        self.exitCode = exitCode
        self.startedAt = startedAt
        self.elapsedSeconds = elapsedSeconds
    }

    var isRunning: Bool { status == .running }

    var displayTitle: String {
        if !detail.isEmpty { return detail }
        return command
    }
}

/// Host/workspace callbacks injected into Inspector views so Tracks A-D stay decoupled from `PCodeWorkspace`.
struct PCodeInspectorActions {
    let insertIntoComposer: @MainActor (String) -> Void
    let openInEditor: @MainActor (_ path: String, _ line: Int?) -> Void
    let openInFilesTab: @MainActor (_ path: String, _ line: Int?, _ endLine: Int?) -> Void
    let copyToClipboard: @MainActor (String) -> Void
    let stopJob: @MainActor (_ jobID: String) -> Void
    let prefillWorkerAction: @MainActor (_ text: String) -> Void
    let manageWorker: (@MainActor (
        _ action: String,
        _ workerID: String,
        _ text: String,
        _ completion: @escaping @MainActor (Result<String, Error>) -> Void
    ) -> Void)?
    let scrollToTurn: @MainActor (_ turnID: String) -> Void
    let navigateTree: (@MainActor (
        _ nodeID: String?,
        _ edit: Bool,
        _ completion: @escaping @MainActor (Result<String, Error>) -> Void
    ) -> Void)?

    init(
        insertIntoComposer: @escaping @MainActor (String) -> Void = { _ in },
        openInEditor: @escaping @MainActor (_ path: String, _ line: Int?) -> Void = { _, _ in },
        openInFilesTab: @escaping @MainActor (_ path: String, _ line: Int?, _ endLine: Int?) -> Void = { _, _, _ in },
        copyToClipboard: @escaping @MainActor (String) -> Void = { text in
            NSPasteboard.general.clearContents()
            NSPasteboard.general.setString(text, forType: .string)
        },
        stopJob: @escaping @MainActor (_ jobID: String) -> Void = { _ in },
        prefillWorkerAction: (@MainActor (_ text: String) -> Void)? = nil,
        manageWorker: (@MainActor (
            _ action: String,
            _ workerID: String,
            _ text: String,
            _ completion: @escaping @MainActor (Result<String, Error>) -> Void
        ) -> Void)? = nil,
        scrollToTurn: @escaping @MainActor (_ turnID: String) -> Void = { _ in },
        navigateTree: (@MainActor (
            _ nodeID: String?,
            _ edit: Bool,
            _ completion: @escaping @MainActor (Result<String, Error>) -> Void
        ) -> Void)? = nil
    ) {
        self.insertIntoComposer = insertIntoComposer
        self.openInEditor = openInEditor
        self.openInFilesTab = openInFilesTab
        self.copyToClipboard = copyToClipboard
        self.stopJob = stopJob
        self.prefillWorkerAction = prefillWorkerAction ?? insertIntoComposer
        self.manageWorker = manageWorker
        self.scrollToTurn = scrollToTurn
        self.navigateTree = navigateTree
    }

    static let noop = PCodeInspectorActions()
}

/// Per-session UI state for the right-hand Session Inspector.
@MainActor
final class PCodeInspectorState: ObservableObject {
    /// Windows narrower than this degrade from a side-by-side split to a slide-over drawer.
    /// Minimum detail-pane width (excluding sidebar) before the Inspector degrades to a slide-over drawer.
    /// Corresponds to ~1110pt total window width with a default 250pt sidebar.
    static let splitMinContainerWidth: CGFloat = 860
    static let minSplitWidth: CGFloat = 320
    static let defaultSplitFraction: CGFloat = 0.42
    static let minSplitFraction: CGFloat = 0.25
    static let maxSplitFraction: CGFloat = 0.72

    static let sessionDiffViewModeKey = "PCodeInspectorSessionDiffViewMode"
    static let diffWrapLinesKey = "PCodeInspectorDiffWrapLines"

    @Published var isOpen: Bool = true {
        didSet {
            if !isOpen && isQuickOpenPresented {
                isQuickOpenPresented = false
            }
        }
    }
    @Published var activeTab: PCodeInspectorTab = .changes {
        didSet {
            if activeTab != .files && isQuickOpenPresented {
                isQuickOpenPresented = false
            }
        }
    }
    @Published var diffScope: PCodeDiffScope = .session
    @Published var sessionDiffViewMode: PCodeSessionDiffViewMode = {
        if let raw = UserDefaults.standard.string(forKey: PCodeInspectorState.sessionDiffViewModeKey),
           let mode = PCodeSessionDiffViewMode(rawValue: raw) {
            return mode
        }
        return .stream
    }() {
        didSet {
            UserDefaults.standard.set(sessionDiffViewMode.rawValue, forKey: Self.sessionDiffViewModeKey)
        }
    }
    @Published var diffLayout: PCodeDiffLayout = .unified
    @Published var wrapDiffLines: Bool = UserDefaults.standard.bool(forKey: PCodeInspectorState.diffWrapLinesKey) {
        didSet {
            UserDefaults.standard.set(wrapDiffLines, forKey: Self.diffWrapLinesKey)
        }
    }
    @Published var isExpanded: Bool = false
    @Published var followAgent: Bool = true
    @Published var splitFraction: CGFloat = PCodeInspectorState.defaultSplitFraction
    @Published var focusedFilePath: String?
    @Published var focusedAgentID: String?
    @Published var focusedCommandID: String?
    @Published var collapsedFiles: Set<String> = []
    @Published var isQuickOpenPresented: Bool = false

    static func shouldUseOverlay(containerWidth: CGFloat) -> Bool {
        containerWidth < splitMinContainerWidth
    }

    static func clampedWidth(in total: CGFloat, fraction: CGFloat) -> CGFloat {
        let clampedFraction = min(maxSplitFraction, max(minSplitFraction, fraction))
        let floor = min(total, minSplitWidth)
        let upper = max(floor, total * maxSplitFraction)
        return min(upper, max(floor, total * clampedFraction))
    }

    func toggle() {
        isOpen.toggle()
        if !isOpen { isExpanded = false }
    }

    func close() {
        isOpen = false
        isExpanded = false
    }

    func toggleExpanded() {
        if !isOpen { isOpen = true }
        isExpanded.toggle()
    }

    func openChanges(filePath: String? = nil, scope: PCodeDiffScope? = nil) {
        if let scope { diffScope = scope }
        if let filePath {
            if diffScope == .session {
                sessionDiffViewMode = .files
            }
            focusedFilePath = filePath
            collapsedFiles.remove(filePath)
        }
        activeTab = .changes
        isOpen = true
    }

    func openFiles() {
        activeTab = .files
        isOpen = true
    }

    func openAgent(callID: String? = nil) {
        if let callID { focusedAgentID = callID }
        activeTab = .agents
        isOpen = true
    }

    func openCommand(callID: String? = nil) {
        if let callID { focusedCommandID = callID }
        activeTab = .commands
        isOpen = true
    }

    func openTree() {
        activeTab = .tree
        isOpen = true
    }

    func toggleCollapsed(filePath: String) {
        if collapsedFiles.contains(filePath) {
            collapsedFiles.remove(filePath)
        } else {
            collapsedFiles.insert(filePath)
        }
    }
}
