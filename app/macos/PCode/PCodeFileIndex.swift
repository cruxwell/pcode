import Foundation

/// The workspace's files for the native composer's `@` references, ported from
/// `src/pcode/file_refs.py` so the app offers and inserts what the terminal
/// editor would: completing `@ui.py` inserts `./src/pcode/ui.py `.
///
/// The listing comes from `git ls-files` (tracked and untracked, honouring
/// ignore rules), or outside a checkout a bounded directory walk that skips
/// `ignoredDirectories` and hidden entries. pcode tries ripgrep between the
/// two; the app skips it, as the walk only runs off the main thread here.
///
/// Reads are cheap and never block: `matches` answers from the cached listing
/// and starts a background refresh once it is older than `cacheSeconds`.
/// `onUpdate` runs on the main queue when a refresh lands, so an open menu can
/// re-query.
final class PCodeFileIndex: @unchecked Sendable {
    /// One file a reference can name.
    struct Item: Equatable, Sendable {
        /// Workspace-relative, as listed: `src/pcode/ui.py`.
        let path: String

        /// What replaces the `@fragment`: `./path `, quoted when the path holds
        /// whitespace so its end stays unambiguous in prose.
        var insertion: String {
            let reference = "./\(path)"
            return reference.contains(where: \.isWhitespace) ? "\"\(reference)\" " : "\(reference) "
        }
    }

    /// The `@fragment` being typed before the caret.
    struct Token: Equatable, Sendable {
        /// UTF-16 range of `@fragment` in the text (what an `NSTextView` replaces).
        let range: NSRange
        /// The text after the `@`, possibly empty.
        let fragment: String
    }

    // Same values as file_refs.py.
    static let ignoredDirectories: Set<String> = [
        ".git", ".hg", ".svn", ".mypy_cache", ".pytest_cache", ".ruff_cache", ".tox",
        ".venv", "__pycache__", "build", "dist", "node_modules", "venv",
    ]
    static let maxFiles = 20_000
    static let maxCompletions = 50
    static let cacheSeconds: TimeInterval = 10
    static let gitTimeout: TimeInterval = 5

    let root: URL
    let cacheSeconds: TimeInterval
    /// Runs on the main queue after each refresh.
    var onUpdate: (() -> Void)? {
        get { lock.withLock { _onUpdate } }
        set { lock.withLock { _onUpdate = newValue } }
    }

    private let lock = NSLock()
    private var _paths: [String] = []
    private var _loadedAt: Date?
    private var _refreshing = false
    private var _onUpdate: (() -> Void)?
    private static let queue = DispatchQueue(label: "pcode.file-index", qos: .userInitiated, attributes: .concurrent)

    init(root: URL, cacheSeconds: TimeInterval = PCodeFileIndex.cacheSeconds) {
        self.root = root.standardizedFileURL
        self.cacheSeconds = cacheSeconds
    }

    // MARK: Per-workspace cache

    private static let sharedLock = NSLock()
    nonisolated(unsafe) private static var sharedIndexes: [String: PCodeFileIndex] = [:]

    /// The index for `workspace`, created (and its first listing started) on first use.
    static func shared(for workspace: String) -> PCodeFileIndex {
        let url = URL(fileURLWithPath: workspace).standardizedFileURL
        let index = sharedLock.withLock {
            if let existing = sharedIndexes[url.path] { return existing }
            let created = PCodeFileIndex(root: url)
            sharedIndexes[url.path] = created
            return created
        }
        index.refreshIfStale()
        return index
    }

    // MARK: Listing

    /// The cached listing, possibly empty before the first refresh lands.
    var paths: [String] { lock.withLock { _paths } }

    /// True once a listing has been read.
    var isLoaded: Bool { lock.withLock { _loadedAt != nil } }

    /// Start a background refresh unless one is running or the listing is fresh.
    func refreshIfStale(now: Date = Date()) {
        let start: Bool = lock.withLock {
            if _refreshing { return false }
            if let loaded = _loadedAt, now.timeIntervalSince(loaded) < cacheSeconds { return false }
            _refreshing = true
            return true
        }
        guard start else { return }
        Self.queue.async { [self] in
            let listed = Self.listing(root: root)
            let callback: (() -> Void)? = lock.withLock {
                _paths = listed
                _loadedAt = Date()
                _refreshing = false
                return _onUpdate
            }
            if let callback { DispatchQueue.main.async(execute: callback) }
        }
    }

    /// Read the listing now, on the calling thread. For tests and callers already off main.
    func refreshNow() {
        let listed = Self.listing(root: root)
        lock.withLock {
            _paths = listed
            _loadedAt = Date()
        }
    }

    /// git's listing, else the walk; an empty git listing (a wholly ignored
    /// directory) falls through to the walk as pcode's does.
    static func listing(root: URL) -> [String] {
        if let listed = gitFiles(root: root), !listed.isEmpty {
            return Array(listed.prefix(maxFiles))
        }
        return walkedFiles(root: root)
    }

    /// Tracked and untracked files below root, or nil outside a checkout or on timeout.
    static func gitFiles(root: URL, timeout: TimeInterval = gitTimeout) -> [String]? {
        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/usr/bin/git")
        process.arguments = ["-C", root.path, "ls-files", "--cached", "--others", "--exclude-standard", "-z"]
        let output = Pipe()
        process.standardOutput = output
        process.standardError = FileHandle.nullDevice
        process.standardInput = FileHandle.nullDevice
        do { try process.run() } catch { return nil }
        // Read concurrently so a large listing cannot fill the pipe and stall git.
        final class Box: @unchecked Sendable { var data = Data() }
        let box = Box()
        let done = DispatchGroup()
        done.enter()
        DispatchQueue.global(qos: .userInitiated).async {
            box.data = output.fileHandleForReading.readDataToEndOfFile()
            process.waitUntilExit()
            done.leave()
        }
        if done.wait(timeout: .now() + timeout) == .timedOut {
            process.terminate()
            return nil
        }
        guard process.terminationStatus == 0 else { return nil }
        // A file staged for deletion is still cached and can be listed twice;
        // keep the first of each.
        var seen = Set<String>()
        var paths: [String] = []
        for chunk in box.data.split(separator: 0) {
            guard let path = String(data: Data(chunk), encoding: .utf8), !path.isEmpty,
                  seen.insert(path).inserted else { continue }
            paths.append(path)
        }
        return paths
    }

    /// Files below root, as `os.walk` with sorted entries lists them in
    /// `_walked_files`: each directory's files, then its subdirectories in
    /// turn, skipping hidden names and `ignoredDirectories`; at most `limit`.
    static func walkedFiles(root: URL, limit: Int = maxFiles) -> [String] {
        var paths: [String] = []
        let manager = FileManager.default
        func walk(_ directory: URL, _ prefix: String) -> Bool {
            guard let names = try? manager.contentsOfDirectory(atPath: directory.path) else { return true }
            var files: [String] = []
            var subdirectories: [String] = []
            for name in names where !name.hasPrefix(".") {
                var isDirectory: ObjCBool = false
                // Follows links, as os.walk sorts entries; a linked directory
                // is then not entered, as os.walk does not follow links.
                guard manager.fileExists(atPath: directory.appendingPathComponent(name).path, isDirectory: &isDirectory) else {
                    continue
                }
                if isDirectory.boolValue {
                    if ignoredDirectories.contains(name) { continue }
                    let url = directory.appendingPathComponent(name)
                    let isLink = (try? url.resourceValues(forKeys: [.isSymbolicLinkKey]))?.isSymbolicLink ?? false
                    if !isLink { subdirectories.append(name) }
                } else {
                    files.append(name)
                }
            }
            for name in files.sorted(by: pythonOrder) {
                paths.append(prefix + name)
                if paths.count >= limit { return false }
            }
            for name in subdirectories.sorted(by: pythonOrder) {
                if !walk(directory.appendingPathComponent(name), prefix + name + "/") { return false }
            }
            return true
        }
        _ = walk(root, "")
        return paths
    }

    // MARK: Matching

    /// Paths for `query` (an `@fragment`, with or without the `@`), best first.
    /// Triggers a background refresh when the listing is stale.
    func matches(_ query: String, limit: Int = PCodeFileIndex.maxCompletions) -> [Item] {
        refreshIfStale()
        return Self.rank(paths, query: query, limit: limit).map(Item.init(path:))
    }

    /// pcode's ranking, widened with `PCodePickMatch`'s word-prefix matching.
    ///
    /// A substring hit in the basename outranks one in the directory, an
    /// earlier hit outranks a later one, then shorter paths win, so `ui.py`
    /// offers `src/pcode/ui.py` before `tests/test_ui.py`. Paths that only
    /// match fuzzily (`ed_ui` for `edit_ui.py`) follow, shortest first. An
    /// empty query lists the shallowest paths first.
    static func rank(_ paths: [String], query: String, limit: Int) -> [String] {
        var trimmed = Substring(query)
        while trimmed.hasPrefix("@") { trimmed = trimmed.dropFirst() }
        if trimmed.hasPrefix("./") { trimmed = trimmed.dropFirst(2) }
        let needle = PCodePickMatch.casefold(String(trimmed))
        if needle.isEmpty {
            let depths: [(depth: Int, path: String)] = paths.map { path in
                (path.utf8.filter { $0 == UInt8(ascii: "/") }.count, path)
            }
            let shallowest = depths.sorted { lhs, rhs in
                lhs.depth != rhs.depth ? lhs.depth < rhs.depth : pythonOrder(lhs.path, rhs.path)
            }
            return shallowest.prefix(limit).map(\.path)
        }
        let terms = PCodePickMatch.terms(needle)
        typealias Ranked = (tier: Int, position: Int, length: Int, path: String)
        var ranked: [Ranked] = []
        for path in paths {
            let folded = PCodePickMatch.casefold(path)
            let name = folded.split(separator: "/", omittingEmptySubsequences: false).last.map(String.init) ?? folded
            if let position = offset(of: needle, in: name) {
                ranked.append((0, position, path.count, path))
            } else if let position = offset(of: needle, in: folded) {
                ranked.append((1, position, path.count, path))
            } else if PCodePickMatch.matches([folded], terms: terms, fuzzy: true) {
                ranked.append((2, 0, path.count, path))
            }
        }
        ranked.sort {
            if $0.tier != $1.tier { return $0.tier < $1.tier }
            if $0.position != $1.position { return $0.position < $1.position }
            if $0.length != $1.length { return $0.length < $1.length }
            return pythonOrder($0.path, $1.path)
        }
        return ranked.prefix(limit).map(\.path)
    }

    /// The file's size ("1.2 KB"), shown beside a reference; "file" if unreadable.
    func describe(_ path: String) -> String {
        guard let attributes = try? FileManager.default.attributesOfItem(atPath: root.appendingPathComponent(path).path),
              let size = (attributes[.size] as? NSNumber)?.intValue else { return "file" }
        return Self.humanSize(size)
    }

    /// file_refs.human_size.
    static func humanSize(_ size: Int) -> String {
        if size < 1024 { return "\(size) B" }
        if size < 1024 * 1024 { return String(format: "%.1f KB", Double(size) / 1024) }
        return String(format: "%.1f MB", Double(size) / (1024 * 1024))
    }

    // MARK: Trigger

    /// The `@fragment` ending at `caret` (a UTF-16 offset), or nil.
    ///
    /// As `reference_fragment`: the last whitespace-delimited word before the
    /// caret must start with `@`, so emails and decorators never open the
    /// menu, and a caret right after whitespace closes it. Text after the
    /// caret does not matter; only the `@fragment` before it is replaced.
    static func activeToken(in text: String, caret: Int) -> Token? {
        let string = text as NSString
        guard caret > 0, caret <= string.length else { return nil }
        let before = string.substring(to: caret)
        guard let last = before.last, !last.isWhitespace else { return nil }
        let word = before.reversed().prefix { !$0.isWhitespace }
        guard word.last == "@" else { return nil }
        let wordText = String(word.reversed())
        let length = (wordText as NSString).length
        return Token(range: NSRange(location: caret - length, length: length), fragment: String(wordText.dropFirst()))
    }

    /// `text` with `token` replaced by `item`'s insertion, and the caret after it.
    static func apply(_ item: Item, token: Token, to text: String) -> (text: String, caret: Int) {
        let replaced = (text as NSString).replacingCharacters(in: token.range, with: item.insertion)
        return (replaced, token.range.location + (item.insertion as NSString).length)
    }

    // MARK: Helpers

    /// Python's code-point string order, which `sorted()` uses.
    static func pythonOrder(_ lhs: String, _ rhs: String) -> Bool {
        lhs.unicodeScalars.lexicographicallyPrecedes(rhs.unicodeScalars)
    }

    /// Character offset of the first `needle` in `haystack`, like `str.find`.
    private static func offset(of needle: String, in haystack: String) -> Int? {
        guard let range = haystack.range(of: needle, options: .literal) else { return nil }
        return haystack.distance(from: haystack.startIndex, to: range.lowerBound)
    }
}
