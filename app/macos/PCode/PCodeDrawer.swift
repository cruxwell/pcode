import AppKit
import GhosttyKit
import SwiftUI

/// A drawer over the right of a terminal surface that shows a pcode /btw thread.
///
/// pcode drives it with APCs from the terminal (see `PCode`):
///
/// - `pcode;btw;<host>;<thread>`: show the thread (`host` is 8 lowercase hex
///   digits, `thread` 1-64 plain characters), connecting to the host if needed.
/// - `pcode;close`: close the drawer and drop its connection.
/// - `pcode;hide`: hide the drawer but keep it connected.
/// - `pcode;show`: show it again, if it has a thread.
///
/// `pcode;?` is answered in Zig (`stream_handler.zig`) with `pcode;ok;1`.
/// Popups (`pcode;popup;...`) are `PCodePopup`'s.
///
/// The thread is drawn natively (`PCodeBtwDrawerContent`) with the
/// transcript's block views, from the host's side-question stream
/// (`PCodeAsideObserver`, the stream `pcode.btw_tail` draws in a terminal).
/// Nothing in the APC reaches the host: the checked host id only names its
/// socket, and the thread is matched against what the host sends.
final class PCodeDrawer: NSObject, ObservableObject {
    @Published var visible = false
    /// The drawer's share of the surface's width, set by dragging its left edge.
    @Published var fraction: CGFloat = 0.70
    /// The thread shown while the drawer is open; dropping it ends its connection.
    @Published private(set) var thread: PCodeBtwThreadModel?
    /// pcode's terminal font as the thread opened, for the heading and footer.
    private(set) var fonts: PCodePickFonts?
    /// The drawer's transcript while it is in a window, to tell whether it has the keyboard.
    weak var contentView: NSView?

    static let minWidth: CGFloat = 260
    /// Minimum width when the docked editor card lives inside the drawer.
    static let dockedMinWidth: CGFloat = 360
    static let maxFraction: CGFloat = 0.85

    /// True while the drawer is actually shown on screen with a thread.
    var isOpen: Bool { visible && thread != nil }

    static func width(in total: CGFloat, fraction: CGFloat, minWidth: CGFloat = PCodeDrawer.minWidth) -> CGFloat {
        let floor = min(total, minWidth)
        let upper = max(floor, total * maxFraction)
        return min(upper, max(floor, total * fraction))
    }

    weak var surfaceView: Ghostty.SurfaceView?
    /// A native-composer pane's drawer has no terminal: this gives its composer the keyboard.
    var onFocusInput: (() -> Void)?

    init(surfaceView: Ghostty.SurfaceView) {
        self.surfaceView = surfaceView
    }

    /// A drawer for a native-composer pane (`PCodeNativeBtwDrawer`), driven by its composer
    /// rather than by APCs.
    init(onFocusInput: @escaping () -> Void) {
        self.onFocusInput = onFocusInput
    }

    deinit {
        thread?.stop()
    }

    // MARK: Commands

    /// From pcode in the terminal this drawer belongs to.
    func handle(_ payload: String) {
        let parts = payload.split(separator: ";", omittingEmptySubsequences: false).map(String.init)
        switch parts.first {
        case "btw" where parts.count == 3:
            guard PCode.valid(parts[1], length: 8...8, hex: true), PCode.valid(parts[2], length: 1...64) else {
                return
            }
            show(host: parts[1], thread: parts[2])
        case "close":
            close()
        case "hide" where parts.count == 1:
            // While pcode draws a full-screen popup of its own over the terminal.
            let hadFocus = contentHasFocus
            visible = false
            if hadFocus { focusTerminal() }
        case "show" where parts.count == 1:
            if thread != nil { visible = true }
        default:
            break
        }
    }

    func show(host: String, thread name: String) {
        if let thread, thread.host == host {
            // Another thread of the same host keeps the connection.
            if thread.thread != name { thread.thread = name }
            thread.refreshHints()
        } else {
            thread?.stop()
            thread = PCodeBtwThreadModel(host: host, thread: name)
            fonts = PCodePickFonts.of(surfaceView?.surface)
        }
        // Shown beside the terminal without taking its keyboard: typing goes on
        // in pcode's editor, which replies to the thread.
        visible = true
    }

    func close() {
        let hadFocus = contentHasFocus
        visible = false
        thread?.stop()
        thread = nil
        if hadFocus { focusTerminal() }
    }

    func focusTerminal() {
        if let surfaceView {
            Ghostty.moveFocus(to: surfaceView)
        } else {
            onFocusInput?()
        }
    }

    private var contentHasFocus: Bool {
        guard let contentView, let responder = contentView.window?.firstResponder as? NSView else { return false }
        return responder === contentView || responder.isDescendant(of: contentView)
    }
}

/// The terminal surface, with its pcode drawer laid over its right side while one is shown.
///
/// An overlay rather than a split: the terminal keeps its size, so nothing
/// reflows and pcode's editor stays where it is, and the surface never leaves
/// the view tree, so it keeps the keyboard. The drawer slides in from the right
/// and stops several rows above the bottom so the editor and status bar stay
/// uncovered.
struct PCodeDrawerSurface: View {
    @EnvironmentObject var ghostty: Ghostty.App
    @ObservedObject var surfaceView: Ghostty.SurfaceView
    @ObservedObject var drawer: PCodeDrawer
    var isSplit: Bool
    /// SPIKE: the docked layout draws the terminal and the drawer as separate
    /// layers (`PCodeDockedSurface`), so the drawer spans the full height and
    /// reserves space at its bottom for the docked editor card.
    var drawsSurface = true
    var drawsDrawer = true
    /// Height reserved at the bottom of the drawer for the docked editor card.
    /// When set, the drawer spans the full pane height and hosts the card at
    /// its bottom; when nil, the drawer stops above the undocked inline editor.
    var dockFootprint: CGFloat?

    /// `drawer.fraction` when the current drag began: drags report distance from their start.
    @State private var dragStart: CGFloat?
    private static let minHeight: CGFloat = 120
    /// Terminal rows left uncovered at the bottom so pcode's inline editor and status bar stay visible.
    private static let bottomLines: CGFloat = 6
    private static let fallbackLineHeight: CGFloat = 18

    var body: some View {
        GeometryReader { geometry in
            ZStack {
                if drawsSurface {
                    Ghostty.SurfaceWrapper(surfaceView: surfaceView, isSplit: isSplit)
                } else {
                    Color.clear.allowsHitTesting(false)
                }
            }
                .overlay(alignment: .topTrailing) {
                    ZStack(alignment: .topTrailing) {
                        Color.clear
                        if drawsDrawer, let thread = drawer.thread {
                            let drawerWidth = width(in: geometry.size.width)
                            overlay(
                                thread,
                                width: drawerWidth,
                                height: height(in: geometry.size.height),
                                total: geometry.size.width,
                                totalHeight: geometry.size.height
                            )
                            // Keep the view mounted across `pcode;hide` and `pcode;show` so its
                            // scroll offset and selection survive while a popup covers the pane.
                            .offset(x: drawer.visible ? 0 : drawerWidth + 16)
                            .opacity(drawer.visible ? 1 : 0)
                            .allowsHitTesting(drawer.visible)
                            .transition(.move(edge: .trailing))
                            .zIndex(1)
                        }
                    }
                    .clipped()
                    .animation(.easeOut(duration: 0.18), value: drawer.visible)
                }
        }
    }

    private var minDrawerWidth: CGFloat {
        dockFootprint == nil ? PCodeDrawer.minWidth : PCodeDrawer.dockedMinWidth
    }

    private func width(in total: CGFloat) -> CGFloat {
        PCodeDrawer.width(in: total, fraction: drawer.fraction, minWidth: minDrawerWidth)
    }

    private func height(in total: CGFloat) -> CGFloat {
        if dockFootprint != nil { return total }
        let lineHeight = surfaceView.cellSize.height > 0 ? surfaceView.cellSize.height : Self.fallbackLineHeight
        let extraPadding: CGFloat
        if let rows = surfaceView.surfaceSize?.rows, rows > 0, surfaceView.cellSize.height > 0 {
            extraPadding = max(0, total - CGFloat(rows) * lineHeight) / 2
        } else {
            extraPadding = 0
        }
        let inset = min(lineHeight * Self.bottomLines + extraPadding, max(0, total - Self.minHeight))
        return max(0, total - inset)
    }

    private func overlay(
        _ thread: PCodeBtwThreadModel,
        width: CGFloat,
        height: CGFloat,
        total: CGFloat,
        totalHeight: CGFloat
    ) -> some View {
        VStack(spacing: 0) {
            HStack(spacing: 0) {
                // The left edge: a thin line to see, a wider strip to drag.
                Rectangle()
                    .fill(ghostty.config.splitDividerColor)
                    .frame(width: 1)
                    .overlay(
                        Color.clear
                            .frame(width: 8)
                            .contentShape(Rectangle())
                            .backport.pointerStyle(.resizeLeftRight)
                            .onHover { inside in
                                if #available(macOS 15, *) { return }
                                if inside { NSCursor.resizeLeftRight.push() } else { NSCursor.pop() }
                            }
                            .gesture(
                                DragGesture(coordinateSpace: .global)
                                    .onChanged { gesture in
                                        guard total > 0 else { return }
                                        let start = dragStart ?? (width / total)
                                        dragStart = start
                                        let next = start - gesture.translation.width / total
                                        let minFraction = min(1, minDrawerWidth / total)
                                        let maxFraction = max(minFraction, PCodeDrawer.maxFraction)
                                        drawer.fraction = min(maxFraction, max(minFraction, next))
                                    }
                                    .onEnded { _ in dragStart = nil }
                            )
                    )
                    .zIndex(1)
                VStack(spacing: 0) {
                    PCodeBtwDrawerContent(
                        model: thread,
                        docked: dockFootprint != nil,
                        fonts: drawer.fonts,
                        onFocusTerminal: { drawer.focusTerminal() },
                        onClose: { drawer.close() },
                        onContentView: { drawer.contentView = $0 })
                    .id(ObjectIdentifier(thread))
                    if let dockFootprint {
                        // Reserve the card's exact footprint inside the drawer so the
                        // thread and hint bar always end above it, with no overlap.
                        Color.clear
                            .frame(height: min(dockFootprint, max(0, totalHeight - Self.minHeight)))
                            .allowsHitTesting(false)
                    }
                }
                // Snap the content's height without animating so it lays out
                // once per row change rather than on every animation frame.
                .animation(nil, value: dockFootprint)
            }
            if dockFootprint == nil {
                // Bottom edge above the uncovered inline editor rows.
                Rectangle()
                    .fill(ghostty.config.splitDividerColor)
                    .frame(height: 1)
            }
        }
        .frame(width: width, height: height)
        .background(
            // Shadow the drawer's backdrop rather than its VStack, or SwiftUI
            // casts a separate drop shadow from the terminal surface, dividers
            // and every keycap in the hint footer.
            ghostty.config.backgroundColor
                .shadow(color: .black.opacity(0.35), radius: 8, x: -2, y: 2)
        )
    }

    /// The drawer's own scheme: its background is the terminal's, not the system's.
    private var colorScheme: ColorScheme {
        NSColor(ghostty.config.backgroundColor).isLightColor ? .light : .dark
    }
}

