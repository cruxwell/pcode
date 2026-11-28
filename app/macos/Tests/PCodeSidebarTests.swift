import CoreGraphics
import Foundation
import Testing
@testable import Ghostty

@Suite
struct PCodeSidebarTests {
    @Test func clampsWidthWithinBounds() {
        #expect(PCodeSidebarLayout.clampedWidth(100, totalWidth: 1200) == PCodeSidebarLayout.minWidth)
        #expect(PCodeSidebarLayout.clampedWidth(320, totalWidth: 1200) == 320)
        #expect(PCodeSidebarLayout.clampedWidth(700, totalWidth: 1200) == PCodeSidebarLayout.maxWidth)
    }

    @Test func clampsWidthToReserveDetailPaneSpace() {
        // 700 - 360 = 340 max sidebar width in a narrow window.
        #expect(PCodeSidebarLayout.clampedWidth(420, totalWidth: 700) == 340)
        // Even in a tiny window, never shrink below minWidth.
        #expect(PCodeSidebarLayout.clampedWidth(420, totalWidth: 400) == PCodeSidebarLayout.minWidth)
    }

    @Test func fallsBackToDefaultForNonPositiveOrNonFiniteWidth() {
        #expect(PCodeSidebarLayout.clampedWidth(0, totalWidth: 1200) == PCodeSidebarLayout.defaultWidth)
        #expect(PCodeSidebarLayout.clampedWidth(-50, totalWidth: 1200) == PCodeSidebarLayout.defaultWidth)
        #expect(PCodeSidebarLayout.clampedWidth(.nan, totalWidth: 1200) == PCodeSidebarLayout.defaultWidth)
        #expect(PCodeSidebarLayout.clampedWidth(.infinity, totalWidth: 1200) == PCodeSidebarLayout.defaultWidth)
    }

    @Test func revealEdgeHitZone() {
        let bounds = CGRect(x: 0, y: 0, width: 1200, height: 800)
        #expect(PCodeSidebarLayout.shouldReveal(at: CGPoint(x: 0, y: 400), in: bounds))
        #expect(PCodeSidebarLayout.shouldReveal(at: CGPoint(x: PCodeSidebarLayout.revealEdgeWidth, y: 400), in: bounds))
        #expect(!PCodeSidebarLayout.shouldReveal(at: CGPoint(x: PCodeSidebarLayout.revealEdgeWidth + 1, y: 400), in: bounds))
        #expect(!PCodeSidebarLayout.shouldReveal(at: CGPoint(x: -1, y: 400), in: bounds))
        #expect(!PCodeSidebarLayout.shouldReveal(at: CGPoint(x: 2, y: 801), in: bounds))
    }

    @Test func revealedZoneIncludesGraceMargin() {
        let bounds = CGRect(x: 0, y: 0, width: 1200, height: 800)
        let width: CGFloat = 280
        #expect(PCodeSidebarLayout.isInsideRevealedZone(at: CGPoint(x: 100, y: 400), in: bounds, sidebarWidth: width))
        #expect(PCodeSidebarLayout.isInsideRevealedZone(
            at: CGPoint(x: width + PCodeSidebarLayout.hideGraceMargin, y: 400),
            in: bounds,
            sidebarWidth: width
        ))
        #expect(!PCodeSidebarLayout.isInsideRevealedZone(
            at: CGPoint(x: width + PCodeSidebarLayout.hideGraceMargin + 1, y: 400),
            in: bounds,
            sidebarWidth: width
        ))
        #expect(!PCodeSidebarLayout.isInsideRevealedZone(at: CGPoint(x: -1, y: 400), in: bounds, sidebarWidth: width))
    }

    @Test func displayTitlePrefersLiveTitleOverSavedAndHandlesPlaceholders() {
        #expect(PCodeWorkspace.displayTitle(paneTitle: "", savedLabel: nil) == "New session")
        #expect(PCodeWorkspace.displayTitle(paneTitle: "👻", savedLabel: nil) == "New session")
        #expect(PCodeWorkspace.displayTitle(paneTitle: "👻", savedLabel: "Saved title") == "Saved title")
        #expect(PCodeWorkspace.displayTitle(paneTitle: "Renamed title", savedLabel: "Old title") == "Renamed title")
        #expect(PCodeWorkspace.displayTitle(paneTitle: "  Trimmed title  ", savedLabel: nil) == "Trimmed title")
        // Shortening `/rename` to a prefix of the old saved label must take effect immediately.
        #expect(PCodeWorkspace.displayTitle(paneTitle: "Fix auth bug", savedLabel: "Fix auth bug in login flow") == "Fix auth bug")
        // Normalize internal newlines and consecutive whitespace.
        #expect(PCodeWorkspace.displayTitle(paneTitle: "👻", savedLabel: "Line one\n  line   two") == "Line one line two")
        // Keep full saved title when OSC 0 live title is a 120-char truncated prefix of it.
        let full = String(repeating: "a", count: 140)
        let prefix = String(full.prefix(120))
        #expect(PCodeWorkspace.displayTitle(paneTitle: prefix, savedLabel: full) == full)
        let fullWithWhitespace = String(repeating: "a", count: 60) + "\n  " + String(repeating: "b", count: 80)
        let normalizedFull = String(repeating: "a", count: 60) + " " + String(repeating: "b", count: 80)
        let normalizedPrefix = String(normalizedFull.prefix(120))
        #expect(PCodeWorkspace.displayTitle(paneTitle: normalizedPrefix, savedLabel: fullWithWhitespace) == normalizedFull)
    }

    @Test func pathCompleterDetectsAndCompletesTildeAndAbsolutePaths() throws {
        let fm = FileManager.default
        let fakeHome = fm.temporaryDirectory.appendingPathComponent("pcode-path-test-\(UUID().uuidString)")
        let pDir = fakeHome.appendingPathComponent("p")
        try fm.createDirectory(at: pDir.appendingPathComponent("pcode"), withIntermediateDirectories: true)
        try fm.createDirectory(at: pDir.appendingPathComponent("pcode-docs"), withIntermediateDirectories: true)
        try fm.createDirectory(at: pDir.appendingPathComponent("alpha"), withIntermediateDirectories: true)
        try fm.createDirectory(at: pDir.appendingPathComponent(".hidden"), withIntermediateDirectories: true)
        try Data("hi".utf8).write(to: pDir.appendingPathComponent("pcode-file.txt"))
        defer { try? fm.removeItem(at: fakeHome) }

        let homePath = PCodeWorkspace.normalizePath(fakeHome.path)

        #expect(PCodePathCompleter.isPathQuery("~"))
        #expect(PCodePathCompleter.isPathQuery("~/p"))
        #expect(PCodePathCompleter.isPathQuery("/tmp"))
        #expect(!PCodePathCompleter.isPathQuery("pcode"))
        #expect(!PCodePathCompleter.isPathQuery(""))

        // Bare tilde completes to ~/
        let bare = PCodePathCompleter.complete(query: "~", home: homePath, fileManager: fm)
        #expect(bare == .init(completed: "~/", isUnique: true))

        // Unique tilde match preserves ~/ and appends /
        let uniqueTilde = PCodePathCompleter.complete(query: "~/p/al", home: homePath, fileManager: fm)
        #expect(uniqueTilde == .init(completed: "~/p/alpha/", isUnique: true))

        // Unique absolute match preserves absolute path and appends /
        let uniqueAbs = PCodePathCompleter.complete(query: "\(homePath)/p/al", home: homePath, fileManager: fm)
        #expect(uniqueAbs == .init(completed: "\(homePath)/p/alpha/", isUnique: true))

        // Multiple matches complete to longest common prefix (ignoring regular files)
        let lcp = PCodePathCompleter.complete(query: "~/p/pc", home: homePath, fileManager: fm)
        #expect(lcp == .init(completed: "~/p/pcode", isUnique: false))

        // Already at LCP: Tab completes into the currently selected candidate + /
        let drill = PCodePathCompleter.complete(
            query: "~/p/pcode",
            selectedPath: "\(homePath)/p/pcode-docs",
            home: homePath,
            fileManager: fm
        )
        #expect(drill == .init(completed: "~/p/pcode-docs/", isUnique: true))

        // Non-path query with selected candidate drills into ~/.../selected/
        let fromProject = PCodePathCompleter.complete(
            query: "pco",
            selectedPath: "\(homePath)/p/pcode",
            home: homePath,
            fileManager: fm
        )
        #expect(fromProject == .init(completed: "~/p/pcode/", isUnique: true))

        // Hidden directories skipped unless prefix starts with '.'
        let normalSubs = PCodePathCompleter.matchingSubdirectories(in: "\(homePath)/p", prefix: "", fileManager: fm)
        #expect(!normalSubs.contains("\(homePath)/p/.hidden"))
        let hiddenSubs = PCodePathCompleter.matchingSubdirectories(in: "\(homePath)/p", prefix: ".", fileManager: fm)
        #expect(hiddenSubs == ["\(homePath)/p/.hidden"])

        // Directory suggestions put exact match first, followed by matching subdirectories
        let suggestions = PCodePathCompleter.directorySuggestions(for: "~/p/pcode", home: homePath, fileManager: fm)
        #expect(suggestions == ["\(homePath)/p/pcode", "\(homePath)/p/pcode-docs"])

        // Case-insensitive exact match preserves canonical on-disk casing without duplicates
        try fm.createDirectory(at: pDir.appendingPathComponent("MixedCase"), withIntermediateDirectories: true)
        let canonical = PCodePathCompleter.directorySuggestions(for: "~/p/mixedcase", home: homePath, fileManager: fm)
        #expect(canonical == ["\(homePath)/p/MixedCase"])

        // Trailing spaces are preserved when completing spaced directory names
        try fm.createDirectory(at: pDir.appendingPathComponent("My App"), withIntermediateDirectories: true)
        try fm.createDirectory(at: pDir.appendingPathComponent("MyOther"), withIntermediateDirectories: true)
        let spaced = PCodePathCompleter.complete(query: "~/p/My ", home: homePath, fileManager: fm)
        #expect(spaced == .init(completed: "~/p/My App/", isUnique: true))

        // Unexpanded ~user without slash does not probe /~user
        #expect(PCodePathCompleter.split("~nonexistentuserxyz", home: homePath) == nil)
        #expect(PCodePathCompleter.directorySuggestions(for: "~nonexistentuserxyz", home: homePath, fileManager: fm).isEmpty)
    }

    /// A throwaway session root under the temporary directory.
    private struct SessionRoot {
        let url: URL
        let fm = FileManager.default

        init(_ label: String) throws {
            url = FileManager.default.temporaryDirectory.appendingPathComponent("pcode-\(label)-\(UUID().uuidString)")
            try fm.createDirectory(at: url, withIntermediateDirectories: true)
        }

        func write(
            _ id: String, workspace: String = "/tmp/proj", turns: Int = 1,
            created: String? = nil, updated: String = "2026-11-26T01:00:00+00:00", title: String? = nil
        ) throws {
            let dir = url.appendingPathComponent(id)
            try fm.createDirectory(at: dir, withIntermediateDirectories: true)
            let createdField = created.map { #""created":"\#($0)","# } ?? ""
            let json = """
            {"id":"\(id)","workspace":"\(workspace)",\(createdField)"updated":"\(updated)","title":"\(title ?? id)","turns":\(turns)}
            """
            try Data(json.utf8).write(to: dir.appendingPathComponent("session.json"))
        }

        func remove() { try? fm.removeItem(at: url) }
    }

    @Test func sessionLoadIncludesZeroTurnLiveHostedSessions() throws {
        let root = try SessionRoot("sessions-test")
        defer { root.remove() }
        try root.write("empty-idle", turns: 0, updated: "2026-11-26T01:00:00+00:00")
        try root.write("live-zero-turns", turns: 0, updated: "2026-11-26T02:00:00+00:00")
        try root.write("completed-new", turns: 2, updated: "2026-11-26T03:00:00+00:00")
        try root.write("live-old", turns: 1, updated: "2026-11-25T01:00:00+00:00")

        let loaded = PCodeSessionInfo.load(
            root: root.url, hosted: ["live-zero-turns", "live-old"], cache: PCodeSessionInfo.LoadCache())
        // Every session with a turn or a live host, newest first; no cap.
        #expect(loaded.map(\.id) == ["completed-new", "live-zero-turns", "live-old"])
    }

    @Test func sessionLoadCacheParsesOnlyChangedFiles() throws {
        let root = try SessionRoot("cache-test")
        defer { root.remove() }
        try root.write("a", title: "A")
        try root.write("b", title: "B")
        // Not sessions: a symlink to one, a symlinked session.json, and a
        // session.json naming another id.
        try root.fm.createSymbolicLink(
            at: root.url.appendingPathComponent("link"), withDestinationURL: root.url.appendingPathComponent("a"))
        try root.write("d")
        try root.fm.removeItem(at: root.url.appendingPathComponent("d/session.json"))
        try root.fm.createSymbolicLink(
            at: root.url.appendingPathComponent("d/session.json"),
            withDestinationURL: root.url.appendingPathComponent("a/session.json"))
        try root.write("c")
        try Data(#"{"id":"other","workspace":"/tmp/proj","updated":"x","turns":1}"#.utf8)
            .write(to: root.url.appendingPathComponent("c/session.json"))
        let cache = PCodeSessionInfo.LoadCache()
        func load() -> [String: String] {
            Dictionary(uniqueKeysWithValues: cache.infos(root: root.url).map { ($0.id, $0.label) })
        }

        #expect(load() == ["a": "A", "b": "B"])
        #expect(cache.decodes == 3)
        #expect(load() == ["a": "A", "b": "B"])
        #expect(cache.decodes == 3)  // Nothing changed: nothing parsed, not even the bad one.
        try root.write("b", title: "B renamed")
        #expect(load() == ["a": "A", "b": "B renamed"])
        #expect(cache.decodes == 4)
        try root.fm.removeItem(at: root.url.appendingPathComponent("a"))
        #expect(load() == ["b": "B renamed"])
    }

    @Test func persistedSessionIndexRoundTripsAndParsesOnlyChangedFiles() throws {
        let root = try SessionRoot("index-test")
        defer { root.remove() }
        let index = root.fm.temporaryDirectory.appendingPathComponent("pcode-index-\(UUID().uuidString).json")
        defer { try? root.fm.removeItem(at: index) }
        try root.write("a", created: "2026-11-25T10:00:00+00:00", updated: "2026-11-26T01:00:00+00:00", title: "A")
        try root.write("b", created: "2026-11-25T12:00:00.25+00:00", updated: "2026-11-26T02:00:00.5+00:00", title: "B")
        try root.write("zero", turns: 0)
        try root.write("bad")
        try Data("not json".utf8).write(to: root.url.appendingPathComponent("bad/session.json"))

        let first = PCodeSessionInfo.LoadCache(indexURL: index, persistingFor: root.url)
        let firstLoad = first.infos(root: root.url)
        first.flush()
        #expect(first.decodes == 4)
        #expect(root.fm.fileExists(atPath: index.path))

        // A second cache (the next launch) reads the index and parses only what changed.
        try root.write("a", updated: "2026-11-26T03:00:00+00:00", title: "A again")
        let second = PCodeSessionInfo.LoadCache(indexURL: index, persistingFor: root.url)
        let secondLoad = second.infos(root: root.url)
        #expect(second.decodes == 1)
        #expect(secondLoad.map(\.id) == ["a", "b", "zero"])
        #expect(secondLoad.map(\.label) == ["A again", "B", "zero"])
        // Rows rebuilt from the index match freshly parsed ones, derived fields included.
        #expect(secondLoad.dropFirst() == firstLoad.filter { $0.id != "a" }[...])
        #expect(secondLoad[1].createdDate == PCodeWorkspace.parseISO8601("2026-11-25T12:00:00.25+00:00"))
        #expect(secondLoad[1].updatedDate == PCodeWorkspace.parseISO8601("2026-11-26T02:00:00.5+00:00"))

        // Unchanged since: a third launch parses nothing.
        second.flush()
        let third = PCodeSessionInfo.LoadCache(indexURL: index, persistingFor: root.url)
        #expect(third.infos(root: root.url) == secondLoad)
        #expect(third.decodes == 0)

        // Loading another root neither reads nor writes the index.
        let other = try SessionRoot("index-other")
        defer { other.remove() }
        try other.write("x")
        let before = try Data(contentsOf: index)
        #expect(third.infos(root: other.url).map(\.id) == ["x"])
        third.flush()
        #expect(try Data(contentsOf: index) == before)
    }

    @Test func corruptOrOldSessionIndexIsDiscarded() throws {
        let root = try SessionRoot("index-bad")
        defer { root.remove() }
        let index = root.fm.temporaryDirectory.appendingPathComponent("pcode-index-\(UUID().uuidString).json")
        defer { try? root.fm.removeItem(at: index) }
        try root.write("a", title: "A")

        for contents in [
            "garbage",
            #"{"version":0,"root":"\#(root.url.standardizedFileURL.path)","rows":[]}"#,
            #"{"version":\#(PCodeSessionInfo.LoadCache.indexVersion),"root":"/elsewhere","rows":[]}"#,
        ] {
            try Data(contents.utf8).write(to: index)
            let cache = PCodeSessionInfo.LoadCache(indexURL: index, persistingFor: root.url)
            #expect(cache.infos(root: root.url).map(\.label) == ["A"])
            #expect(cache.decodes == 1)
            cache.flush()
            // Rewritten as a current index.
            let next = PCodeSessionInfo.LoadCache(indexURL: index, persistingFor: root.url)
            #expect(next.infos(root: root.url).map(\.label) == ["A"])
            #expect(next.decodes == 0)
        }
    }

    private func makeInfo(_ id: String, workspace: String, updated: String) throws -> PCodeSessionInfo {
        let json = """
        {"id":"\(id)","workspace":"\(workspace)","updated":"\(updated)","title":"\(id)","turns":1}
        """
        return try JSONDecoder().decode(PCodeSessionInfo.self, from: Data(json.utf8))
    }

    private func makeInfo(_ id: String, workspace: String, created: String) throws -> PCodeSessionInfo {
        let json = """
        {"id":"\(id)","workspace":"\(workspace)","created":"\(created)","updated":"\(created)","title":"\(id)","turns":1}
        """
        return try JSONDecoder().decode(PCodeSessionInfo.self, from: Data(json.utf8))
    }

    @Test func dormantSessionsAreOpenRowsNotRecents() throws {
        let proj = PCodeWorkspace.normalizePath("/tmp/dormant-proj")
        let old = try makeInfo("old", workspace: proj, created: "2026-01-01T00:00:00+00:00")
        let new = try makeInfo("new", workspace: proj, created: "2026-02-01T00:00:00+00:00")
        let recent = try makeInfo("recent", workspace: proj, created: "2026-03-01T00:00:00+00:00")
        let groups = PCodeWorkspace.buildProjectGroups(
            panes: [], projectPathForPane: { $0.projectPath }, history: [recent, new, old],
            detachedHostStatuses: [:], pinnedFolders: [], hiddenFolders: [proj],
            starredSessionIDs: ["old"], dormant: [old, new])
        // Listed even in a hidden folder, as open panes are.
        let group = try #require(groups.first)
        #expect(group.openRows.map(\.sessionID) == ["old", "new"], "starred first, then newest")
        #expect(group.starredOpenRows.map(\.sessionID) == ["old"])
        #expect(group.recentSessions.map(\.id) == ["recent"])
        #expect(group.activity == .idle)
        #expect(group.hasUnseen(["new"]))
    }

    private func composer(jobs: Int = 0, btwReady: Int = 0, draft: Bool? = false) throws -> PCodeComposerStatus {
        let draftField = draft.map { ",\"draft\":\($0)" } ?? ""
        let json = """
        {"busy":false,"phase":"","send_mode":"steering","send_mode_once":false,"starting":false,"steering_pending":0,"queued":0,"jobs":\(jobs),"btw_running":0,"btw_ready":\(btwReady),"cache_note":""\(draftField)}
        """
        return try #require(PCodeComposerStatus.decode(base64: Data(json.utf8).base64EncodedString()))
    }

    @Test func hibernatesOnlyAnIdleHiddenPaneWithNothingToLose() throws {
        func decide(
            hiddenFor: TimeInterval = 700, after: TimeInterval = 600, ready: Bool = true,
            activity: PCodeSessionPane.Activity = .idle, hostState: String? = "idle",
            composer: PCodeComposerStatus? = nil, hasShell: Bool = false, turns: Int = 3
        ) -> Bool {
            let work = composer == nil ? PCodePaneWork() : PCodePaneWork(status: composer)
            return PCodeWorkspace.shouldHibernate(
                hiddenFor: hiddenFor, after: after, ready: ready, activity: activity,
                hostState: hostState, work: work, hasShell: hasShell, turns: turns)
        }
        #expect(decide())
        #expect(!decide(after: 0), "0 minutes turns hibernation off")
        #expect(!decide(hiddenFor: 100))
        #expect(!decide(ready: false))
        #expect(!decide(activity: .running))
        #expect(!decide(activity: .failed), "an unseen failure stays live")
        #expect(!decide(hostState: nil), "no host to release")
        #expect(!decide(hostState: "working"))
        #expect(!decide(hasShell: true), "its shell would be killed")
        #expect(!decide(turns: 0), "its host would delete a turnless session")
        #expect(!decide(composer: try composer(draft: true)), "unsent text")
        #expect(!decide(composer: try composer(jobs: 1)))
        #expect(!decide(composer: try composer(btwReady: 1)))
        #expect(!decide(composer: try composer(draft: nil)), "a pcode too old to report drafts")
        #expect(PCodePaneWork(queued: 1).isQuiet == false, "a native composer's queued prompt")
        #expect(!PCodeWorkspace.shouldHibernate(
            hiddenFor: 700, after: 600, ready: true, activity: .idle, hostState: "idle",
            work: nil, hasShell: false, turns: 3), "a composer not attached can't tell")
    }

    @Test func quietProjectKeepsItsFolderAndExactCounts() throws {
        let busy = PCodeWorkspace.normalizePath("/tmp/busy-proj")
        let quiet = PCodeWorkspace.normalizePath("/tmp/quiet-proj")
        // 300 newer sessions in one project, then three old ones in another.
        var all = try (0..<300).map { i in
            try makeInfo("busy-\(1000 - i)", workspace: busy, updated: "2026-11-26T\(String(format: "%02d", 23 - i / 60)):\(String(format: "%02d", 59 - i % 60)):00+00:00")
        }
        all += try (1...3).map { try makeInfo("quiet-\($0)", workspace: quiet, updated: "2026-01-0\($0)T00:00:00+00:00") }
        all.sort { $0.updated > $1.updated }

        let slice = PCodeWorkspace.sliceHistory(all, keep: [], recentLimits: [:], defaultLimit: 5)
        #expect(slice.sessions.count == 8)
        #expect(slice.remaining == [busy: 295])
        let groups = PCodeWorkspace.buildProjectGroups(
            panes: [], projectPathForPane: { $0.projectPath }, history: slice.sessions,
            detachedHostStatuses: [:], pinnedFolders: [], hiddenFolders: [],
            remainingRecentCounts: slice.remaining)
        #expect(groups.map(\.path) == [busy, quiet])
        #expect(groups[0].recentSessions.count == 5)
        #expect(groups[0].remainingRecentCount == 295)
        #expect(groups[0].recentCount == 300)
        #expect(groups[1].recentSessions.map(\.id) == ["quiet-3", "quiet-2", "quiet-1"])
        #expect(groups[1].recentCount == 3)
    }

    @Test func showingMoreReslicesFromMemoryWithExactRemaining() throws {
        let proj = PCodeWorkspace.normalizePath("/tmp/more-proj")
        let all = try (0..<120).map { i in
            try makeInfo(String(format: "s%03d", i), workspace: proj, updated: String(format: "2026-11-26T01:%02d:%02d+00:00", 59 - i / 60, 59 - i % 60))
        }
        // A starred, an open, and a hosted session never count against the limit.
        let keep: Set<String> = ["s000", "s050", "s119"]
        let collapsed = PCodeWorkspace.sliceHistory(all, keep: keep, recentLimits: [:], defaultLimit: 5)
        #expect(collapsed.sessions.map(\.id) == ["s000", "s001", "s002", "s003", "s004", "s005", "s050", "s119"])
        #expect(collapsed.remaining == [proj: 112])

        let page = PCodeWorkspace.initialRecentLimit + PCodeWorkspace.recentPageSize
        let expanded = PCodeWorkspace.sliceHistory(all, keep: keep, recentLimits: [proj: page], defaultLimit: 5)
        #expect(expanded.sessions.count == page + keep.count)
        #expect(expanded.remaining == [proj: 117 - page])
        let groups = PCodeWorkspace.buildProjectGroups(
            panes: [], projectPathForPane: { $0.projectPath }, history: expanded.sessions,
            detachedHostStatuses: [:], pinnedFolders: [], hiddenFolders: [], starredSessionIDs: keep,
            remainingRecentCounts: expanded.remaining)
        #expect(groups[0].unstarredRecentSessions.count == page)
        #expect(groups[0].remainingRecentCount == 117 - page)

        let everything = PCodeWorkspace.sliceHistory(all, keep: keep, recentLimits: [proj: page + 100], defaultLimit: 5)
        #expect(everything.sessions.count == 120)
        #expect(everything.remaining.isEmpty)
    }

    @Test func hiddenFolderCountsRemainingOnlyWhenItListsUnstarredSessions() throws {
        let starredOnly = PCodeWorkspace.normalizePath("/tmp/hidden-starred-only")
        let withHost = PCodeWorkspace.normalizePath("/tmp/hidden-with-host")
        let all = try (0..<8).map { i in
            try makeInfo("a\(i)", workspace: starredOnly, updated: "2026-11-26T0\(9 - i):00:00+00:00")
        } + (0..<8).map { i in
            try makeInfo("b\(i)", workspace: withHost, updated: "2026-11-25T0\(9 - i):00:00+00:00")
        }
        let host = PCodeHostStatus(
            hostID: "h", pid: 1, sessionID: "b0", state: "idle", title: "", outcome: "", turns: 1, unseen: false, updated: 1)
        let keep: Set<String> = ["a3", "b0"]
        let slice = PCodeWorkspace.sliceHistory(all, keep: keep, recentLimits: [:], defaultLimit: 5)
        #expect(slice.remaining == [starredOnly: 2, withHost: 2])
        let groups = PCodeWorkspace.buildProjectGroups(
            panes: [], projectPathForPane: { $0.projectPath }, history: slice.sessions,
            detachedHostStatuses: ["b0": host], pinnedFolders: [], hiddenFolders: [starredOnly, withHost],
            starredSessionIDs: ["a3"], remainingRecentCounts: slice.remaining)
        #expect(groups.map(\.path) == [withHost, starredOnly])
        // A live host shows the hidden folder's recents, and counts the rest.
        #expect(groups[0].recentSessions.count == 5)
        #expect(groups[0].remainingRecentCount == 2)
        // Shown only for its starred session: no others, so nothing to count.
        #expect(groups[1].recentSessions.map(\.id) == ["a3"])
        #expect(groups[1].remainingRecentCount == 0)
    }

    @Test func hostedSessionsSurfaceActivityAndUnseenInProjectGroup() throws {
        let data = Data("""
        {"id":"s1","workspace":"/tmp/proj","updated":"2026-11-26T02:00:00+00:00","title":"Saved Title","turns":0}
        """.utf8)
        let info = try JSONDecoder().decode(PCodeSessionInfo.self, from: data)

        let workingHost = PCodeHostStatus(
            hostID: "h1", pid: 42, sessionID: "s1", state: "working",
            title: "Live Title", outcome: "", turns: 0, unseen: false, updated: 1_700_000_000)
        let working = PCodeHostedSession(info: info, host: workingHost)
        #expect(working.title == "Live Title")
        #expect(working.activity == .running)
        #expect(working.updatedDate == Date(timeIntervalSince1970: 1_700_000_000))

        let workingGroup = PCodeProjectGroup(path: "/tmp/proj", openPanes: [], hostedSessions: [working], recentSessions: [])
        #expect(workingGroup.activity == .running)
        #expect(!workingGroup.hasUnseen([]))
        #expect(workingGroup.hasUnseen(["s1"]))

        let failedHost = PCodeHostStatus(
            hostID: "h1", pid: 42, sessionID: "s1", state: "idle",
            title: "", outcome: "failed", turns: 1, unseen: true, updated: 0)
        let failed = PCodeHostedSession(info: info, host: failedHost)
        #expect(failed.title == "Saved Title")
        #expect(failed.activity == .failed)
        #expect(failed.updatedDate == info.updatedDate)

        let failedGroup = PCodeProjectGroup(path: "/tmp/proj", openPanes: [], hostedSessions: [failed], recentSessions: [])
        #expect(failedGroup.activity == .failed)
        #expect(failedGroup.hasUnseen([]))

        let startingHost = PCodeHostStatus(
            hostID: "h1", pid: 42, sessionID: "s1", state: "starting",
            title: "Booting", outcome: "cancelled", turns: 1, unseen: false, updated: 102)
        let idleGroup = PCodeProjectGroup(
            path: "/tmp/proj", openPanes: [],
            hostedSessions: [PCodeHostedSession(info: info, host: startingHost)],
            recentSessions: [])
        #expect(idleGroup.activity == .idle)
    }

    @Test func buildProjectGroupsPreservesNewerRecentsInHiddenFolderWithHostedSession() throws {
        func makeInfo(id: String, workspace: String, updated: String) throws -> PCodeSessionInfo {
            let json = """
            {"id":"\(id)","workspace":"\(workspace)","updated":"\(updated)","title":"\(id)","turns":1}
            """
            return try JSONDecoder().decode(PCodeSessionInfo.self, from: Data(json.utf8))
        }

        let hiddenPath = PCodeWorkspace.normalizePath("/tmp/hidden-proj")
        let visiblePath = PCodeWorkspace.normalizePath("/tmp/visible-proj")
        let newerRecent = try makeInfo(id: "newer-recent", workspace: hiddenPath, updated: "2026-11-26T03:00:00+00:00")
        let hostedOld = try makeInfo(id: "hosted-old", workspace: hiddenPath, updated: "2026-11-26T01:00:00+00:00")
        let olderRecent = try makeInfo(id: "older-recent", workspace: hiddenPath, updated: "2026-11-25T23:00:00+00:00")
        let visibleRecent = try makeInfo(id: "visible-recent", workspace: visiblePath, updated: "2026-11-26T02:00:00+00:00")

        let host = PCodeHostStatus(
            hostID: "h1", pid: 42, sessionID: "hosted-old", state: "working",
            title: "Hosted", outcome: "", turns: 0, unseen: false, updated: 100)

        let groups = PCodeWorkspace.buildProjectGroups(
            panes: [],
            projectPathForPane: { $0.projectPath },
            history: [newerRecent, visibleRecent, hostedOld, olderRecent],
            detachedHostStatuses: ["hosted-old": host],
            pinnedFolders: [],
            hiddenFolders: [hiddenPath])

        #expect(groups.map(\.path) == [hiddenPath, visiblePath])
        #expect(groups[0].hostedSessions.map(\.id) == ["hosted-old"])
        // Both newer and older recents in the hidden folder are preserved because it has a live host.
        #expect(groups[0].recentSessions.map(\.id) == ["newer-recent", "older-recent"])
        #expect(groups[1].recentSessions.map(\.id) == ["visible-recent"])
    }

    @Test func clampsFolderHeightWithinBounds() {
        #expect(PCodeSidebarLayout.clampedFolderHeight(10) == PCodeSidebarLayout.minFolderHeight)
        #expect(PCodeSidebarLayout.clampedFolderHeight(180) == 180)
        #expect(PCodeSidebarLayout.clampedFolderHeight(2000) == PCodeSidebarLayout.maxFolderHeight)
        #expect(PCodeSidebarLayout.clampedFolderHeight(0) == PCodeSidebarLayout.minFolderHeight)
        #expect(PCodeSidebarLayout.clampedFolderHeight(.nan) == PCodeSidebarLayout.minFolderHeight)
    }

    @Test func sessionSubtitleCombinesWorktreeAndRelativeTime() {
        let now = Date(timeIntervalSince1970: 1_700_000_000)
        let fourHoursAgo = now.addingTimeInterval(-4 * 3600)
        #expect(PCodeWorkspace.sessionSubtitle(worktreeName: nil, updatedDate: fourHoursAgo, now: now) == "4h")
        #expect(PCodeWorkspace.sessionSubtitle(worktreeName: "fix-ui", updatedDate: fourHoursAgo, now: now) == "fix-ui · 4h")
        #expect(PCodeWorkspace.sessionSubtitle(worktreeName: "fix-ui", updatedDate: nil, now: now) == "fix-ui")
        #expect(PCodeWorkspace.sessionSubtitle(worktreeName: nil, updatedDate: nil, now: now) == nil)
    }

    @Test func buildProjectGroupsFloatsStarredToTopOfFolderAndSortsHostedByLastActive() throws {
        func makeInfo(id: String, workspace: String, updated: String) throws -> PCodeSessionInfo {
            let json = """
            {"id":"\(id)","workspace":"\(workspace)","updated":"\(updated)","title":"\(id)","turns":1}
            """
            return try JSONDecoder().decode(PCodeSessionInfo.self, from: Data(json.utf8))
        }

        let proj = PCodeWorkspace.normalizePath("/tmp/star-proj")
        let hiddenProj = PCodeWorkspace.normalizePath("/tmp/hidden-star-proj")

        let recentNew = try makeInfo(id: "recent-new", workspace: proj, updated: "2026-11-26T05:00:00+00:00")
        let recentOldStarred = try makeInfo(id: "recent-old-starred", workspace: proj, updated: "2026-11-25T01:00:00+00:00")
        let hostedOlder = try makeInfo(id: "hosted-older", workspace: proj, updated: "2026-11-26T01:00:00+00:00")
        let hostedNewer = try makeInfo(id: "hosted-newer", workspace: proj, updated: "2026-11-26T04:00:00+00:00")
        let hostedStarred = try makeInfo(id: "hosted-starred", workspace: proj, updated: "2026-11-25T02:00:00+00:00")
        let hiddenStarred = try makeInfo(id: "hidden-starred", workspace: hiddenProj, updated: "2026-11-25T03:00:00+00:00")

        let hosts: [String: PCodeHostStatus] = [
            "hosted-older": PCodeHostStatus(hostID: "h1", pid: 1, sessionID: "hosted-older", state: "idle", title: "", outcome: "", turns: 1, unseen: false, updated: 100),
            "hosted-newer": PCodeHostStatus(hostID: "h2", pid: 2, sessionID: "hosted-newer", state: "working", title: "", outcome: "", turns: 1, unseen: false, updated: 300),
            "hosted-starred": PCodeHostStatus(hostID: "h3", pid: 3, sessionID: "hosted-starred", state: "idle", title: "", outcome: "failed", turns: 1, unseen: true, updated: 50),
        ]

        let groups = PCodeWorkspace.buildProjectGroups(
            panes: [],
            projectPathForPane: { $0.projectPath },
            history: [recentNew, hostedNewer, hostedOlder, hiddenStarred, hostedStarred, recentOldStarred],
            detachedHostStatuses: hosts,
            pinnedFolders: [],
            hiddenFolders: [hiddenProj],
            starredSessionIDs: ["recent-old-starred", "hosted-starred", "hidden-starred"])

        #expect(groups.map(\.path) == [proj, hiddenProj])
        // Starred hosted floats first; remaining hosted sorted by updated descending.
        #expect(groups[0].hostedSessions.map(\.id) == ["hosted-starred", "hosted-newer", "hosted-older"])
        #expect(groups[0].starredHostedSessions.map(\.id) == ["hosted-starred"])
        #expect(groups[0].unstarredHostedSessions.map(\.id) == ["hosted-newer", "hosted-older"])
        // Starred recent floats first even though it is older.
        #expect(groups[0].recentSessions.map(\.id) == ["recent-old-starred", "recent-new"])
        #expect(groups[0].starredRecentSessions.map(\.id) == ["recent-old-starred"])
        #expect(groups[0].unstarredRecentSessions.map(\.id) == ["recent-new"])
        // Folder status flags surface running, failed, and unseen simultaneously.
        #expect(groups[0].hasRunning)
        #expect(groups[0].hasFailed)
        #expect(groups[0].hasUnseen([]))
        // Hidden folder stays visible because it contains a starred session.
        #expect(groups[1].starredRecentSessions.map(\.id) == ["hidden-starred"])
    }

    @Test func openPaneSortDateUsesSessionCreationTimeAndIgnoresLaterUpdates() throws {
        let paneOpenedAt = Date(timeIntervalSince1970: 1_800_000_000)

        // Brand-new pane with no saved session yet uses the pane's open time.
        #expect(PCodeSessionPane.sortDate(saved: nil, createdAt: paneOpenedAt) == paneOpenedAt)

        // Saved session with `created` stays anchored to creation time even after newer responses.
        let created = "2026-11-20T08:00:00+00:00"
        let infoWithCreated = try JSONDecoder().decode(
            PCodeSessionInfo.self,
            from: Data("""
            {"id":"s1","workspace":"/tmp/proj","created":"\(created)","updated":"2026-11-26T20:00:00+00:00","title":"Old","turns":12}
            """.utf8)
        )
        #expect(PCodeSessionPane.sortDate(saved: infoWithCreated, createdAt: paneOpenedAt) == PCodeWorkspace.parseISO8601(created))

        // Saved session missing `created` falls back to pane open time rather than bouncing on `updated`.
        let infoWithoutCreated = try makeInfo("s2", workspace: "/tmp/proj", updated: "2026-11-26T21:00:00+00:00")
        #expect(PCodeSessionPane.sortDate(saved: infoWithoutCreated, createdAt: paneOpenedAt) == paneOpenedAt)
    }
}
