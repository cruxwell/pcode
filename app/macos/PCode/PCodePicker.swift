import AppKit
import SwiftUI

/// A pick list pcode asked us to draw natively (`pcode;pick;<token>;<layout>`).
///
/// No terminal and no bridge: we connect to pcode's popup socket for the
/// token ourselves and speak newline-delimited JSON (`pcode.popup_surface`):
///
/// - we send `pcode-pick 1\n`;
/// - pcode sends a list, one JSON object per line (`pcode.pick_ui.payload`);
/// - we send `{"shown": true}` once it is on screen, or `{"error": …}` and
///   hang up if we cannot read it (pcode then shows it itself);
/// - we send the pick, `{"id": …, "action": …, "query": …}`, and wait;
/// - pcode sends another list to show in its place, or closes the socket,
///   which closes the picker. Us closing it is pcode's cancel.
///
/// The callbacks stay in pcode: we only filter, select and answer. The picker
/// is shown, and takes the keyboard, only once the first list has arrived.
/// Everything here runs on the main queue but the socket's own thread (not
/// `@MainActor`: `PCodePopup`, which drives it, is not isolated either).
final class PCodePicker: ObservableObject {
    struct Item: Decodable {
        let id: String
        let label: String
        let detail: String
        let marker: String
        let depth: Int
        let search: [String]
        let confirm: String
    }

    struct Action: Decodable {
        let key: String
        let label: String
        let needsItem: Bool
        let confirm: String
        let delete: Bool

        enum CodingKeys: String, CodingKey {
            case key, label, confirm, delete
            case needsItem = "needs_item"
        }
    }

    struct Payload: Decodable {
        let title: String
        let header: String
        let empty: String
        let selected: String?
        let query: String
        let match: String
        let custom: String?
        let items: [Item]
        let actions: [Action]
        /// Ctrl chords that cancel, as prompt_toolkit names them (`c-l`).
        let cancelKeys: [String]?
        /// pcode's colors over the terminal's; older pcode sends none.
        let style: PCodePickStyle?
        /// The footer's key hints (`PCodeHint`); older pcode sends none.
        let hints: [PCodeHint]?

        enum CodingKeys: String, CodingKey {
            case title, header, empty, selected, query, match, custom, items, actions, style, hints
            case cancelKeys = "cancel_keys"
        }

        init(from decoder: Decoder) throws {
            let container = try decoder.container(keyedBy: CodingKeys.self)
            title = try container.decode(String.self, forKey: .title)
            header = try container.decode(String.self, forKey: .header)
            empty = try container.decode(String.self, forKey: .empty)
            selected = try container.decodeIfPresent(String.self, forKey: .selected)
            query = try container.decode(String.self, forKey: .query)
            match = try container.decode(String.self, forKey: .match)
            custom = try container.decodeIfPresent(String.self, forKey: .custom)
            items = try container.decode([Item].self, forKey: .items)
            actions = try container.decode([Action].self, forKey: .actions)
            cancelKeys = try container.decodeIfPresent([String].self, forKey: .cancelKeys)
            style = try container.decodeIfPresent(PCodePickStyle.self, forKey: .style)
            // A footer pcode got wrong costs only the footer, never the list.
            hints = try? container.decodeIfPresent([PCodeHint].self, forKey: .hints)
        }

        /// The footer: pcode's hints, else the ones an older pcode left to us.
        var footer: [PCodeHint] {
            if let hints { return Array(hints.filter { !$0.key.isEmpty }.prefix(PCodeHint.maxCount)) }
            var result = [PCodeHint(key: "↩", label: "Choose", keep: true)]
            for action in actions {
                result.append(PCodeHint(key: PCodePicker.keyLabel(action.key), label: action.label, keep: false))
                if action.delete { result.append(PCodeHint(key: "⌦", label: action.label, keep: false)) }
            }
            result.append(PCodeHint(key: "esc", label: "Cancel", keep: true))
            return result
        }
    }

    /// A row on show: an item, its tree guide, or the query as a custom row.
    struct Row {
        let id: String
        let label: String
        let detail: String
        let marker: String
        let guide: String
        let confirm: String
    }

    static let hello = Data("pcode-pick 1\n".utf8)
    static let customMarker = "custom"
    /// The longest list line we read.
    static let lineLimit = 16 << 20
    /// Lists this long refilter once typing pauses (`filterDelay`), not on every key.
    static let instantFilterLimit = 500
    static let filterDelay: TimeInterval = 0.06

    @Published private(set) var payload: Payload?
    @Published var query = "" {
        didSet { if query != oldValue { queryChanged() } }
    }
    @Published private(set) var rows: [Row] = []
    @Published private(set) var selected = 0
    @Published private(set) var status = ""
    /// A pick was sent; keys wait for pcode's next list or its close.
    @Published private(set) var busy = false

    /// The list's title, for the popup's header; called on each list.
    var onList: ((String) -> Void)?
    /// pcode closed the socket or it failed: the popup closes.
    var onClosed: (() -> Void)?

    /// The terminal columns the widest shown row's guide and label take, when
    /// some row has a detail to line up after them; nil when none has.
    @Published private(set) var labelColumns: Int?

    weak var field: PCodePickField?
    /// The theme last worked out, kept until the config, fonts or style change.
    private var themeCache: (config: Ghostty.Config, fonts: PCodePickFonts?, style: PCodePickStyle?,
                             theme: PCodePickTheme)?
    private var guides: [String] = []
    private var custom: NSRegularExpression?
    private var armed: (key: String, id: String)?
    private var connection: Connection?
    /// A list the app built itself (`showLocal`): the pick goes here, not to pcode.
    private var localPick: ((_ id: String?, _ action: String?) -> Void)?
    private var keyMonitor: Any?
    private var pendingFilter: DispatchWorkItem?

    // MARK: The connection

    /// Connect to `token`'s socket and wait for pcode's list.
    func start(token: String) {
        let connection = Connection()
        self.connection = connection
        connection.start(
            path: PCode.statePath("popups/\(token).sock"),
            onLine: { [weak self] line in self?.receive(line) },
            onClosed: { [weak self] in self?.connectionClosed() })
    }

    /// Show a list the app built (the composer's `@` file picker), with no pcode behind
    /// it: `onPick` gets the chosen row's id and Cmd+key action, then the picker closes.
    /// `list` is the same JSON object `pcode.pick_ui.payload` sends.
    func showLocal(_ list: [String: Any], onPick: @escaping (_ id: String?, _ action: String?) -> Void) {
        guard let data = try? JSONSerialization.data(withJSONObject: list),
              let payload = try? JSONDecoder().decode(Payload.self, from: data) else { return }
        localPick = onPick
        show(payload)
    }

    /// Close the connection and stop taking keys; pcode takes it as a cancel.
    func stop() {
        connection?.close()
        connection = nil
        localPick = nil
        pendingFilter?.cancel()
        pendingFilter = nil
        if let keyMonitor { NSEvent.removeMonitor(keyMonitor) }
        keyMonitor = nil
    }

    private func connectionClosed() {
        guard connection != nil else { return }
        stop()
        onClosed?()
    }

    private func receive(_ line: Data) {
        guard let connection else { return }
        let list: Payload
        do {
            list = try JSONDecoder().decode(Payload.self, from: line)
        } catch {
            // Not a list we can show (a newer or older pcode): say so, so pcode
            // shows it itself, and close.
            connection.send(Self.line(["error": String(String(describing: error).prefix(300))]), thenClose: true)
            self.connection = nil
            stop()
            onClosed?()
            return
        }
        show(list)
        connection.send(Self.line(["shown": true]))
    }

    private func show(_ list: Payload) {
        let first = payload == nil
        payload = list
        guides = PCodePickMatch.treeGuides(list.items.map(\.depth))
        custom = list.custom.flatMap(PCodePickMatch.anchored)
        armed = nil
        status = ""
        busy = false
        // A new list: no row carries over, so pcode's selection or the first.
        rows = []
        if query != list.query { query = list.query }
        // Filtered now, whatever the query's change left waiting.
        pendingFilter?.cancel()
        pendingFilter = nil
        refilter()
        select(list.selected.flatMap { wanted in rows.firstIndex { $0.id == wanted } } ?? 0)
        onList?(list.title)
        if first { watchKeys() }
    }

    /// `object` as one line of JSON, if it can be.
    private static func line(_ object: [String: Any]) -> Data? {
        guard var data = try? JSONSerialization.data(withJSONObject: object) else { return nil }
        data.append(0x0A)
        return data
    }

    private func send(id: String?, action: String?) {
        if let localPick {
            stop()
            localPick(id, action)
            onClosed?()
            return
        }
        guard let connection, !busy,
              let data = Self.line(["id": id ?? NSNull(), "action": action ?? NSNull(), "query": query])
        else { return }
        busy = true
        connection.send(data)
    }

    // MARK: Filtering and selection

    private func queryChanged() {
        pendingFilter?.cancel()
        pendingFilter = nil
        guard let payload, payload.items.count >= Self.instantFilterLimit else {
            refilter()
            return
        }
        let work = DispatchWorkItem { [weak self] in
            self?.flushFilter()
        }
        pendingFilter = work
        DispatchQueue.main.asyncAfter(deadline: .now() + Self.filterDelay, execute: work)
    }

    /// Filter now if typing left a filter waiting: before acting on the rows.
    private func flushFilter() {
        guard pendingFilter != nil else { return }
        pendingFilter?.cancel()
        pendingFilter = nil
        refilter()
    }

    private func refilter() {
        guard let payload else { rows = []; return }
        let previous = rows.indices.contains(selected) ? rows[selected].id : nil
        let terms = PCodePickMatch.terms(query)
        let fuzzy = payload.match != "substring"
        // Guides only while the query is empty: matches list flat.
        let nested = terms.isEmpty
        var shown: [Row] = []
        let trimmed = query.trimmingCharacters(in: .whitespacesAndNewlines)
        if let custom, !trimmed.isEmpty, PCodePickMatch.fullMatch(custom, trimmed),
           !payload.items.contains(where: { $0.id == trimmed }) {
            shown.append(Row(id: trimmed, label: trimmed, detail: "", marker: Self.customMarker, guide: "", confirm: ""))
        }
        for (index, item) in payload.items.enumerated()
        where terms.isEmpty || PCodePickMatch.matches(item.search, terms: terms, fuzzy: fuzzy) {
            shown.append(Row(
                id: item.id, label: item.label, detail: item.detail, marker: item.marker,
                guide: nested ? guides[index] : "", confirm: item.confirm))
        }
        rows = shown
        labelColumns = shown.contains { !$0.detail.isEmpty }
            ? shown.lazy.map { $0.label.count + PCodePickGuide.columns($0.guide) }.max()
            : nil
        select(shown.firstIndex(where: { $0.id == previous }) ?? 0)
    }

    func select(_ index: Int) {
        selected = max(0, min(rows.count - 1, index))
        // An armed action stays armed only on its own row.
        if let armed, current?.id != armed.id {
            self.armed = nil
            status = ""
        }
    }

    func move(_ offset: Int) {
        guard !busy else { return }
        flushFilter()
        select(selected + offset)
    }

    var current: Row? { rows.indices.contains(selected) ? rows[selected] : nil }

    // MARK: Keys

    func accept() {
        guard !busy else { return }
        flushFilter()
        guard let current else { return }
        send(id: current.id, action: nil)
    }

    /// Escape: clear the search, else cancel. Waiting on pcode, it cancels.
    func escape() {
        if busy || query.isEmpty { cancel() } else { query = "" }
    }

    /// Cancel: close the connection, then the popup.
    func cancel() {
        guard connection != nil || localPick != nil else { return }
        stop()
        onClosed?()
    }

    /// Run `action`: once armed if it asks first, then picks.
    func run(_ action: Action, label: String) {
        guard !busy else { return }
        flushFilter()
        let row = current
        if action.needsItem && row == nil { return }
        if !action.confirm.isEmpty, let row, armed?.key != action.key || armed?.id != row.id {
            armed = (action.key, row.id)
            status = "Press \(label) again to \(row.confirm.isEmpty ? action.confirm : row.confirm)"
            return
        }
        send(id: row?.id, action: action.key)
    }

    /// The action Cmd+`key` runs, if any.
    func action(forKey key: String) -> Action? {
        payload?.actions.first { $0.key == key && PCode.valid($0.key, length: 1...1) }
    }

    /// The action Delete runs while the search is empty, if any.
    var deleteAction: Action? {
        query.isEmpty ? payload?.actions.first(where: \.delete) : nil
    }

    /// The empty list's text, with `{action:K}` as the key that runs action K.
    var emptyText: String {
        let text = payload?.empty ?? ""
        guard let pattern = try? NSRegularExpression(pattern: "\\{action:([a-z])\\}") else { return text }
        var result = text
        for match in pattern.matches(in: text, range: NSRange(text.startIndex..., in: text)).reversed() {
            guard let whole = Range(match.range, in: text), let key = Range(match.range(at: 1), in: text)
            else { continue }
            let offset = text.distance(from: text.startIndex, to: whole.lowerBound)
            let start = result.index(result.startIndex, offsetBy: offset)
            let end = result.index(start, offsetBy: text.distance(from: whole.lowerBound, to: whole.upperBound))
            result.replaceSubrange(start..<end, with: Self.keyLabel(String(text[key])))
        }
        return result
    }

    static func keyLabel(_ key: String) -> String { "⌘" + key.uppercased() }

    /// Keys the search field would otherwise take, while it has the keyboard:
    /// Cmd+key actions; Cmd+W, which closes the picker rather than pcode's
    /// terminal under it; Ctrl+U/Ctrl+D half pages; and pcode's cancel chords.
    private func watchKeys() {
        guard keyMonitor == nil else { return }
        keyMonitor = NSEvent.addLocalMonitorForEvents(matching: .keyDown) { [weak self] event in
            self?.handleKey(event) ?? event
        }
    }

    private func handleKey(_ event: NSEvent) -> NSEvent? {
        guard let field, field.hasKeyboard, event.window === field.window,
              let key = event.charactersIgnoringModifiers?.lowercased() else { return event }
        // Not `.deviceIndependentFlagsMask`, which has Caps Lock and the function key.
        let modifiers = event.modifierFlags.intersection([.command, .option, .control, .shift])
        if modifiers == .control {
            switch key {
            case "u":
                move(-PCodePickerView.pageRows / 2)
                return nil
            case "d":
                move(PCodePickerView.pageRows / 2)
                return nil
            default:
                if payload?.cancelKeys?.contains("c-\(key)") == true {
                    cancel()
                    return nil
                }
                return event
            }
        }
        guard modifiers.subtracting(.shift) == .command else { return event }
        if key == "w" {
            cancel()
            return nil
        }
        if let action = action(forKey: key) {
            run(action, label: Self.keyLabel(key))
            return nil
        }
        return event
    }

    /// Give the search field the keyboard.
    /// The theme for `config` and `fonts`, under the current list's style.
    func theme(config: Ghostty.Config, fonts: PCodePickFonts?) -> PCodePickTheme {
        let style = payload?.style
        if let cache = themeCache, cache.config === config, cache.fonts == fonts, cache.style == style {
            return cache.theme
        }
        let theme = PCodePickTheme(base: PCodePickTheme.Base(config), style: style, fonts: fonts)
        themeCache = (config, fonts, style, theme)
        return theme
    }

    func focus() {
        guard let field, let window = field.window else { return }
        window.makeFirstResponder(field)
    }
}

// MARK: - The socket

extension PCodePicker {
    /// pcode's popup socket, read on a thread of its own; every callback is on the main queue.
    final class Connection {
        private let lock = NSLock()
        private var fd: Int32 = -1
        private var closed = false
        private let writes = DispatchQueue(label: "pcode.pick.write")
        /// What we say once connected: a picker's, or a completion list's.
        private let hello: Data

        init(hello: Data = PCodePicker.hello) {
            self.hello = hello
        }

        func start(
            path: String,
            onLine: @escaping (Data) -> Void,
            onClosed: @escaping () -> Void
        ) {
            Thread.detachNewThread { [self] in
                if connect(path) {
                    readLines(onLine)
                }
                lock.lock()
                if fd >= 0 { Darwin.close(fd) }
                fd = -1
                closed = true
                lock.unlock()
                DispatchQueue.main.async(execute: onClosed)
            }
        }

        /// Hang up: a blocked reader or writer wakes, the reader closes the
        /// socket and reports it closed.
        func close() {
            lock.lock()
            closed = true
            if fd >= 0 { shutdown(fd, SHUT_RDWR) }
            lock.unlock()
        }

        /// Write `data`, off the main queue; then hang up if `thenClose`.
        func send(_ data: Data?, thenClose: Bool = false) {
            writes.async { [self] in
                // A copy of the socket, taken under the lock and written outside
                // it: `close()` never waits on a write, and its shutdown wakes one
                // that blocks. The reader closing its fd leaves this one valid.
                lock.lock()
                let copy = fd >= 0 && !closed ? dup(fd) : -1
                lock.unlock()
                if copy >= 0 {
                    if let data { Self.writeAll(copy, data) }
                    Darwin.close(copy)
                }
                if thenClose { close() }
            }
        }

        /// Connect to `path` and say hello, if it is this user's socket in this
        /// user's own directory, and this user is listening on it.
        private func connect(_ path: String) -> Bool {
            var directory = stat()
            var info = stat()
            let parent = (path as NSString).deletingLastPathComponent
            guard lstat(parent, &directory) == 0, directory.st_mode & S_IFMT == S_IFDIR,
                  directory.st_uid == getuid(), directory.st_mode & 0o077 == 0,
                  lstat(path, &info) == 0, info.st_mode & S_IFMT == S_IFSOCK, info.st_uid == getuid()
            else { return false }
            let socketFD = socket(AF_UNIX, SOCK_STREAM, 0)
            guard socketFD >= 0 else { return false }
            var address = sockaddr_un()
            address.sun_family = sa_family_t(AF_UNIX)
            let bytes = Array(path.utf8)
            let capacity = MemoryLayout.size(ofValue: address.sun_path)
            guard bytes.count < capacity else {
                Darwin.close(socketFD)
                return false
            }
            withUnsafeMutableBytes(of: &address.sun_path) { buffer in
                buffer.copyBytes(from: bytes)
                buffer[bytes.count] = 0
            }
            var noSigPipe: Int32 = 1
            setsockopt(socketFD, SOL_SOCKET, SO_NOSIGPIPE, &noSigPipe, socklen_t(MemoryLayout<Int32>.size))
            let connected = withUnsafePointer(to: &address) {
                $0.withMemoryRebound(to: sockaddr.self, capacity: 1) {
                    Darwin.connect(socketFD, $0, socklen_t(MemoryLayout<sockaddr_un>.size))
                }
            }
            var peerUID = uid_t.max
            var peerGID = gid_t.max
            let samePeer = connected == 0 && getpeereid(socketFD, &peerUID, &peerGID) == 0 && peerUID == getuid()
            lock.lock()
            defer { lock.unlock() }
            guard samePeer, !closed else {
                Darwin.close(socketFD)
                return false
            }
            fd = socketFD
            return Self.writeAll(socketFD, hello)
        }

        private func readLines(_ onLine: @escaping (Data) -> Void) {
            lock.lock()
            let socketFD = fd
            lock.unlock()
            var pending = Data()
            var chunk = [UInt8](repeating: 0, count: 65536)
            while true {
                let count = chunk.withUnsafeMutableBytes { read(socketFD, $0.baseAddress, $0.count) }
                if count < 0 && errno == EINTR { continue }
                guard count > 0 else { return }
                // Only the new bytes can hold the next newline.
                var searchFrom = pending.count
                pending.append(contentsOf: chunk[0..<count])
                var lineStart = 0
                while let newline = pending[(pending.startIndex + searchFrom)...].firstIndex(of: 0x0A) {
                    let line = Data(pending[(pending.startIndex + lineStart)..<newline])
                    DispatchQueue.main.async { onLine(line) }
                    lineStart = newline + 1 - pending.startIndex
                    searchFrom = lineStart
                }
                if lineStart > 0 { pending = Data(pending[(pending.startIndex + lineStart)...]) }
                if pending.count > PCodePicker.lineLimit { return }
            }
        }

        @discardableResult
        private static func writeAll(_ fd: Int32, _ data: Data) -> Bool {
            data.withUnsafeBytes { buffer in
                var offset = 0
                while offset < buffer.count {
                    let written = write(fd, buffer.baseAddress! + offset, buffer.count - offset)
                    if written < 0 && errno == EINTR { continue }
                    guard written > 0 else { return false }
                    offset += written
                }
                return true
            }
        }
    }
}

// MARK: - The search field

/// The picker's search line: an AppKit text field, so the list's keys reach
/// us before the field editor acts on them (SwiftUI's key handling needs macOS 14).
final class PCodePickField: NSTextField, NSTextFieldDelegate {
    weak var picker: PCodePicker?

    /// True while this field's editor has its window's keyboard.
    var hasKeyboard: Bool {
        guard let editor = window?.firstResponder as? NSTextView else { return false }
        return (editor.delegate as AnyObject?) === self
    }

    /// The theme last applied (`apply(_:)`).
    fileprivate var appliedTheme: PCodePickTheme?

    override class var cellClass: AnyClass? {
        get { PCodePickFieldCell.self }
        set { super.cellClass = newValue }
    }

    /// This field's own field editor, never the window's shared one.
    var editor: NSTextView? { (cell as? PCodePickFieldCell)?.editor }

    override func viewDidMoveToWindow() {
        super.viewDidMoveToWindow()
        // Mounted once the first list arrived: take the keyboard then.
        if window != nil { DispatchQueue.main.async { [weak self] in self?.picker?.focus() } }
    }

    func control(_ control: NSControl, textView: NSTextView, doCommandBy selector: Selector) -> Bool {
        guard let picker else { return false }
        switch selector {
        // Ctrl+P and Ctrl+N are moveUp: and moveDown: in AppKit's key bindings.
        case #selector(NSResponder.moveUp(_:)):
            picker.move(-1)
        case #selector(NSResponder.moveDown(_:)):
            picker.move(1)
        case #selector(NSResponder.pageUp(_:)), #selector(NSResponder.scrollPageUp(_:)):
            picker.move(-PCodePickerView.pageRows)
        case #selector(NSResponder.pageDown(_:)), #selector(NSResponder.scrollPageDown(_:)):
            picker.move(PCodePickerView.pageRows)
        case #selector(NSResponder.insertNewline(_:)):
            picker.accept()
        case #selector(NSResponder.cancelOperation(_:)):
            picker.escape()
        case #selector(NSResponder.insertTab(_:)), #selector(NSResponder.insertBacktab(_:)):
            break  // The search line keeps the keyboard: there is nothing else to focus.
        case #selector(NSResponder.deleteForward(_:)):
            // Only the forward-delete key: AppKit sends Ctrl+D here too.
            guard NSApp.currentEvent?.keyCode == 0x75, let action = picker.deleteAction else { return false }
            picker.run(action, label: "Delete")
        default:
            return false
        }
        return true
    }

    func controlTextDidChange(_ notification: Notification) {
        if picker?.query != stringValue { picker?.query = stringValue }
    }
}

/// The search field's cell: it edits with a field editor of its own, so the
/// colors we give the caret and selection never reach the window's other fields.
final class PCodePickFieldCell: NSTextFieldCell {
    let editor: NSTextView = {
        let editor = NSTextView()
        editor.isFieldEditor = true
        return editor
    }()

    override func fieldEditor(for controlView: NSView) -> NSTextView? { editor }
}

struct PCodePickFieldView: NSViewRepresentable {
    @ObservedObject var picker: PCodePicker
    let theme: PCodePickTheme

    func makeNSView(context: Context) -> PCodePickField {
        let field = PCodePickField()
        field.picker = picker
        field.delegate = field
        field.isBordered = false
        field.drawsBackground = false
        field.focusRingType = .none
        field.cell?.usesSingleLineMode = true
        field.cell?.lineBreakMode = .byTruncatingTail
        field.apply(theme)
        picker.field = field
        return field
    }

    func updateNSView(_ field: PCodePickField, context: Context) {
        // pcode's next list may bring a query, or a style, of its own.
        if field.stringValue != picker.query { field.stringValue = picker.query }
        field.apply(theme)
    }
}

extension PCodePickField {
    /// The terminal's font and colors; the caret follows the foreground and
    /// the selection the selected row, so both stay readable.
    func apply(_ theme: PCodePickTheme) {
        guard theme != appliedTheme else { return }
        appliedTheme = theme
        let font = theme.fonts?.regular ?? .systemFont(ofSize: 15)
        let foreground = NSColor(theme.foreground)
        self.font = font
        textColor = foreground
        editor?.insertionPointColor = foreground
        editor?.selectedTextAttributes = [
            .backgroundColor: theme.selectedBackground.nsColor,
            .foregroundColor: theme.selectedText.nsColor,
        ]
    }
}

// MARK: - Tree guides

/// A row's tree guide (`PCodePickMatch.treeGuides`) drawn as lines, which
/// run on through the row's padding into the rows above and below.
struct PCodePickGuide: Shape {
    enum Cell {
        case blank, line, tee, elbow
    }

    /// Terminal columns per guide cell.
    static let cellColumns = 2
    /// The gap between rows, which a line crosses to meet the next row's.
    static let rowGap: CGFloat = 1

    let cells: [Cell]

    init(_ guide: String) {
        cells = Self.cells(guide)
    }

    /// The cells of a guide: three characters each (`├─ `, `└─ `, `│  `, `   `).
    static func cells(_ guide: String) -> [Cell] {
        let characters = Array(guide)
        return stride(from: 0, to: characters.count, by: 3).map { start in
            switch characters[start] {
            case "├": .tee
            case "└": .elbow
            case "│": .line
            default: .blank
            }
        }
    }

    static func columns(_ guide: String) -> Int {
        (guide.count + 2) / 3 * cellColumns
    }

    func path(in rect: CGRect) -> Path {
        var path = Path()
        guard !cells.isEmpty else { return path }
        let width = rect.width / CGFloat(cells.count)
        for (index, cell) in cells.enumerated() where cell != .blank {
            let x = (rect.minX + width * (CGFloat(index) + 0.5)).rounded(.down) + 0.5
            let bottom = cell == .elbow ? rect.midY : rect.maxY + Self.rowGap
            path.move(to: CGPoint(x: x, y: rect.minY))
            path.addLine(to: CGPoint(x: x, y: bottom))
            if cell != .line {
                path.move(to: CGPoint(x: x, y: rect.midY))
                path.addLine(to: CGPoint(x: rect.minX + width * CGFloat(index + 1) - 2, y: rect.midY))
            }
        }
        return path
    }
}

// MARK: - The view

/// The picker in the popup's panel: a search line over the rows, a status
/// line for an action that asks first, and the keys it answers.
struct PCodePickerView: View {
    @ObservedObject var picker: PCodePicker
    @ObservedObject private var typography = PCodeTypography.shared
    /// The terminal's config, for its colors.
    let config: Ghostty.Config
    /// Optional font override (defaults to live `PCodeTypography.shared`).
    let fonts: PCodePickFonts?

    /// Rows PgUp/PgDn move by.
    static let pageRows = 10
    /// The widest label column, in terminal columns, that details line up after.
    static let maxLabelColumns = 40
    /// A guide cell's width without the terminal font.
    static let fallbackGuideCell: CGFloat = 14
    static let rowPadding: CGFloat = 5

    var body: some View {
        let liveFonts = PCodePickFonts(typography: typography.snapshot)
        let theme = picker.theme(config: config, fonts: liveFonts)
        VStack(alignment: .leading, spacing: 0) {
            if let header = picker.payload?.header, !header.isEmpty {
                Text(header)
                    .font(theme.smallFont(fallbackSize: 12))
                    .foregroundStyle(theme.muted)
                    .lineLimit(1)
                    .padding(.horizontal, 12)
                    .padding(.bottom, 6)
            }
            HStack(spacing: 8) {
                Image(systemName: "magnifyingglass").foregroundStyle(theme.muted)
                ZStack(alignment: .leading) {
                    // Drawn in SwiftUI rather than NSTextField's placeholder so it
                    // centers with the icon and leaves a space after the caret.
                    if picker.query.isEmpty {
                        Text("Search")
                            .font(theme.labelFont(fallbackSize: 15))
                            .foregroundStyle(theme.muted)
                            .padding(.leading, 2 + (theme.fonts?.advance ?? 8))
                            .allowsHitTesting(false)
                    }
                    PCodePickFieldView(picker: picker, theme: theme)
                        .fixedSize(horizontal: false, vertical: true)
                }
                .frame(maxWidth: .infinity, alignment: .leading)
            }
            .padding(.horizontal, 12)
            .padding(.vertical, 8)
            .background(RoundedRectangle(cornerRadius: 6).fill(Color.primary.opacity(0.06)))
            .padding(.horizontal, 8)
            list(theme)
            if !picker.status.isEmpty {
                Text(picker.status)
                    .font(theme.smallFont(fallbackSize: 12))
                    .foregroundStyle(theme.status)
                    .lineLimit(2)
                    .padding(.horizontal, 12)
                    .padding(.top, 6)
            }
            footer(theme)
        }
        .padding(.vertical, 8)
        .opacity(picker.busy ? 0.6 : 1)
    }

    @ViewBuilder
    private func list(_ theme: PCodePickTheme) -> some View {
        if picker.rows.isEmpty {
            Text(picker.emptyText)
                .font(theme.labelFont(fallbackSize: 13))
                .foregroundStyle(theme.muted)
                .frame(maxWidth: .infinity, maxHeight: .infinity)
        } else {
            // Details line up after the widest label, in the terminal's font.
            let column = theme.fonts.flatMap { fonts in
                picker.labelColumns.map { CGFloat(min($0, Self.maxLabelColumns)) * fonts.advance }
            }
            ScrollViewReader { proxy in
                ScrollView {
                    LazyVStack(alignment: .leading, spacing: PCodePickGuide.rowGap) {
                        ForEach(Array(picker.rows.enumerated()), id: \.offset) { index, row in
                            rowView(row, chosen: index == picker.selected, theme: theme, column: column)
                                .id(index)
                                .contentShape(Rectangle())
                                .onTapGesture(count: 2) {
                                    picker.select(index)
                                    picker.accept()
                                }
                                .onTapGesture { picker.select(index) }
                        }
                    }
                    .padding(.horizontal, 8)
                    .padding(.vertical, 6)
                }
                .onChange(of: picker.selected) { index in proxy.scrollTo(index) }
            }
        }
    }

    private func rowView(
        _ row: PCodePicker.Row, chosen: Bool, theme: PCodePickTheme, column: CGFloat?
    ) -> some View {
        // The selected row's text is one readable color throughout.
        let text = chosen ? theme.selectedText.color : nil
        let cell = theme.fonts.map { $0.advance * CGFloat(PCodePickGuide.cellColumns) } ?? Self.fallbackGuideCell
        let guide = PCodePickGuide(row.guide)
        let guideWidth = cell * CGFloat(guide.cells.count)
        return HStack(spacing: 0) {
            if !guide.cells.isEmpty {
                // Outside the row's vertical padding, so its lines meet the next row's.
                guide
                    .stroke(text ?? theme.muted, lineWidth: 1)
                    .frame(width: guideWidth)
            }
            HStack(spacing: 8) {
                Text(row.label)
                    .font(theme.labelFont(fallbackSize: 13))
                    .foregroundStyle(text ?? theme.foreground)
                    .lineLimit(1)
                    .truncationMode(.tail)
                    .frame(width: column.map { max(0, $0 - guideWidth) }, alignment: .leading)
                    .layoutPriority(1)
                if !row.detail.isEmpty {
                    Text(row.detail)
                        .font(theme.smallFont(fallbackSize: 12))
                        .foregroundStyle(text.map { $0.opacity(0.8) } ?? theme.muted)
                        .lineLimit(1)
                        .truncationMode(.middle)
                }
                Spacer(minLength: 8)
                if !row.marker.isEmpty {
                    Text(row.marker)
                        .font(theme.smallFont(fallbackSize: 11, weight: .medium))
                        .padding(.horizontal, 6)
                        .padding(.vertical, 1)
                        .background(Capsule().fill((text ?? theme.badge).opacity(0.15)))
                        .foregroundStyle(text ?? theme.badge)
                }
            }
            .padding(.vertical, Self.rowPadding)
        }
        .padding(.horizontal, 8)
        .background(
            RoundedRectangle(cornerRadius: 5)
                .fill(chosen ? theme.selectedBackground.color : Color.clear))
    }

    private func footer(_ theme: PCodePickTheme) -> some View {
        PCodeHintBar(hints: picker.payload?.footer ?? [], fonts: theme.fonts, muted: theme.muted)
    }
}
