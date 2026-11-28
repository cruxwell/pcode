import Foundation

/// The `/btw` drawer for a native-composer pane, which has no terminal to send it
/// `pcode;btw;...` APCs: the composer opens it on `read_asides` (bare `/btw`) and when
/// an answer arrives, and while it shows a thread Enter answers that thread, as
/// `PreviewApp.btw_submit` does in the terminal editor.
extension PCodeComposerSession {
    /// The side questions oldest first, as the host keeps them.
    var asideList: [[String: Any]] { asideOrder.compactMap { asides[$0] } }

    /// `Asides.latest`: the newest unread answer, else the newest question.
    static func latestThread(_ asides: [[String: Any]]) -> String? {
        let unread = asides.filter { ($0["status"] as? String) != "running" && ($0["read"] as? Bool) != true }
        return ((unread.last ?? asides.last)?["thread"] as? String).flatMap { $0.isEmpty ? nil : $0 }
    }

    /// Thread names in the order they were started.
    static func threads(_ asides: [[String: Any]]) -> [String] {
        var seen: Set<String> = []
        return asides.compactMap { $0["thread"] as? String }.filter { !$0.isEmpty && seen.insert($0).inserted }
    }

    /// `read_asides`: show the newest thread (Ctrl+\ steps to the others).
    func openBtw(thread wanted: String? = nil) {
        guard let host = hostID, let thread = wanted ?? Self.latestThread(asideList) else {
            return showToast("No side questions yet: ask one with /btw QUESTION.")
        }
        btwDrawer.show(host: host, thread: thread)
        markThreadRead(thread)
    }

    func closeBtw() {
        if btwDrawer.thread != nil { btwDrawer.close() }
    }

    /// Ctrl+\: the next thread, wrapping. False when no drawer is open.
    func nextBtwThread() -> Bool {
        guard btwDrawer.isOpen, let current = btwDrawer.thread?.thread else { return false }
        let all = Self.threads(asideList)
        guard all.count > 1 else { return true }
        let next = all[((all.firstIndex(of: current) ?? -1) + 1) % all.count]
        openBtw(thread: next)
        return true
    }

    /// A side question changed: keep the shown thread marked read, and open the drawer
    /// for a fresh answer when nothing is being typed (it would take the next Enter).
    func asideChanged(_ aside: [String: Any], answered: Bool) {
        guard let thread = aside["thread"] as? String else { return }
        if btwDrawer.isOpen, btwDrawer.thread?.thread == thread {
            markThreadRead(thread)
        } else if answered, !btwDrawer.isOpen, textView?.string.isEmpty ?? true {
            openBtw(thread: thread)
        }
    }

    /// The host learns the shown answers were read, so nothing shows them as ready.
    private func markThreadRead(_ thread: String) {
        let ids = asideList.filter {
            $0["thread"] as? String == thread && ($0["status"] as? String) != "running" && ($0["read"] as? Bool) != true
        }.compactMap { $0["id"] as? String }
        guard !ids.isEmpty else { return }
        for id in ids { asides[id]?["read"] = true }
        sendIntent("asides_read", [ids])
        refreshAsideCounts()
    }

    /// What Enter does with `text` while a thread is open (`btw_submit`): nil leaves it to
    /// the conversation, true takes it, false keeps the draft (refused with a toast).
    func btwSubmit(_ text: String) -> Bool? {
        guard btwDrawer.isOpen, let thread = btwDrawer.thread?.thread else { return nil }
        let parts = text.split(maxSplits: 1, whereSeparator: \.isWhitespace)
        guard let first = parts.first.map(String.init) else { return nil }
        let argument = parts.count > 1 ? String(parts[1]).trimmingCharacters(in: .whitespaces) : ""
        if Self.isShellCommand(text) {
            closeBtw()  // A `!command` runs in the shell, as ever.
            return nil
        }
        // `/etc/hosts …` is a path, so a question.
        if !text.hasPrefix("/") || first.dropFirst().contains("/") {
            askHost("follow_up_aside", [thread, text], restoring: text)
            return true
        }
        switch first {
        case "/close":
            closeBtw()
            return true
        case "/stop":
            sendIntent("cancel_asides", [])
            return true
        case "/merge", "/summarize":
            let action = first == "/merge" ? "merge" : "summary"
            askHost("keep_thread", [thread, action, argument], restoring: text) { [weak self] in
                if self?.btwDrawer.thread?.thread == thread { self?.closeBtw() }
            }
            return true
        case "/copy", "/links", "/btw", "/side":
            return nil
        default:
            // An unknown command keeps the thread and the draft, as in the terminal.
            if completion.catalog.find(first) == nil {
                showToast("Unknown command: \(first)")
                return false
            }
            closeBtw()
            return nil
        }
    }

    /// A host `query` for the thread; on failure the reason shows and `text` comes back
    /// into an empty composer, as the terminal keeps the draft of a refused reply.
    private func askHost(_ name: String, _ args: [Any], restoring text: String, then done: (() -> Void)? = nil) {
        request("query", [name] + args) { [weak self] _, error in
            guard let self else { return }
            guard let error else {
                done?()
                return
            }
            self.showToast(error, seconds: 5)
            if let view = self.textView, view.string.isEmpty { self.replaceText(with: text) }
        }
    }
}
