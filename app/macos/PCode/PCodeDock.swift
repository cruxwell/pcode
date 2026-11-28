import AppKit
import GhosttyKit
import SwiftUI

/// Live status & send-mode state sent by pcode's docked editor (`pcode;composer;<base64 json>`).
struct PCodeComposerStatus: Equatable, Decodable {
    let busy: Bool
    let phase: String
    let sendMode: String
    let sendModeOnce: Bool
    let starting: Bool
    let steeringPending: Int
    let queued: Int
    let jobs: Int
    let btwRunning: Int
    let btwReady: Int
    let cacheNote: String

    private enum CodingKeys: String, CodingKey {
        case busy
        case phase
        case sendMode = "send_mode"
        case sendModeOnce = "send_mode_once"
        case starting
        case steeringPending = "steering_pending"
        case queued
        case jobs
        case btwRunning = "btw_running"
        case btwReady = "btw_ready"
        case cacheNote = "cache_note"
    }

    static func decode(base64: String) -> PCodeComposerStatus? {
        guard !base64.isEmpty, let data = Data(base64Encoded: base64) else { return nil }
        return try? JSONDecoder().decode(PCodeComposerStatus.self, from: data)
    }

    static func formatElapsed(_ seconds: TimeInterval) -> String {
        let total = max(0, Int(seconds))
        if total < 60 { return "\(total)s" }
        let minutes = total / 60
        let rem = total % 60
        return rem == 0 ? "\(minutes)m" : "\(minutes)m \(rem)s"
    }
}

/// pcode's editor, tasks and status bar in a card docked at the bottom of a session pane.
///
/// In Pcode.app, each workspace pane runs `pcode --host` with `PCODE_EDITOR_ONLY=1`
/// directly on the pane's single `Ghostty.SurfaceView`, which is laid out as
/// `PCodeDockCard` below the native transcript (`PCodeMainWindowTranscriptView`).
///
/// Until pcode's editor is ready the surface fills the pane instead, unclipped,
/// so whatever pcode prints before then (the trust prompt, startup errors) is
/// seen and answered at full height.
///
/// - `pcode;ready`: switch to the card and give it the keyboard.
/// - `pcode;dock-rows;<n>`: the rows its layout needs.
/// - `pcode;composer;<base64 json>`: native status & steering strip state.
final class PCodeDock: NSObject, ObservableObject {
    @Published private(set) var ready = false
    /// Starts at the last count any dock reported, so a resumed pane's boot card
    /// and the room its transcript keeps usually match the first real frame.
    @Published private(set) var rows = PCodeDock.lastReportedRows
    private static var lastReportedRows = 3
    /// pcode waits on the user before its editor starts (`pcode;startup-input`):
    /// the trust prompt or a held startup error, which the boot cover must not hide.
    @Published private(set) var needsInput = false
    /// A resumed pane's compact boot card stands in for this card until the pane
    /// is ready: the full-pane terminal stays hidden so the saved transcript shows,
    /// and the transcript keeps the card's room so nothing moves when it appears.
    @Published var bootCovered = false
    /// Native status & send-mode strip state from `pcode;composer;...`.
    @Published private(set) var composerStatus: PCodeComposerStatus?
    /// When the current busy phase started, for the native elapsed timer.
    @Published private(set) var phaseStartedAt: Date?

    var showsStatusStrip: Bool { ready && composerStatus != nil }

    static let statusStripHeight: CGFloat = 22
    static let statusStripSpacing: CGFloat = 4

    static let maxRows = 40
    /// The dock's terminal is always this tall and never resizes; the card
    /// shows only its bottom `rows`, so growing is a clip, not a grid resize.
    static let gridRows = maxRows

    /// Vertical padding inside the card, in points: covers the terminal's
    /// bottom padding and leaves symmetric breathing room above the top row
    /// regardless of font size.
    static let cardPadding: CGFloat = 12
    /// The card's top and bottom margins, inside `footprint`.
    static let topMargin: CGFloat = 2
    static let bottomMargin: CGFloat = 8
    /// Maximum visible width of the docked editor card in points.
    /// Wider than `PCodeTranscriptDocumentView.maxContentWidth` (800pt) by ~20
    /// monospace characters on each side (~160pt) so the composer stays roomy
    /// under the reading column while stopping on wide windows.
    static let maxCardWidth: CGFloat = 1120

    /// The card's own height for `rows` terminal rows of `cell` points.
    /// When `surfaceRows` is given and smaller than `gridRows` (e.g. a user
    /// configured `window-padding-y` > `cardPadding / 2`), expands the clip by
    /// the surface's actual vertical padding so the top row is never clipped.
    static let paddingRowsSlack = 2

    static func cardHeight(rows: Int, cell: CGFloat, surfaceRows: Int? = nil) -> CGFloat {
        let surfaceHeight = CGFloat(gridRows) * cell + cardPadding
        let padding: CGFloat
        // Only a grid within `paddingRowsSlack` of `gridRows` is the card's own: a
        // smaller one is the pre-ready full-pane grid, still reported for a frame
        // after the switch (Ghostty publishes `surfaceSize` asynchronously), and
        // taking its padding would jump the card and the transcript above it.
        if let surfaceRows, surfaceRows >= gridRows - paddingRowsSlack, surfaceRows < gridRows {
            padding = max(cardPadding, surfaceHeight - CGFloat(surfaceRows) * cell)
        } else {
            padding = cardPadding
        }
        return CGFloat(rows) * cell + padding
    }

    /// The height of the editor terminal that takes clicks, from its bottom
    /// edge: the card's while shown (the rest of the grid is clipped off), or
    /// nil, the whole terminal, before pcode is ready and it fills the pane.
    static func visibleHeight(ready: Bool, rows: Int, cell: CGFloat, surfaceRows: Int? = nil) -> CGFloat? {
        guard ready, cell > 0 else { return nil }
        return cardHeight(rows: rows, cell: cell, surfaceRows: surfaceRows)
    }

    /// The editor terminal's frame height in a pane of `paneHeight`: a fixed
    /// `gridRows` grid once ready, or the whole pane inside the card's margins before.
    static func editorHeight(ready: Bool, cell: CGFloat, paneHeight: CGFloat) -> CGFloat {
        ready ? cardHeight(rows: gridRows, cell: cell) : max(0, paneHeight - topMargin - bottomMargin)
    }

    /// Everything the card covers at the bottom of pcode's pane for `rows`
    /// rows of `cell` points, margins and optional native status strip included.
    static func footprint(
        rows: Int,
        cell: CGFloat,
        surfaceRows: Int? = nil,
        statusStrip: Bool = false
    ) -> CGFloat {
        cardHeight(rows: rows, cell: cell, surfaceRows: surfaceRows)
            + topMargin
            + bottomMargin
            + (statusStrip ? statusStripHeight + statusStripSpacing : 0)
    }

    /// Horizontal placement of `PCodeDockCard`'s outer container (including `bottomMargin`
    /// on each side) inside a pane of `paneWidth`.
    struct HorizontalLayout: Equatable {
        /// Width of the pane (or `/btw` drawer) slot the dock's click backstop spans.
        let slotWidth: CGFloat
        /// Leading edge of the card's outer container in pane coordinates.
        let outerMinX: CGFloat
        /// Width of the card's outer container (visible card width + `2 * bottomMargin`).
        let outerWidth: CGFloat
        /// Trailing padding from the pane's right edge (for `.bottomTrailing` alignment).
        let trailingInset: CGFloat
        /// Visible card width inside `bottomMargin` on both sides.
        var cardWidth: CGFloat { max(0, outerWidth - 2 * bottomMargin) }
        /// Leading edge of the visible card (and its terminal surface) in pane coordinates.
        var editorMinX: CGFloat { outerMinX + bottomMargin }
    }

    /// Computes the horizontal bounds of the dock card inside `paneWidth`: fills the
    /// pane (or the `/btw` drawer slot) up to `maxCardWidth`, and centers within that
    /// slot once wider.
    static func horizontalLayout(
        in paneWidth: CGFloat,
        drawerOpen: Bool = false,
        drawerFraction: CGFloat = 0.45
    ) -> HorizontalLayout {
        let maxOuter = maxCardWidth + 2 * bottomMargin
        let slotWidth: CGFloat
        let slotMinX: CGFloat
        if drawerOpen {
            slotWidth = max(0, PCodeDrawer.width(
                in: paneWidth,
                fraction: drawerFraction,
                minWidth: PCodeDrawer.dockedMinWidth) - 1)
            slotMinX = max(0, paneWidth - slotWidth)
        } else {
            slotWidth = max(0, paneWidth)
            slotMinX = 0
        }
        let outerWidth = min(slotWidth, maxOuter)
        let outerMinX = slotMinX + floor((slotWidth - outerWidth) * 0.5)
        let trailingInset = max(0, paneWidth - outerMinX - outerWidth)
        return HorizontalLayout(
            slotWidth: slotWidth,
            outerMinX: outerMinX,
            outerWidth: outerWidth,
            trailingInset: trailingInset
        )
    }

    struct CardLayout: Equatable {
        /// The docked terminal surface's frame in the pane (top-left origin).
        let editorRect: CGRect
        /// Distance from the pane's bottom edge to the top of the visible card.
        let cardTopFromBottom: CGFloat
    }

    /// Where a shown dock card's terminal surface sits inside a pane of `size`
    /// (top-left origin), including when narrowed into the `/btw` drawer.
    static func cardLayout(
        in size: CGSize,
        rows: Int,
        cell: CGFloat,
        surfaceRows: Int? = nil,
        editorHeight: CGFloat? = nil,
        drawerOpen: Bool = false,
        drawerFraction: CGFloat = 0.45,
        statusStrip: Bool = false
    ) -> CardLayout {
        let horizontal = horizontalLayout(
            in: size.width,
            drawerOpen: drawerOpen,
            drawerFraction: drawerFraction)
        let minX = max(0, min(horizontal.editorMinX, size.width))
        let width = max(0, min(horizontal.cardWidth, size.width - minX))
        let height: CGFloat
        if let editorHeight, editorHeight > 0 {
            height = editorHeight
        } else if cell > 0 {
            height = cardHeight(rows: gridRows, cell: cell)
        } else {
            height = 0
        }
        let maxY = size.height - bottomMargin
        let minY = maxY - height
        let stripExtra: CGFloat = statusStrip ? (topMargin + statusStripSpacing + statusStripHeight) : 0
        let cardTop = cell > 0
            ? bottomMargin + cardHeight(rows: rows, cell: cell, surfaceRows: surfaceRows) + stripExtra
            : 0
        return CardLayout(
            editorRect: CGRect(x: minX, y: minY, width: width, height: height),
            cardTopFromBottom: cardTop
        )
    }

    /// Where the active dock card sits inside `size`, or nil when not shown.
    func cardLayout(in size: CGSize, drawer: PCodeDrawer?) -> CardLayout? {
        guard ready, let editor = surfaceView else { return nil }
        let cell = editor.cellSize.height
        let surfaceRows = editor.surfaceSize.map { Int($0.rows) }
        let drawerOpen = drawer?.isOpen == true
        return Self.cardLayout(
            in: size,
            rows: rows,
            cell: cell,
            surfaceRows: surfaceRows,
            editorHeight: editor.bounds.height,
            drawerOpen: drawerOpen,
            drawerFraction: drawer?.fraction ?? 0.45,
            statusStrip: !drawerOpen && showsStatusStrip
        )
    }

    weak var surfaceView: Ghostty.SurfaceView?

    init(surfaceView: Ghostty.SurfaceView) {
        self.surfaceView = surfaceView
    }

    /// From pcode in the editor terminal this card belongs to.
    func handle(_ payload: String) {
        let parts = payload.split(separator: ";", omittingEmptySubsequences: false).map(String.init)
        switch parts.first {
        case "ready":
            guard !ready else { return }
            // Never animated: each frame from full height to the card would resize the grid.
            var transaction = Transaction()
            transaction.disablesAnimations = true
            withTransaction(transaction) { ready = true }
            if let surfaceView { Ghostty.moveFocus(to: surfaceView) }
        case "startup-input":
            if !needsInput { needsInput = true }
        case "dock-rows" where parts.count == 2:
            guard let wanted = Int(parts[1]) else { return }
            let clamped = min(Self.maxRows, max(1, wanted))
            if clamped != rows { rows = clamped }
            Self.lastReportedRows = clamped
        case "composer" where parts.count == 2:
            guard let decoded = PCodeComposerStatus.decode(base64: parts[1]) else { return }
            let wasActive = (composerStatus?.busy ?? false) || (composerStatus?.starting ?? false)
            let isActive = decoded.busy || decoded.starting
            let oldPhase = composerStatus?.phase ?? ""
            if isActive {
                if !wasActive || oldPhase != decoded.phase {
                    phaseStartedAt = Date()
                }
            } else {
                phaseStartedAt = nil
            }
            if composerStatus != decoded {
                composerStatus = decoded
            }
        default:
            break
        }
    }
}

/// A session pane's content: the native AppKit transcript, the `/btw` drawer,
/// and pcode's single terminal surface laid out as the docked editor card,
/// in a split as anywhere else.
///
/// While the `/btw` drawer is open, the drawer spans the full height on the right
/// and reserves the card's live footprint at its bottom; the card narrows into
/// that slot without leaving the view tree, so it keeps keyboard focus and the
/// drawer's content and hint bar never go underneath it.
struct PCodeDockedSurface: View {
    @EnvironmentObject var ghostty: Ghostty.App
    @ObservedObject var surfaceView: Ghostty.SurfaceView
    @ObservedObject var dock: PCodeDock
    @ObservedObject var drawer: PCodeDrawer
    @State private var sessionID: String?
    @State private var hostID: String?
    var isSplit: Bool

    private static let fallbackLineHeight: CGFloat = 18

    init(surfaceView: Ghostty.SurfaceView, dock: PCodeDock, isSplit: Bool) {
        self.surfaceView = surfaceView
        self.dock = dock
        self.drawer = surfaceView.pcodeDrawer
        self._sessionID = State(initialValue: surfaceView.pcodeSession)
        self._hostID = State(initialValue: surfaceView.pcodeHost)
        self.isSplit = isSplit
    }

    var body: some View {
        let drawerOpen = drawer.isOpen
        GeometryReader { geometry in
            if !surfaceView.pcodeIsSessionPane {
                PCodeDrawerSurface(
                    surfaceView: surfaceView,
                    drawer: drawer,
                    isSplit: isSplit)
            } else {
                let cardHorizontal = PCodeDock.horizontalLayout(
                    in: geometry.size.width,
                    drawerOpen: drawerOpen,
                    drawerFraction: drawer.fraction)
                let sid = surfaceView.pcodeSession ?? sessionID
                let hid = surfaceView.pcodeHost ?? hostID
                ZStack(alignment: .bottomTrailing) {
                    PCodeDockedTranscriptLayer(
                        sessionID: sid,
                        hostID: hid,
                        dock: dock,
                        docked: surfaceView,
                        fallbackCell: paneCell,
                        drawerOpen: drawerOpen)

                    PCodeDockedDrawerLayer(
                        surfaceView: surfaceView,
                        drawer: drawer,
                        dock: dock,
                        docked: surfaceView,
                        fallbackCell: paneCell,
                        isSplit: isSplit)

                    // The editor card: centered at the bottom up to `maxCardWidth` when no
                    // drawer is open, or docked into the bottom of the drawer when /btw is
                    // open. Keeping it at the same structural spot preserves its surface
                    // and focus; width snaps once instead of animating so the PTY doesn't
                    // reflow on every frame.
                    PCodeDockCard(
                        docked: surfaceView,
                        dock: dock,
                        fallbackCell: paneCell,
                        inDrawer: drawerOpen,
                        paneHeight: geometry.size.height,
                        horizontal: cardHorizontal,
                        isSplit: isSplit)
                        .frame(width: cardHorizontal.slotWidth)
                }
                .onReceive(NotificationCenter.default.publisher(for: PCodeWorkspace.sessionChanged, object: surfaceView)) { _ in
                    sessionID = surfaceView.pcodeSession
                    hostID = surfaceView.pcodeHost
                }
                .onChange(of: ObjectIdentifier(surfaceView)) { _ in
                    sessionID = surfaceView.pcodeSession
                    hostID = surfaceView.pcodeHost
                }
                .onAppear {
                    if sessionID != surfaceView.pcodeSession {
                        sessionID = surfaceView.pcodeSession
                    }
                    if hostID != surfaceView.pcodeHost {
                        hostID = surfaceView.pcodeHost
                    }
                }
            }
        }
    }

    private var paneCell: CGFloat {
        surfaceView.cellSize.height > 0 ? surfaceView.cellSize.height : Self.fallbackLineHeight
    }
}

/// Observes `dock` and `dock.surface` so the main-window native transcript reserves
/// the exact live bottom footprint of `PCodeDockCard` as the editor grows or zooms.
private struct PCodeDockedTranscriptLayer: View {
    let sessionID: String?
    var hostID: String? = nil
    @ObservedObject var dock: PCodeDock
    @ObservedObject var docked: Ghostty.SurfaceView
    var fallbackCell: CGFloat
    var drawerOpen: Bool

    var body: some View {
        let cell = docked.cellSize.height > 0 ? docked.cellSize.height : fallbackCell
        let surfaceRows = docked.surfaceSize.map { Int($0.rows) }
        // Before ready the terminal fills the pane, so its row count says nothing of the card's.
        let bottomInset: CGFloat = (!drawerOpen && (dock.ready || dock.bootCovered))
            ? PCodeDock.footprint(
                rows: dock.rows,
                cell: cell,
                surfaceRows: dock.ready ? surfaceRows : nil,
                statusStrip: dock.showsStatusStrip)
            : 0
        PCodeMainWindowTranscriptView(
            sessionID: sessionID,
            hostID: hostID,
            bottomContentInset: bottomInset,
            composerSurface: docked,
            booting: dock.bootCovered && !dock.ready)
    }
}

/// Observes the dock's surface so font-size changes update the drawer's reserved
/// bottom footprint immediately in lockstep with `PCodeDockCard`.
private struct PCodeDockedDrawerLayer: View {
    @ObservedObject var surfaceView: Ghostty.SurfaceView
    @ObservedObject var drawer: PCodeDrawer
    @ObservedObject var dock: PCodeDock
    @ObservedObject var docked: Ghostty.SurfaceView
    var fallbackCell: CGFloat
    var isSplit: Bool

    var body: some View {
        let cell = docked.cellSize.height > 0 ? docked.cellSize.height : fallbackCell
        let surfaceRows = docked.surfaceSize.map { Int($0.rows) }
        PCodeDrawerSurface(
            surfaceView: surfaceView,
            drawer: drawer,
            isSplit: isSplit,
            drawsSurface: false,
            dockFootprint: PCodeDock.footprint(rows: dock.rows, cell: cell, surfaceRows: surfaceRows))
    }
}

/// The docked editor's card, with a backstop over its whole footprint.
///
/// Its own view so it observes the dock's terminal: the card's height is in
/// that terminal's cells, and pcode reckons the pane rows the card covers from
/// them, so a font change there must resize the card at once.
///
/// Before `pcode;ready` the same card fills the pane, unclipped: the view
/// tree stays put, so the terminal keeps its focus when it shrinks to the card.
private struct PCodeDockCard: View {
    @EnvironmentObject var ghostty: Ghostty.App
    @ObservedObject var docked: Ghostty.SurfaceView
    @ObservedObject var dock: PCodeDock
    var fallbackCell: CGFloat
    var inDrawer: Bool
    var paneHeight: CGFloat
    var horizontal: PCodeDock.HorizontalLayout
    var isSplit: Bool

    private static let cornerRadius: CGFloat = 12

    var body: some View {
        let cell = docked.cellSize.height > 0 ? docked.cellSize.height : fallbackCell
        let surfaceRows = docked.surfaceSize.map { Int($0.rows) }
        let showStrip = !inDrawer && dock.showsStatusStrip
        ZStack(alignment: .bottomTrailing) {
            // The dock's whole bottom band across the slot (pane or /btw drawer),
            // gutters included, takes the clicks the card itself doesn't: the
            // transcript rows under it are faded or covered by the drawer, and a
            // drag there would start a selection in them. Leave the leading 4pt
            // clear at the drawer's left edge so the drawer's 8pt drag handle
            // works alongside the card.
            Color.clear
                .frame(height: PCodeDock.footprint(
                    rows: dock.rows,
                    cell: cell,
                    surfaceRows: surfaceRows,
                    statusStrip: showStrip))
                .contentShape(Rectangle())
                .gesture(DragGesture(minimumDistance: 0))
                .padding(.leading, inDrawer ? 4 : 0)
                .allowsHitTesting(dock.ready)
            VStack(spacing: PCodeDock.statusStripSpacing) {
                if showStrip, let status = dock.composerStatus {
                    PCodeComposerStatusStrip(status: status, startedAt: dock.phaseStartedAt)
                        .padding(.horizontal, PCodeDock.bottomMargin + 4)
                }
                card(cell: cell, surfaceRows: surfaceRows)
            }
                .frame(width: horizontal.outerWidth)
                .padding(.trailing, horizontal.trailingInset)
                // Not removed: the view tree stays put so the terminal keeps its grid and focus.
                .opacity(dock.ready || !dock.bootCovered ? 1 : 0)
                .allowsHitTesting(dock.ready || !dock.bootCovered)
        }
        // Only the card's clip animates as `dock.rows` changes, never its width.
        .animation(.easeOut(duration: 0.12), value: dock.rows)
    }

    private func card(cell: CGFloat, surfaceRows: Int?) -> some View {
        // The card is the dock's terminal itself, edge to edge: pcode tints its
        // background (OSC 11) a shade off the pane's, and its own padding takes
        // that tint, so the whole card is one raised surface. The backdrop only
        // gives the shadow its shape.
        //
        // The terminal keeps a fixed `gridRows` height, bottom-aligned in the
        // card and clipped: pcode draws its layout at the bottom of it, so a
        // taller layout is already drawn when the card grows to show it.
        // Before pcode is ready it fills the pane, clipped only at the rounded corners. The
        // switch is not animated: each frame of it would resize the grid.
        let editorHeight = PCodeDock.editorHeight(ready: dock.ready, cell: cell, paneHeight: paneHeight)
        let shownHeight = dock.ready
            ? PCodeDock.cardHeight(rows: dock.rows, cell: cell, surfaceRows: surfaceRows)
            : editorHeight
        return ZStack(alignment: .bottom) {
            Ghostty.SurfaceWrapper(surfaceView: docked, isSplit: isSplit)
                .id(ObjectIdentifier(docked))
                .frame(height: editorHeight)
                // The grid's size never animates, even when `dock.rows` changes in
                // the same update: only the card's clip does.
                .transaction { $0.animation = nil }
        }
            .frame(height: shownHeight, alignment: .bottom)
            .clipShape(RoundedRectangle(cornerRadius: Self.cornerRadius))
            .overlay(
                RoundedRectangle(cornerRadius: Self.cornerRadius)
                    .strokeBorder(inDrawer ? ghostty.config.splitDividerColor : .clear, lineWidth: 1)
                    .allowsHitTesting(false)
            )
            .background(
                RoundedRectangle(cornerRadius: Self.cornerRadius)
                    .fill(ghostty.config.backgroundColor)
                    .shadow(
                        color: inDrawer ? .clear : .black.opacity(0.28),
                        radius: inDrawer ? 0 : 18,
                        x: 0,
                        y: inDrawer ? 0 : -2)
            )
            .padding(.horizontal, PCodeDock.bottomMargin)
            .padding(.top, PCodeDock.topMargin)
            .padding(.bottom, PCodeDock.bottomMargin)
    }
}

/// Thin native status & steering strip perched directly above the docked editor card.
/// Shows coarse turn status (spinner + phase + elapsed clock) on the left while busy,
/// and live pending counts + send-mode pill (`steering` / `queue`) on the right.
/// The native composer passes `onToggleSendMode`, which makes the pill a button.
struct PCodeComposerStatusStrip: View {
    @EnvironmentObject var ghostty: Ghostty.App
    @Environment(\.pcodeWindowOnScreen) private var onScreen
    let status: PCodeComposerStatus
    let startedAt: Date?
    var onToggleSendMode: (() -> Void)? = nil
    /// The native composer's `@` file picker (Cmd+Shift+A).
    var onOpenFiles: (() -> Void)? = nil
    /// The native composer's plan: a summary that opens the checklist (`PCodeTaskStrip`).
    var tasks: [PCodePlanItem] = []
    var tasksShown: Binding<Bool>? = nil

    private var stripColorScheme: ColorScheme {
        NSColor(ghostty.config.backgroundColor).isLightColor ? .light : .dark
    }

    private var activityBadges: [String] {
        var items: [String] = []
        if status.steeringPending > 0 {
            items.append("\(status.steeringPending) steering")
        }
        if status.queued > 0 {
            items.append("\(status.queued) queued")
        }
        if status.jobs > 0 {
            items.append("\(status.jobs) \(status.jobs == 1 ? "job" : "jobs")")
        }
        if status.btwRunning > 0 {
            items.append("\(status.btwRunning) btw running")
        }
        if status.btwReady > 0 {
            items.append("\(status.btwReady) btw ready")
        }
        return items
    }

    private var sendModeLabel: String {
        status.sendModeOnce ? "\(status.sendMode) (once)" : status.sendMode
    }

    var body: some View {
        HStack(spacing: 8) {
            if status.busy || status.starting {
                let colors = PCodeGyroSpinner.paletteColors(for: ghostty.config)
                HStack(spacing: 6) {
                    PCodeGyroSpinner(
                        primary: colors.primary,
                        secondary: colors.secondary,
                        isLight: stripColorScheme == .light,
                        compact: true
                    )
                    .frame(width: 14, height: 14)
                    Text(status.phase.isEmpty ? "Working" : status.phase)
                        .font(.system(size: 11.5, weight: .medium))
                        .foregroundStyle(.primary.opacity(0.82))
                        .lineLimit(1)
                        .truncationMode(.tail)
                    if let startedAt {
                        if onScreen {
                            TimelineView(.periodic(from: startedAt, by: 1)) { context in
                                elapsedLabel(context.date.timeIntervalSince(startedAt))
                            }
                        } else {
                            elapsedLabel(Date().timeIntervalSince(startedAt))
                        }
                    }
                }
                .layoutPriority(1)
            }

            if !tasks.isEmpty, let tasksShown {
                PCodeTaskStripSummary(items: tasks, isPresented: tasksShown)
                    .layoutPriority(0.5)
            }

            if !status.cacheNote.isEmpty {
                Text(status.cacheNote)
                    .font(.system(size: 10.5, weight: .medium))
                    .foregroundStyle(.orange.opacity(0.9))
                    .lineLimit(1)
                    .truncationMode(.tail)
            }

            Spacer(minLength: 8)

            HStack(spacing: 6) {
                if let onOpenFiles {
                    Button(action: onOpenFiles) {
                        Text("@ file")
                            .font(.system(size: 10.5, weight: .medium))
                            .foregroundStyle(.secondary)
                            .padding(.horizontal, 7)
                            .padding(.vertical, 1.5)
                            .background(Capsule(style: .continuous).fill(Color.primary.opacity(0.06)))
                            .contentShape(Capsule())
                    }
                    .buttonStyle(.plain)
                    .help("Insert a file reference (Cmd+Shift+A)")
                }
                ForEach(Array(activityBadges.enumerated()), id: \.offset) { _, badge in
                    Text(badge)
                        .font(.system(size: 10.5, weight: .medium).monospacedDigit())
                        .foregroundStyle(.secondary)
                        .lineLimit(1)
                        .fixedSize(horizontal: true, vertical: false)
                        .padding(.horizontal, 6)
                        .padding(.vertical, 1.5)
                        .background(
                            Capsule(style: .continuous)
                                .fill(Color.primary.opacity(0.06))
                        )
                }

                if let onToggleSendMode {
                    Button(action: onToggleSendMode) { sendModePill }
                        .buttonStyle(.plain)
                        .help("Send mode for the next message (Ctrl+S)")
                } else {
                    sendModePill
                }
            }
        }
        .frame(height: PCodeDock.statusStripHeight)
        .clipped()
        .background(ghostty.config.backgroundColor)
        .environment(\.colorScheme, stripColorScheme)
        .allowsHitTesting(onToggleSendMode != nil)
    }

    private var sendModePill: some View {
        Text(sendModeLabel)
            .font(.system(size: 10.5, weight: .medium))
            .foregroundStyle(.secondary)
            .lineLimit(1)
            .fixedSize(horizontal: true, vertical: false)
            .padding(.horizontal, 7)
            .padding(.vertical, 1.5)
            .background(
                Capsule(style: .continuous)
                    .fill(Color.primary.opacity(0.06))
            )
            .overlay(
                Capsule(style: .continuous)
                    .stroke(Color.primary.opacity(0.08), lineWidth: 0.5)
            )
            .contentShape(Capsule())
    }

    @ViewBuilder
    private func elapsedLabel(_ elapsed: TimeInterval) -> some View {
        if elapsed >= 1 {
            Text(PCodeComposerStatus.formatElapsed(elapsed))
                .font(.system(size: 11, weight: .regular).monospacedDigit())
                .foregroundStyle(.secondary.opacity(0.75))
                .fixedSize(horizontal: true, vertical: false)
        }
    }
}
