import AppKit
import CryptoKit
import Foundation

/// A pasted or dropped image for the native composer, saved and named in the
/// prompt as pcode's `/paste-image` does (`src/pcode/prompt_images.py`,
/// `PcodeApp._paste_image` in `src/pcode/app.py`). The prompt stays text: the
/// host attaches any image file a prompt names, so only the path is inserted.
///
/// Pasted data is saved as `paste-<first 12 hex of its PNG's sha256>.png` in
/// `$XDG_STATE_HOME/pcode/pastes` (default `~/.local/state`), mode 0600, and
/// pastes older than 30 days are pruned on the way. One too large to send is
/// shrunk to `pasteSide` on its longest side, as pcode does with `sips`.
enum PCodePastedImage {
    /// How long a pasted image is kept for a prompt (or a resend) to name it.
    static let pasteDays = 30
    /// The longest side an image too large to send is shrunk to.
    static let pasteSide = 2000
    /// `pcode.images.MAX_IMAGE_BYTES`: 5 MiB once base64-encoded.
    static let maxImageBytes = 5 * 1024 * 1024 * 3 / 4
    /// `pcode.images.MAX_IMAGE_SIDE`.
    static let maxImageSide = 8000

    struct Pasted: Equatable {
        /// What to insert at the caret (see `insertion(_:before:)`).
        let reference: String
        /// The saved file, nil when the pasteboard named an existing file.
        let savedPath: URL?
        /// Why the image cannot be sent, if so; pcode warns and inserts nothing then.
        let unsendable: String?
    }

    static var pastesDirectory: URL {
        let state = ProcessInfo.processInfo.environment["XDG_STATE_HOME"].flatMap { $0.isEmpty ? nil : $0 }
            .map { URL(fileURLWithPath: $0) }
            ?? FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent(".local/state")
        return state.appendingPathComponent("pcode/pastes")
    }

    /// True if the pasteboard holds something `paste(from:)` would use.
    static func canPaste(_ pasteboard: NSPasteboard) -> Bool {
        !fileURLs(pasteboard).isEmpty || pasteboard.availableType(from: [.png, .tiff]) != nil
    }

    /// The references for `pasteboard`: its file URLs as they are (as a terminal
    /// types a dropped file's path), else its image saved as a PNG. Empty when
    /// it holds neither.
    static func paste(from pasteboard: NSPasteboard, directory: URL = pastesDirectory) -> [Pasted] {
        let urls = fileURLs(pasteboard)
        if !urls.isEmpty {
            return urls.map { Pasted(reference: reference($0), savedPath: nil, unsendable: nil) }
        }
        guard let data = pasteboard.data(forType: .png) ?? pasteboard.data(forType: .tiff),
              let pasted = save(data, directory: directory) else { return [] }
        return [pasted]
    }

    private static func fileURLs(_ pasteboard: NSPasteboard) -> [URL] {
        (pasteboard.readObjects(forClasses: [NSURL.self], options: [.urlReadingFileURLsOnly: true]) as? [URL]) ?? []
    }

    /// `prompt_images.paste`: save image data (PNG, or anything `NSBitmapImageRep`
    /// reads, converted to PNG), shrinking it if it is too large to send.
    static func save(_ data: Data, directory: URL = pastesDirectory, now: Date = Date()) -> Pasted? {
        guard let png = pngData(data) else { return nil }
        let manager = FileManager.default
        do {
            try manager.createDirectory(at: directory, withIntermediateDirectories: true, attributes: [.posixPermissions: 0o700])
        } catch {
            return nil
        }
        prune(directory, now: now)
        let hash = SHA256.hash(data: png).map { String(format: "%02x", $0) }.joined().prefix(12)
        let path = directory.appendingPathComponent("paste-\(hash).png")
        guard write(png, to: path) else { return nil }
        var why = unsendable(png)
        if why != nil, let shrunk = downscaled(png, side: pasteSide), write(shrunk, to: path) {
            why = unsendable(shrunk)
        }
        return Pasted(reference: reference(path), savedPath: path, unsendable: why)
    }

    /// `prompt_images.reference`: the path with `~` for home, quoted when it holds whitespace.
    static func reference(_ path: URL, home: String = NSHomeDirectory()) -> String {
        var text = path.path
        if text.hasPrefix(home + "/") { text = "~" + text.dropFirst(home.count) }
        return text.contains(where: \.isWhitespace) ? "\"\(text)\"" : text
    }

    /// What to insert at the caret, as pcode inserts a paste: a space first
    /// unless the text before the caret is empty or ends in whitespace, and one after.
    static func insertion(_ reference: String, before: String) -> String {
        let space = before.isEmpty || before.last!.isWhitespace ? "" : " "
        return "\(space)\(reference) "
    }

    /// `pcode.images.unsendable`: why no provider would take the image, or nil.
    static func unsendable(_ data: Data) -> String? {
        if data.count > maxImageBytes {
            return "\(data.count) bytes, and the limit is \(maxImageBytes)"
        }
        if let rep = NSBitmapImageRep(data: data), max(rep.pixelsWide, rep.pixelsHigh) > maxImageSide {
            return "\(rep.pixelsWide)x\(rep.pixelsHigh) pixels, and the limit is \(maxImageSide) on a side"
        }
        return nil
    }

    /// `data` as PNG; PNG data is kept byte for byte.
    static func pngData(_ data: Data) -> Data? {
        if data.starts(with: [0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A]) { return data }
        return NSBitmapImageRep(data: data)?.representation(using: .png, properties: [:])
    }

    /// `sips --resampleHeightWidthMax side`: the PNG scaled so its longest side
    /// is `side` pixels, or nil when it is already within it.
    static func downscaled(_ png: Data, side: Int) -> Data? {
        guard let rep = NSBitmapImageRep(data: png) else { return nil }
        let width = rep.pixelsWide, height = rep.pixelsHigh
        let longest = max(width, height)
        guard longest > side, let image = rep.cgImage else { return nil }
        let scale = Double(side) / Double(longest)
        let newWidth = max(1, Int((Double(width) * scale).rounded()))
        let newHeight = max(1, Int((Double(height) * scale).rounded()))
        guard let context = CGContext(
            data: nil, width: newWidth, height: newHeight, bitsPerComponent: 8, bytesPerRow: 0,
            space: image.colorSpace ?? CGColorSpaceCreateDeviceRGB(),
            bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue
        ) ?? CGContext(
            data: nil, width: newWidth, height: newHeight, bitsPerComponent: 8, bytesPerRow: 0,
            space: CGColorSpaceCreateDeviceRGB(), bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue
        ) else { return nil }
        context.interpolationQuality = .high
        context.draw(image, in: CGRect(x: 0, y: 0, width: newWidth, height: newHeight))
        guard let scaled = context.makeImage() else { return nil }
        return NSBitmapImageRep(cgImage: scaled).representation(using: .png, properties: [:])
    }

    /// Remove `paste-*.png` files older than `pasteDays`.
    private static func prune(_ directory: URL, now: Date) {
        let manager = FileManager.default
        let cutoff = now.addingTimeInterval(-Double(pasteDays) * 24 * 60 * 60)
        for name in (try? manager.contentsOfDirectory(atPath: directory.path)) ?? []
        where name.hasPrefix("paste-") && name.hasSuffix(".png") {
            let url = directory.appendingPathComponent(name)
            if let modified = (try? manager.attributesOfItem(atPath: url.path))?[.modificationDate] as? Date, modified < cutoff {
                try? manager.removeItem(at: url)
            }
        }
    }

    /// Write with mode 0600: a screenshot can hold anything that was on screen.
    private static func write(_ data: Data, to path: URL) -> Bool {
        let fd = open(path.path, O_WRONLY | O_CREAT | O_TRUNC, 0o600)
        guard fd >= 0 else { return false }
        let handle = FileHandle(fileDescriptor: fd, closeOnDealloc: true)
        do {
            try handle.write(contentsOf: data)
            try handle.close()
            return true
        } catch {
            return false
        }
    }
}
