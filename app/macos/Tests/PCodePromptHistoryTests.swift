import Foundation
import Testing
@testable import Ghostty

/// Recall and drafts must behave, and land on disk, as pcode's terminal
/// editor's (`src/pcode/drafts.py`), which reads the same directory.
struct PCodePromptHistoryTests {
    private func temporaryDirectory() -> URL {
        FileManager.default.temporaryDirectory.appendingPathComponent("pcode-drafts-\(UUID().uuidString)")
    }

    private func files(_ directory: URL) -> [String] {
        ((try? FileManager.default.contentsOfDirectory(atPath: directory.path)) ?? []).sorted()
    }

    private func contents(_ directory: URL) -> Set<String> {
        Set(files(directory).compactMap { try? String(contentsOf: directory.appendingPathComponent($0), encoding: .utf8) })
    }

    // MARK: Navigation

    @Test func upAndDownKeepTheTextInProgress() {
        let history = PCodePromptHistory(entries: ["one", "two"])
        #expect(history.isAtBottom)
        #expect(history.up(current: "typing") == "two")
        #expect(history.up(current: "two") == "one")
        #expect(history.up(current: "one") == nil)
        #expect(history.down(current: "one") == "two")
        #expect(history.down(current: "two") == "typing")
        #expect(history.down(current: "typing") == nil)
        #expect(history.isAtBottom)
    }

    @Test func editsToARecalledEntryStayWhileNavigating() {
        let history = PCodePromptHistory(entries: ["one", "two"])
        _ = history.up(current: "")
        #expect(history.up(current: "two edited") == "one")
        #expect(history.down(current: "one") == "two edited")
        #expect(history.edited(current: "two edited") == [1: "two edited"])
    }

    @Test func sendAppendsAndResets() {
        let history = PCodePromptHistory(entries: ["one"])
        history.send("two")
        history.send("two")  // a repeat is not added twice in a row
        #expect(history.entries == ["one", "two"])
        #expect(history.isAtBottom)
        #expect(history.text == "")
    }

    @Test func unsentEditsJoinTheHistoryAheadOfTheSentPrompt() {
        let history = PCodePromptHistory(entries: ["one"])
        _ = history.up(current: "half typed")
        // Edit the recalled entry and send that.
        history.send("one, but better")
        #expect(history.entries == ["one", "half typed", "one, but better"])
        // The recalled entry itself is untouched.
        #expect(history.up(current: "") == "one, but better")
        #expect(history.up(current: "one, but better") == "half typed")
        #expect(history.up(current: "half typed") == "one")
    }

    @Test func clearKeepsTheText() {
        let history = PCodePromptHistory()
        history.clear(current: "draft")
        history.clear(current: "draft")
        history.clear(current: "   ")
        #expect(history.entries == ["draft"])
        #expect(history.text == "")
    }

    // MARK: Drafts

    @Test func draftsAreWrittenGrowInPlaceAndDeletedOnSend() throws {
        let directory = temporaryDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }
        let history = PCodePromptHistory(drafts: PCodeDraftStore(directory: directory))
        history.saveDrafts(current: "")
        #expect(files(directory).isEmpty)

        history.saveDrafts(current: "hello")
        let first = files(directory)
        #expect(first.count == 1)
        #expect(first[0].hasSuffix(".txt"))
        // `<time_ns>-<pid>-<serial>.txt`
        let parts = first[0].dropLast(4).split(separator: "-")
        #expect(parts.count == 3)
        #expect(parts[1] == "\(getpid())")

        history.saveDrafts(current: "hello world")
        #expect(files(directory) == first)
        #expect(contents(directory) == ["hello world"])

        let attributes = try FileManager.default.attributesOfItem(atPath: directory.appendingPathComponent(first[0]).path)
        #expect((attributes[.posixPermissions] as? NSNumber)?.intValue == 0o600)

        history.send("hello world")
        #expect(files(directory).isEmpty)
    }

    @Test func aNewSessionLoadsDraftsIntoItsHistory() {
        let directory = temporaryDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }
        let first = PCodePromptHistory(drafts: PCodeDraftStore(directory: directory))
        first.saveDrafts(current: "left behind")

        let second = PCodePromptHistory(drafts: PCodeDraftStore(directory: directory))
        #expect(second.entries == ["left behind"])
        #expect(second.up(current: "") == "left behind")
        // Sending a recalled draft unchanged deletes its file.
        second.send("left behind")
        #expect(files(directory).isEmpty)
    }

    @Test func sendingAnEditedDraftDeletesTheOriginal() {
        let directory = temporaryDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }
        PCodePromptHistory(drafts: PCodeDraftStore(directory: directory)).saveDrafts(current: "old")
        let history = PCodePromptHistory(drafts: PCodeDraftStore(directory: directory))
        _ = history.up(current: "")
        history.send("old plus more")
        #expect(files(directory).isEmpty)
    }

    @Test func loadKeepsTheNewestAndDropsDuplicatesAndStaleTemps() throws {
        let directory = temporaryDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        let base = Date(timeIntervalSince1970: 1_000_000)
        func put(_ name: String, _ text: String, _ offset: TimeInterval) throws {
            let url = directory.appendingPathComponent(name)
            try Data(text.utf8).write(to: url)
            try FileManager.default.setAttributes([.modificationDate: base.addingTimeInterval(offset)], ofItemAtPath: url.path)
        }
        for number in 0..<(PCodeDraftStore.keep + 2) {
            try put("\(number)-1-0.txt", "draft \(number)", TimeInterval(number))
        }
        try put("dup-1-0.txt", "draft 10", 500)
        try put("blank-1-0.txt", "  \n", 501)
        try put("stale-1-0.tmp", "x", -1000)
        try put("fresh-1-0.tmp", "x", 0)

        let texts = PCodeDraftStore(directory: directory).load(now: base.addingTimeInterval(10))
        #expect(texts.count == PCodeDraftStore.keep - 1 - 1)  // blank and the older duplicate drop out
        #expect(texts.first == "draft 4")
        #expect(texts.last == "draft 10")
        #expect(!texts.dropLast().contains("draft 10"))
        let left = files(directory)
        #expect(!left.contains("0-1-0.txt"))
        #expect(!left.contains("10-1-0.txt"))
        #expect(!left.contains("stale-1-0.tmp"))
        #expect(left.contains("fresh-1-0.tmp"))
    }

    @Test func defaultDirectoryIsPcodesState() {
        #expect(PCodeDraftStore.defaultDirectory.path.hasSuffix("pcode/drafts"))
    }
}
