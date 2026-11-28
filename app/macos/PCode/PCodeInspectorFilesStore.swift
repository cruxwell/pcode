import AppKit
import Darwin
import Foundation

/// Parsed target from a `read_file` (or similar file-reading) tool call detail string.
struct PCodeReadFileReference: Equatable, Hashable, Sendable {
    let path: String
    let startLine: Int?
    let endLine: Int?

    static func isReadFileTool(_ name: String) -> Bool {
        switch name.lowercased() {
        case "read_file", "view_file", "read":
            return true
        default:
            return false
        }
    }

    /// Parses strings such as:
    /// - `src/pcode/app.py`
    /// - `src/pcode/app.py:42`
    /// - `src/pcode/app.py:42-80`
    /// - `src/pcode/app.py:L42-L80`
    /// - `src/pcode/app.py (lines 10-20)`
    static func parse(detail: String) -> PCodeReadFileReference? {
        let trimmed = detail.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty, trimmed != "path unavailable" else { return nil }

        // Handle trailing `(lines 10-20)` or `(line 10)`.
        if let parenRange = trimmed.range(of: #"\s*\(lines?\s+(\d+)(?:\s*[-–]\s*(\d+))?\)$"#, options: .regularExpression) {
            let pathPart = String(trimmed[..<parenRange.lowerBound]).trimmingCharacters(in: .whitespaces)
            let suffix = String(trimmed[parenRange])
            let nums = suffix.components(separatedBy: CharacterSet.decimalDigits.inverted)
                .compactMap { Int($0) }
            guard !pathPart.isEmpty else { return nil }
            let start = nums.first
            let end = nums.count >= 2 ? nums[1] : start
            return PCodeReadFileReference(path: pathPart, startLine: start, endLine: end)
        }

        // Handle `:L10-L20`, `:10-20`, `:L10`, `:10`.
        if let colonRange = trimmed.range(of: #":L?(\d+)(?:-L?(\d+))?$"#, options: .regularExpression) {
            let pathPart = String(trimmed[..<colonRange.lowerBound]).trimmingCharacters(in: .whitespaces)
            let suffix = String(trimmed[colonRange])
            let nums = suffix.components(separatedBy: CharacterSet.decimalDigits.inverted)
                .compactMap { Int($0) }
            guard !pathPart.isEmpty else { return nil }
            let start = nums.first
            let end = nums.count >= 2 ? nums[1] : start
            return PCodeReadFileReference(path: pathPart, startLine: start, endLine: end)
        }

        return PCodeReadFileReference(path: trimmed, startLine: nil, endLine: nil)
    }
}

/// One open file buffer in the Inspector's Files tab.
struct PCodeOpenFileBuffer: Identifiable, Equatable {
    let id: String
    /// Standardized absolute path on disk.
    let path: String
    /// Relative display path inside the session directory (or full path if outside).
    var displayPath: String
    var basename: String
    var text: String
    var diskText: String
    var diskChangedConflict: Bool = false
    var isMissingOnDisk: Bool = false
    var errorMessage: String?
    var isBinary: Bool = false
    var isTooLarge: Bool = false
    var fileSize: Int64 = 0
    var lastLoadedAt: Date = Date()
    /// 1-based line range to highlight (e.g. from a `read_file` tool call).
    var highlightedLineRange: ClosedRange<Int>?
    /// 1-based line number the editor should scroll to when `pendingJumpToken` changes.
    var pendingJumpLine: Int?
    var pendingJumpToken: UUID = UUID()
    /// Current 1-based selection / cursor line range reported by the editor.
    var selectedLineRange: ClosedRange<Int>?

    var isDirty: Bool {
        errorMessage == nil && !isBinary && !isTooLarge && text != diskText
    }

    var canEdit: Bool {
        errorMessage == nil && !isBinary && !isTooLarge
    }

    /// Best 1-based line to pass to an external editor (`⌘O`).
    var preferredExternalEditorLine: Int? {
        selectedLineRange?.lowerBound ?? highlightedLineRange?.lowerBound ?? pendingJumpLine
    }

    /// Formats an `@path:Lstart-Lend` reference for `⌘L` composer injection.
    func composerReference() -> String {
        let range = selectedLineRange ?? highlightedLineRange
        if let range {
            if range.lowerBound == range.upperBound {
                return "@\(displayPath):L\(range.lowerBound) "
            }
            return "@\(displayPath):L\(range.lowerBound)-L\(range.upperBound) "
        }
        return "@\(displayPath) "
    }
}

/// Flattened row in the Inspector's workspace file tree.
struct PCodeFileTreeEntry: Identifiable, Equatable, Hashable {
    let id: String
    let path: String
    let relativePath: String
    let name: String
    let isDirectory: Bool
    let depth: Int
}

/// Per-session store managing open file buffers, workspace directory browsing,
/// `⌘P` file indexing, live disk-change watching, and `read_file` tracking.
@MainActor
final class PCodeInspectorFilesStore: ObservableObject {
    static let maxEditableFileBytes: Int64 = 5 * 1024 * 1024 // 5 MB
    static let maxOpenBuffers: Int = 24
    static let maxRecentlyRead: Int = 30
    static let maxQuickOpenFiles: Int = 20_000

    static let showFileTreeKey = "PCodeInspectorShowFileTree"
    static let followReadsKey = "PCodeInspectorFollowReads"

    static let ignoredDirectoryNames: Set<String> = [
        ".git",
        ".hg",
        ".svn",
        ".venv",
        "venv",
        "node_modules",
        ".build",
        "DerivedData",
        "__pycache__",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        ".next",
        ".nuxt",
        "dist",
        "target",
        ".worktrees",
    ]

    static let ignoredFileNames: Set<String> = [
        ".DS_Store",
        ".git",
    ]

    @Published private(set) var sessionDirectory: String
    @Published private(set) var openBuffers: [PCodeOpenFileBuffer] = []
    @Published var activeFilePath: String? {
        didSet {
            if oldValue != activeFilePath {
                armWatcherForActiveFile()
            }
        }
    }
    @Published private(set) var recentlyReadPaths: [String] = []
    @Published private(set) var treeEntries: [PCodeFileTreeEntry] = []
    @Published private(set) var expandedDirectories: Set<String> = []
    @Published private(set) var allProjectFiles: [String] = []
    @Published private(set) var isIndexingFiles: Bool = false
    @Published private(set) var statusMessage: String?

    @Published var showFileTree: Bool = UserDefaults.standard.bool(forKey: PCodeInspectorFilesStore.showFileTreeKey) {
        didSet {
            UserDefaults.standard.set(showFileTree, forKey: Self.showFileTreeKey)
        }
    }

    @Published var followReads: Bool = UserDefaults.standard.bool(forKey: PCodeInspectorFilesStore.followReadsKey) {
        didSet {
            UserDefaults.standard.set(followReads, forKey: Self.followReadsKey)
        }
    }

    /// Monotonically incremented when disk content reloads cleanly so the editor view can sync.
    @Published private(set) var reloadGeneration: Int = 0

    private var fileWatcher: DispatchSourceFileSystemObject?
    private var watcherDebounceWork: DispatchWorkItem?
    private var indexTask: Task<Void, Never>?
    private var indexGeneration: Int = 0
    private var statusClearWork: DispatchWorkItem?
    private var hasLoadedInitialTree = false

    init(sessionDirectory: String) {
        self.sessionDirectory = Self.standardizeDirectory(sessionDirectory)
    }

    deinit {
        watcherDebounceWork?.cancel()
        statusClearWork?.cancel()
        fileWatcher?.cancel()
        fileWatcher = nil
        indexTask?.cancel()
    }

    var activeBuffer: PCodeOpenFileBuffer? {
        guard let activeFilePath else { return nil }
        return openBuffers.first(where: { $0.path == activeFilePath })
    }

    var dirtyBufferCount: Int {
        openBuffers.filter(\.isDirty).count
    }

    func updateSessionDirectory(_ directory: String) {
        let standardized = Self.standardizeDirectory(directory)
        guard !standardized.isEmpty, standardized != sessionDirectory else { return }
        sessionDirectory = standardized
        expandedDirectories.removeAll()
        hasLoadedInitialTree = false
        indexTask?.cancel()
        indexTask = nil
        indexGeneration &+= 1
        isIndexingFiles = false
        allProjectFiles = []
        refreshTree()
    }

    func ensureTreeAndIndexLoaded() {
        if !hasLoadedInitialTree {
            refreshTree()
        }
        if allProjectFiles.isEmpty && !isIndexingFiles {
            refreshFileIndex()
        }
    }

    // MARK: - Host Event Integration

    func apply(_ event: PCodeHostEvent) {
        apply(event, isHistoryReplay: false)
    }

    private func apply(_ event: PCodeHostEvent, isHistoryReplay: Bool) {
        switch event {
        case .toolStarted(_, _, let name, let detail, _, _, _):
            handlePossibleReadFileTool(name: name, detail: detail, allowAutoFollow: !isHistoryReplay)
        case .toolSummary(_, _, let name, let detail, _, _, _, _, _, _, _):
            handlePossibleReadFileTool(name: name, detail: detail, allowAutoFollow: false)
            if !isHistoryReplay {
                recheckActiveFileOnDisk()
            }
        case .editCompleted:
            if !isHistoryReplay {
                recheckAllOpenFilesOnDisk()
            }
        case .history(let events):
            for nested in events {
                apply(nested, isHistoryReplay: true)
            }
        default:
            break
        }
    }

    private func handlePossibleReadFileTool(name: String, detail: String, allowAutoFollow: Bool) {
        guard PCodeReadFileReference.isReadFileTool(name),
              let ref = PCodeReadFileReference.parse(detail: detail)
        else { return }

        let resolved = resolvePath(ref.path)
        guard FileManager.default.fileExists(atPath: resolved) else { return }
        var isDir: ObjCBool = false
        guard FileManager.default.fileExists(atPath: resolved, isDirectory: &isDir), !isDir.boolValue else {
            return
        }

        recordRecentlyRead(resolved)

        if allowAutoFollow && followReads {
            // Never yank away from an active buffer that has unsaved user edits.
            if activeBuffer?.isDirty != true {
                openFile(path: resolved, line: ref.startLine, endLine: ref.endLine)
            }
        }
    }

    private func recordRecentlyRead(_ absolutePath: String) {
        recentlyReadPaths.removeAll(where: { $0 == absolutePath })
        recentlyReadPaths.insert(absolutePath, at: 0)
        if recentlyReadPaths.count > Self.maxRecentlyRead {
            recentlyReadPaths.removeLast(recentlyReadPaths.count - Self.maxRecentlyRead)
        }
    }

    // MARK: - Opening, Editing, Saving & Reloading

    @discardableResult
    func openFile(path rawPath: String, line: Int? = nil, endLine: Int? = nil) -> PCodeOpenFileBuffer? {
        let resolved = resolvePath(rawPath)
        guard !resolved.isEmpty else { return nil }

        let normalizedLine = line.map { max(1, $0) }
        let highlightRange: ClosedRange<Int>? = {
            guard let start = normalizedLine else { return nil }
            let end = max(start, endLine ?? start)
            return start...end
        }()

        if let idx = openBuffers.firstIndex(where: { $0.path == resolved }) {
            openBuffers[idx].highlightedLineRange = highlightRange
            if let normalizedLine {
                openBuffers[idx].pendingJumpLine = normalizedLine
                openBuffers[idx].pendingJumpToken = UUID()
            }
            activeFilePath = resolved
            recheckFileOnDisk(path: resolved)
            return openBuffers[idx]
        }

        let buffer = loadBufferFromDisk(
            absolutePath: resolved,
            highlightedLineRange: highlightRange,
            pendingJumpLine: normalizedLine
        )
        openBuffers.append(buffer)
        if openBuffers.count > Self.maxOpenBuffers {
            // Evict the oldest clean non-active buffer.
            if let evictIdx = openBuffers.firstIndex(where: { $0.path != resolved && !$0.isDirty }) {
                openBuffers.remove(at: evictIdx)
            }
        }
        activeFilePath = resolved
        return buffer
    }

    func updateText(_ newText: String, for path: String) {
        guard let idx = openBuffers.firstIndex(where: { $0.path == path }) else { return }
        guard openBuffers[idx].text != newText else { return }
        openBuffers[idx].text = newText
    }

    func updateSelection(_ lineRange: ClosedRange<Int>?, for path: String) {
        guard let idx = openBuffers.firstIndex(where: { $0.path == path }) else { return }
        if openBuffers[idx].selectedLineRange != lineRange {
            openBuffers[idx].selectedLineRange = lineRange
        }
    }

    @discardableResult
    func saveActiveFile() -> Bool {
        guard let activeFilePath else { return false }
        return saveFile(path: activeFilePath)
    }

    @discardableResult
    func saveFile(path: String) -> Bool {
        guard let idx = openBuffers.firstIndex(where: { $0.path == path }),
              openBuffers[idx].canEdit
        else { return false }

        let textToWrite = openBuffers[idx].text
        do {
            try textToWrite.write(toFile: path, atomically: true, encoding: .utf8)
            openBuffers[idx].diskText = textToWrite
            openBuffers[idx].diskChangedConflict = false
            openBuffers[idx].isMissingOnDisk = false
            openBuffers[idx].lastLoadedAt = Date()
            openBuffers[idx].fileSize = Int64(textToWrite.utf8.count)
            // Atomic writes replace the inode; re-arm the watcher on the new descriptor.
            if activeFilePath == path {
                armWatcherForActiveFile()
            }
            showTransientStatus("Saved \(openBuffers[idx].basename)")
            return true
        } catch {
            showTransientStatus("Save failed: \(error.localizedDescription)")
            return false
        }
    }

    func reloadFile(path: String) {
        guard let idx = openBuffers.firstIndex(where: { $0.path == path }) else { return }
        let preservedHighlight = openBuffers[idx].highlightedLineRange
        let fresh = loadBufferFromDisk(
            absolutePath: path,
            highlightedLineRange: preservedHighlight,
            pendingJumpLine: nil
        )
        openBuffers[idx] = fresh
        reloadGeneration &+= 1
        if activeFilePath == path {
            armWatcherForActiveFile()
        }
        showTransientStatus("Reloaded \(fresh.basename)")
    }

    func dismissConflict(for path: String) {
        guard let idx = openBuffers.firstIndex(where: { $0.path == path }) else { return }
        openBuffers[idx].diskChangedConflict = false
    }

    func selectFile(path: String) {
        guard openBuffers.contains(where: { $0.path == path }) else { return }
        activeFilePath = path
        recheckFileOnDisk(path: path)
    }

    func closeFile(path: String) {
        guard let idx = openBuffers.firstIndex(where: { $0.path == path }) else { return }
        openBuffers.remove(at: idx)
        if activeFilePath == path {
            if openBuffers.isEmpty {
                activeFilePath = nil
            } else {
                let nextIdx = min(idx, openBuffers.count - 1)
                let nextPath = openBuffers[nextIdx].path
                activeFilePath = nextPath
                recheckFileOnDisk(path: nextPath)
            }
        }
    }

    // MARK: - Directory Tree

    func refreshTree() {
        hasLoadedInitialTree = true
        var result: [PCodeFileTreeEntry] = []
        buildTreeEntries(directory: sessionDirectory, depth: 0, into: &result)
        treeEntries = result
    }

    func toggleDirectory(_ path: String) {
        if expandedDirectories.contains(path) {
            expandedDirectories.remove(path)
        } else {
            expandedDirectories.insert(path)
        }
        refreshTree()
    }

    private func buildTreeEntries(directory: String, depth: Int, into result: inout [PCodeFileTreeEntry]) {
        guard depth < 12 else { return }
        let fm = FileManager.default
        let dirURL = URL(fileURLWithPath: directory, isDirectory: true)
        guard let urls = try? fm.contentsOfDirectory(
            at: dirURL,
            includingPropertiesForKeys: [.isDirectoryKey, .isSymbolicLinkKey],
            options: []
        ) else { return }

        struct RawItem {
            let path: String
            let name: String
            let isDir: Bool
        }

        var items: [RawItem] = []
        items.reserveCapacity(urls.count)
        for url in urls {
            let name = url.lastPathComponent
            if Self.ignoredFileNames.contains(name) { continue }
            let values = try? url.resourceValues(forKeys: [.isDirectoryKey, .isSymbolicLinkKey])
            let isDir = values?.isDirectory ?? false
            if isDir && Self.ignoredDirectoryNames.contains(name) { continue }
            items.append(RawItem(path: url.standardizedFileURL.path, name: name, isDir: isDir))
        }

        items.sort { lhs, rhs in
            if lhs.isDir != rhs.isDir {
                return lhs.isDir
            }
            return lhs.name.localizedStandardCompare(rhs.name) == .orderedAscending
        }

        for item in items {
            let rel = displayPath(for: item.path)
            result.append(PCodeFileTreeEntry(
                id: item.path,
                path: item.path,
                relativePath: rel,
                name: item.name,
                isDirectory: item.isDir,
                depth: depth
            ))
            if item.isDir && expandedDirectories.contains(item.path) {
                buildTreeEntries(directory: item.path, depth: depth + 1, into: &result)
            }
        }
    }

    // MARK: - Quick Open File Index

    func refreshFileIndex() {
        indexTask?.cancel()
        let root = sessionDirectory
        guard !root.isEmpty else {
            isIndexingFiles = false
            return
        }
        indexGeneration &+= 1
        let gen = indexGeneration
        isIndexingFiles = true
        indexTask = Task.detached(priority: .utility) { [weak self] in
            let files = Self.collectProjectFiles(in: root)
            guard !Task.isCancelled else { return }
            await MainActor.run {
                guard let self, self.sessionDirectory == root, self.indexGeneration == gen else { return }
                self.allProjectFiles = files
                self.isIndexingFiles = false
            }
        }
    }

    func filteredQuickOpenFiles(query: String, limit: Int = 80) -> [String] {
        let terms = PCodePickMatch.terms(query)
        guard !terms.isEmpty else {
            // Show recently read + open files first when query is empty.
            var prioritized: [String] = []
            var seen = Set<String>()
            for path in openBuffers.map(\.displayPath) + recentlyReadPaths.map({ displayPath(for: $0) }) {
                if seen.insert(path).inserted {
                    prioritized.append(path)
                }
            }
            for file in allProjectFiles {
                if prioritized.count >= limit { break }
                if seen.insert(file).inserted {
                    prioritized.append(file)
                }
            }
            return Array(prioritized.prefix(limit))
        }

        var basenameHits: [String] = []
        var pathHits: [String] = []
        for file in allProjectFiles {
            let foldedPath = PCodePickMatch.casefold(file)
            let basename = (file as NSString).lastPathComponent
            let foldedBase = PCodePickMatch.casefold(basename)
            if PCodePickMatch.matches([foldedBase], terms: terms, fuzzy: true) {
                basenameHits.append(file)
            } else if PCodePickMatch.matches([foldedPath], terms: terms, fuzzy: true) {
                pathHits.append(file)
            }
            if basenameHits.count >= limit && pathHits.count >= limit {
                break
            }
        }
        return Array((basenameHits + pathHits).prefix(limit))
    }

    nonisolated private static func collectProjectFiles(in root: String) -> [String] {
        if let gitFiles = collectGitFiles(in: root), !gitFiles.isEmpty {
            return Array(gitFiles.prefix(maxQuickOpenFiles))
        }
        return collectFileManagerFiles(in: root)
    }

    nonisolated private static func collectGitFiles(in root: String) -> [String]? {
        guard !Task.isCancelled else { return nil }
        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/usr/bin/git")
        process.arguments = ["-C", root, "ls-files", "--cached", "--others", "--exclude-standard", "-z"]
        let outPipe = Pipe()
        process.standardOutput = outPipe
        process.standardError = FileHandle.nullDevice
        do {
            try process.run()
        } catch {
            return nil
        }
        let data = outPipe.fileHandleForReading.readDataToEndOfFile()
        if Task.isCancelled {
            if process.isRunning { process.terminate() }
            return nil
        }
        process.waitUntilExit()
        guard process.terminationStatus == 0 else { return nil }
        var files: [String] = []
        data.split(separator: 0).forEach { slice in
            if let path = String(data: Data(slice), encoding: .utf8), !path.isEmpty {
                files.append(path)
            }
        }
        return files
    }

    nonisolated private static func collectFileManagerFiles(in root: String) -> [String] {
        guard !Task.isCancelled else { return [] }
        let fm = FileManager.default
        let rootURL = URL(fileURLWithPath: root, isDirectory: true)
        guard let enumerator = fm.enumerator(
            at: rootURL,
            includingPropertiesForKeys: [.isDirectoryKey],
            options: [.skipsPackageDescendants]
        ) else { return [] }

        var files: [String] = []
        let prefix = root.hasSuffix("/") ? root : root + "/"
        while let url = enumerator.nextObject() as? URL {
            if Task.isCancelled { return [] }
            let name = url.lastPathComponent
            let isDir = (try? url.resourceValues(forKeys: [.isDirectoryKey]).isDirectory) ?? false
            if isDir {
                if ignoredDirectoryNames.contains(name) {
                    enumerator.skipDescendants()
                }
                continue
            }
            if ignoredFileNames.contains(name) { continue }
            let full = url.standardizedFileURL.path
            let rel = full.hasPrefix(prefix) ? String(full.dropFirst(prefix.count)) : full
            files.append(rel)
            if files.count >= maxQuickOpenFiles { break }
        }
        return files.sorted()
    }

    // MARK: - File Watching & Disk Sync

    private func teardownWatcher() {
        watcherDebounceWork?.cancel()
        watcherDebounceWork = nil
        // Descriptor is closed asynchronously inside the source's cancel handler.
        fileWatcher?.cancel()
        fileWatcher = nil
    }

    private func armWatcherForActiveFile() {
        teardownWatcher()
        guard let path = activeFilePath, !path.isEmpty else { return }

        let fd = Darwin.open(path, O_EVTONLY)
        guard fd >= 0 else { return }

        let source = DispatchSource.makeFileSystemObjectSource(
            fileDescriptor: fd,
            eventMask: [.write, .rename, .delete, .extend, .attrib],
            queue: DispatchQueue.global(qos: .utility)
        )
        source.setEventHandler { [weak self, weak source] in
            let flags = source?.data ?? []
            let inodeChanged = flags.contains(.rename) || flags.contains(.delete)
            Task { @MainActor [weak self] in
                self?.scheduleDiskRecheck(for: path, rearmWatcher: inodeChanged)
            }
        }
        source.setCancelHandler {
            Darwin.close(fd)
        }
        fileWatcher = source
        source.resume()
    }

    private func scheduleDiskRecheck(for path: String, rearmWatcher: Bool) {
        watcherDebounceWork?.cancel()
        let work = DispatchWorkItem { [weak self] in
            Task { @MainActor [weak self] in
                self?.recheckFileOnDisk(path: path, rearmWatcher: rearmWatcher)
            }
        }
        watcherDebounceWork = work
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.15, execute: work)
    }

    func recheckActiveFileOnDisk() {
        guard let activeFilePath else { return }
        recheckFileOnDisk(path: activeFilePath)
    }

    func recheckAllOpenFilesOnDisk() {
        let paths = openBuffers.map(\.path)
        for path in paths {
            recheckFileOnDisk(path: path)
        }
    }

    func recheckFileOnDisk(path: String, rearmWatcher: Bool = false) {
        guard let idx = openBuffers.firstIndex(where: { $0.path == path }) else { return }
        let current = openBuffers[idx]
        guard current.canEdit else { return }

        guard FileManager.default.fileExists(atPath: path) else {
            openBuffers[idx].isMissingOnDisk = true
            if current.isDirty {
                openBuffers[idx].diskChangedConflict = true
            }
            return
        }

        if openBuffers[idx].isMissingOnDisk {
            openBuffers[idx].isMissingOnDisk = false
        }

        let resolvedURL = URL(fileURLWithPath: path).resolvingSymlinksInPath()
        let resolvedSize = (try? resolvedURL.resourceValues(forKeys: [.fileSizeKey]).fileSize).map(Int64.init) ?? 0
        guard resolvedSize <= Self.maxEditableFileBytes,
              let data = try? Data(contentsOf: resolvedURL),
              Int64(data.count) <= Self.maxEditableFileBytes,
              !Self.looksBinary(data),
              let freshText = Self.decodeText(data)
        else {
            if rearmWatcher && activeFilePath == path {
                armWatcherForActiveFile()
            }
            return
        }

        if freshText == current.diskText {
            if rearmWatcher && activeFilePath == path {
                armWatcherForActiveFile()
            }
            return
        }

        if current.isDirty {
            openBuffers[idx].diskChangedConflict = true
        } else {
            openBuffers[idx].text = freshText
            openBuffers[idx].diskText = freshText
            openBuffers[idx].diskChangedConflict = false
            openBuffers[idx].isMissingOnDisk = false
            openBuffers[idx].lastLoadedAt = Date()
            openBuffers[idx].fileSize = Int64(data.count)
            reloadGeneration &+= 1
        }
        if activeFilePath == path {
            armWatcherForActiveFile()
        }
    }

    // MARK: - Path & File Loading Helpers

    func resolvePath(_ rawPath: String) -> String {
        let trimmed = rawPath.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return "" }
        let expanded = (trimmed as NSString).expandingTildeInPath
        if (expanded as NSString).isAbsolutePath {
            return URL(fileURLWithPath: expanded).standardizedFileURL.path
        }
        return URL(fileURLWithPath: sessionDirectory, isDirectory: true)
            .appendingPathComponent(expanded)
            .standardizedFileURL
            .path
    }

    func displayPath(for absolutePath: String) -> String {
        let prefix = sessionDirectory.hasSuffix("/") ? sessionDirectory : sessionDirectory + "/"
        if absolutePath.hasPrefix(prefix) {
            return String(absolutePath.dropFirst(prefix.count))
        }
        return absolutePath
    }

    private func loadBufferFromDisk(
        absolutePath: String,
        highlightedLineRange: ClosedRange<Int>?,
        pendingJumpLine: Int?
    ) -> PCodeOpenFileBuffer {
        let display = displayPath(for: absolutePath)
        let basename = (absolutePath as NSString).lastPathComponent
        let fm = FileManager.default

        var isDir: ObjCBool = false
        guard fm.fileExists(atPath: absolutePath, isDirectory: &isDir) else {
            return PCodeOpenFileBuffer(
                id: absolutePath,
                path: absolutePath,
                displayPath: display,
                basename: basename,
                text: "",
                diskText: "",
                errorMessage: "File does not exist on disk."
            )
        }
        if isDir.boolValue {
            return PCodeOpenFileBuffer(
                id: absolutePath,
                path: absolutePath,
                displayPath: display,
                basename: basename,
                text: "",
                diskText: "",
                errorMessage: "Cannot open a directory in the editor."
            )
        }

        let resolvedURL = URL(fileURLWithPath: absolutePath).resolvingSymlinksInPath()
        let size = (try? resolvedURL.resourceValues(forKeys: [.fileSizeKey]).fileSize).map(Int64.init)
            ?? ((try? fm.attributesOfItem(atPath: resolvedURL.path)[.size] as? NSNumber)?.int64Value ?? 0)
        if size > Self.maxEditableFileBytes {
            return PCodeOpenFileBuffer(
                id: absolutePath,
                path: absolutePath,
                displayPath: display,
                basename: basename,
                text: "",
                diskText: "",
                isTooLarge: true,
                fileSize: size
            )
        }

        guard let data = try? Data(contentsOf: resolvedURL) else {
            return PCodeOpenFileBuffer(
                id: absolutePath,
                path: absolutePath,
                displayPath: display,
                basename: basename,
                text: "",
                diskText: "",
                errorMessage: "Could not read file contents.",
                fileSize: size
            )
        }

        if Int64(data.count) > Self.maxEditableFileBytes {
            return PCodeOpenFileBuffer(
                id: absolutePath,
                path: absolutePath,
                displayPath: display,
                basename: basename,
                text: "",
                diskText: "",
                isTooLarge: true,
                fileSize: Int64(data.count)
            )
        }

        if Self.looksBinary(data) {
            return PCodeOpenFileBuffer(
                id: absolutePath,
                path: absolutePath,
                displayPath: display,
                basename: basename,
                text: "",
                diskText: "",
                isBinary: true,
                fileSize: Int64(data.count)
            )
        }

        guard let text = Self.decodeText(data) else {
            return PCodeOpenFileBuffer(
                id: absolutePath,
                path: absolutePath,
                displayPath: display,
                basename: basename,
                text: "",
                diskText: "",
                isBinary: true,
                fileSize: Int64(data.count)
            )
        }

        return PCodeOpenFileBuffer(
            id: absolutePath,
            path: absolutePath,
            displayPath: display,
            basename: basename,
            text: text,
            diskText: text,
            fileSize: Int64(data.count),
            lastLoadedAt: Date(),
            highlightedLineRange: highlightedLineRange,
            pendingJumpLine: pendingJumpLine
        )
    }

    private func showTransientStatus(_ message: String) {
        statusClearWork?.cancel()
        statusMessage = message
        let work = DispatchWorkItem { [weak self] in
            Task { @MainActor [weak self] in
                if self?.statusMessage == message {
                    self?.statusMessage = nil
                }
            }
        }
        statusClearWork = work
        DispatchQueue.main.asyncAfter(deadline: .now() + 2.5, execute: work)
    }

    private static func decodeText(_ data: Data) -> String? {
        String(data: data, encoding: .utf8) ?? String(data: data, encoding: .isoLatin1)
    }

    private static func looksBinary(_ data: Data) -> Bool {
        let sample = data.prefix(8192)
        return sample.contains(0)
    }

    private static func standardizeDirectory(_ directory: String) -> String {
        guard !directory.isEmpty else { return "" }
        return URL(fileURLWithPath: directory, isDirectory: true).standardizedFileURL.path
    }
}
