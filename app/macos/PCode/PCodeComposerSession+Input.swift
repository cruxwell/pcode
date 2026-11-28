import AppKit
import Foundation

/// The composer's editing helpers: history recall and drafts (`PCodePromptHistory`),
/// image paste and drop (`PCodePastedImage`), the `/` and `@` menu
/// (`PCodeComposerCompletion`), and the `@` file picker (`PCodePicker`).
extension PCodeComposerSession {
    /// Hook the text view up once, when it is made.
    func wireInput(_ textView: PCodeComposerTextView) {
        textView.isMenuOpen = { [weak self] in self?.completion.isOpen ?? false }
        textView.onTextChange = { [weak self] text, caret in
            guard let self, !self.settingText else { return }
            self.completion.update(text: text, caret: caret.location)
        }
        textView.onCaretMove = { [weak self] in
            // As prompt_toolkit: moving the caret re-reads what is around it.
            guard let self, !self.settingText, self.completion.isOpen else { return }
            self.completion.update(text: textView.string, caret: textView.selectedRange().location)
        }
        textView.onPasteImage = { [weak self] board in self?.pasteImage(from: board) ?? false }
        startDraftTimer()
        // Quitting closes no pane, so without this the last `interval` of typing is lost.
        terminateObserver = NotificationCenter.default.addObserver(
            forName: NSApplication.willTerminateNotification, object: nil, queue: .main
        ) { [weak self] _ in self?.saveDrafts() }
    }

    // MARK: History and drafts

    /// Up (-1) or Down (+1) at the composer's edge: the previous or next entry, keeping
    /// what was typed. False at either end, so the caret just moves.
    func recall(_ delta: Int) -> Bool {
        guard let textView else { return false }
        let current = textView.string
        guard let text = delta < 0 ? history.up(current: current) : history.down(current: current) else {
            return false
        }
        replaceText(with: text)
        return true
    }

    /// Replace the whole draft, caret at the end, without opening the completion menu.
    func replaceText(with text: String) {
        guard let textView else { return }
        settingText = true
        textView.string = text
        textView.setSelectedRange(NSRange(location: (text as NSString).length, length: 0))
        textView.didChangeText()
        settingText = false
        completion.close()
        editor.reportHeight()
    }

    /// Write unsent text to draft files (`drafts.py`'s format and directory), if it
    /// changed since the last save: every `PCodeDraftStore.interval`, and when the pane
    /// stops showing the composer.
    func saveDrafts() {
        guard let textView else { return }
        let text = textView.string
        guard text != lastDraftText else { return }
        lastDraftText = text
        history.saveDrafts(current: text)
    }

    private func startDraftTimer() {
        draftTimer?.invalidate()
        let timer = Timer(timeInterval: PCodeDraftStore.interval, repeats: true) { [weak self] _ in
            self?.saveDrafts()
        }
        timer.tolerance = 1
        RunLoop.main.add(timer, forMode: .common)
        draftTimer = timer
    }

    // MARK: Pasting images

    /// ⌘V with an image (and no text) on the pasteboard, or a dropped file or image:
    /// save it and name it at the caret. False when the pasteboard holds neither.
    func pasteImage(from board: NSPasteboard) -> Bool {
        guard PCodePastedImage.canPaste(board) else { return false }
        PCodePastedImage.paste(from: board) { [weak self] pasted in
            DispatchQueue.main.async { self?.insertPasted(pasted) }
        }
        return true
    }

    func insertPasted(_ pasted: [PCodePastedImage.Pasted]) {
        guard let textView else { return }
        if pasted.isEmpty {
            showToast("The clipboard holds no image.")
            return
        }
        for item in pasted {
            if let why = item.unsendable {
                // As pcode: warn and insert nothing, rather than a path the model can't see.
                showToast(why, seconds: 5)
                continue
            }
            let caret = textView.selectedRange()
            let before = (textView.string as NSString).substring(to: caret.location)
            textView.insertText(PCodePastedImage.insertion(item.reference, before: before), replacementRange: caret)
        }
        focus()
    }

    // MARK: Completion

    /// A session's `session_state` arrived: save its catalog for the next first frame,
    /// and complete from it live from now on.
    func catalogChanged(_ session: [String: Any]) {
        guard let snapshot = PCodeCommandCatalog.Snapshot(sessionState: session) else { return }
        if snapshot != completion.catalog.snapshot {
            completion.catalog = PCodeCommandCatalog(snapshot: snapshot)
            let store = catalogStore
            let workspace = (session["workspace"] as? String).flatMap { $0.isEmpty ? nil : $0 } ?? directory
            DispatchQueue.global(qos: .utility).async { store.save(snapshot, workspace: workspace) }
        }
    }

    /// Put a menu row into the draft.
    func take(_ row: PCodeComposerCompletion.Row) {
        guard let textView else { return }
        let range = row.range
        guard range.location >= 0, NSMaxRange(range) <= (textView.string as NSString).length else {
            completion.close()
            return
        }
        // Through `insertText` so undo and the change notification work as for typing.
        textView.insertText(row.insertion, replacementRange: range)
        completion.close()
    }

    /// A row was clicked.
    func takeRow(_ index: Int) {
        if let row = completion.row(index) { take(row) }
        focus()
    }

    /// Where a menu row's replaced text starts, in the text view's coordinates.
    func menuAnchorX() -> CGFloat? {
        guard let row = completion.rows.first, let textView else { return nil }
        return textView.characterOrigin(at: row.range.location)?.x
    }

    // MARK: The @ file picker

    /// The workspace's files in a searchable list; the pick is named at the caret
    /// (replacing an `@fragment` being typed there).
    func openFilePicker() {
        guard filePicker == nil, let files = completion.files else { return }
        completion.close()
        let paths = files.paths
        let picker = PCodePicker()
        picker.onClosed = { [weak self, weak picker] in
            guard let self, self.filePicker === picker else { return }
            self.filePicker = nil
            self.focus()
        }
        picker.showLocal(Self.filePickerList(paths, loaded: files.isLoaded)) { [weak self] id, _ in
            guard let self, let id else { return }
            self.insertReference(PCodeFileIndex.Item(path: id))
        }
        filePicker = picker
    }

    /// The picker's list, in `pcode.pick_ui.payload`'s shape.
    static func filePickerList(_ paths: [String], loaded: Bool) -> [String: Any] {
        [
            "title": "Files",
            "header": "",
            "empty": loaded ? "No files match." : "Listing the workspace…",
            "selected": NSNull(),
            "query": "",
            "match": "fuzzy",
            "custom": NSNull(),
            "items": paths.map { path -> [String: Any] in
                let name = (path as NSString).lastPathComponent
                return ["id": path, "label": path, "detail": "", "marker": "", "depth": 0,
                        // Casefolded, as `PickItem.texts` sends them: the matcher folds only the query.
                        "search": [PCodePickMatch.casefold(name), PCodePickMatch.casefold(path)], "confirm": ""]
            },
            "actions": [[String: Any]](),
            "hints": [
                ["key": "↩", "label": "Insert reference", "keep": true],
                ["key": "esc", "label": "Cancel", "keep": true],
            ],
        ]
    }

    func insertReference(_ item: PCodeFileIndex.Item) {
        guard let textView else { return }
        let text = textView.string
        let caret = textView.selectedRange()
        if caret.length == 0, let token = PCodeFileIndex.activeToken(in: text, caret: caret.location) {
            textView.insertText(item.insertion, replacementRange: token.range)
        } else {
            let before = (text as NSString).substring(to: caret.location)
            let space = before.isEmpty || before.last!.isWhitespace ? "" : " "
            textView.insertText(space + item.insertion, replacementRange: caret)
        }
        completion.close()
    }
}
