import AppKit
import GhosttyKit
import SwiftUI

/// pcode's editor completion menu (`/` commands, `@` files), drawn natively
/// over the terminal instead of in its grid (`pcode.native_completions`).
///
/// Display only: the list never takes the keyboard. pcode keeps every key
/// (typing, arrows, Tab, Enter, Escape) and runs its completers itself; it
/// only tells us what its menu holds after each redraw. APCs (protocol 3):
///
/// - `pcode;popup?` is answered `pcode;popup;4` in Zig (`stream_handler.zig`).
/// - `pcode;complete;<token>`, from the terminal: connect to
///   `~/.local/state/pcode/popups/<token>.sock` and say `pcode-complete 1\n`.
///   One per pcode session; a new one replaces it.
///
/// Then newline-delimited JSON: pcode sends the menu to show (`Menu`) or
/// `{"hide": true}`, and we send `{"accept": <row>, "seq": <menu's seq>}`
/// when a row is clicked. pcode closing the socket hides the list.
final class PCodeCompletion: ObservableObject {
    struct Item: Decodable {
        let text: String
        let meta: String
    }

    struct Menu: Decodable {
        let seq: Int
        let items: [Item]
        /// Into `items`, which are `total` completions' window from `offset`.
        let selected: Int?
        let offset: Int
        let total: Int
        /// The completed word's start, in cells from the cursor (zero or less).
        let dx: Int
        /// Rows from the editor box's top down to the cursor's row.
        let dy: Int
        let style: PCodePickStyle?
    }

    static let hello = Data("pcode-complete 1\n".utf8)

    /// The menu on show, or nil while hidden.
    @Published private(set) var menu: Menu?

    weak var surfaceView: Ghostty.SurfaceView?
    private var connection: PCodePicker.Connection?
    private var themeCache: (config: Ghostty.Config, fonts: PCodePickFonts?, style: PCodePickStyle?,
                             theme: PCodePickTheme)?

    init(surfaceView: Ghostty.SurfaceView) {
        self.surfaceView = surfaceView
    }

    /// From pcode in the terminal this list belongs to.
    func handle(_ payload: String) {
        let parts = payload.split(separator: ";", omittingEmptySubsequences: false).map(String.init)
        guard parts.count == 2, parts[0] == "complete", PCode.valid(parts[1], length: 16...16, hex: true)
        else { return }
        start(token: parts[1])
    }

    private func start(token: String) {
        stop()
        let connection = PCodePicker.Connection(hello: Self.hello)
        self.connection = connection
        connection.start(
            path: PCode.statePath("popups/\(token).sock"),
            onLine: { [weak self, weak connection] line in
                guard let self, let connection, self.connection === connection else { return }
                self.receive(line)
            },
            onClosed: { [weak self, weak connection] in
                guard let self, let connection, self.connection === connection else { return }
                self.stop()
            })
    }

    func stop() {
        connection?.close()
        connection = nil
        menu = nil
    }

    private func receive(_ line: Data) {
        if let object = try? JSONSerialization.jsonObject(with: line) as? [String: Any],
           object["hide"] as? Bool == true {
            menu = nil
            return
        }
        // A menu we cannot read (a newer pcode) is hidden rather than half shown.
        menu = try? JSONDecoder().decode(Menu.self, from: line)
        guard menu != nil else { return }
        // The cursor is read as the list is laid out, but pcode's frame comes
        // over the pty, parsed on another thread, and may land just after this
        // menu: place the list again a frame later.
        DispatchQueue.main.asyncAfter(deadline: .now() + Self.replaceDelay) { [weak self] in
            guard let self, self.menu != nil else { return }
            self.objectWillChange.send()
        }
    }

    static let replaceDelay: TimeInterval = 0.03

    /// A row was clicked: pcode takes it, as Enter on it would.
    func accept(_ row: Int) {
        restoreFocus()
        guard let menu, let connection, menu.items.indices.contains(row),
              let data = try? JSONSerialization.data(withJSONObject: ["accept": row, "seq": menu.seq])
        else { return }
        connection.send(data + Data([0x0A]))
    }

    /// A click anywhere on the list must leave the keyboard with pcode's terminal.
    func restoreFocus() {
        if let editor = editorSurface, editor.window?.firstResponder !== editor {
            Ghostty.moveFocus(to: editor)
        }
    }

    private var editorSurface: Ghostty.SurfaceView? {
        surfaceView
    }

    func theme(config: Ghostty.Config, fonts: PCodePickFonts?) -> PCodePickTheme {
        let style = menu?.style
        if let cache = themeCache, cache.config === config, cache.fonts == fonts, cache.style == style {
            return cache.theme
        }
        let theme = PCodePickTheme(base: PCodePickTheme.Base(config), style: style, fonts: fonts)
        themeCache = (config, fonts, style, theme)
        return theme
    }

    /// The cursor's cell in the pane view, top-left origin, in points.
    ///
    /// When the editor runs in `PCodeDockCard`, offsets the terminal's cursor
    /// into the pane's coordinate space (`size`) so the pane-level list layer
    /// hangs from the card.
    func cursorCell(in size: CGSize? = nil) -> CGRect? {
        guard let surfaceView, let cell = surfaceView.pcodeCursorCell else { return nil }
        if let size,
           let dock = surfaceView.existingPCodeDock,
           let layout = dock.cardLayout(in: size, drawer: surfaceView.existingPCodeDrawer)
        {
            return cell.offsetBy(dx: layout.editorRect.minX, dy: layout.editorRect.minY)
        }
        return cell
    }
}

/// The rows of a completion menu, shared by the terminal editor's list
/// (`PCodeCompletionLayer`) and the native composer's (`PCodeComposerCompletionLayer`).
struct PCodeCompletionList: View {
    let rows: [(text: String, meta: String)]
    let selected: Int?
    /// Changes whenever the list does, to scroll the selection back into view.
    let revision: Int
    let theme: PCodePickTheme
    let rowHeight: CGFloat
    let onAccept: (Int) -> Void

    var body: some View {
        let textWidth = PCodeCompletionLayer.columns(rows.map(\.text), max: PCodeCompletionLayer.maxTextColumns)
            * (theme.fonts?.advance ?? 8)
        ScrollViewReader { proxy in
            ScrollView {
                LazyVStack(alignment: .leading, spacing: 0) {
                    ForEach(Array(rows.enumerated()), id: \.offset) { index, item in
                        row(item, chosen: index == selected, textWidth: textWidth)
                            .frame(height: rowHeight)
                            .id(index)
                            .contentShape(Rectangle())
                            .onTapGesture { onAccept(index) }
                    }
                }
                .padding(PCodeCompletionLayer.listPadding)
            }
            .onAppear { proxy.scrollTo(selected ?? 0) }
            // Keep the selected row in view; with none (a fresh or refiltered list), show the top.
            .onChange(of: revision) { _ in proxy.scrollTo(selected ?? 0) }
            .onChange(of: selected) { _ in proxy.scrollTo(selected ?? 0) }
        }
    }

    private func row(_ item: (text: String, meta: String), chosen: Bool, textWidth: CGFloat) -> some View {
        let text = chosen ? theme.selectedText.color : nil
        return HStack(spacing: 12) {
            Text(item.text)
                .font(theme.labelFont(fallbackSize: 13))
                .foregroundStyle(text ?? theme.foreground)
                .lineLimit(1)
                .truncationMode(.tail)
                .frame(width: textWidth, alignment: .leading)
            if !item.meta.isEmpty {
                Text(item.meta)
                    .font(theme.smallFont(fallbackSize: 12))
                    .foregroundStyle(text.map { $0.opacity(0.8) } ?? theme.muted)
                    .lineLimit(1)
                    .truncationMode(.tail)
            }
            Spacer(minLength: 0)
        }
        .padding(.horizontal, PCodeCompletionLayer.textInset)
        .frame(maxHeight: .infinity)
        .background(
            RoundedRectangle(cornerRadius: 5)
                .fill(chosen ? theme.selectedBackground.color : Color.clear))
    }
}

/// The completion list, hung over pcode's editor box from where the word
/// being completed starts; below the cursor if there is no room above.
struct PCodeCompletionLayer: View {
    @EnvironmentObject var ghostty: Ghostty.App
    @ObservedObject var completion: PCodeCompletion
    @ObservedObject private var typography = PCodeTypography.shared

    static let maxRows = 10
    static let margin: CGFloat = 4
    static let rowPadding: CGFloat = 3
    static let listPadding: CGFloat = 4
    static let textInset: CGFloat = 8
    static let cornerRadius: CGFloat = 8
    /// Terminal columns a row's text and meta may take before they are cut.
    static let maxTextColumns = 48
    static let maxMetaColumns = 56

    /// The layer's coordinate size: the full pane unless the Ghostty inspector
    /// splits the surface view horizontally inside `InspectableSurface`.
    static func layerSize(_ geometry: CGSize, surface: Ghostty.SurfaceView?) -> CGSize {
        guard let surface, surface.inspectorVisible,
              surface.bounds.width > 0, surface.bounds.height > 0
        else { return geometry }
        return CGSize(
            width: min(geometry.width, surface.bounds.width),
            height: min(geometry.height, surface.bounds.height))
    }

    var body: some View {
        GeometryReader { geometry in
            let size = Self.layerSize(geometry.size, surface: completion.surfaceView)
            if let menu = completion.menu, !menu.items.isEmpty, let cursor = completion.cursorCell(in: size) {
                let fonts = PCodePickFonts(typography: typography.snapshot)
                let theme = completion.theme(config: ghostty.config, fonts: fonts)
                let frame = Self.frame(menu, cursor: cursor, fonts: fonts, in: size)
                list(menu, theme: theme, rowHeight: Self.rowHeight(cursor: cursor))
                    .frame(width: frame.width, height: frame.height)
                    .background(ghostty.config.backgroundColor)
                    .clipShape(RoundedRectangle(cornerRadius: Self.cornerRadius))
                    .overlay(
                        RoundedRectangle(cornerRadius: Self.cornerRadius)
                            .strokeBorder(ghostty.config.splitDividerColor, lineWidth: 1))
                    .shadow(color: .black.opacity(0.35), radius: 10, x: 0, y: 4)
                    .simultaneousGesture(TapGesture().onEnded { completion.restoreFocus() })
                    .environment(\.colorScheme, colorScheme)
                    .offset(x: frame.minX, y: frame.minY)
            }
        }
    }

    private func list(_ menu: PCodeCompletion.Menu, theme: PCodePickTheme, rowHeight: CGFloat) -> some View {
        PCodeCompletionList(
            rows: menu.items.map { ($0.text, $0.meta) },
            selected: menu.selected,
            revision: menu.seq,
            theme: theme,
            rowHeight: rowHeight,
            onAccept: { completion.accept($0) })
    }

    /// The widest of `texts` in terminal columns (characters, near enough), up to `max`.
    static func columns(_ texts: [String], max limit: Int) -> CGFloat {
        CGFloat(min(texts.map(\.count).max() ?? 0, limit))
    }

    static func rowHeight(cursor: CGRect) -> CGFloat {
        cursor.height + 2 * rowPadding
    }

    /// Where the list goes in a view of `size`: above the editor box, starting
    /// under the completed word's first cell, kept inside the view.
    static func frame(_ menu: PCodeCompletion.Menu, cursor: CGRect, fonts: PCodePickFonts?, in size: CGSize) -> CGRect {
        let advance = fonts?.advance ?? cursor.width
        let text = columns(menu.items.map(\.text), max: maxTextColumns) * advance
        let metaColumns = columns(menu.items.map(\.meta), max: maxMetaColumns)
        let meta = metaColumns > 0 ? 12 + metaColumns * advance * PCodePickFonts.smallScale : 0
        let width = min(max(text + meta + 2 * (textInset + listPadding) + 4, 160), size.width - 2 * margin)
        let wanted = CGFloat(min(menu.items.count, maxRows)) * rowHeight(cursor: cursor) + 2 * listPadding
        let left = cursor.minX + CGFloat(menu.dx) * cursor.width - textInset - listPadding
        let x = min(max(left, margin), size.width - width - margin)
        // Above the editor box's top row, else below the cursor's.
        let above = cursor.minY - CGFloat(menu.dy) * cursor.height - margin
        let below = cursor.maxY + margin
        let roomAbove = above - margin
        let roomBelow = size.height - below - margin
        if wanted <= roomAbove || roomAbove >= roomBelow {
            let height = max(0, min(wanted, roomAbove))
            return CGRect(x: x, y: above - height, width: width, height: height)
        }
        return CGRect(x: x, y: below, width: width, height: max(0, min(wanted, roomBelow)))
    }

    /// The list's own scheme: its background is the terminal's, not the system's.
    private var colorScheme: ColorScheme {
        NSColor(ghostty.config.backgroundColor).isLightColor ? .light : .dark
    }
}
