import Foundation

/// The native composer's completion menu: `/` commands and their arguments from the
/// command catalog (`PCodeCommandCatalog`, cached so it works before the host is up),
/// `$model`/`+effort` words, and `@` file references from the workspace index
/// (`PCodeFileIndex`, ranked off the main thread).
///
/// It follows the terminal editor's menu (prompt_toolkit's, with
/// `complete_while_typing`): the menu opens as you type with nothing selected,
/// Up/Down (and Ctrl+P/Ctrl+N) select, Tab and Ctrl+L take the selection or else
/// the first row, Enter takes a selected row and otherwise sends, and Escape closes
/// it until the text changes. Unlike prompt_toolkit, moving through the rows does
/// not insert each one as it goes: the text changes only when a row is taken.
final class PCodeComposerCompletion: ObservableObject {
    struct Row: Equatable {
        /// What the menu shows.
        let display: String
        let meta: String
        /// The UTF-16 range of the text it replaces.
        let range: NSRange
        let insertion: String
    }

    @Published private(set) var rows: [Row] = []
    @Published private(set) var selected: Int?
    /// Bumped whenever the rows change, so the list scrolls back to its top.
    @Published private(set) var revision = 0

    var catalog: PCodeCommandCatalog
    let files: PCodeFileIndex?
    /// Bumped per query so a slow file ranking never lands over newer text.
    private var generation = 0
    /// Escape closed the menu for this text; it reopens once the text changes.
    private var dismissed: String?
    private var last: (text: String, caret: Int)?
    private var filesObserver: NSObjectProtocol?

    init(catalog: PCodeCommandCatalog, files: PCodeFileIndex?) {
        self.catalog = catalog
        self.files = files
        guard let files else { return }
        // A notification, not `files.onUpdate`: the index is shared by every pane in
        // this folder, and one callback would leave all but the newest menu stale.
        filesObserver = NotificationCenter.default.addObserver(
            forName: PCodeFileIndex.didUpdate, object: files, queue: .main
        ) { [weak self] _ in
            // A refresh changed the listing: an open `@` menu ranks again.
            guard let self, let last = self.last, !self.rows.isEmpty else { return }
            self.update(text: last.text, caret: last.caret)
        }
    }

    deinit {
        if let filesObserver { NotificationCenter.default.removeObserver(filesObserver) }
    }

    var isOpen: Bool { !rows.isEmpty }

    /// The text or caret changed: work out what the menu offers now.
    func update(text: String, caret: Int) {
        last = (text, caret)
        generation += 1
        if let dismissed {
            if dismissed == text { return show([]) }
            self.dismissed = nil
        }
        if let token = PCodeFileIndex.activeToken(in: text, caret: caret) {
            guard let files else { return show([]) }
            if rows.isEmpty {
                // The menu is opening: a file made a moment ago should be in it. The
                // refresh lands in the background, and `onUpdate` ranks again.
                files.refreshIfStale(maxAge: Self.openRefreshAge)
            }
            let wanted = generation
            files.matches(token.fragment) { [weak self] items in
                guard let self, self.generation == wanted else { return }
                self.show(items.map {
                    Row(display: $0.path, meta: "", range: token.range, insertion: $0.insertion)
                })
            }
            return
        }
        show(Self.commandRows(catalog, text: text, caret: caret))
    }

    /// The `/` and `$model` rows for `text` with the caret at `caret` (a UTF-16 offset).
    static func commandRows(_ catalog: PCodeCommandCatalog, text: String, caret: Int) -> [Row] {
        let string = text as NSString
        guard caret >= 0, caret <= string.length else { return [] }
        let before = string.substring(to: caret)
        let after = string.substring(from: caret)
        return catalog.completions(before: before, after: after).map {
            Row(display: $0.display, meta: $0.meta,
                range: NSRange(location: caret - $0.replaceLength, length: $0.replaceLength),
                insertion: $0.text)
        }
    }

    private func show(_ next: [Row]) {
        if next != rows {
            rows = next
            selected = nil
            revision &+= 1
        }
    }

    /// Up (-1) or Down (+1): select the next row, wrapping as prompt_toolkit does.
    func move(_ delta: Int) {
        guard !rows.isEmpty else { return }
        guard let current = selected else {
            selected = delta > 0 ? 0 : rows.count - 1
            return
        }
        selected = (current + delta + rows.count) % rows.count
    }

    /// How old the file listing may be when an `@` menu opens.
    static let openRefreshAge: TimeInterval = 2

    /// Tab with the menu closed: open it for the text around the caret, even if Escape
    /// closed it (prompt_toolkit's Tab starts completion; it never types a tab).
    func reopen(text: String, caret: Int) {
        dismissed = nil
        update(text: text, caret: caret)
    }

    /// Escape: close until the text changes.
    func dismiss() {
        dismissed = last?.text
        show([])
    }

    func close() { show([]) }

    /// The row Tab or Ctrl+L takes: the selected one, else the first.
    var offered: Row? { selected.map { rows[$0] } ?? rows.first }

    /// The row Enter takes: only a selected one (otherwise Enter sends).
    var chosen: Row? { selected.map { rows[$0] } }

    func row(_ index: Int) -> Row? { rows.indices.contains(index) ? rows[index] : nil }

    /// `text` with `row` put in, and where the caret goes.
    static func apply(_ row: Row, to text: String) -> (text: String, caret: Int) {
        let string = text as NSString
        guard row.range.location >= 0, NSMaxRange(row.range) <= string.length else {
            return (text, string.length)
        }
        let replaced = string.replacingCharacters(in: row.range, with: row.insertion)
        return (replaced, row.range.location + (row.insertion as NSString).length)
    }
}
