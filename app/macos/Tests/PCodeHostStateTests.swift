import Foundation
import Testing
@testable import Ghostty

/// Session state from the host's `observe_transcript` stream (`state`, `session_changed`).
struct PCodeHostStateTests {
    @Test func decodesStateDeltaOfTrackedFields() {
        let event = PCodeHostEventDecoder.decodeRPCCall(
            method: "state",
            args: [["busy": true, "prompt_state": "running", "turn_outcome": "", "queued": 2]],
            kwargs: [:])
        #expect(event == .sessionState(PCodeHostSessionDelta(busy: true, promptState: "running", turnOutcome: "")))
    }

    @Test func decodesPartialStateDelta() {
        let event = PCodeHostEventDecoder.decodeRPCCall(
            method: "state", args: [["turn_outcome": "failed"]], kwargs: [:])
        #expect(event == .sessionState(PCodeHostSessionDelta(turnOutcome: "failed")))
    }

    @Test func dropsStateDeltaOfOnlyUntrackedFields() {
        let event = PCodeHostEventDecoder.decodeRPCCall(
            method: "state", args: [["queued": 1, "notice": "hi", "notice_expires": 3.5]], kwargs: [:])
        #expect(event == nil)
        #expect(PCodeHostEventDecoder.decodeRPCCall(method: "state", args: [], kwargs: [:]) == nil)
    }

    @Test func decodesStateDeltaFromJSONWire() throws {
        // Booleans arrive as NSNumber from JSONSerialization, as on the socket.
        let line = #"{"type":"notify","method":"state","args":[{"busy":false,"prompt_state":"done"}],"kwargs":{}}"#
        let obj = try #require(try JSONSerialization.jsonObject(with: Data(line.utf8)) as? [String: Any])
        let event = PCodeHostEventDecoder.decodeRPCCall(
            method: obj["method"] as! String, args: obj["args"] as! [Any], kwargs: obj["kwargs"] as! [String: Any])
        #expect(event == .sessionState(PCodeHostSessionDelta(busy: false, promptState: "done")))
    }

    @Test func decodesSessionChanged() {
        #expect(PCodeHostEventDecoder.decodeRPCCall(method: "session_changed", args: [], kwargs: [:]) == .sessionChanged(""))
        #expect(PCodeHostEventDecoder.decodeRPCCall(method: "session_changed", args: ["s2"], kwargs: [:]) == .sessionChanged("s2"))
    }

    @Test func decodesNotesWarningsAndErrors() {
        #expect(PCodeHostEventDecoder.decodeRPCCall(method: "note", args: ["loaded"], kwargs: [:]) == .note("loaded"))
        #expect(PCodeHostEventDecoder.decodeRPCCall(method: "retained_note", args: ["context"], kwargs: [:]) == .note("context"))
        #expect(PCodeHostEventDecoder.decodeRPCCall(method: "warning", args: ["heads up"], kwargs: [:]) == .warning("heads up"))
        #expect(PCodeHostEventDecoder.decodeRPCCall(method: "error", args: ["boom"], kwargs: [:]) == .error("boom"))
        // Nothing to show: no event.
        #expect(PCodeHostEventDecoder.decodeRPCCall(method: "note", args: [""], kwargs: [:]) == nil)
        #expect(PCodeHostEventDecoder.decodeRPCCall(method: "warning", args: [""], kwargs: [:]) == nil)
        #expect(PCodeHostEventDecoder.decodeRPCCall(method: "error", args: [""], kwargs: [:]) == nil)
        #expect(PCodeHostEventDecoder.decodeRPCCall(method: "error", args: [], kwargs: [:]) == nil)
        #expect(PCodeHostEventDecoder.decodeRPCCall(method: "retained_note", args: [], kwargs: [:]) == nil)
    }

    @Test func foldsDeltasAndKeepsUnchangedFields() {
        var state = PCodeHostSessionState()
        state.sessionID = "abc"
        var changed = state.apply(.sessionState(PCodeHostSessionDelta(busy: true, promptState: "running")))
        #expect(changed)
        #expect(state.busy && state.promptState == "running" && state.turnOutcome == "")
        changed = state.apply(.sessionState(PCodeHostSessionDelta(busy: false, turnOutcome: "done")))
        #expect(changed)
        #expect(state == {
            var s = PCodeHostSessionState()
            s.sessionID = "abc"
            s.promptState = "running"
            s.turnOutcome = "done"
            return s
        }())
        // Repeating a value is not a change; transcript events leave the state alone.
        for event in [PCodeHostEvent.sessionState(PCodeHostSessionDelta(busy: false)), .textDelta("hi"), .sessionChanged(""), .sessionChanged("abc")] {
            changed = state.apply(event)
            #expect(!changed)
        }
        changed = state.apply(.sessionChanged("def"))
        #expect(changed)
        #expect(state.sessionID == "def")
    }

    @Test func conversationResetClearsSessionAndOutcome() {
        var state = PCodeHostSessionState()
        state.sessionID = "abc"
        state.turnOutcome = "failed"
        state.promptState = "failed"
        let changed = state.apply(.conversationReset)
        #expect(changed)
        #expect(state.sessionID == "" && state.turnOutcome == "")
        #expect(state.promptState == "failed")
    }

    @Test func welcomeSeedsSessionAndBusy() {
        let welcome = PCodeHostSocketClient.decodeWelcome([
            "type": "ready", "id": "h1", "session_id": "s1", "title": "t", "state": "working",
            "queued_prompts": ["steer me", "next turn", "wake"],
            "queued_modes": ["steering", "queue", "provider_wake"],
            "history": [], "calls": [["state", [["turn_outcome": "done"]], [:]]],
        ])
        #expect(welcome.hostState == "working")
        var state = PCodeHostSessionState(welcome: welcome)
        #expect(state.sessionID == "s1" && state.busy && state.promptState == "running")
        #expect(state.pendingPrompts.map(\.mode) == [.steering, .queued])
        #expect(state.pendingPrompts.map(\.text) == ["steer me", "next turn"])
        for event in welcome.catchUpCalls { state.apply(event) }
        #expect(state.turnOutcome == "done")
        let idle = PCodeHostSessionState(welcome: PCodeHostSocketClient.decodeWelcome(["state": "idle"]))
        #expect(!idle.busy && idle.promptState == "" && idle.pendingPrompts.isEmpty)
    }

    @Test func foldsQueuedPromptsAndClearsOnReset() {
        var state = PCodeHostSessionState()
        let deltaEvent = PCodeHostEventDecoder.decodeRPCCall(
            method: "state",
            args: [["queued_prompts": ["also fix docs", "then deploy"], "queued_modes": ["steering", "queue"]]],
            kwargs: [:])
        #expect(deltaEvent != nil)
        let applied = state.apply(deltaEvent!)
        #expect(applied)
        #expect(state.pendingPrompts.count == 2)
        #expect(state.pendingPrompts[0].mode == .steering && state.pendingPrompts[0].text == "also fix docs")
        #expect(state.pendingPrompts[1].mode == .queued && state.pendingPrompts[1].text == "then deploy")

        let reset = state.apply(.conversationReset)
        #expect(reset)
        #expect(state.pendingPrompts.isEmpty)
    }

    @Test func decodesSessionMetadataAndWelcomeFields() {
        let welcome = PCodeHostSocketClient.decodeWelcome([
            "type": "ready", "id": "h1", "session_id": "s1", "title": "t", "state": "idle",
            "model": "antigravity:gemini-4-neon",
            "effort": "medium",
            "context": "42k/200k",
            "history": [], "calls": [],
        ])
        #expect(welcome.model == "antigravity:gemini-4-neon")
        #expect(welcome.effort == "medium")
        #expect(welcome.context == "42k/200k")

        let legacyWelcome = PCodeHostSocketClient.decodeWelcome([
            "type": "ready", "id": "h1", "session_id": "s1", "title": "t", "state": "idle",
            "history": [], "calls": [],
        ])
        #expect(legacyWelcome.model == "")
        #expect(legacyWelcome.effort == "")
        #expect(legacyWelcome.context == "")

        let event = PCodeHostEventDecoder.decodeRPCCall(
            method: "session_metadata",
            args: [["model": "anthropic:claude-sonnet", "effort": "high", "context": "~12.5k/200k"]],
            kwargs: [:])
        #expect(event == .sessionMetadata(model: "anthropic:claude-sonnet", effort: "high", context: "~12.5k/200k"))
        #expect(PCodeHostEventDecoder.decodeRPCCall(method: "session_metadata", args: [], kwargs: [:]) == nil)
    }

    @Test func formatsModelProviderEffortAndContextRatio() {
        let split = PCodeSessionMetadataDisplay.splitModel("antigravity:gemini-4-neon")
        #expect(split.provider == "antigravity")
        #expect(split.name == "gemini-4-neon")

        let multiColon = PCodeSessionMetadataDisplay.splitModel("ollama:llama3:8b")
        #expect(multiColon.provider == "ollama")
        #expect(multiColon.name == "llama3:8b")

        for edge in ["preview", ":foo", "foo:"] {
            let parsed = PCodeSessionMetadataDisplay.splitModel(edge)
            #expect(parsed.provider == nil)
            #expect(parsed.name == edge)
        }

        #expect(PCodeSessionMetadataDisplay.normalizedEffort("medium") == "medium")
        #expect(PCodeSessionMetadataDisplay.normalizedEffort("n/a") == nil)
        #expect(PCodeSessionMetadataDisplay.normalizedEffort("") == nil)

        #expect(abs((PCodeSessionMetadataDisplay.contextFillRatio("50k/200k") ?? -1) - 0.25) < 0.001)
        #expect(abs((PCodeSessionMetadataDisplay.contextFillRatio("~1.5m/2m") ?? -1) - 0.75) < 0.001)
        #expect(abs((PCodeSessionMetadataDisplay.contextFillRatio("250k/200k") ?? -1) - 1.0) < 0.001)
        #expect(PCodeSessionMetadataDisplay.contextFillRatio("0/200k") == 0.0)
        #expect(PCodeSessionMetadataDisplay.contextFillRatio("42k/?") == nil)
        #expect(PCodeSessionMetadataDisplay.contextFillRatio("inf/200k") == nil)
        #expect(PCodeSessionMetadataDisplay.contextFillRatio("") == nil)
    }

    @Test func decodesProviderUsageAndMatchesAntigravityGroups() throws {
        let json = """
        {
          "provider": "antigravity",
          "windows": [{"key": "5h", "label": "5h", "used_percent": 20}],
          "groups": [
            {
              "name": "Gemini Models",
              "windows": [
                {"key": "5h", "label": "5h", "used_percent": 20, "reset_at": 1007200},
                {"key": "weekly", "label": "wk", "used_percent": 6, "reset_at": 1536400}
              ]
            },
            {
              "name": "Claude and GPT models",
              "windows": [
                {"key": "5h", "label": "5h", "used_percent": 14}
              ]
            }
          ]
        }
        """
        let usage = try JSONDecoder().decode(PCodeProviderUsage.self, from: Data(json.utf8))
        let geminiWindows = usage.windows(forModel: "antigravity:gemini-4-neon")
        #expect(geminiWindows.map(\.usedPercent) == [20, 6])
        #expect(geminiWindows.map(\.resetAt) == [1_007_200, 1_536_400])
        #expect(usage.windows(forModel: "antigravity:claude-sonnet-5-5").map(\.usedPercent) == [14])
        #expect(usage.windows(forModel: "antigravity:gpt-oss-120b").map(\.usedPercent) == [14])
        // Unrecognized Antigravity model falls back to the first non-empty group.
        #expect(usage.windows(forModel: "antigravity:other-model").map(\.usedPercent) == [20, 6])

        // Weekly window replaces static `wk` with compact remaining duration; `5h` stays `5h`.
        let now = Date(timeIntervalSince1970: 1_000_000)
        #expect(geminiWindows[0].displayLabel(now: now) == "5h")
        #expect(geminiWindows[1].displayLabel(now: now) == "6d5h")
        #expect(
            PCodeUsageWindow(key: "weekly", label: "wk", usedPercent: 6, resetAt: 1_000_000 + 6 * 86_400)
                .displayLabel(now: now) == "6d"
        )
        #expect(
            PCodeUsageWindow(key: "weekly", label: "wk", usedPercent: 6, resetAt: 1_000_000 + 5 * 3_600 + 30 * 60)
                .displayLabel(now: now) == "5h30m"
        )
        #expect(
            PCodeUsageWindow(key: "weekly", label: "wk", usedPercent: 6, resetAt: 1_000_000 + 5 * 3_600)
                .displayLabel(now: now) == "5h"
        )
        #expect(
            PCodeUsageWindow(key: "weekly", label: "wk", usedPercent: 6, resetAt: 1_000_000 + 45 * 60)
                .displayLabel(now: now) == "45m"
        )
        #expect(
            PCodeUsageWindow(key: "weekly", label: "wk", usedPercent: 6, resetAt: 1_000_000 + 15)
                .displayLabel(now: now) == "1m"
        )
        // Missing, already-elapsed, non-finite, or overflowing reset times fall back to `wk`.
        #expect(
            PCodeUsageWindow(key: "weekly", label: "wk", usedPercent: 6, resetAt: nil)
                .displayLabel(now: now) == "wk"
        )
        #expect(
            PCodeUsageWindow(key: "weekly", label: "wk", usedPercent: 6, resetAt: 1_000_000)
                .displayLabel(now: now) == "wk"
        )
        #expect(
            PCodeUsageWindow(key: "weekly", label: "wk", usedPercent: 6, resetAt: 999_000)
                .displayLabel(now: now) == "wk"
        )
        #expect(
            PCodeUsageWindow(key: "weekly", label: "wk", usedPercent: 6, resetAt: .nan)
                .displayLabel(now: now) == "wk"
        )
        #expect(
            PCodeUsageWindow(key: "weekly", label: "wk", usedPercent: 6, resetAt: .infinity)
                .displayLabel(now: now) == "wk"
        )
        #expect(
            PCodeUsageWindow(key: "weekly", label: "wk", usedPercent: 6, resetAt: 1e30)
                .displayLabel(now: now) == "wk"
        )
    }

    @Test @MainActor func usageStoreCachesAndHonorsCooldowns() {
        var fetches: [String] = []
        var nextResult: PCodeProviderUsage? = PCodeProviderUsage(
            provider: "claude",
            windows: [
                PCodeUsageWindow(key: "5h", label: "5h", usedPercent: 6),
                PCodeUsageWindow(key: "weekly", label: "wk", usedPercent: 9),
            ],
            groups: nil)
        var pendingCompletion: (@Sendable (PCodeProviderUsage?) -> Void)?
        var holdNext = false
        let store = PCodeUsageStore(ttl: 180, turnRefreshCooldown: 60, failureBackoff: 120) { provider, _, done in
            fetches.append(provider)
            if holdNext {
                pendingCompletion = done
            } else {
                done(nextResult)
            }
        }
        let t0 = Date(timeIntervalSince1970: 1_000)
        // Unsupported provider does not fetch.
        store.refreshIfNeeded(forModel: "anthropic:claude-sonnet", directory: "/tmp", now: t0)
        #expect(fetches.isEmpty)

        // First fetch populates cache.
        store.refreshIfNeeded(forModel: "claude:claude-sonnet", directory: "/tmp", now: t0)
        #expect(fetches == ["claude"])
        #expect(store.windows(forModel: "claude:claude-sonnet").map(\.label) == ["5h", "wk"])

        // Within TTL (and within turnRefreshCooldown), does not refetch.
        store.refreshIfNeeded(forModel: "claude:claude-sonnet", directory: "/tmp", now: t0.addingTimeInterval(30))
        store.refreshIfNeeded(forModel: "claude:claude-sonnet", directory: "/tmp", afterTurn: true, now: t0.addingTimeInterval(30))
        #expect(fetches.count == 1)

        // After a turn past turnRefreshCooldown (60s), refetches; if it fails, enforces failureBackoff (120s).
        nextResult = nil
        store.refreshIfNeeded(forModel: "claude:claude-sonnet", directory: "/tmp", afterTurn: true, now: t0.addingTimeInterval(90))
        #expect(fetches.count == 2)
        // Must back off for 120s after the failed refresh even though prior success is >60s old.
        store.refreshIfNeeded(forModel: "claude:claude-sonnet", directory: "/tmp", afterTurn: true, now: t0.addingTimeInterval(150))
        #expect(fetches.count == 2)

        // Once failureBackoff elapses, refetches and deduplicates while in-flight.
        holdNext = true
        store.refreshIfNeeded(forModel: "claude:claude-sonnet", directory: "/tmp", now: t0.addingTimeInterval(220))
        store.refreshIfNeeded(forModel: "claude:claude-sonnet", directory: "/tmp", now: t0.addingTimeInterval(221))
        #expect(fetches.count == 3)
        pendingCompletion?(nil)
    }

    @Test func decodesComposerStatusAPCAndFormatsElapsed() throws {
        let json = """
        {"busy":true,"phase":"Thinking","send_mode":"steering","send_mode_once":true,"starting":false,"steering_pending":1,"queued":2,"jobs":0,"btw_running":0,"btw_ready":1,"cache_note":""}
        """
        let b64 = Data(json.utf8).base64EncodedString()
        let decoded = try #require(PCodeComposerStatus.decode(base64: b64))
        #expect(decoded.busy == true)
        #expect(decoded.phase == "Thinking")
        #expect(decoded.sendMode == "steering")
        #expect(decoded.sendModeOnce == true)
        #expect(decoded.steeringPending == 1)
        #expect(decoded.queued == 2)
        #expect(decoded.btwReady == 1)
        #expect(PCodeComposerStatus.decode(base64: "not-valid-base64!") == nil)

        #expect(PCodeComposerStatus.formatElapsed(5) == "5s")
        #expect(PCodeComposerStatus.formatElapsed(60) == "1m")
        #expect(PCodeComposerStatus.formatElapsed(125) == "2m 5s")

        // Footprint and cardLayout account for the native status strip when shown.
        let baseFootprint = PCodeDock.footprint(rows: 3, cell: 18, statusStrip: false)
        let stripFootprint = PCodeDock.footprint(rows: 3, cell: 18, statusStrip: true)
        #expect(stripFootprint == baseFootprint + PCodeDock.statusStripHeight + PCodeDock.statusStripSpacing)

        let baseLayout = PCodeDock.cardLayout(in: CGSize(width: 800, height: 600), rows: 3, cell: 18, statusStrip: false)
        let stripLayout = PCodeDock.cardLayout(in: CGSize(width: 800, height: 600), rows: 3, cell: 18, statusStrip: true)
        #expect(stripLayout.cardTopFromBottom == baseLayout.cardTopFromBottom + PCodeDock.topMargin + PCodeDock.statusStripSpacing + PCodeDock.statusStripHeight)
    }

    @Test func resolvesThreeZoneTitleBarGeometry() {
        // Wide window (1200pt total = 76 leading + 1112 avail + 12 trailing):
        // title centers over the full window at x = 600 (524 in content coords).
        let wide = PCodeTitleBarGeometry.resolve(
            availableWidth: 1112,
            leadingInset: 76,
            trailingInset: 12,
            leadingIdeal: 140,
            centerIdeal: 200,
            trailingIdeal: 280,
            spacing: 12
        )
        #expect(wide.leadingWidth == 140)
        #expect(wide.centerWidth == 200)
        #expect(wide.trailingWidth == 280)
        #expect(abs((wide.centerX + wide.centerWidth / 2) - (1200 / 2 - 76)) < 0.001)
        #expect(wide.centerX >= wide.leadingWidth + 12)
        #expect(wide.centerX + wide.centerWidth <= wide.trailingX - 12)

        // Fullscreen (16 leading + 1172 avail + 12 trailing = 1200pt):
        // true window center shifts to 600 - 16 = 584 in content coords.
        let fullscreen = PCodeTitleBarGeometry.resolve(
            availableWidth: 1172,
            leadingInset: 16,
            trailingInset: 12,
            leadingIdeal: 140,
            centerIdeal: 200,
            trailingIdeal: 280,
            spacing: 12
        )
        #expect(abs((fullscreen.centerX + fullscreen.centerWidth / 2) - (1200 / 2 - 16)) < 0.001)

        // Medium window where true window center (328 -> [228, 428]) would collide with wide
        // trailing pills ([400, 720]): title shifts left to end at rightEdge (388 -> [188, 388]).
        let medium = PCodeTitleBarGeometry.resolve(
            availableWidth: 720,
            leadingInset: 76,
            trailingInset: 12,
            leadingIdeal: 140,
            centerIdeal: 200,
            trailingIdeal: 320,
            spacing: 12
        )
        #expect(medium.leadingWidth == 140)
        #expect(medium.trailingWidth == 320)
        #expect(medium.centerWidth == 200)
        #expect(medium.centerX == 188)

        // Wide leading cluster pushes title right of true window center (328 -> [228, 428]).
        let wideLeading = PCodeTitleBarGeometry.resolve(
            availableWidth: 720,
            leadingInset: 76,
            trailingInset: 12,
            leadingIdeal: 260,
            centerIdeal: 200,
            trailingIdeal: 120,
            spacing: 12
        )
        #expect(wideLeading.leadingWidth == 260)
        #expect(wideLeading.centerWidth == 200)
        #expect(wideLeading.centerX == 272)

        // Narrow window: center retains up to 180pt before outer pills hoard width, and nothing overlaps.
        let narrow = PCodeTitleBarGeometry.resolve(
            availableWidth: 420,
            leadingInset: 76,
            trailingInset: 12,
            leadingIdeal: 160,
            centerIdeal: 260,
            trailingIdeal: 300,
            spacing: 12
        )
        #expect(narrow.leadingWidth >= 0)
        #expect(narrow.centerWidth >= 180)
        #expect(narrow.trailingWidth >= 0)
        #expect(narrow.centerX >= narrow.leadingWidth + 12)
        #expect(narrow.centerX + narrow.centerWidth <= narrow.trailingX - 12)

        // Trailing drops cleanly to 0 when budget falls below trailingMin (no 1pt sliver).
        let droppedTrailing = PCodeTitleBarGeometry.resolve(
            availableWidth: 300,
            leadingInset: 76,
            trailingInset: 12,
            leadingIdeal: 120,
            centerIdeal: 200,
            trailingIdeal: 150,
            leadingMin: 40,
            trailingMin: 60,
            spacing: 12
        )
        #expect(droppedTrailing.trailingWidth == 0)
        #expect(droppedTrailing.leadingWidth == 108)
        #expect(droppedTrailing.centerWidth == 180)
        #expect(droppedTrailing.centerX == 120)

        // Empty outer clusters: center gets full window centering with no wasted gaps.
        let centerOnly = PCodeTitleBarGeometry.resolve(
            availableWidth: 500,
            leadingInset: 76,
            trailingInset: 12,
            leadingIdeal: 0,
            centerIdeal: 200,
            trailingIdeal: 0,
            spacing: 12
        )
        #expect(centerOnly.leadingWidth == 0)
        #expect(centerOnly.trailingWidth == 0)
        #expect(centerOnly.centerWidth == 200)
        #expect(abs((centerOnly.centerX + centerOnly.centerWidth / 2) - ((500 + 76 + 12) / 2 - 76)) < 0.001)

        // Empty trailing cluster: no trailing gap reserved.
        let noTrailing = PCodeTitleBarGeometry.resolve(
            availableWidth: 400,
            leadingInset: 16,
            trailingInset: 12,
            leadingIdeal: 120,
            centerIdeal: 300,
            trailingIdeal: 0,
            spacing: 12
        )
        #expect(noTrailing.trailingWidth == 0)
        #expect(noTrailing.centerX == 132)
        #expect(noTrailing.centerWidth == 268)

        // Degenerate zero width never produces negative dimensions.
        let zero = PCodeTitleBarGeometry.resolve(
            availableWidth: 0,
            leadingInset: 76,
            trailingInset: 12,
            leadingIdeal: 120,
            centerIdeal: 200,
            trailingIdeal: 200,
            spacing: 12
        )
        #expect(zero.leadingWidth == 0)
        #expect(zero.centerWidth == 0)
        #expect(zero.trailingWidth == 0)
    }
}
