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
/// `didUpdate` is posted on the main queue when a refresh changes the listing, so
/// an open menu can re-query.
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

    /// A listed path with what ranking needs, folded once per refresh rather
    /// than once per keystroke.
    struct Entry: Sendable {
        let path: String
        /// Casefolded path and basename, as UTF-8.
        let folded: [UInt8]
        let foldedName: [UInt8]
        /// Characters in the path, for the shorter-first tie-break.
        let length: Int
        /// `/` count, for the empty query's shallowest-first order.
        let depth: Int

        init(_ path: String) {
            self.path = path
            folded = Array(PCodePickMatch.casefold(path).utf8)
            let slash = folded.lastIndex(of: UInt8(ascii: "/")).map { $0 + 1 } ?? 0
            foldedName = Array(folded[slash...])
            length = path.count
            depth = path.utf8.reduce(0) { $1 == UInt8(ascii: "/") ? $0 + 1 : $0 }
        }
    }

    /// A rankable listing: entries in listing order, and their indices
    /// shallowest first for the empty query.
    struct Listing: Sendable {
        let entries: [Entry]
        let shallowest: [Int]

        init(_ paths: [String]) {
            let entries = paths.map(Entry.init)
            self.entries = entries
            shallowest = entries.indices.sorted { lhs, rhs in
                let a = entries[lhs], b = entries[rhs]
                return a.depth != b.depth ? a.depth < b.depth : PCodeFileIndex.pythonOrder(a.path, b.path)
            }
        }

        var paths: [String] { entries.map(\.path) }
    }

    /// What `git ls-files` gave.
    enum GitResult: Equatable {
        case listed([String])
        /// git did not run or refused: not a checkout, or no command-line tools.
        case unavailable
        case timedOut
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
    /// Posted (object: the index) on the main queue after a refresh that changed the
    /// listing. Indexes are shared per root, so every composer there observes this.
    static let didUpdate = Notification.Name("PCodeFileIndexDidUpdate")

    /// Runs on the main queue after a refresh that changed the listing.
    var onUpdate: (() -> Void)? {
        get { lock.withLock { _onUpdate } }
        set { lock.withLock { _onUpdate = newValue } }
    }

    private let lock = NSLock()
    private var _listing = Listing([])
    private var _loadedAt: Date?
    private var _refreshing = false
    /// Bumped as each refresh starts; a result older than the one applied is dropped.
    private var _started = 0
    private var _applied = 0
    /// git could not list this root. Not retried for this index: without the
    /// command-line tools, `/usr/bin/git` offers an install dialog on every run.
    /// A timeout is retried.
    private var _gitUnavailable = false
    private var _onUpdate: (() -> Void)?
    private static let queue = DispatchQueue(label: "pcode.file-index", qos: .userInitiated, attributes: .concurrent)

    init(root: URL, cacheSeconds: TimeInterval = PCodeFileIndex.cacheSeconds) {
        self.root = Self.canonical(root)
        self.cacheSeconds = cacheSeconds
    }

    /// The workspace as Python's `Path.resolve()` names it, so `/tmp/x` and
    /// `/private/tmp/x` are one workspace. `realpath(3)`, not Foundation's
    /// `resolvingSymlinksInPath`, which strips a leading `/private` where
    /// Python keeps it. A path that does not exist is only standardized.
    static func canonical(_ url: URL) -> URL {
        let standardized = url.standardizedFileURL
        guard let resolved = realpath(standardized.path, nil) else { return standardized }
        defer { free(resolved) }
        return URL(fileURLWithPath: String(cString: resolved))
    }

    // MARK: Per-workspace cache

    private static let sharedLock = NSLock()
    nonisolated(unsafe) private static var sharedIndexes: [String: PCodeFileIndex] = [:]

    /// The index for `workspace`, created (and its first listing started) on first use.
    static func shared(for workspace: String) -> PCodeFileIndex {
        let url = canonical(URL(fileURLWithPath: workspace))
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
    var paths: [String] { lock.withLock { _listing }.paths }

    /// True once a listing has been read.
    var isLoaded: Bool { lock.withLock { _loadedAt != nil } }

    /// Start a background refresh unless one is running or the listing is fresh:
    /// younger than `maxAge` (default `cacheSeconds`).
    func refreshIfStale(now: Date = Date(), maxAge: TimeInterval? = nil) {
        let start: (generation: Int, useGit: Bool)? = lock.withLock {
            if _refreshing { return nil }
            if let loaded = _loadedAt, now.timeIntervalSince(loaded) < (maxAge ?? cacheSeconds) { return nil }
            _refreshing = true
            _started += 1
            return (_started, !_gitUnavailable)
        }
        guard let start else { return }
        Self.queue.async { [self] in
            let result = Self.listing(root: root, useGit: start.useGit)
            if let callback = apply(result, generation: start.generation, background: true) {
                DispatchQueue.main.async(execute: callback)
            }
        }
    }

    /// Read the listing now, on the calling thread (keep it off main: it runs
    /// git). A background refresh already running cannot overwrite it.
    func refreshNow() {
        let start: (generation: Int, useGit: Bool) = lock.withLock {
            _started += 1
            return (_started, !_gitUnavailable)
        }
        let result = Self.listing(root: root, useGit: start.useGit)
        if let callback = apply(result, generation: start.generation, background: false) {
            DispatchQueue.main.async(execute: callback)
        }
    }

    /// Store a listing unless a newer one landed first; returns the update
    /// callback when what is listed changed.
    private func apply(
        _ result: (paths: [String], gitUnavailable: Bool), generation: Int, background: Bool
    ) -> (() -> Void)? {
        // Folding 20k paths is the slow part, so it happens outside the lock.
        let unchanged = lock.withLock { _listing }.paths == result.paths
        let listing = unchanged ? nil : Listing(result.paths)
        return lock.withLock {
            if background { _refreshing = false }
            if result.gitUnavailable { _gitUnavailable = true }
            guard generation > _applied else { return nil }
            _applied = generation
            _loadedAt = Date()
            guard let listing, _listing.paths != result.paths else { return nil }
            _listing = listing
            let callback = _onUpdate
            return { [self] in
                callback?()
                NotificationCenter.default.post(name: Self.didUpdate, object: self)
            }
        }
    }

    /// git's listing, else the walk; an empty git listing (a wholly ignored
    /// directory) falls through to the walk as pcode's does.
    static func listing(root: URL) -> [String] {
        listing(root: root, useGit: true).paths
    }

    /// `listing(root:)`, skipping git when `useGit` is false, and whether git
    /// was unavailable (so the caller stops asking).
    static func listing(root: URL, useGit: Bool) -> (paths: [String], gitUnavailable: Bool) {
        guard useGit else { return (walkedFiles(root: root), true) }
        switch git(root: root, timeout: gitTimeout) {
        case .listed(let listed) where !listed.isEmpty:
            return (Array(listed.prefix(maxFiles)), false)
        case .listed, .timedOut:
            return (walkedFiles(root: root), false)
        case .unavailable:
            return (walkedFiles(root: root), true)
        }
    }

    /// Tracked and untracked files below root, or nil outside a checkout or on timeout.
    static func gitFiles(root: URL, timeout: TimeInterval = gitTimeout) -> [String]? {
        if case .listed(let paths) = git(root: root, timeout: timeout) { return paths }
        return nil
    }

    static func git(root: URL, timeout: TimeInterval = gitTimeout) -> GitResult {
        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/usr/bin/git")
        process.arguments = ["-C", root.path, "ls-files", "--cached", "--others", "--exclude-standard", "-z"]
        let output = Pipe()
        process.standardOutput = output
        process.standardError = FileHandle.nullDevice
        process.standardInput = FileHandle.nullDevice
        do { try process.run() } catch { return .unavailable }
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
            return .timedOut
        }
        guard process.terminationStatus == 0 else { return .unavailable }
        // A file staged for deletion is still cached and can be listed twice;
        // keep the first of each.
        var seen = Set<String>()
        var paths: [String] = []
        for chunk in box.data.split(separator: 0) {
            guard let path = String(data: Data(chunk), encoding: .utf8), !path.isEmpty,
                  seen.insert(path).inserted else { continue }
            paths.append(path)
        }
        return .listed(paths)
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
    /// Starts a background refresh when the listing is stale.
    ///
    /// Safe on any thread, including main: it copies the listing (folded once
    /// per refresh) under the lock and ranks it with byte comparisons, a few
    /// milliseconds for `maxFiles` paths. `matches(_:limit:completion:)` ranks
    /// off main instead.
    func matches(_ query: String, limit: Int = PCodeFileIndex.maxCompletions) -> [Item] {
        refreshIfStale()
        let listing = lock.withLock { _listing }
        return Self.rank(listing, query: query, limit: limit).map(Item.init(path:))
    }

    /// `matches`, ranked on a background queue; `completion` runs on main.
    func matches(_ query: String, limit: Int = PCodeFileIndex.maxCompletions, completion: @escaping @Sendable ([Item]) -> Void) {
        Self.queue.async { [self] in
            let items = matches(query, limit: limit)
            DispatchQueue.main.async { completion(items) }
        }
    }

    /// `rank` over plain paths, folding them first (tests and one-off callers).
    static func rank(_ paths: [String], query: String, limit: Int) -> [String] {
        rank(Listing(paths), query: query, limit: limit)
    }

    /// pcode's ranking, widened with `PCodePickMatch`'s word-prefix matching.
    ///
    /// A substring hit in the basename outranks one in the directory, an
    /// earlier hit outranks a later one, then shorter paths win, so `ui.py`
    /// offers `src/pcode/ui.py` before `tests/test_ui.py`. Paths that only
    /// match fuzzily (`ed_ui` for `edit_ui.py`) follow, shortest first. An
    /// empty query lists the shallowest paths first. Hit positions are UTF-8
    /// offsets, which order like pcode's character offsets for ASCII paths.
    static func rank(_ listing: Listing, query: String, limit: Int) -> [String] {
        var trimmed = Substring(query)
        while trimmed.hasPrefix("@") { trimmed = trimmed.dropFirst() }
        if trimmed.hasPrefix("./") { trimmed = trimmed.dropFirst(2) }
        let folded = PCodePickMatch.casefold(String(trimmed))
        if folded.isEmpty {
            return listing.shallowest.prefix(limit).map { listing.entries[$0].path }
        }
        let needle = Array(folded.utf8)
        let terms = PCodePickMatch.terms(folded)
        // The query's word characters in order: what any fuzzy match needs at least.
        let wordBytes = needle.filter { (0x61...0x7A).contains($0) || (0x30...0x39).contains($0) }
        typealias Ranked = (tier: Int, position: Int, entry: Int)
        var ranked: [Ranked] = []
        for (index, entry) in listing.entries.enumerated() {
            if let position = find(needle, in: entry.foldedName) {
                ranked.append((0, position, index))
            } else if let position = find(needle, in: entry.folded) {
                ranked.append((1, position, index))
            } else if !wordBytes.isEmpty, isSubsequence(wordBytes, of: entry.folded),
                      PCodePickMatch.matches([String(decoding: entry.folded, as: UTF8.self)], terms: terms, fuzzy: true) {
                ranked.append((2, 0, index))
            }
        }
        ranked.sort {
            if $0.tier != $1.tier { return $0.tier < $1.tier }
            if $0.position != $1.position { return $0.position < $1.position }
            let a = listing.entries[$0.entry], b = listing.entries[$1.entry]
            if a.length != b.length { return a.length < b.length }
            return pythonOrder(a.path, b.path)
        }
        return ranked.prefix(limit).map { listing.entries[$0.entry].path }
    }

    private static func find(_ needle: [UInt8], in haystack: [UInt8]) -> Int? {
        guard !needle.isEmpty, needle.count <= haystack.count else { return nil }
        let first = needle[0]
        for start in 0...(haystack.count - needle.count)
        where haystack[start] == first && haystack[start..<(start + needle.count)].elementsEqual(needle) {
            return start
        }
        return nil
    }

    private static func isSubsequence(_ needle: [UInt8], of haystack: [UInt8]) -> Bool {
        var index = 0
        for byte in haystack where byte == needle[index] {
            index += 1
            if index == needle.count { return true }
        }
        return false
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
}
