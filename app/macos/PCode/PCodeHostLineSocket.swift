import Darwin
import Foundation

/// Unix-socket plumbing shared by every `pcode.host` client in the app: the read-only
/// transcript observer (`PCodeHostSocketClient`) and the composer's full client
/// (`PCodeHostRPCClient`).
///
/// `AF_UNIX`, `SO_NOSIGPIPE`, a `getpeereid` same-user check (as `PCodePicker` does),
/// whole-buffer writes, and newline-delimited JSON split with an O(N) scan cursor.
enum PCodeHostLineSocket {
    /// Connect to the socket at `path`. Returns the descriptor, or a message for the user.
    static func connect(path: String) -> Result<Int32, PCodeHostSocketError> {
        let sock = Darwin.socket(AF_UNIX, SOCK_STREAM, 0)
        guard sock >= 0 else { return .failure(.init("Failed to create Unix socket.")) }
        var noSigPipe: Int32 = 1
        setsockopt(sock, SOL_SOCKET, SO_NOSIGPIPE, &noSigPipe, socklen_t(MemoryLayout<Int32>.size))

        var addr = sockaddr_un()
        addr.sun_family = sa_family_t(AF_UNIX)
        let pathBytes = Array(path.utf8)
        let maxLen = MemoryLayout.size(ofValue: addr.sun_path) - 1
        guard pathBytes.count <= maxLen else {
            Darwin.close(sock)
            return .failure(.init("Host socket path exceeds Unix socket limit."))
        }
        withUnsafeMutableBytes(of: &addr.sun_path) { raw in
            for i in 0..<pathBytes.count { raw[i] = pathBytes[i] }
            raw[pathBytes.count] = 0
        }
        let connected = withUnsafePointer(to: &addr) { ptr in
            ptr.withMemoryRebound(to: sockaddr.self, capacity: 1) { sockPtr in
                Darwin.connect(sock, sockPtr, socklen_t(MemoryLayout<sockaddr_un>.size))
            }
        }
        guard connected == 0 else {
            Darwin.close(sock)
            return .failure(.init("Could not connect to session host socket."))
        }
        var euid: uid_t = 0
        var egid: gid_t = 0
        guard Darwin.getpeereid(sock, &euid, &egid) == 0, euid == Darwin.geteuid() else {
            Darwin.close(sock)
            return .failure(.init("Refusing to connect to host owned by another user."))
        }
        return .success(sock)
    }

    /// Write all of `data`, retrying on `EINTR`; false once the peer is gone.
    static func writeAll(_ sock: Int32, _ data: Data) -> Bool {
        data.withUnsafeBytes { raw in
            guard let base = raw.baseAddress else { return true }
            var written = 0
            while written < data.count {
                let n = Darwin.write(sock, base.advanced(by: written), data.count - written)
                if n < 0 {
                    if errno == EINTR { continue }
                    return false
                }
                if n == 0 { return false }
                written += n
            }
            return true
        }
    }
}

struct PCodeHostSocketError: Error, Equatable {
    let message: String
    init(_ message: String) { self.message = message }
}

/// Bytes read off a host socket, handed back one complete line at a time.
struct PCodeLineBuffer {
    /// A single line larger than this (a welcome snapshot is the biggest) is refused.
    static let maxLineBytes = 64 * 1024 * 1024

    private var buffer = Data()
    private var scanOffset = 0

    init() { buffer.reserveCapacity(64 * 1024) }

    /// Add what was read; false when an unfinished line has outgrown `maxLineBytes`.
    mutating func append(_ bytes: ArraySlice<UInt8>) -> Bool {
        buffer.append(contentsOf: bytes)
        return buffer.count - scanOffset <= Self.maxLineBytes
    }

    /// The next complete line without its newline, or nil until more arrives.
    /// Empty lines are skipped.
    mutating func nextLine() -> Data? {
        while scanOffset < buffer.count, let newline = buffer[scanOffset...].firstIndex(of: 0x0A) {
            let line = buffer[scanOffset..<newline]
            scanOffset = newline + 1
            if !line.isEmpty { return Data(line) }
        }
        compact()
        return nil
    }

    /// The next complete line parsed as a JSON object; lines that aren't one are skipped.
    mutating func nextObject() -> [String: Any]? {
        while let line = nextLine() {
            if let obj = try? JSONSerialization.jsonObject(with: line) as? [String: Any] { return obj }
        }
        return nil
    }

    private mutating func compact() {
        if scanOffset >= buffer.count {
            buffer.removeAll(keepingCapacity: true)
            scanOffset = 0
        } else if scanOffset >= 64 * 1024 {
            buffer.removeSubrange(0..<scanOffset)
            scanOffset = 0
        }
    }
}
