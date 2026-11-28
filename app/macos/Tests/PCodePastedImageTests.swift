import AppKit
import CryptoKit
import Foundation
import Testing
@testable import Ghostty

/// A pasted image is saved and named as pcode's `/paste-image` does it
/// (`src/pcode/prompt_images.py`), so the host attaches it.
struct PCodePastedImageTests {
    private func temporaryDirectory() -> URL {
        FileManager.default.temporaryDirectory.appendingPathComponent("pcode-pastes-\(UUID().uuidString)")
    }

    private func png(width: Int, height: Int) -> Data {
        let rep = NSBitmapImageRep(
            bitmapDataPlanes: nil, pixelsWide: width, pixelsHigh: height, bitsPerSample: 8, samplesPerPixel: 4,
            hasAlpha: true, isPlanar: false, colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0)!
        return rep.representation(using: .png, properties: [:])!
    }

    @Test func savesUnderAContentHashWithPrivateMode() throws {
        let directory = temporaryDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }
        let data = png(width: 4, height: 3)
        let pasted = try #require(PCodePastedImage.save(data, directory: directory))
        let hash = SHA256.hash(data: data).map { String(format: "%02x", $0) }.joined().prefix(12)
        #expect(pasted.savedPath?.lastPathComponent == "paste-\(hash).png")
        #expect(pasted.unsendable == nil)
        #expect(try Data(contentsOf: pasted.savedPath!) == data)
        let attributes = try FileManager.default.attributesOfItem(atPath: pasted.savedPath!.path)
        #expect((attributes[.posixPermissions] as? NSNumber)?.intValue == 0o600)
        // The same image keeps one file.
        #expect(PCodePastedImage.save(data, directory: directory)?.savedPath == pasted.savedPath)
        #expect(try FileManager.default.contentsOfDirectory(atPath: directory.path).count == 1)
    }

    @Test func tiffIsConvertedToPNG() throws {
        let directory = temporaryDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }
        let tiff = try #require(NSBitmapImageRep(data: png(width: 2, height: 2))?.tiffRepresentation)
        let pasted = try #require(PCodePastedImage.save(tiff, directory: directory))
        let saved = try Data(contentsOf: pasted.savedPath!)
        #expect(saved.starts(with: [0x89, 0x50, 0x4E, 0x47]))
    }

    @Test func oldPastesArePruned() throws {
        let directory = temporaryDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
        let old = directory.appendingPathComponent("paste-000000000000.png")
        let other = directory.appendingPathComponent("keep.png")
        for url in [old, other] {
            try Data("x".utf8).write(to: url)
            try FileManager.default.setAttributes([.modificationDate: Date(timeIntervalSinceNow: -31 * 86400)], ofItemAtPath: url.path)
        }
        _ = PCodePastedImage.save(png(width: 1, height: 1), directory: directory)
        #expect(!FileManager.default.fileExists(atPath: old.path))
        #expect(FileManager.default.fileExists(atPath: other.path))
    }

    @Test func tooManyPixelsIsShrunkToPasteSide() throws {
        let directory = temporaryDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }
        let pasted = try #require(PCodePastedImage.save(png(width: 8100, height: 10), directory: directory))
        #expect(pasted.unsendable == nil)
        let rep = try #require(NSBitmapImageRep(data: try Data(contentsOf: pasted.savedPath!)))
        #expect(rep.pixelsWide == PCodePastedImage.pasteSide)
        #expect(rep.pixelsHigh == 2)
    }

    @Test func unsendableReasonsMatchPcode() {
        #expect(PCodePastedImage.unsendable(png(width: 8001, height: 1)) == "8001x1 pixels, and the limit is 8000 on a side")
        let big = Data(count: PCodePastedImage.maxImageBytes + 1)
        #expect(PCodePastedImage.unsendable(big) == "\(big.count) bytes, and the limit is 3932160")
    }

    @Test func referencesAbbreviateHomeAndQuoteSpaces() {
        let home = "/Users/me"
        #expect(PCodePastedImage.reference(URL(fileURLWithPath: "/Users/me/.local/state/pcode/pastes/paste-a.png"), home: home)
            == "~/.local/state/pcode/pastes/paste-a.png")
        #expect(PCodePastedImage.reference(URL(fileURLWithPath: "/tmp/My Shot.png"), home: home) == "\"/tmp/My Shot.png\"")
        #expect(PCodePastedImage.reference(URL(fileURLWithPath: "/Users/meow/a.png"), home: home) == "/Users/meow/a.png")
    }

    @Test func insertionSpacing() {
        #expect(PCodePastedImage.insertion("~/a.png", before: "") == "~/a.png ")
        #expect(PCodePastedImage.insertion("~/a.png", before: "look ") == "~/a.png ")
        #expect(PCodePastedImage.insertion("~/a.png", before: "look") == " ~/a.png ")
    }

    @Test func pasteboardImageAndFiles() throws {
        let directory = temporaryDirectory()
        defer { try? FileManager.default.removeItem(at: directory) }
        let board = NSPasteboard(name: .init("pcode-test-\(UUID().uuidString)"))
        defer { board.releaseGlobally() }
        board.clearContents()
        #expect(!PCodePastedImage.canPaste(board))
        #expect(PCodePastedImage.paste(from: board, directory: directory).isEmpty)

        board.setData(png(width: 2, height: 2), forType: .png)
        #expect(PCodePastedImage.canPaste(board))
        let pasted = PCodePastedImage.paste(from: board, directory: directory)
        #expect(pasted.count == 1)
        #expect(pasted.first?.savedPath?.deletingLastPathComponent().lastPathComponent == directory.lastPathComponent)

        board.clearContents()
        board.writeObjects([URL(fileURLWithPath: "/tmp/one.png") as NSURL, URL(fileURLWithPath: "/tmp/two words.jpg") as NSURL])
        let files = PCodePastedImage.paste(from: board, directory: directory)
        #expect(files.map(\.reference) == ["/tmp/one.png", "\"/tmp/two words.jpg\""])
        #expect(files.allSatisfy { $0.savedPath == nil })
    }
}
