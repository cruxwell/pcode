import Foundation
import Testing
@testable import Ghostty

/// `@` references must offer and insert what pcode's `file_refs.py` would.
struct PCodeFileIndexTests {
    private func temporaryDirectory() throws -> URL {
        let url = FileManager.default.temporaryDirectory
            .appendingPathComponent("pcode-file-index-\(UUID().uuidString)")
        try FileManager.default.createDirectory(at: url, withIntermediateDirectories: true)
        return url
    }

    private func touch(_ root: URL, _ relative: String) throws {
        let url = root.appendingPathComponent(relative)
        try FileManager.default.createDirectory(at: url.deletingLastPathComponent(), withIntermediateDirectories: true)
        try Data("x".utf8).write(to: url)
    }

    // MARK: Ranking

    @Test func basenameHitOutranksDirectoryHit() {
        let paths = ["tests/test_ui.py", "src/pcode/ui.py", "ui/readme.md", "src/pcode/edit_ui.py"]
        let ranked = PCodeFileIndex.rank(paths, query: "ui.py", limit: 10)
        #expect(ranked == ["src/pcode/ui.py", "tests/test_ui.py", "src/pcode/edit_ui.py"])
    }

    @Test func directoryHitsFollowBasenameHits() {
        let ranked = PCodeFileIndex.rank(["ui/readme.md", "src/ui.py"], query: "ui", limit: 10)
        #expect(ranked == ["src/ui.py", "ui/readme.md"])
    }

    @Test func fuzzyWordPrefixesMatchAfterSubstrings() {
        let ranked = PCodeFileIndex.rank(["src/edit_ui.py", "src/ed_ui.txt", "other.py"], query: "ed_ui", limit: 10)
        #expect(ranked == ["src/ed_ui.txt", "src/edit_ui.py"])
    }

    @Test func atAndDotSlashAreStrippedAndCaseIgnored() {
        let ranked = PCodeFileIndex.rank(["Src/README.md"], query: "@./readme", limit: 10)
        #expect(ranked == ["Src/README.md"])
    }

    @Test func emptyQueryListsShallowestFirst() {
        let ranked = PCodeFileIndex.rank(["a/b/c.py", "z.py", "a/b.py", "b.py"], query: "", limit: 3)
        #expect(ranked == ["b.py", "z.py", "a/b.py"])
    }

    // MARK: Insertion

    @Test func insertionIsDotSlashWithTrailingSpaceQuotedWhenSpaced() {
        #expect(PCodeFileIndex.Item(path: "src/ui.py").insertion == "./src/ui.py ")
        #expect(PCodeFileIndex.Item(path: "My Docs/a.md").insertion == "\"./My Docs/a.md\" ")
    }

    // MARK: Trigger

    @Test func tokenAtWordStart() {
        let token = PCodeFileIndex.activeToken(in: "look at @ui", caret: 11)
        #expect(token == .init(range: NSRange(location: 8, length: 3), fragment: "ui"))
    }

    @Test func bareAtOpensTheMenu() {
        #expect(PCodeFileIndex.activeToken(in: "@", caret: 1)?.fragment == "")
    }

    @Test func noTokenInsideAWordOrAfterWhitespace() {
        #expect(PCodeFileIndex.activeToken(in: "me@example.com", caret: 14) == nil)
        #expect(PCodeFileIndex.activeToken(in: "@ui ", caret: 4) == nil)
        #expect(PCodeFileIndex.activeToken(in: "", caret: 0) == nil)
        #expect(PCodeFileIndex.activeToken(in: "plain", caret: 5) == nil)
    }

    @Test func tokenEndsAtTheCaretNotTheText() {
        let text = "see @ui and more"
        let token = PCodeFileIndex.activeToken(in: text, caret: 7)
        #expect(token?.fragment == "ui")
        let applied = PCodeFileIndex.apply(.init(path: "src/ui.py"), token: token!, to: text)
        #expect(applied.text == "see ./src/ui.py  and more")
        #expect(applied.caret == 16)
    }

    @Test func tokenAfterNewline() {
        #expect(PCodeFileIndex.activeToken(in: "first\n@do", caret: 9)?.fragment == "do")
    }

    @Test func utf16OffsetsSurviveEmoji() {
        let text = "😀 @a"
        let caret = (text as NSString).length
        let token = PCodeFileIndex.activeToken(in: text, caret: caret)
        #expect(token?.range == NSRange(location: 3, length: 2))
    }

    // MARK: Listing

    @Test func walkSkipsHiddenAndIgnoredInOsWalkOrder() throws {
        let root = try temporaryDirectory()
        defer { try? FileManager.default.removeItem(at: root) }
        for path in ["b.txt", "a.txt", ".hidden", "src/z.py", "src/a/deep.py", "node_modules/x.js", ".venv/y.py", "build/out.o"] {
            try touch(root, path)
        }
        #expect(PCodeFileIndex.walkedFiles(root: root) == ["a.txt", "b.txt", "src/z.py", "src/a/deep.py"])
        #expect(PCodeFileIndex.walkedFiles(root: root, limit: 2) == ["a.txt", "b.txt"])
    }

    @Test func outsideGitTheListingFallsBackToTheWalk() throws {
        let root = try temporaryDirectory()
        defer { try? FileManager.default.removeItem(at: root) }
        try touch(root, "notes.md")
        #expect(PCodeFileIndex.gitFiles(root: root) == nil)
        let index = PCodeFileIndex(root: root)
        index.refreshNow()
        #expect(index.isLoaded)
        #expect(index.paths == ["notes.md"])
        #expect(index.matches("note").map(\.path) == ["notes.md"])
        #expect(index.describe("notes.md") == "1 B")
    }

    @Test func gitListingIncludesUntrackedAndHonoursIgnores() throws {
        let root = try temporaryDirectory()
        defer { try? FileManager.default.removeItem(at: root) }
        let git = Process()
        git.executableURL = URL(fileURLWithPath: "/usr/bin/git")
        git.arguments = ["-C", root.path, "init", "-q"]
        try git.run()
        git.waitUntilExit()
        try #require(git.terminationStatus == 0)
        try touch(root, "kept.py")
        try touch(root, "skip.log")
        try touch(root, "dir with space/file.md")
        try Data("*.log\n".utf8).write(to: root.appendingPathComponent(".gitignore"))
        let listed = try #require(PCodeFileIndex.gitFiles(root: root))
        #expect(Set(listed) == [".gitignore", "kept.py", "dir with space/file.md"])
    }

    @Test func workspacesResolveLikePython() {
        #expect(PCodeFileIndex.canonical(URL(fileURLWithPath: "/tmp")).path == "/private/tmp")
        #expect(PCodeFileIndex.canonical(URL(fileURLWithPath: "/no/such/dir/")).path == "/no/such/dir")
        #expect(PCodeFileIndex.shared(for: "/tmp") === PCodeFileIndex.shared(for: "/private/tmp/"))
    }

    @Test func gitUnavailableIsReportedSoTheIndexStopsAsking() throws {
        let root = try temporaryDirectory()
        defer { try? FileManager.default.removeItem(at: root) }
        try touch(root, "a.txt")
        #expect(PCodeFileIndex.git(root: root) == .unavailable)
        let result = PCodeFileIndex.listing(root: root, useGit: true)
        #expect(result.paths == ["a.txt"])
        #expect(result.gitUnavailable)
        #expect(PCodeFileIndex.listing(root: root, useGit: false).paths == ["a.txt"])
    }

    @Test @MainActor func updatesOnlyWhenTheListingChanges() async throws {
        let root = try temporaryDirectory()
        defer { try? FileManager.default.removeItem(at: root) }
        try touch(root, "a.txt")
        let index = PCodeFileIndex(root: root)
        final class Counter: @unchecked Sendable { var count = 0 }
        let counter = Counter()
        index.onUpdate = { counter.count += 1 }
        index.refreshNow()
        index.refreshNow()
        try touch(root, "b.txt")
        index.refreshNow()
        // The callbacks were queued on main; let them run.
        for _ in 0..<5 { await Task.yield() }
        try await Task.sleep(nanoseconds: 50_000_000)
        #expect(counter.count == 2)
        #expect(index.paths == ["a.txt", "b.txt"])
    }

    @Test func aStaleBackgroundListingCannotOverwriteANewerOne() throws {
        let root = try temporaryDirectory()
        defer { try? FileManager.default.removeItem(at: root) }
        try touch(root, "a.txt")
        let index = PCodeFileIndex(root: root, cacheSeconds: 0)
        // Start a background refresh, then land a newer synchronous one; whichever
        // order they finish in, the newest generation's listing stays.
        index.refreshIfStale()
        try touch(root, "b.txt")
        index.refreshNow()
        Thread.sleep(forTimeInterval: 0.3)
        #expect(index.paths == ["a.txt", "b.txt"])
    }

    @Test func asyncMatchesCallBackOnMain() async throws {
        let root = try temporaryDirectory()
        defer { try? FileManager.default.removeItem(at: root) }
        try touch(root, "notes.md")
        let index = PCodeFileIndex(root: root)
        index.refreshNow()
        let paths: [String] = await withCheckedContinuation { continuation in
            index.matches("note") { items in
                #expect(Thread.isMainThread)
                continuation.resume(returning: items.map(\.path))
            }
        }
        #expect(paths == ["notes.md"])
    }

    @Test func humanSizes() {
        #expect(PCodeFileIndex.humanSize(10) == "10 B")
        #expect(PCodeFileIndex.humanSize(1536) == "1.5 KB")
        #expect(PCodeFileIndex.humanSize(3 * 1024 * 1024) == "3.0 MB")
    }
}
