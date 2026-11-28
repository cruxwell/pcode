import AppKit
import Foundation

/// Everything the session host asks of the native composer, in one place.
///
/// The host hands a terminal-side command back with `run_command` (anything in
/// `TERMINAL_COMMANDS`, `pcode.controller`), and asks its latest client for popups:
/// `choose_model`, `read_secret`, `browse_jobs`, `read_asides`. Every request gets a
/// reply, or the host's command would wait on it forever.
///
/// Phase 1 answers what it can and says plainly what isn't native yet. Later phases
/// route the commands with native views here (`/tools`, `/diffs`, `/agents`, `/tree`,
/// `/resume`, `/config`, the model picker...).
///
/// Which commands are the session's and which the composer's is the command catalog's
/// call (`PCodeCommandCatalog.handling(for:)`); this runs the composer's.
enum PCodeHostRequests {
    static func handle(
        id: Int, method: String, args: [Any], kwargs: [String: Any],
        session: PCodeComposerSession, client: PCodeHostRPCClient
    ) {
        switch method {
        case "run_command":
            let text = (args.first as? String) ?? (kwargs["text"] as? String) ?? ""
            runTerminalCommand(text, session: session)
            client.reply(id: id, result: nil)
        case "choose_model":
            // Nil is "cancelled", as when a terminal's picker is closed.
            session.showToast("The model picker isn't in the native composer yet: type /model NAME.")
            client.reply(id: id, result: nil)
        case "read_secret":
            let title = (args.first as? String) ?? "pcode"
            let prompt = (args.count > 1 ? args[1] as? String : nil) ?? ""
            let detail = (args.count > 2 ? args[2] as? String : nil) ?? ""
            readSecret(title: title, prompt: prompt, detail: detail, session: session) { secret in
                client.reply(id: id, result: secret)
            }
        case "browse_jobs", "read_asides":
            session.showToast(method == "browse_jobs"
                ? "/jobs isn't in the native composer yet."
                : "The /btw viewer isn't in the native composer yet.")
            client.reply(id: id, result: nil)
        default:
            client.replyError(id: id, type: "PermissionError", message: "\(method) is not supported by Pcode.app's composer.")
        }
    }

    /// A terminal command typed in the composer, or handed back by the host.
    static func runTerminalCommand(_ text: String, session: PCodeComposerSession) {
        let name = text.trimmingCharacters(in: .whitespacesAndNewlines)
            .split(maxSplits: 1, whereSeparator: \.isWhitespace).first.map(String.init) ?? text
        switch name {
        case "/quit", "/exit":
            session.onCloseRequested?()
        case "/redraw":
            break  // Nothing native to repaint.
        case "/paste-image":
            // Unlike ⌘V, an explicit ask: the image wins even beside text.
            if !session.pasteImage(from: .general) { session.showToast("The clipboard holds no image.") }
        default:
            session.showToast("\(name) isn't in the native composer yet.")
        }
    }

    /// The host needs a secret (an API key, a sign-in code): ask for it in a sheet on the
    /// workspace window, naming the pane it is for (it may not be the one shown). Closing
    /// the pane cancels it (`PCodeComposerSession.stop`), which answers nil.
    static func readSecret(
        title: String, prompt: String, detail: String, session: PCodeComposerSession,
        completion: @escaping (String?) -> Void
    ) {
        guard session.secretAlert == nil,
              let window = session.textView?.window ?? PCodeWorkspaceController.existing?.window
        else {
            // One prompt at a time, and never an app-modal one with no window to own it.
            completion(nil)
            return
        }
        let alert = NSAlert()
        alert.messageText = title
        let pane = "For \(session.paneLabel)."
        alert.informativeText = [pane, prompt, detail].filter { !$0.isEmpty }.joined(separator: "\n\n")
        let field = NSSecureTextField(frame: NSRect(x: 0, y: 0, width: 320, height: 24))
        alert.accessoryView = field
        alert.addButton(withTitle: "OK")
        alert.addButton(withTitle: "Cancel")
        alert.window.initialFirstResponder = field
        session.secretAlert = alert
        alert.beginSheetModal(for: window) { [weak session] response in
            session?.secretAlert = nil
            completion(response == .alertFirstButtonReturn ? field.stringValue : nil)
        }
    }
}
