import Darwin
import Foundation

/// Structured diff payload (`EditPreview` morphs into `EditCompleted` under the same `call_id`,
/// coalescing with the matching `edit_file` / `write_file` tool call).
struct PCodeDiffPayload: Equatable {
    let callID: String
    let path: String
    let operation: String
    let patch: String
    let added: Int
    let removed: Int
    let isPreview: Bool
    let truncated: Bool
    let depth: Int
    let toolName: String
    let failed: Bool
    let elapsedSeconds: Double?

    init(
        callID: String,
        path: String,
        operation: String,
        patch: String,
        added: Int,
        removed: Int,
        isPreview: Bool,
        truncated: Bool,
        depth: Int,
        toolName: String = "",
        failed: Bool = false,
        elapsedSeconds: Double? = nil
    ) {
        self.callID = callID
        self.path = path
        self.operation = operation
        self.patch = patch
        self.added = added
        self.removed = removed
        self.isPreview = isPreview
        self.truncated = truncated
        self.depth = depth
        self.toolName = toolName
        self.failed = failed
        self.elapsedSeconds = elapsedSeconds
    }

    var displayToolName: String {
        if !toolName.isEmpty { return toolName }
        return operation.isEmpty ? "edit_file" : operation
    }

    var formattedElapsed: String? {
        guard let elapsedSeconds, elapsedSeconds > 0 else { return nil }
        if elapsedSeconds < 1 {
            return String(format: "%.2fs", elapsedSeconds)
        }
        if elapsedSeconds < 60 {
            return String(format: "%.1fs", elapsedSeconds)
        }
        let mins = Int(elapsedSeconds) / 60
        let secs = Int(elapsedSeconds) % 60
        return "\(mins)m\(secs)s"
    }
}

/// Unified domain event decoded from both saved `transcript.jsonl` (flat fields)
/// and live `pcode.host` Unix socket RPC notifications (nested `fields` under `$event`).
enum PCodeHostEvent: Equatable {
    case turnStarted(prompt: String, isSide: Bool, runID: String)
    case textDelta(String)
    case message(markdown: String)
    case thinkingDelta(String)
    case thinking(text: String, elapsedSeconds: Double?)
    case toolStarted(
        callID: String,
        parentCallID: String,
        name: String,
        detail: String,
        command: String,
        purpose: String,
        arguments: String = "")
    case toolSummary(
        callID: String,
        parentCallID: String,
        name: String,
        detail: String,
        command: String,
        failed: Bool,
        elapsedSeconds: Double?,
        error: String,
        result: String?,
        isBackgroundJob: Bool,
        exitCode: Int? = nil)
    case commandOutput(callID: String, command: String, output: String)
    case editPreview(callID: String, path: String, text: String, kind: String)
    case editCompleted(
        callID: String,
        parentCallID: String,
        path: String,
        operation: String,
        patch: String,
        added: Int,
        removed: Int,
        truncated: Bool)
    case workerStatus(
        callID: String,
        workerID: String,
        question: String?,
        answered: Bool?,
        status: String?)
    case turnEnded(status: PCodePeekTurn.Status)
    case user(text: String)
    case shellResult(command: String, output: String, failed: Bool, elapsedSeconds: Double?)
    case dropOutput(callID: String)
    case note(String)
    case warning(String)
    case error(String)
    case conversationReset
    /// The whole settled conversation, replacing what is shown (`history`: the host loaded a
    /// resumed session, switched to another, or moved to another branch).
    case history([PCodeHostEvent])
    /// A `state` notify: the session fields that changed since the last one.
    case sessionState(PCodeHostSessionDelta)
    /// The conversation's identity or settings changed (`session_changed`); carries the host's current session id.
    case sessionChanged(String)
    /// `/show-thinking` changed on the host (`thinking_mode`); carries pcode's mode name.
    case thinkingMode(String)
    /// Model, effort, and context window usage for the native window titlebar (`session_metadata`).
    case sessionMetadata(model: String, effort: String, context: String)
}

/// Parsing helpers for displaying `session_metadata` in the native title bar.
enum PCodeSessionMetadataDisplay {
    /// Splits `"provider:model"` on the first colon when both sides are non-empty.
    static func splitModel(_ raw: String) -> (provider: String?, name: String) {
        let trimmed = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        guard let colon = trimmed.firstIndex(of: ":") else {
            return (nil, trimmed)
        }
        let provider = String(trimmed[..<colon])
        let name = String(trimmed[trimmed.index(after: colon)...])
        guard !provider.isEmpty, !name.isEmpty else {
            return (nil, trimmed)
        }
        return (provider, name)
    }

    /// Returns the effort label when active, or nil when empty or `"n/a"`.
    static func normalizedEffort(_ raw: String) -> String? {
        let trimmed = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty, trimmed.lowercased() != "n/a" else { return nil }
        return trimmed
    }

    /// Parses `"used/limit"` (e.g. `"42k/200k"`, `"~1.2m/2m"`) into a clamped `0...1` ratio.
    static func contextFillRatio(_ raw: String) -> Double? {
        let cleaned = raw
            .trimmingCharacters(in: .whitespacesAndNewlines)
            .replacingOccurrences(of: "·", with: "")
            .trimmingCharacters(in: .whitespacesAndNewlines)
        let parts = cleaned.split(separator: "/", maxSplits: 1)
        guard parts.count == 2,
              let used = parseCompactTokens(String(parts[0])),
              let limit = parseCompactTokens(String(parts[1])),
              limit > 0
        else { return nil }
        return min(max(used / limit, 0), 1)
    }

    private static func parseCompactTokens(_ raw: String) -> Double? {
        var text = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        if text.hasPrefix("~") { text.removeFirst() }
        guard !text.isEmpty else { return nil }
        let multiplier: Double
        if let last = text.last?.lowercased(), last == "k" || last == "m" || last == "b" {
            text.removeLast()
            switch last {
            case "k": multiplier = 1_000
            case "m": multiplier = 1_000_000
            default: multiplier = 1_000_000_000
            }
        } else {
            multiplier = 1
        }
        guard let value = Double(text), value.isFinite, value >= 0 else { return nil }
        return value * multiplier
    }
}

/// One user message waiting to be delivered to the model (either steering the active turn
/// or queued for the next turn).
struct PCodePendingPrompt: Identifiable, Equatable {
    enum Mode: String, Equatable {
        case steering
        case queued
    }

    let id: String
    let text: String
    let mode: Mode
}

/// The `SESSION_FIELDS` (`pcode.controller`) a `state` notify carries that the app tracks.
/// Each is nil when that field did not change; the host sends only what changed.
struct PCodeHostSessionDelta: Equatable {
    var busy: Bool?
    var promptState: String?
    var turnOutcome: String?
    var queuedPrompts: [String]?
    var queuedModes: [String]?

    var isEmpty: Bool {
        busy == nil && promptState == nil && turnOutcome == nil
            && queuedPrompts == nil && queuedModes == nil
    }
}

/// The session state a host observer mirrors: the `state` deltas folded together, plus the
/// session id from the welcome (a `conversation_reset` clears it, as the host does).
struct PCodeHostSessionState: Equatable {
    var sessionID = ""
    var busy = false
    /// `Activity.prompt_state`: "", "running", or how the last prompt ended.
    var promptState = ""
    /// `Activity.turn_outcome`: "", "done", "cancelled" or "failed".
    var turnOutcome = ""
    /// `Activity.queued_prompts` / `queued_modes`: pending user messages waiting for delivery.
    var queuedPrompts: [String] = []
    var queuedModes: [String] = []

    /// User-typed pending messages (steering or queued), skipping internal wake/continue modes.
    var pendingPrompts: [PCodePendingPrompt] {
        var result: [PCodePendingPrompt] = []
        var seen: [String: Int] = [:]
        for (idx, text) in queuedPrompts.enumerated() {
            let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
            guard !trimmed.isEmpty else { continue }
            let rawMode = idx < queuedModes.count ? queuedModes[idx] : "queue"
            let mode: PCodePendingPrompt.Mode
            switch rawMode {
            case "steering":
                mode = .steering
            case "queue", "interrupt", "shell":
                mode = .queued
            default:
                continue
            }
            let baseKey = "\(mode.rawValue):\(trimmed)"
            let occurrence = seen[baseKey, default: 0]
            seen[baseKey] = occurrence + 1
            result.append(PCodePendingPrompt(
                id: "\(baseKey)#\(occurrence)",
                text: trimmed,
                mode: mode))
        }
        return result
    }

    init() {}

    /// The state a host reports at handshake, before its catch-up calls are folded in.
    init(welcome: PCodeHostSocketClient.Welcome) {
        sessionID = welcome.sessionID
        busy = welcome.hostState == "working"
        if busy { promptState = "running" }
        queuedPrompts = welcome.queuedPrompts
        queuedModes = welcome.queuedModes
    }

    /// Fold one host event in; returns whether anything changed.
    @discardableResult
    mutating func apply(_ event: PCodeHostEvent) -> Bool {
        let before = self
        switch event {
        case .sessionState(let delta):
            if let busy = delta.busy { self.busy = busy }
            if let promptState = delta.promptState { self.promptState = promptState }
            if let turnOutcome = delta.turnOutcome { self.turnOutcome = turnOutcome }
            if let queuedPrompts = delta.queuedPrompts { self.queuedPrompts = queuedPrompts }
            if let queuedModes = delta.queuedModes { self.queuedModes = queuedModes }
        case .sessionChanged(let id):
            if !id.isEmpty { sessionID = id }
        case .conversationReset:
            sessionID = ""
            turnOutcome = ""
            queuedPrompts = []
            queuedModes = []
        default:
            break
        }
        return self != before
    }
}

/// Dual-format event decoder for `transcript.jsonl` and live `pcode.host` RPC notifications.
enum PCodeHostEventDecoder {
    /// Decode one line from `transcript.jsonl` (fields spread beside `"kind"`).
    static func decodeJournalRecord(_ record: [String: Any]) -> PCodeHostEvent? {
        guard let kind = record["kind"] as? String else { return nil }
        switch kind {
        case "turn_started":
            guard let prompt = record["prompt"] as? String else { return nil }
            let isAside = (record["aside"] as? Bool) == true
            let hasSidePayload: Bool
            if let b = record["side"] as? Bool {
                hasSidePayload = b
            } else {
                hasSidePayload = (record["side"] != nil)
            }
            let runID = (record["run_id"] as? String) ?? ""
            return .turnStarted(prompt: prompt, isSide: isAside || hasSidePayload, runID: runID)
        case "steering":
            // A message the user sent mid-turn, journaled when it was delivered.
            guard let prompt = record["prompt"] as? String, !prompt.isEmpty else { return nil }
            return .user(text: prompt)
        case "turn_completed":
            return .turnEnded(status: .complete)
        case "turn_cancelled":
            return .turnEnded(status: .cancelled)
        case "turn_failed":
            return .turnEnded(status: .failed)
        default:
            return decodeRuntimeEvent(kind: kind, fields: record)
        }
    }

    /// Decode one live RPC view call (`[method, args, kwargs]` or `notify` message).
    static func decodeRPCCall(method: String, args: [Any], kwargs: [String: Any]) -> PCodeHostEvent? {
        switch method {
        case "turn_started":
            let prompt = (args.first as? String) ?? (kwargs["prompt"] as? String) ?? ""
            let isSide = (kwargs["side"] as? Bool) == true || (kwargs["aside"] as? Bool) == true
            return .turnStarted(prompt: prompt, isSide: isSide, runID: "")
        case "cancelled":
            return .turnEnded(status: .cancelled)
        case "after_turn":
            let raw = (args.first as? String) ?? (kwargs["outcome"] as? String) ?? "complete"
            let status: PCodePeekTurn.Status
            switch raw {
            case "cancelled": status = .cancelled
            case "failed", "error": status = .failed
            default: status = .complete
            }
            return .turnEnded(status: status)
        case "turn_event":
            guard let first = args.first as? [String: Any],
                  let wrapped = first["$event"] as? [String: Any],
                  let kind = wrapped["kind"] as? String,
                  let fields = wrapped["fields"] as? [String: Any]
            else { return nil }
            return decodeRuntimeEvent(kind: kind, fields: fields)
        case "user":
            if let text = args.first as? String, !text.isEmpty {
                return .user(text: text)
            }
            return nil
        case "shell_result":
            let command = (args.first as? String) ?? (kwargs["command"] as? String) ?? ""
            let output = (args.count > 1 ? args[1] as? String : nil) ?? (kwargs["output"] as? String) ?? ""
            let failed = (kwargs["failed"] as? Bool) == true
            let elapsed = kwargs["elapsed_seconds"] as? Double
            return .shellResult(command: command, output: output, failed: failed, elapsedSeconds: elapsed)
        case "drop_output":
            if let callID = args.first as? String, !callID.isEmpty {
                return .dropOutput(callID: callID)
            }
            return nil
        case "note", "retained_note":
            if let text = args.first as? String, !text.isEmpty {
                return .note(text)
            }
            return nil
        case "warning":
            if let text = args.first as? String, !text.isEmpty {
                return .warning(text)
            }
            return nil
        case "error":
            if let text = args.first as? String, !text.isEmpty {
                return .error(text)
            }
            return nil
        case "conversation_reset":
            return .conversationReset
        case "history":
            guard let records = args.first as? [[String: Any]] else { return nil }
            return .history(records.compactMap(decodeJournalRecord))
        case "state":
            guard let changes = args.first as? [String: Any] else { return nil }
            let delta = PCodeHostSessionDelta(
                busy: changes["busy"] as? Bool,
                promptState: changes["prompt_state"] as? String,
                turnOutcome: changes["turn_outcome"] as? String,
                queuedPrompts: changes["queued_prompts"] as? [String],
                queuedModes: changes["queued_modes"] as? [String])
            // Deltas of only untracked fields (notice, status) are dropped here.
            return delta.isEmpty ? nil : .sessionState(delta)
        case "session_changed":
            return .sessionChanged((args.first as? String) ?? "")
        case "thinking_mode":
            guard let mode = args.first as? String, !mode.isEmpty else { return nil }
            return .thinkingMode(mode)
        case "session_metadata":
            guard let dict = args.first as? [String: Any] else { return nil }
            let model = (dict["model"] as? String) ?? ""
            let effort = (dict["effort"] as? String) ?? ""
            let context = (dict["context"] as? String) ?? ""
            return .sessionMetadata(model: model, effort: effort, context: context)
        default:
            // Terminal-only or unknown notifications are ignored safely.
            return nil
        }
    }

    static func decodeRuntimeEvent(kind: String, fields: [String: Any]) -> PCodeHostEvent? {
        switch kind {
        case "TextDelta":
            guard let text = fields["text"] as? String else { return nil }
            return .textDelta(text)
        case "Message":
            guard let markdown = fields["markdown"] as? String else { return nil }
            return .message(markdown: markdown)
        case "ThinkingDelta":
            guard let text = fields["text"] as? String else { return nil }
            return .thinkingDelta(text)
        case "Thinking":
            guard let text = fields["text"] as? String else { return nil }
            return .thinking(text: text, elapsedSeconds: (fields["elapsed_seconds"] as? NSNumber)?.doubleValue)
        case "ToolStarted":
            let callID = (fields["call_id"] as? String) ?? ""
            guard !callID.isEmpty else { return nil }
            let name = (fields["name"] as? String) ?? ""
            let arguments: String = PCodeSessionChangesStore.isSynthesizableEditTool(name)
                ? ((fields["arguments"] as? String) ?? "")
                : ""
            return .toolStarted(
                callID: callID,
                parentCallID: (fields["parent_call_id"] as? String) ?? "",
                name: name,
                detail: (fields["detail"] as? String) ?? "",
                command: (fields["command"] as? String) ?? "",
                purpose: (fields["purpose"] as? String) ?? "",
                arguments: arguments)
        case "ToolSummary", "JobFinished":
            let callID = (fields["call_id"] as? String) ?? ""
            return .toolSummary(
                callID: callID,
                parentCallID: (fields["parent_call_id"] as? String) ?? "",
                name: (fields["name"] as? String) ?? "",
                detail: (fields["detail"] as? String) ?? "",
                command: (fields["command"] as? String) ?? "",
                failed: (fields["failed"] as? Bool) == true,
                elapsedSeconds: fields["elapsed_seconds"] as? Double,
                error: (fields["error"] as? String) ?? "",
                result: fields["result"] as? String,
                isBackgroundJob: kind == "JobFinished" || (fields["execution"] as? String) == "background",
                exitCode: PCodePeekToolCall.exitCode(from: fields["exit_code"]))
        case "CommandOutput":
            let callID = (fields["call_id"] as? String) ?? ""
            guard !callID.isEmpty else { return nil }
            return .commandOutput(
                callID: callID,
                command: (fields["command"] as? String) ?? "",
                output: (fields["output"] as? String) ?? "")
        case "EditPreview":
            let callID = (fields["call_id"] as? String) ?? ""
            guard !callID.isEmpty else { return nil }
            return .editPreview(
                callID: callID,
                path: (fields["path"] as? String) ?? "",
                text: (fields["text"] as? String) ?? "",
                kind: (fields["kind"] as? String) ?? "edit")
        case "EditCompleted":
            let callID = (fields["call_id"] as? String) ?? ""
            return .editCompleted(
                callID: callID,
                parentCallID: (fields["parent_call_id"] as? String) ?? "",
                path: (fields["path"] as? String) ?? "",
                operation: (fields["operation"] as? String) ?? "edit",
                patch: (fields["patch"] as? String) ?? "",
                added: (fields["added"] as? Int) ?? 0,
                removed: (fields["removed"] as? Int) ?? 0,
                truncated: (fields["truncated"] as? Bool) == true)
        case "WorkerStatus":
            let callID = (fields["call_id"] as? String) ?? ""
            guard !callID.isEmpty else { return nil }
            let workerID = (fields["worker_id"] as? String) ?? ""
            let question = fields["question"] as? String
            let answered = fields["answered"] as? Bool
            let status = fields["status"] as? String
            return .workerStatus(
                callID: callID,
                workerID: workerID,
                question: question,
                answered: answered,
                status: status)
        case "PlanUpdated", "PlanPreview", "ChildText", "ChildPlan", "RunStatus", "CacheBust":
            // Display-only plan/status events do not create permanent transcript items:
            // a plan is a live snapshot, not conversation history.
            return nil
        default:
            return nil
        }
    }
}

/// Stateful event reducer that maps `PCodeHostEvent`s into ID-addressed mutations on
/// `PCodeTranscriptDocumentView`:
/// - `call_id` upserts (`ToolStarted` -> `CommandOutput`/`EditPreview` -> `ToolSummary`/`JobFinished`/`EditCompleted`)
/// - `parent_call_id` flattened nesting (`depth = 1`, with orphan buffering when child arrives before parent)
/// - `ThinkingDelta` / `Thinking` collapsed-by-default thinking cards
final class PCodeTranscriptEventReducer {
    static let maxCommandOutputLines = 80

    private weak var documentView: PCodeTranscriptDocumentView?
    private var messageCounter = 0
    private var toolCounter = 0
    private var thinkingCounter = 0
    private var noteCounter = 0
    private var streamedTextInCurrentMessage = false
    private var turnAlreadyTerminated = false
    private var activeThinkingID: String?
    private var activeThinkingText: String = ""
    /// Wall-clock start of the live thinking block (nil while replaying history).
    private var activeThinkingStartedAt: Date?
    private var activeShellModeCallID: String?
    private var toolCallsByID: [String: PCodePeekToolCall] = [:]
    private var diffsByCallID: [String: PCodeDiffPayload] = [:]
    private var toolDepthByID: [String: Int] = [:]
    private var pendingChildrenByParent: [String: [PCodeTranscriptItem]] = [:]

    init(documentView: PCodeTranscriptDocumentView) {
        self.documentView = documentView
    }

    func reset() {
        messageCounter = 0
        toolCounter = 0
        thinkingCounter = 0
        noteCounter = 0
        streamedTextInCurrentMessage = false
        turnAlreadyTerminated = false
        activeThinkingID = nil
        activeThinkingText = ""
        activeThinkingStartedAt = nil
        activeShellModeCallID = nil
        toolCallsByID.removeAll()
        diffsByCallID.removeAll()
        toolDepthByID.removeAll()
        pendingChildrenByParent.removeAll()
    }

    /// Replace the transcript with settled history and land on its bottom, settling any tools
    /// or edit previews it leaves running. Built in one batch (`reloadHistory`).
    func loadHistory(_ events: [PCodeHostEvent]) {
        guard let doc = documentView else { return }
        reset()
        doc.reloadHistory {
            for event in events {
                apply(event, reveal: false, isLive: false)
            }
            settleRunningItems(failed: false, in: doc)
        }
    }

    func apply(_ event: PCodeHostEvent, reveal: Bool, isLive: Bool? = nil) {
        guard let doc = documentView else { return }
        let liveClock = isLive ?? reveal
        switch event {
        case .conversationReset:
            reset()
            doc.setItems([], pinToBottom: true)

        case .history(let events):
            loadHistory(events)

        case .turnStarted(let prompt, let isSide, let runID):
            settleRunningItems(failed: false, in: doc)
            settleActiveThinking(in: doc, reveal: reveal)
            activeShellModeCallID = nil
            streamedTextInCurrentMessage = false
            turnAlreadyTerminated = false
            doc.beginStreamingTurn(
                prompt: prompt,
                isSide: isSide,
                turnID: runID.isEmpty ? nil : runID,
                forcePin: false,
                reveal: reveal)

        case .textDelta(let delta):
            settleActiveThinking(in: doc, reveal: reveal)
            if !streamedTextInCurrentMessage {
                messageCounter += 1
                doc.beginStreamingMessage(id: "host-msg-\(messageCounter)")
                streamedTextInCurrentMessage = true
            }
            doc.appendStreamingDelta(delta, reveal: reveal)

        case .message(let markdown):
            settleActiveThinking(in: doc, reveal: reveal)
            if streamedTextInCurrentMessage {
                // Deltas already carried this assistant message; settle the tail without duplicating.
                streamedTextInCurrentMessage = false
                doc.finishStreamingMessage(reveal: reveal)
            } else {
                messageCounter += 1
                doc.beginStreamingMessage(id: "host-msg-\(messageCounter)")
                doc.appendStreamingDelta(markdown, reveal: reveal)
                doc.finishStreamingMessage(reveal: reveal)
            }

        case .thinkingDelta(let delta):
            guard !delta.isEmpty else { return }
            if activeThinkingID == nil {
                thinkingCounter += 1
                activeThinkingID = "host-thinking-\(thinkingCounter)"
                activeThinkingText = ""
                // Only a block streaming in front of the user: a connect's catch-up replay
                // is live but instant, so timing it would always say "1s".
                activeThinkingStartedAt = reveal ? Date() : nil
            }
            let id = activeThinkingID!
            activeThinkingText.append(delta)
            upsertThinking(
                id: id,
                PCodeThinkingPayload(text: activeThinkingText, isStreaming: true),
                in: doc,
                reveal: reveal)

        case .thinking(let text, let elapsedSeconds):
            let id: String
            if let existing = activeThinkingID {
                id = existing
            } else {
                thinkingCounter += 1
                id = "host-thinking-\(thinkingCounter)"
            }
            let measured = elapsedSeconds ?? activeThinkingStartedAt.map { Date().timeIntervalSince($0) }
            activeThinkingID = nil
            activeThinkingText = ""
            activeThinkingStartedAt = nil
            guard !text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else { return }
            upsertThinking(
                id: id,
                PCodeThinkingPayload(text: text, isStreaming: false, elapsedSeconds: measured),
                in: doc,
                reveal: reveal)

        case .toolStarted(let callID, let parentCallID, let name, let detail, let command, let purpose, _):
            // A retried request can skip the `Thinking` marker; never leave a block pulsing.
            if parentCallID.isEmpty { settleActiveThinking(in: doc, reveal: reveal) }
            finishActiveStreamIfNeeded(in: doc, reveal: reveal)
            let existing = toolCallsByID[callID]
            if existing == nil {
                toolCounter += 1
            }
            let call = PCodePeekToolCall(
                id: existing?.id ?? toolCounter,
                name: name.isEmpty ? (existing?.name ?? "tool") : name,
                detail: detail.isEmpty ? (existing?.detail ?? "") : detail,
                failed: false,
                elapsedSeconds: nil,
                command: command.isEmpty ? (existing?.command ?? "") : command,
                output: existing?.output ?? "",
                isRunning: true,
                startedAt: existing?.startedAt ?? (liveClock ? Date() : nil),
                isBackgroundJob: existing?.isBackgroundJob ?? false,
                purpose: purpose.isEmpty ? (existing?.purpose ?? "") : purpose)
            toolCallsByID[callID] = call
            let depth = parentCallID.isEmpty ? 0 : 1
            toolDepthByID[callID] = depth
            let itemID = "call-\(callID)"
            let item = PCodeTranscriptItem(
                id: itemID,
                payload: .tool(call, isExpanded: doc.isItemExpanded(id: itemID)),
                spacingAfter: 5,
                depth: depth)
            insertWithParentNesting(item: item, parentCallID: parentCallID, reveal: reveal, in: doc)
            flushOrphans(forParentCallID: callID, reveal: reveal, in: doc)

        case .toolSummary(let callID, let parentCallID, let name, let detail, let command, let failed, let elapsed, let error, let result, let isBackgroundJob, let exitCode):
            finishActiveStreamIfNeeded(in: doc, reveal: reveal)
            toolCounter += 1
            let key = callID.isEmpty ? "anon-\(toolCounter)" : callID
            let itemID = "call-\(key)"
            // If an `EditCompleted` / `EditPreview` diff already owns this `call_id`, merge
            // tool name / status / elapsed onto the collapsible diff row instead of replacing it.
            if let existingDiff = diffsByCallID[key] {
                let merged = PCodeDiffPayload(
                    callID: existingDiff.callID,
                    path: existingDiff.path,
                    operation: existingDiff.operation,
                    patch: existingDiff.patch,
                    added: existingDiff.added,
                    removed: existingDiff.removed,
                    isPreview: false,
                    truncated: existingDiff.truncated,
                    depth: existingDiff.depth,
                    toolName: name.isEmpty ? existingDiff.toolName : name,
                    failed: failed,
                    elapsedSeconds: elapsed ?? existingDiff.elapsedSeconds)
                diffsByCallID[key] = merged
                toolCallsByID.removeValue(forKey: key)
                let item = PCodeTranscriptItem(
                    id: itemID,
                    payload: .diff(merged, isExpanded: doc.isItemExpanded(id: itemID)),
                    spacingAfter: 5,
                    depth: merged.depth)
                insertWithParentNesting(item: item, parentCallID: parentCallID, reveal: reveal, in: doc)
                if !callID.isEmpty {
                    flushOrphans(forParentCallID: callID, reveal: reveal, in: doc)
                }
                return
            }

            let existing = toolCallsByID[key]
            let mergedDetail: String
            if !detail.isEmpty {
                mergedDetail = detail
            } else if !error.isEmpty {
                mergedDetail = error
            } else {
                mergedDetail = existing?.detail ?? ""
            }
            let call = PCodePeekToolCall(
                id: existing?.id ?? toolCounter,
                name: name.isEmpty ? (existing?.name ?? "tool") : name,
                detail: mergedDetail,
                failed: failed,
                elapsedSeconds: elapsed,
                command: command.isEmpty ? (existing?.command ?? "") : command,
                output: result ?? existing?.output ?? "",
                isRunning: false,
                startedAt: nil,
                isBackgroundJob: isBackgroundJob,
                exitCode: exitCode,
                purpose: existing?.purpose ?? "")
            toolCallsByID[key] = call
            let depth = toolDepthByID[key] ?? (parentCallID.isEmpty ? 0 : 1)
            let item = PCodeTranscriptItem(
                id: itemID,
                payload: .tool(call, isExpanded: call.canExpand && doc.isItemExpanded(id: itemID)),
                spacingAfter: 5,
                depth: depth)
            insertWithParentNesting(item: item, parentCallID: parentCallID, reveal: reveal, in: doc)
            if !callID.isEmpty {
                flushOrphans(forParentCallID: callID, reveal: reveal, in: doc)
            }

        case .commandOutput(let callID, let command, let output):
            // Cap live command output snapshots to the last N lines so they never pump the conveyor.
            let capped = Self.capTailLines(output, maxLines: Self.maxCommandOutputLines)
            let existing = toolCallsByID[callID]
            if existing == nil {
                toolCounter += 1
            }
            if callID.hasPrefix("shell_mode_") {
                activeShellModeCallID = callID
            }
            let stillRunning = existing?.isRunning ?? true
            let call = PCodePeekToolCall(
                id: existing?.id ?? toolCounter,
                name: existing?.name ?? "shell",
                detail: existing?.detail ?? command,
                failed: existing?.failed ?? false,
                elapsedSeconds: existing?.elapsedSeconds,
                command: command.isEmpty ? (existing?.command ?? "") : command,
                output: capped,
                isRunning: stillRunning,
                startedAt: stillRunning ? (existing?.startedAt ?? (liveClock ? Date() : nil)) : nil,
                isBackgroundJob: existing?.isBackgroundJob ?? false,
                exitCode: existing?.exitCode,
                purpose: existing?.purpose ?? "")
            toolCallsByID[callID] = call
            let depth = toolDepthByID[callID] ?? 0
            let itemID = "call-\(callID)"
            let item = PCodeTranscriptItem(
                id: itemID,
                payload: .tool(call, isExpanded: doc.isItemExpanded(id: itemID)),
                spacingAfter: 5,
                depth: depth)
            doc.upsertItem(item, reveal: false)

        case .editPreview(let callID, let path, let text, let kind):
            let itemID = "call-\(callID)"
            if path.isEmpty {
                diffsByCallID.removeValue(forKey: callID)
                doc.removeItem(id: itemID)
                return
            }
            guard kind == "edit" else { return }
            let depth = toolDepthByID[callID] ?? 0
            let existingTool = toolCallsByID.removeValue(forKey: callID)
            let existingDiff = diffsByCallID[callID]
            let payload = PCodeDiffPayload(
                callID: callID,
                path: path,
                operation: "edit",
                patch: text,
                added: 0,
                removed: 0,
                isPreview: true,
                truncated: false,
                depth: depth,
                toolName: existingTool?.name ?? existingDiff?.toolName ?? "edit_file",
                failed: existingTool?.failed ?? existingDiff?.failed ?? false,
                elapsedSeconds: existingTool?.elapsedSeconds ?? existingDiff?.elapsedSeconds)
            diffsByCallID[callID] = payload
            let item = PCodeTranscriptItem(
                id: itemID,
                payload: .diff(payload, isExpanded: doc.isItemExpanded(id: itemID)),
                spacingAfter: 5,
                depth: depth)
            doc.upsertItem(item, reveal: reveal)

        case .editCompleted(let callID, let parentCallID, let path, let operation, let patch, let added, let removed, let truncated):
            finishActiveStreamIfNeeded(in: doc, reveal: reveal)
            toolCounter += 1
            let key = callID.isEmpty ? "edit-\(toolCounter)" : callID
            let depth = parentCallID.isEmpty ? (toolDepthByID[key] ?? 0) : 1
            let existingTool = toolCallsByID.removeValue(forKey: key)
            let existingDiff = diffsByCallID[key]
            let itemID = "call-\(key)"
            let payload = PCodeDiffPayload(
                callID: key,
                path: path,
                operation: operation,
                patch: patch,
                added: added,
                removed: removed,
                isPreview: false,
                truncated: truncated,
                depth: depth,
                toolName: existingTool?.name ?? existingDiff?.toolName ?? "",
                failed: existingTool?.failed ?? existingDiff?.failed ?? false,
                elapsedSeconds: existingTool?.elapsedSeconds ?? existingDiff?.elapsedSeconds)
            diffsByCallID[key] = payload
            let item = PCodeTranscriptItem(
                id: itemID,
                payload: .diff(payload, isExpanded: doc.isItemExpanded(id: itemID)),
                spacingAfter: 5,
                depth: depth)
            insertWithParentNesting(item: item, parentCallID: parentCallID, reveal: reveal, in: doc)

        case .workerStatus:
            // Display-only for the Inspector Agents tab; does not create a transcript item.
            break

        case .turnEnded(let status):
            settleActiveThinking(in: doc, reveal: reveal)
            activeShellModeCallID = nil
            streamedTextInCurrentMessage = false
            // Flush any unresolved orphan children BEFORE settling running items so flushed
            // orphans are settled too and cannot overwrite settled items back to `isRunning`.
            if !pendingChildrenByParent.isEmpty {
                let leftovers = pendingChildrenByParent.values.flatMap { $0 }
                pendingChildrenByParent.removeAll()
                for orphan in leftovers {
                    doc.upsertItem(orphan, reveal: reveal)
                }
            }
            // Settle any tool calls or edit previews that were still running when the turn ended
            // (e.g. cancelled or failed turns, or historical replays without a closing ToolSummary).
            settleRunningItems(failed: status != .complete, in: doc)
            if turnAlreadyTerminated && status == .complete {
                // `cancelled` was already emitted right before `after_turn`; don't overwrite with `.complete`.
                return
            }
            if status != .complete {
                turnAlreadyTerminated = true
            }
            doc.finishStreamingTurn(status: status, reveal: reveal)

        case .user(let text):
            // `!command` shell mode emits `user` outside a model turn; a steering
            // message emits it mid-turn, when it joins the next model request.
            // Either way it is a prompt row, and a running turn carries on below it.
            activeShellModeCallID = nil
            settleActiveThinking(in: doc, reveal: reveal)
            streamedTextInCurrentMessage = false
            doc.beginStreamingTurn(prompt: text, isSide: false, forcePin: false, reveal: reveal)

        case .shellResult(let command, let output, let failed, let elapsedSeconds):
            let key: String
            if let active = activeShellModeCallID {
                key = active
                activeShellModeCallID = nil
            } else {
                toolCounter += 1
                key = "shell_mode_\(toolCounter)"
            }
            let existing = toolCallsByID[key]
            let call = PCodePeekToolCall(
                id: existing?.id ?? toolCounter,
                name: "shell",
                detail: command.isEmpty ? (existing?.detail ?? "") : command,
                failed: failed,
                elapsedSeconds: elapsedSeconds,
                command: command.isEmpty ? (existing?.command ?? "") : command,
                output: Self.capTailLines(output, maxLines: Self.maxCommandOutputLines),
                isRunning: false,
                startedAt: nil,
                isBackgroundJob: false)
            toolCallsByID[key] = call
            let itemID = "call-\(key)"
            let item = PCodeTranscriptItem(
                id: itemID,
                payload: .tool(call, isExpanded: call.canExpand && doc.isItemExpanded(id: itemID)),
                spacingAfter: 5,
                depth: 0)
            doc.upsertItem(item, reveal: reveal)
            doc.finishStreamingTurn(status: failed ? .failed : .complete, reveal: reveal)

        case .dropOutput(let callID):
            // `drop_output` always fires in `run_shell`'s finally block before `shell_result`;
            // stop the spinner without prematurely flashing red (a real failure sets `failed`
            // in `shell_result` or `turnEnded`).
            guard let existing = toolCallsByID[callID], existing.isRunning else { return }
            let settled = PCodePeekToolCall(
                id: existing.id,
                name: existing.name,
                detail: existing.detail,
                failed: existing.failed,
                elapsedSeconds: existing.elapsedSeconds,
                command: existing.command,
                output: existing.output,
                isRunning: false,
                startedAt: nil,
                isBackgroundJob: existing.isBackgroundJob,
                exitCode: existing.exitCode,
                purpose: existing.purpose)
            toolCallsByID[callID] = settled
            let itemID = "call-\(callID)"
            let item = PCodeTranscriptItem(
                id: itemID,
                payload: .tool(settled, isExpanded: settled.canExpand && doc.isItemExpanded(id: itemID)),
                spacingAfter: 5,
                depth: toolDepthByID[callID] ?? 0)
            doc.upsertItem(item, reveal: false)

        case .note(let text):
            noteCounter += 1
            let item = PCodeTranscriptItem(
                id: "host-note-\(noteCounter)",
                payload: .note(text: text, tone: .info),
                spacingAfter: 4)
            doc.upsertItem(item, reveal: reveal)

        case .warning(let text):
            noteCounter += 1
            let item = PCodeTranscriptItem(
                id: "host-note-\(noteCounter)",
                payload: .note(text: text, tone: .warning),
                spacingAfter: 4)
            doc.upsertItem(item, reveal: reveal)

        case .error(let text):
            noteCounter += 1
            let item = PCodeTranscriptItem(
                id: "host-note-\(noteCounter)",
                payload: .note(text: text, tone: .error),
                spacingAfter: 6)
            doc.upsertItem(item, reveal: reveal)

        case .sessionState, .sessionChanged, .sessionMetadata:
            // Session state and titlebar metadata, not transcript content.
            break

        case .thinkingMode(let mode):
            doc.showThinking = PCodeThinkingVisibility.isShown(mode: mode)
        }
    }

    private func settleRunningItems(failed: Bool, in doc: PCodeTranscriptDocumentView) {
        for (callID, call) in toolCallsByID where call.isRunning && diffsByCallID[callID] == nil {
            let settled = PCodePeekToolCall(
                id: call.id,
                name: call.name,
                detail: call.detail,
                failed: call.failed || failed,
                elapsedSeconds: call.elapsedSeconds,
                command: call.command,
                output: call.output,
                isRunning: false,
                startedAt: nil,
                isBackgroundJob: call.isBackgroundJob,
                exitCode: call.exitCode,
                purpose: call.purpose)
            toolCallsByID[callID] = settled
            let itemID = "call-\(callID)"
            let item = PCodeTranscriptItem(
                id: itemID,
                payload: .tool(settled, isExpanded: settled.canExpand && doc.isItemExpanded(id: itemID)),
                spacingAfter: 5,
                depth: toolDepthByID[callID] ?? 0)
            doc.upsertItem(item, reveal: false)
        }
        for (callID, diff) in diffsByCallID where diff.isPreview {
            let itemID = "call-\(callID)"
            if callID.hasPrefix("edit-preview:") {
                diffsByCallID.removeValue(forKey: callID)
                doc.removeItem(id: itemID)
                continue
            }
            let settled = PCodeDiffPayload(
                callID: diff.callID,
                path: diff.path,
                operation: diff.operation,
                patch: diff.patch,
                added: diff.added,
                removed: diff.removed,
                isPreview: false,
                truncated: diff.truncated,
                depth: diff.depth,
                toolName: diff.toolName,
                failed: diff.failed || failed,
                elapsedSeconds: diff.elapsedSeconds)
            diffsByCallID[callID] = settled
            let item = PCodeTranscriptItem(
                id: itemID,
                payload: .diff(settled, isExpanded: doc.isItemExpanded(id: itemID)),
                spacingAfter: 5,
                depth: diff.depth)
            doc.upsertItem(item, reveal: false)
        }
    }

    private func upsertThinking(
        id: String,
        _ thinking: PCodeThinkingPayload,
        in doc: PCodeTranscriptDocumentView,
        reveal: Bool
    ) {
        let item = PCodeTranscriptItem(
            id: id,
            payload: .thinking(thinking),
            spacingAfter: 6)
        doc.upsertItem(item, reveal: reveal)
    }

    /// Settle a streamed thinking block that ended without its `Thinking` marker
    /// (answer text started, or the turn ended or was cancelled mid-thought).
    private func settleActiveThinking(in doc: PCodeTranscriptDocumentView, reveal: Bool) {
        guard let id = activeThinkingID else { return }
        let text = activeThinkingText
        let elapsed = activeThinkingStartedAt.map { Date().timeIntervalSince($0) }
        activeThinkingID = nil
        activeThinkingText = ""
        activeThinkingStartedAt = nil
        guard !text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else { return }
        upsertThinking(
            id: id,
            PCodeThinkingPayload(text: text, isStreaming: false, elapsedSeconds: elapsed),
            in: doc,
            reveal: reveal)
    }

    private func finishActiveStreamIfNeeded(in doc: PCodeTranscriptDocumentView, reveal: Bool) {
        streamedTextInCurrentMessage = false
        doc.finishStreamingMessage(reveal: reveal)
    }

    private func flushOrphans(forParentCallID parentCallID: String, reveal: Bool, in doc: PCodeTranscriptDocumentView) {
        if let orphans = pendingChildrenByParent.removeValue(forKey: parentCallID) {
            for orphan in orphans {
                insertWithParentNesting(item: orphan, parentCallID: parentCallID, reveal: reveal, in: doc)
            }
        }
    }

    private func insertWithParentNesting(
        item: PCodeTranscriptItem,
        parentCallID: String,
        reveal: Bool,
        in doc: PCodeTranscriptDocumentView
    ) {
        guard !parentCallID.isEmpty else {
            doc.upsertItem(item, reveal: reveal)
            return
        }
        let parentItemID = "call-\(parentCallID)"
        if let parentIdx = doc.indexOfRawItem(id: parentItemID) {
            // Insert right after the parent and any already-nested siblings.
            var insertIdx = parentIdx + 1
            while insertIdx < doc.rawItems.count && doc.rawItems[insertIdx].depth > 0 {
                insertIdx += 1
            }
            doc.upsertItem(item, insertAt: insertIdx, reveal: reveal)
        } else {
            // Orphan child arrived before parent in catch-up: buffer until parent arrives.
            pendingChildrenByParent[parentCallID, default: []].append(item)
        }
    }

    static func capTailLines(_ text: String, maxLines: Int) -> String {
        guard maxLines > 0 else { return "" }
        let lines = text.split(separator: "\n", omittingEmptySubsequences: false)
        guard lines.count > maxLines else { return text }
        return "… (\(lines.count - maxLines) earlier lines omitted)\n" + lines.suffix(maxLines).joined(separator: "\n")
    }
}

/// Read-only Unix-socket client for `pcode.host` (`observe_transcript` handshake v1).
///
/// Reuses `PCodePicker`'s socket safety pattern: `AF_UNIX`, `SO_NOSIGPIPE`, `getpeereid`
/// same-user check, and a dedicated background reader thread with a growable buffer so
/// multi-megabyte welcome snapshots never block the main thread.
final class PCodeHostSocketClient {
    static let observerVersion = 1

    struct Welcome {
        let hostID: String
        let sessionID: String
        let title: String
        /// The host entry's state at handshake (`working`, `idle`, ...).
        let hostState: String
        let pacedScrollback: PCodeStreamingPaceMode
        /// The host's `show_thinking` mode; nil from a host too old to report it.
        var showThinking: String? = nil
        var model: String = ""
        var effort: String = ""
        var context: String = ""
        var queuedPrompts: [String] = []
        var queuedModes: [String] = []
        let history: [PCodeHostEvent]
        let catchUpCalls: [PCodeHostEvent]
    }

    /// Scoped per-connection state so `close()` only calls `shutdown` while the reader thread
    /// exclusively owns `Darwin.close(sock)` (eliminating FD-reuse and reconnect races).
    private final class ConnectionSession {
        let lock = NSLock()
        var fd: Int32 = -1
        var isClosed = false

        func requestShutdown() {
            lock.lock()
            isClosed = true
            let current = fd
            lock.unlock()
            if current >= 0 {
                Darwin.shutdown(current, SHUT_RDWR)
            }
        }
    }

    private let lock = NSLock()
    private var activeSession: ConnectionSession?

    deinit {
        close()
    }

    /// Connect to a running host's `<id>.sock` and stream welcome + live events on the main queue.
    func connect(
        socketPath: String,
        onReady: @escaping (Welcome) -> Void,
        onEvent: @escaping (PCodeHostEvent) -> Void,
        onError: @escaping (String) -> Void,
        onClosed: @escaping () -> Void
    ) {
        let session = ConnectionSession()
        lock.lock()
        let previous = activeSession
        activeSession = session
        lock.unlock()
        previous?.requestShutdown()

        let thread = Thread { [weak self] in
            self?.runReaderLoop(
                session: session,
                socketPath: socketPath,
                onReady: onReady,
                onEvent: onEvent,
                onError: onError,
                onClosed: onClosed)
        }
        thread.name = "pcode.host.observer"
        thread.qualityOfService = .userInitiated
        thread.start()
    }

    func close() {
        lock.lock()
        let current = activeSession
        activeSession = nil
        lock.unlock()
        current?.requestShutdown()
    }

    private func isCurrent(_ session: ConnectionSession) -> Bool {
        lock.lock()
        defer { lock.unlock() }
        return activeSession === session
    }

    private func runReaderLoop(
        session: ConnectionSession,
        socketPath: String,
        onReady: @escaping (Welcome) -> Void,
        onEvent: @escaping (PCodeHostEvent) -> Void,
        onError: @escaping (String) -> Void,
        onClosed: @escaping () -> Void
    ) {
        func emitError(_ msg: String) {
            guard isCurrent(session) else { return }
            DispatchQueue.main.async { [weak self] in
                guard let self, self.isCurrent(session) else { return }
                onError(msg)
            }
        }

        let sock: Int32
        switch PCodeHostLineSocket.connect(path: socketPath) {
        case .success(let fd): sock = fd
        case .failure(let error):
            emitError(error.message)
            return
        }
        // Reader thread exclusively owns `Darwin.close(sock)`.
        defer {
            session.lock.lock()
            session.fd = -1
            session.isClosed = true
            session.lock.unlock()
            Darwin.close(sock)
        }

        session.lock.lock()
        if session.isClosed {
            session.lock.unlock()
            return
        }
        session.fd = sock
        session.lock.unlock()

        // Send `observe_transcript` handshake.
        let helloLine = "{\"type\":\"observe_transcript\",\"version\":\(Self.observerVersion)}\n"
        if !PCodeHostLineSocket.writeAll(sock, Data(helloLine.utf8)) {
            emitError("Failed to send observe_transcript handshake.")
            return
        }

        // Read newline-delimited JSON off the main thread.
        var lines = PCodeLineBuffer()
        var chunk = [UInt8](repeating: 0, count: 64 * 1024)
        var receivedHandshake = false

        while true {
            let n = Darwin.read(sock, &chunk, chunk.count)
            if n < 0 {
                if errno == EINTR { continue }
                break
            }
            if n == 0 { break }
            if !lines.append(chunk[0..<n]) {
                emitError("Host message exceeded maximum line limit.")
                return
            }

            while let obj = lines.nextObject() {
                if !receivedHandshake {
                    receivedHandshake = true
                    let type = (obj["type"] as? String) ?? ""
                    if type == "error" {
                        let msg = (obj["message"] as? String) ?? "Host rejected transcript observer."
                        emitError(msg)
                        return
                    }
                    guard type == "ready" else {
                        emitError("Unexpected host handshake response.")
                        return
                    }
                    let welcome = Self.decodeWelcome(obj)
                    DispatchQueue.main.async { [weak self] in
                        guard let self, self.isCurrent(session) else { return }
                        onReady(welcome)
                    }
                    continue
                }

                let msgType = (obj["type"] as? String) ?? ""
                if msgType == "notify" {
                    let method = (obj["method"] as? String) ?? ""
                    let args = (obj["args"] as? [Any]) ?? []
                    let kwargs = (obj["kwargs"] as? [String: Any]) ?? [:]
                    if let event = PCodeHostEventDecoder.decodeRPCCall(method: method, args: args, kwargs: kwargs) {
                        DispatchQueue.main.async { [weak self] in
                            guard let self, self.isCurrent(session) else { return }
                            onEvent(event)
                        }
                    }
                } else if msgType == "request", let reqID = obj["id"] as? Int {
                    let reply = "{\"type\":\"reply\",\"id\":\(reqID),\"error\":{\"type\":\"PermissionError\",\"message\":\"Transcript observer is read-only.\"}}\n"
                    _ = PCodeHostLineSocket.writeAll(sock, Data(reply.utf8))
                }
            }
        }

        if isCurrent(session) {
            DispatchQueue.main.async { [weak self] in
                guard let self, self.isCurrent(session) else { return }
                onClosed()
            }
        }
    }

    static func decodeWelcome(_ obj: [String: Any]) -> Welcome {
        let rawPace = (obj["paced_scrollback"] as? String) ?? "rows"
        let pace = PCodeStreamingPaceMode(rawValue: rawPace) ?? .rows
        let rawHistory = (obj["history"] as? [[String: Any]]) ?? []
        let history = rawHistory.compactMap { PCodeHostEventDecoder.decodeJournalRecord($0) }

        let rawCalls = (obj["calls"] as? [[Any]]) ?? []
        let catchUp = rawCalls.compactMap { call -> PCodeHostEvent? in
            guard call.count >= 3,
                  let method = call[0] as? String,
                  let args = call[1] as? [Any],
                  let kwargs = call[2] as? [String: Any]
            else { return nil }
            return PCodeHostEventDecoder.decodeRPCCall(method: method, args: args, kwargs: kwargs)
        }

        return Welcome(
            hostID: (obj["id"] as? String) ?? "",
            sessionID: (obj["session_id"] as? String) ?? "",
            title: (obj["title"] as? String) ?? "",
            hostState: (obj["state"] as? String) ?? "",
            pacedScrollback: pace,
            showThinking: obj["show_thinking"] as? String,
            model: (obj["model"] as? String) ?? "",
            effort: (obj["effort"] as? String) ?? "",
            context: (obj["context"] as? String) ?? "",
            queuedPrompts: (obj["queued_prompts"] as? [String]) ?? [],
            queuedModes: (obj["queued_modes"] as? [String]) ?? [],
            history: history,
            catchUpCalls: catchUp)
    }
}
