import Foundation

/// Unsent composer text on disk, in the same directory and format as pcode's
/// terminal editor (`src/pcode/drafts.py`), so either one recovers what the
/// other left: `$XDG_STATE_HOME/pcode/drafts` (default `~/.local/state`), one
/// `<time_ns>-<pid>-<serial>.txt` per draft, written through a `.tmp` sibling,
/// mode 0600 in a 0700 directory. Loading keeps the newest `keep` files.
///
/// The terminal editor has no other prompt history on disk: a session's
/// history is the drafts earlier sessions left plus what it sends.
final class PCodeDraftStore {
    /// Seconds between autosaves (the caller's timer).
    static let interval: TimeInterval = 10
    /// Saved drafts a new session loads; older files are deleted then.
    static let keep = 50
    /// Seconds after which a `.tmp` write that never finished is litter.
    static let staleWrite: TimeInterval = 60

    static var defaultDirectory: URL {
        let state = ProcessInfo.processInfo.environment["XDG_STATE_HOME"].flatMap { $0.isEmpty ? nil : $0 }
            .map { URL(fileURLWithPath: $0) }
            ?? FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent(".local/state")
        return state.appendingPathComponent("pcode/drafts")
    }

    /// nil writes nothing; history still keeps unsent text in memory.
    let directory: URL?
    /// Saved text -> its file, for every draft loaded or written here.
    private(set) var files: [String: URL] = [:]
    /// Working slot -> the text last written for it.
    private var slots: [Int: String] = [:]
    private var serial = 0

    init(directory: URL? = PCodeDraftStore.defaultDirectory) {
        self.directory = directory
    }

    /// The saved drafts, oldest first, deleting all but the newest `keep`.
    func load(now: Date = Date()) -> [String] {
        guard let directory else { return [] }
        let manager = FileManager.default
        guard let names = try? manager.contentsOfDirectory(atPath: directory.path) else { return [] }
        var found: [(modified: Date, url: URL)] = []
        for name in names {
            let url = directory.appendingPathComponent(name)
            guard let modified = (try? manager.attributesOfItem(atPath: url.path))?[.modificationDate] as? Date else { continue }
            if url.pathExtension == "txt" {
                found.append((modified, url))
            } else if url.pathExtension == "tmp", now.timeIntervalSince(modified) > Self.staleWrite {
                Self.unlink(url)
            }
        }
        found.sort { $0.modified != $1.modified ? $0.modified < $1.modified : $0.url.path < $1.url.path }
        for item in found.dropLast(Self.keep) { Self.unlink(item.url) }
        var texts: [String] = []
        for item in found.suffix(Self.keep) {
            guard let data = try? Data(contentsOf: item.url), let text = String(data: data, encoding: .utf8),
                  !text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else { continue }
            if let older = files[text] {
                // The same text saved twice: the newer file stands for both.
                Self.unlink(older)
                texts.removeAll { $0 == text }
            }
            files[text] = item.url
            texts.append(text)
        }
        return texts
    }

    /// `Drafts.save`: write each edited slot's text that changed since it was
    /// last written, rewriting the slot's own file as the draft grows.
    func save(_ edited: [Int: String]) {
        guard directory != nil else { return }
        for index in edited.keys.sorted() {
            let text = edited[index]!
            let previous = slots[index]
            var path = files[text]
            // Another session may have deleted the file: loading trims the directory.
            if text == previous, let existing = path, FileManager.default.fileExists(atPath: existing.path) { continue }
            path = previous.flatMap { files[$0] } ?? path ?? newPath()
            guard let path, write(text, to: path) else { continue }
            if let previous { files.removeValue(forKey: previous) }
            if let other = files[text], other != path { Self.unlink(other) }
            files[text] = path
            slots[index] = text
        }
    }

    /// Delete the files holding any of `texts` (sent ones), and what slot
    /// `index` last wrote.
    func discard(_ texts: [String], slot index: Int?) {
        var all = texts
        if let index, let written = slots[index] { all.append(written) }
        for text in all {
            if let path = files.removeValue(forKey: text) { Self.unlink(path) }
        }
    }

    /// Slots renumber when the history resets.
    func resetSlots() {
        slots.removeAll()
    }

    private func newPath() -> URL? {
        guard let directory else { return nil }
        defer { serial += 1 }
        let name = "\(clock_gettime_nsec_np(CLOCK_REALTIME))-\(getpid())-\(serial).txt"
        return directory.appendingPathComponent(name)
    }

    private func write(_ text: String, to path: URL) -> Bool {
        let temporary = path.deletingPathExtension().appendingPathExtension("tmp")
        do {
            try FileManager.default.createDirectory(
                at: path.deletingLastPathComponent(), withIntermediateDirectories: true,
                attributes: [.posixPermissions: 0o700])
            let fd = open(temporary.path, O_WRONLY | O_CREAT | O_TRUNC, 0o600)
            guard fd >= 0 else { return false }
            let handle = FileHandle(fileDescriptor: fd, closeOnDealloc: true)
            try handle.write(contentsOf: Data(text.utf8))
            try handle.close()
            guard rename(temporary.path, path.path) == 0 else {
                Self.unlink(temporary)
                return false
            }
            return true
        } catch {
            // Never the composer's problem: a full disk only costs the backup.
            Self.unlink(temporary)
            return false
        }
    }

    private static func unlink(_ url: URL) {
        try? FileManager.default.removeItem(at: url)
    }
}

/// Up/Down prompt recall for the native composer, as prompt_toolkit's buffer
/// does it for pcode's terminal editor, with `drafts.py`'s rules for unsent
/// text.
///
/// There is one working copy per history entry plus the new prompt at the
/// bottom. Moving away from a line keeps its edits there, so the text in
/// progress is waiting at the bottom when Down returns to it. Sending or
/// clearing resets the copies; before that, every edited line that was not
/// sent is added to the history, where Up finds it again, and is saved as a
/// draft file so a later session (native or terminal) finds it too.
final class PCodePromptHistory {
    /// Sent prompts and kept drafts, oldest first.
    private(set) var entries: [String]
    /// The working copies: `entries` then the new prompt.
    private var working: [String]
    /// Which working copy is shown; the last is the new prompt.
    private(set) var index: Int
    let drafts: PCodeDraftStore?

    /// `entries` seed the history; with `drafts`, its saved drafts come first.
    init(entries: [String] = [], drafts: PCodeDraftStore? = nil) {
        self.drafts = drafts
        self.entries = (drafts?.load() ?? []) + entries
        working = self.entries + [""]
        index = self.entries.count
    }

    /// The shown line's text.
    var text: String { working[index] }

    /// True on the new prompt, not a recalled entry.
    var isAtBottom: Bool { index == working.count - 1 }

    /// The previous entry, keeping `current` as the shown line's edit; nil at the top.
    func up(current: String) -> String? {
        guard index > 0 else { return nil }
        working[index] = current
        index -= 1
        return working[index]
    }

    /// The next entry (finally the text in progress); nil at the bottom.
    func down(current: String) -> String? {
        guard index < working.count - 1 else { return nil }
        working[index] = current
        index += 1
        return working[index]
    }

    /// `drafts.edited`: unsent texts by working slot, leaving out `skip`.
    func edited(current: String, skip: Int? = nil) -> [Int: String] {
        working[index] = current
        var result: [Int: String] = [:]
        for (slot, line) in working.enumerated() where slot != skip {
            if line != origin(slot), !line.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
                result[slot] = line
            }
        }
        return result
    }

    /// Write unsent text to draft files. Call every `PCodeDraftStore.interval`
    /// seconds while editing, and when the pane loses the composer.
    func saveDrafts(current: String) {
        drafts?.save(edited(current: current))
    }

    /// `Drafts.accept`: record `text` (the shown line) as sent. The other
    /// unsent lines join the history ahead of it; the drafts that held this
    /// one are deleted. Call once the send is accepted.
    func send(_ text: String) {
        let slot = index
        let recalled = origin(slot)
        remember(current: text, skip: slot)
        if !text.isEmpty, entries.last != text { entries.append(text) }
        drafts?.discard([text, recalled], slot: slot)
        drafts?.save(edited(current: text, skip: slot))
        drafts?.resetSlots()
        reset()
    }

    /// `Drafts.keep`: the composer is cleared unsent (Ctrl+C); everything in
    /// it is kept, in the history and as drafts.
    func clear(current: String) {
        saveDrafts(current: current)
        remember(current: current)
        drafts?.resetSlots()
        reset()
    }

    /// The unsent texts join the history; safe to repeat.
    private func remember(current: String, skip: Int? = nil) {
        var known = Set(entries)
        for slot in edited(current: current, skip: skip).keys.sorted() {
            let line = working[slot]
            if known.insert(line).inserted { entries.append(line) }
        }
    }

    /// The entry a working copy started as; the new prompt started empty.
    /// Entries added since the copies were made sit past them, as in `drafts.origins`.
    private func origin(_ slot: Int) -> String {
        slot < working.count - 1 ? entries[slot] : ""
    }

    private func reset() {
        working = entries + [""]
        index = entries.count
    }
}
