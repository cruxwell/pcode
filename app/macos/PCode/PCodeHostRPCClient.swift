import Darwin
import Foundation

/// A full `pcode.host` client: the `hello` handshake, then both ends are `pcode.rpc`
/// peers (`src/pcode/rpc.py`). The app calls `attach` for its welcome, sends intents
/// (`submit`, `command`, `cancel`, ...), receives every view call as a notify, and
/// answers the host's requests (`run_command`, `choose_model`, `read_secret`, ...).
///
/// Unlike the read-only transcript observer (`PCodeHostSocketClient`), attaching
/// counts as a terminal: the host marks the session seen and routes popups here.
/// So a pane runs exactly one of these or a terminal editor, never both.
///
/// Socket I/O runs on its own threads; every callback arrives on the main queue.
final class PCodeHostRPCClient {
    /// `pcode.host_protocol.PROTOCOL`.
    static let protocolVersion = 2

    enum Message {
        case notify(method: String, args: [Any], kwargs: [String: Any])
        case request(id: Int, method: String, args: [Any], kwargs: [String: Any])
        case reply(id: Int, result: Any?, error: String?)
    }

    /// The attach welcome (`SessionHost.attach`): `session`, `activity`, `plan`, `calls`...
    var onAttached: (([String: Any]) -> Void)?
    var onNotify: ((_ method: String, _ args: [Any], _ kwargs: [String: Any]) -> Void)?
    /// A host request; answer it with `reply` or `replyError` using `id`.
    var onRequest: ((_ id: Int, _ method: String, _ args: [Any], _ kwargs: [String: Any]) -> Void)?
    /// Why a connection ended.
    enum Ending: Equatable {
        /// The socket was not there, or went away: worth connecting again.
        case lost(String?)
        /// The host answered and said no (a protocol mismatch, a failed `attach`):
        /// connecting again gets the same answer.
        case refused(String)
    }

    /// The connection ended (not called after `close()`).
    var onClosed: ((Ending) -> Void)?

    private let lock = NSLock()
    /// Held by a write in progress and by the reader closing the socket, so a write
    /// never lands on a descriptor number the system has reused. Not held by `close()`,
    /// whose `shutdown` is what unblocks a stuck write.
    private let writeLock = NSLock()
    private var fd: Int32 = -1
    private var closed = false
    private var nextID = 1
    private var attachID: Int?
    private var pending: [Int: (Any?, String?) -> Void] = [:]
    /// Writes keep their order and never block the main thread.
    private let writer = DispatchQueue(label: "pcode.host.rpc.writer", qos: .userInitiated)

    deinit { close() }

    func connect(socketPath: String) {
        let thread = Thread { [weak self] in self?.run(socketPath: socketPath) }
        thread.name = "pcode.host.rpc"
        thread.qualityOfService = .userInitiated
        thread.start()
    }

    func close() {
        lock.lock()
        closed = true
        let current = fd
        lock.unlock()
        if current >= 0 { Darwin.shutdown(current, SHUT_RDWR) }
    }

    // MARK: Sending

    func notify(_ method: String, _ args: [Any] = [], _ kwargs: [String: Any] = [:]) {
        send(Self.encode(["type": "notify", "method": method, "args": args, "kwargs": kwargs]))
    }

    func request(
        _ method: String, _ args: [Any] = [], _ kwargs: [String: Any] = [:],
        completion: @escaping (_ result: Any?, _ error: String?) -> Void
    ) {
        lock.lock()
        let id = nextID
        nextID += 1
        pending[id] = completion
        lock.unlock()
        send(Self.encode(["type": "request", "id": id, "method": method, "args": args, "kwargs": kwargs]))
    }

    /// Answer request `id`. A result JSON can't carry is answered with an error instead,
    /// so the host's request never waits forever.
    func reply(id: Int, result: Any?) {
        if let data = Self.encode(["type": "reply", "id": id, "result": result ?? NSNull()]) {
            send(data)
        } else {
            replyError(id: id, type: "TypeError", message: "Pcode.app could not encode its answer.")
        }
    }

    func replyError(id: Int, type: String = "RuntimeError", message: String) {
        send(Self.encode([
            "type": "reply", "id": id,
            "error": ["type": type, "message": message, "value_error": false],
        ]))
    }

    private func send(_ data: Data?) {
        guard let data else { return }
        writer.async { [weak self] in
            guard let self else { return }
            self.writeLock.lock()
            defer { self.writeLock.unlock() }
            self.lock.lock()
            let sock = self.closed ? -1 : self.fd
            self.lock.unlock()
            guard sock >= 0 else { return }
            _ = PCodeHostLineSocket.writeAll(sock, data)
        }
    }

    // MARK: Framing (pure, for tests)

    /// One newline-terminated JSON message, or nil when `object` holds something JSON can't.
    static func encode(_ object: [String: Any]) -> Data? {
        guard JSONSerialization.isValidJSONObject(object),
              var data = try? JSONSerialization.data(withJSONObject: object) else { return nil }
        data.append(0x0A)
        return data
    }

    static func helloLine() -> Data {
        encode(["type": "hello", "protocol": protocolVersion])!
    }

    /// The host's answer to `hello`: nil when ready, else the message to show.
    static func handshakeError(_ obj: [String: Any]) -> String? {
        switch obj["type"] as? String {
        case "ready": return nil
        case "error": return (obj["message"] as? String) ?? "The session host refused the connection."
        default: return "Unexpected session host handshake response."
        }
    }

    static func parse(_ obj: [String: Any]) -> Message? {
        switch obj["type"] as? String {
        case "notify":
            guard let method = obj["method"] as? String else { return nil }
            return .notify(method: method, args: (obj["args"] as? [Any]) ?? [], kwargs: (obj["kwargs"] as? [String: Any]) ?? [:])
        case "request":
            guard let method = obj["method"] as? String, let id = obj["id"] as? Int else { return nil }
            return .request(id: id, method: method, args: (obj["args"] as? [Any]) ?? [], kwargs: (obj["kwargs"] as? [String: Any]) ?? [:])
        case "reply":
            guard let id = obj["id"] as? Int else { return nil }
            var error: String?
            if let err = obj["error"] as? [String: Any] {
                error = (err["message"] as? String) ?? (err["type"] as? String) ?? "Error"
            }
            let result = obj["result"]
            return .reply(id: id, result: result is NSNull ? nil : result, error: error)
        default:
            return nil  // From a newer peer: ignored, as `Peer._dispatch` does.
        }
    }

    // MARK: Reader

    private func run(socketPath: String) {
        func finish(_ ending: Ending) {
            lock.lock()
            let waiting = pending
            pending.removeAll()
            let wasClosed = closed
            closed = true
            lock.unlock()
            DispatchQueue.main.async { [weak self] in
                for (_, completion) in waiting { completion(nil, "The session host connection closed.") }
                // An explicit `close()` reports nothing: the owner already knows.
                if !wasClosed { self?.onClosed?(ending) }
            }
        }

        let sock: Int32
        switch PCodeHostLineSocket.connect(path: socketPath) {
        case .success(let value): sock = value
        case .failure(let error):
            // Another user's socket is a refusal; a missing or dead one may come up yet.
            return finish(error.message.hasPrefix("Refusing") ? .refused(error.message) : .lost(error.message))
        }
        defer {
            // After any write in progress, and with `fd` cleared first, so no later
            // write reaches a descriptor number the system hands out again.
            writeLock.lock()
            lock.lock()
            fd = -1
            closed = true
            lock.unlock()
            Darwin.close(sock)
            writeLock.unlock()
        }
        lock.lock()
        if closed {
            lock.unlock()
            return
        }
        fd = sock
        lock.unlock()

        guard PCodeHostLineSocket.writeAll(sock, Self.helloLine()) else {
            return finish(.lost("Failed to send the session host handshake."))
        }
        var lines = PCodeLineBuffer()
        var chunk = [UInt8](repeating: 0, count: 64 * 1024)
        var handshaken = false
        var failure: Ending = .lost(nil)
        reading: while true {
            let n = Darwin.read(sock, &chunk, chunk.count)
            if n < 0 {
                if errno == EINTR { continue }
                break
            }
            if n == 0 { break }
            guard lines.append(chunk[0..<n]) else {
                failure = .refused("Host message exceeded maximum line limit.")
                break
            }
            while let obj = lines.nextObject() {
                if !handshaken {
                    if let error = Self.handshakeError(obj) {
                        failure = .refused(error)
                        break reading
                    }
                    handshaken = true
                    lock.lock()
                    let id = nextID
                    nextID += 1
                    attachID = id
                    lock.unlock()
                    send(Self.encode(["type": "request", "id": id, "method": "attach", "args": [Any](), "kwargs": [String: Any]()]))
                    continue
                }
                guard let message = Self.parse(obj) else { continue }
                deliver(message)
            }
        }
        finish(failure)
    }

    private func deliver(_ message: Message) {
        switch message {
        case .notify(let method, let args, let kwargs):
            DispatchQueue.main.async { [weak self] in self?.onNotify?(method, args, kwargs) }
        case .request(let id, let method, let args, let kwargs):
            DispatchQueue.main.async { [weak self] in self?.onRequest?(id, method, args, kwargs) }
        case .reply(let id, let result, let error):
            lock.lock()
            let isAttach = id == attachID
            let completion = pending.removeValue(forKey: id)
            lock.unlock()
            DispatchQueue.main.async { [weak self] in
                if isAttach {
                    if let welcome = result as? [String: Any] {
                        self?.onAttached?(welcome)
                    } else {
                        self?.close()
                        self?.onClosed?(.refused(error ?? "The session host did not send its welcome."))
                    }
                } else {
                    completion?(result, error)
                }
            }
        }
    }
}

/// One step of the conversation's plan, as the welcome's `plan` and `plan_changed` carry it
/// (`PlanItem.model_dump`). For the task strip a later phase draws.
struct PCodePlanItem: Equatable {
    let id: String
    let content: String
    let status: String
    let activeForm: String
    let size: String?

    static func decode(_ raw: Any?) -> [PCodePlanItem] {
        guard let items = raw as? [[String: Any]] else { return [] }
        return items.map { item in
            PCodePlanItem(
                id: (item["id"] as? String) ?? "",
                content: (item["content"] as? String) ?? "",
                status: (item["status"] as? String) ?? "pending",
                activeForm: (item["active_form"] as? String) ?? "",
                size: item["size"] as? String)
        }
    }
}

/// What a full client mirrors of the session: the `SESSION_FIELDS` the host sends as
/// `state` deltas (and the welcome's `activity`), the scalar parts of `session_state`,
/// the plan, and side-question counts. Raw `session_state` (the command catalog)
/// is kept beside this by `PCodeComposerSession`, since it is not `Equatable` data.
struct PCodeHostClientState: Equatable {
    // SESSION_FIELDS (`pcode.controller`)
    var busy = false
    var status = ""
    var queuedPrompts: [String] = []
    var queuedModes: [String] = []
    var prompt = ""
    var promptState = ""
    var turnOutcome = ""
    var promptKind = "user"
    var userCommand = false
    var jobCount = 0
    var notice = ""

    // session_state
    var model = ""
    var effort = ""
    var context = ""
    var workspace = ""
    var sessionID = ""
    var sessionTitle = ""
    var startupPending = false
    var startupError = ""
    var thinkingMode = ""

    var plan: [PCodePlanItem] = []
    var asidesRunning = 0
    var asidesReady = 0

    /// Fold a `state` delta (or the welcome's whole `activity`) in.
    mutating func applyActivity(_ fields: [String: Any]) {
        for (key, value) in fields {
            switch key {
            case "busy": busy = (value as? Bool) ?? busy
            case "status": status = (value as? String) ?? ""
            case "queued_prompts": queuedPrompts = (value as? [String]) ?? []
            case "queued_modes": queuedModes = (value as? [String]) ?? []
            case "prompt": prompt = (value as? String) ?? ""
            case "prompt_state": promptState = (value as? String) ?? ""
            case "turn_outcome": turnOutcome = (value as? String) ?? ""
            case "prompt_kind": promptKind = (value as? String) ?? "user"
            case "user_command": userCommand = (value as? Bool) ?? false
            case "job_count": jobCount = (value as? Int) ?? 0
            case "notice": notice = (value as? String) ?? ""
            default: break
            }
        }
    }

    /// Take the scalars of a `session_state` (the welcome's `session`, or a push).
    mutating func applySession(_ state: [String: Any]) {
        model = (state["model"] as? String) ?? ""
        effort = (state["effort"] as? String) ?? ""
        context = ((state["context"] as? String) ?? "")
            .trimmingCharacters(in: .whitespaces)
            .replacingOccurrences(of: "^· ", with: "", options: .regularExpression)
        workspace = (state["workspace"] as? String) ?? workspace
        sessionID = (state["session_id"] as? String) ?? ""
        sessionTitle = (state["session_title"] as? String) ?? ""
        startupPending = (state["startup_pending"] as? Bool) ?? false
        startupError = (state["startup_error"] as? String) ?? ""
        thinkingMode = (state["thinking_mode"] as? String) ?? thinkingMode
    }

    /// The side questions after an `aside_changed`, from the client's own list of them.
    mutating func applyAsides(_ asides: [String: [String: Any]]) {
        var running = 0
        var ready = 0
        for aside in asides.values {
            let isRunning = (aside["status"] as? String) == "running"
            if isRunning { running += 1 } else if (aside["read"] as? Bool) != true { ready += 1 }
        }
        asidesRunning = running
        asidesReady = ready
    }

    /// The turn status the strip shows, as `PreviewApp.native_composer_payload` words it,
    /// short of what only the terminal editor tracks (tool rows, sub-agents).
    var phase: String {
        if startupPending { return "Starting" }
        guard promptState == "running" else { return "" }
        if promptKind != "user", !prompt.isEmpty { return prompt }
        let text = status.trimmingCharacters(in: .whitespaces)
            .replacingOccurrences(of: "…", with: "")
            .trimmingCharacters(in: .whitespaces)
        let phase = text.components(separatedBy: " · ").first ?? ""
        return phase.isEmpty ? "Thinking" : phase
    }

    /// Messages waiting while a turn runs: steering ones and the rest.
    var pendingCounts: (steering: Int, queued: Int) {
        guard busy else { return (0, 0) }
        let steering = queuedModes.filter { $0 == "steering" }.count
        let typed = queuedModes.filter { ["queue", "steering", "interrupt", "shell"].contains($0) }.count
        return (steering, max(0, typed - steering))
    }

    /// The status strip's model, from socket state alone.
    func composerStatus(sendMode: String, sendModeOnce: Bool) -> PCodeComposerStatus {
        let counts = pendingCounts
        return PCodeComposerStatus(
            busy: busy || promptState == "running" || startupPending,
            phase: phase,
            sendMode: sendMode,
            sendModeOnce: sendModeOnce,
            starting: startupPending,
            steeringPending: counts.steering,
            queued: counts.queued,
            jobs: jobCount,
            btwRunning: asidesRunning,
            btwReady: asidesReady,
            cacheNote: "")
    }
}
