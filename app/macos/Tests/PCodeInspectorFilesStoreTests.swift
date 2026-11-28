import Foundation
import Testing
@testable import Ghostty

struct PCodeInspectorFilesStoreTests {
    @Test func parseReadFileReferences() {
        let plain = PCodeReadFileReference.parse(detail: "src/pcode/app.py")
        #expect(plain == PCodeReadFileReference(path: "src/pcode/app.py", startLine: nil, endLine: nil))

        let singleLine = PCodeReadFileReference.parse(detail: "src/pcode/app.py:42")
        #expect(singleLine == PCodeReadFileReference(path: "src/pcode/app.py", startLine: 42, endLine: 42))

        let range = PCodeReadFileReference.parse(detail: "src/pcode/app.py:L10-L25")
        #expect(range == PCodeReadFileReference(path: "src/pcode/app.py", startLine: 10, endLine: 25))

        let parens = PCodeReadFileReference.parse(detail: "app/macos/PCode/PCode.swift (lines 5-18)")
        #expect(parens == PCodeReadFileReference(path: "app/macos/PCode/PCode.swift", startLine: 5, endLine: 18))

        #expect(PCodeReadFileReference.parse(detail: "path unavailable") == nil)
        #expect(PCodeReadFileReference.parse(detail: "   ") == nil)
        #expect(PCodeReadFileReference.isReadFileTool("read_file"))
        #expect(PCodeReadFileReference.isReadFileTool("view_file"))
        #expect(!PCodeReadFileReference.isReadFileTool("edit_file"))
    }

    @Test @MainActor func openEditSaveAndReloadBuffer() throws {
        let fm = FileManager.default
        let tempDir = fm.temporaryDirectory.appendingPathComponent("pcode-files-store-\(UUID().uuidString)", isDirectory: true)
        try fm.createDirectory(at: tempDir, withIntermediateDirectories: true)
        defer { try? fm.removeItem(at: tempDir) }

        let fileURL = tempDir.appendingPathComponent("hello.swift")
        try "let a = 1\nlet b = 2\n".write(to: fileURL, atomically: true, encoding: .utf8)

        let store = PCodeInspectorFilesStore(sessionDirectory: tempDir.path)
        let opened = store.openFile(path: "hello.swift", line: 2, endLine: 2)
        #expect(opened != nil)
        #expect(store.activeFilePath == fileURL.standardizedFileURL.path)
        #expect(store.activeBuffer?.displayPath == "hello.swift")
        #expect(store.activeBuffer?.isDirty == false)
        #expect(store.activeBuffer?.highlightedLineRange == 2...2)
        #expect(store.activeBuffer?.composerReference() == "@hello.swift:L2 ")

        store.updateText("let a = 1\nlet b = 42\n", for: fileURL.standardizedFileURL.path)
        #expect(store.activeBuffer?.isDirty == true)
        #expect(store.dirtyBufferCount == 1)

        #expect(store.saveActiveFile() == true)
        #expect(store.activeBuffer?.isDirty == false)
        let onDisk = try String(contentsOf: fileURL, encoding: .utf8)
        #expect(onDisk == "let a = 1\nlet b = 42\n")

        // Simulate external agent edit while buffer is clean -> auto-reloads cleanly.
        try "let a = 1\nlet b = 99\n".write(to: fileURL, atomically: true, encoding: .utf8)
        store.recheckActiveFileOnDisk()
        #expect(store.activeBuffer?.text == "let a = 1\nlet b = 99\n")
        #expect(store.activeBuffer?.diskChangedConflict == false)

        // Simulate external agent edit while buffer is dirty -> flags conflict without clobbering.
        store.updateText("let a = 1\nlet b = 100\n", for: fileURL.standardizedFileURL.path)
        try "let a = 1\nlet b = 200\n".write(to: fileURL, atomically: true, encoding: .utf8)
        store.recheckActiveFileOnDisk()
        #expect(store.activeBuffer?.text == "let a = 1\nlet b = 100\n")
        #expect(store.activeBuffer?.diskChangedConflict == true)

        store.reloadFile(path: fileURL.standardizedFileURL.path)
        #expect(store.activeBuffer?.text == "let a = 1\nlet b = 200\n")
        #expect(store.activeBuffer?.diskChangedConflict == false)
        #expect(store.activeBuffer?.isDirty == false)
    }

    @Test @MainActor func binaryDetectionAndDirectoryTree() throws {
        let fm = FileManager.default
        let tempDir = fm.temporaryDirectory.appendingPathComponent("pcode-files-tree-\(UUID().uuidString)", isDirectory: true)
        let subDir = tempDir.appendingPathComponent("src", isDirectory: true)
        let ignoredDir = tempDir.appendingPathComponent("node_modules", isDirectory: true)
        try fm.createDirectory(at: subDir, withIntermediateDirectories: true)
        try fm.createDirectory(at: ignoredDir, withIntermediateDirectories: true)
        defer { try? fm.removeItem(at: tempDir) }

        try "print('hi')".write(to: subDir.appendingPathComponent("main.py"), atomically: true, encoding: .utf8)
        try "ignored".write(to: ignoredDir.appendingPathComponent("pkg.js"), atomically: true, encoding: .utf8)
        let binURL = tempDir.appendingPathComponent("blob.bin")
        try Data([0x89, 0x50, 0x4E, 0x47, 0x00, 0x01, 0x02]).write(to: binURL)

        let store = PCodeInspectorFilesStore(sessionDirectory: tempDir.path)
        store.refreshTree()
        #expect(store.treeEntries.map(\.name) == ["src", "blob.bin"])

        store.toggleDirectory(subDir.standardizedFileURL.path)
        #expect(store.treeEntries.map(\.name) == ["src", "main.py", "blob.bin"])

        let binBuf = store.openFile(path: "blob.bin")
        #expect(binBuf?.isBinary == true)
        #expect(binBuf?.canEdit == false)
    }
}
