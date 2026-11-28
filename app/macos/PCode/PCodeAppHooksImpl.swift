import AppKit
import SwiftUI

/// pcode's side of the fork's `PCodeAppHooks`: every upstream file reaches
/// pcode's code through here. The fork finds this class by its Objective-C
/// name, so the fork builds without it and pcode's code can live elsewhere.
@objc(PCodeAppHooksImpl)
final class PCodeAppHooksImpl: NSObject, PCodeAppHooks {
    // MARK: App

    var appDisplayName: String { "Pcode" }

    var replacesTerminalWindows: Bool { PCodeWorkspaceMode.enabled }

    func configureMainMenu(_ menu: NSMenu) {
        PCodeMenu.configureMainMenu(menu)
    }

    func configureDockMenu(_ menu: NSMenu) {
        PCodeMenu.configureDockMenu(menu)
    }

    func configureSurfaceContextMenu(_ menu: NSMenu) {
        PCodeMenu.configureSurfaceContextMenu(menu)
    }

    func showAbout() -> Bool {
        MainActor.assumeIsolated { PCodeAboutController.shared.show() }
        return true
    }

    func showSettings() -> Bool {
        if Thread.isMainThread {
            MainActor.assumeIsolated { PCodeSettingsController.shared.show() }
        } else {
            DispatchQueue.main.async {
                PCodeSettingsController.shared.show()
            }
        }
        return true
    }

    func showHelp() -> Bool {
        if let url = URL(string: "https://aweis89.github.io/pcode/") {
            _ = NSWorkspace.shared.open(url)
        }
        return true
    }

    var configOverrideFilePath: String? {
        guard replacesTerminalWindows else { return nil }
        return PCodeTypography.shared.syncGhosttyOverrideFile()
    }

    func showWorkspace(_ ghostty: Ghostty.App) {
        PCodeWorkspaceController.show(ghostty)
    }

    func newSession(_ ghostty: Ghostty.App) {
        PCodeWorkspaceController.shared(ghostty).workspace.newSession()
    }

    func newWindow(_ ghostty: Ghostty.App) {
        // Bring the window back; start a session only if it was already up or
        // has sessions, so reopening a closed empty window doesn't add one.
        let controller = PCodeWorkspaceController.shared(ghostty)
        let wasVisible = controller.window?.isVisible == true
        if wasVisible || !controller.workspace.panes.isEmpty || !controller.workspace.dormant.isEmpty {
            controller.workspace.newSession()
        }
        PCodeWorkspaceController.show(ghostty)
    }

    // AppKit calls both on the main thread.
    var terminatesImmediately: Bool { MainActor.assumeIsolated { PCodeRestart.terminating } }

    func restart() {
        MainActor.assumeIsolated {
            PCodeRestart.begin(workspace: PCodeWorkspaceController.existing?.workspace)
        }
    }

    // MARK: Window actions

    func cancelKeyboardPicker(in window: NSWindow?) -> Bool {
        guard let picker = PCode.keyboardPicker(in: window) else { return false }
        picker.cancel()
        return true
    }

    func hasKeyboardPicker(in window: NSWindow?) -> Bool {
        PCode.keyboardPicker(in: window) != nil
    }

    func keyboardOverlay(in window: NSWindow?) -> Ghostty.SurfaceView? {
        PCode.keyboardOverlay(in: window)
    }

    // MARK: Surfaces

    func pane(forOverlay view: Ghostty.SurfaceView) -> Ghostty.SurfaceView? {
        view.isPCodeOverlay ? view.pcodeOwner?.surfaceView : nil
    }

    func handleEvent(_ payload: String, from view: Ghostty.SurfaceView) {
        view.pcodeEvent(payload)
    }

    func windowSurface(for view: Ghostty.SurfaceView) -> Ghostty.SurfaceView {
        view.pcodeWindowSurface
    }

    func suppressesResizeOverlay(_ view: Ghostty.SurfaceView) -> Bool {
        view.isPCodeOverlay || view.pcodeIsSessionPane
    }

    func clickableHeight(_ view: Ghostty.SurfaceView) -> CGFloat? {
        view.pcodeDockVisibleHeight
    }

    // No-op since panes run on one surface (no dock handoff); removing the
    // hook from the fork's seam is pending, `dev/native-app-plan.md` step 7.
    func clicksThroughOnFocus(_ view: Ghostty.SurfaceView) -> Bool {
        false
    }

    func ignoresMouse(_ view: Ghostty.SurfaceView) -> Bool { view.pcodeIgnoresMouse }

    func allowsMouseFocus(_ view: Ghostty.SurfaceView) -> Bool { view.pcodeAllowsMouseFocus }

    func didBecomeFirstResponder(_ view: Ghostty.SurfaceView) { view.pcodeDidFocus() }

    // No-op for the same reason as `clicksThroughOnFocus` (plan step 7).
    func didMouseUp(_ view: Ghostty.SurfaceView) {}

    func willKeyDown(_ view: Ghostty.SurfaceView, _ event: NSEvent) { view.pcodeWillKeyDown(event) }

    func restorableSession(_ view: Ghostty.SurfaceView) -> String? { view.pcodeSession }

    func resumeInput(session: String?) -> String? {
        Ghostty.SurfaceView.pcodeResumeInput(session)
    }

    // MARK: SwiftUI

    func surfaceContent(_ view: Ghostty.SurfaceView, isSplit: Bool) -> AnyView {
        AnyView(PCodeDockedSurface(surfaceView: view, dock: view.pcodeDock, isSplit: isSplit))
    }

    func surfaceOverlay(_ view: Ghostty.SurfaceView) -> AnyView {
        // Workspace session panes mount their completion, toast and popup layers
        // at pane level in `PCodeSessionPaneView` so they span the full pane even
        // when the terminal surface is sized to the editor card.
        if view.pcodeIsSessionPane {
            return AnyView(EmptyView())
        }
        return AnyView(PCodeSurfaceOverlay(surfaceView: view))
    }

    // MARK: Debug socket

    func debugCommand(
        _ cmd: String,
        _ args: [String: Any],
        _ finish: @escaping DebugCommands.Reply
    ) throws -> Bool {
        switch cmd {
        case "peek": try Self.peekSession(args, finish)
        case "sidebar": try Self.sidebarCommand(args, finish)
        case "settings":
            let action = (args["action"] as? String) ?? "show"
            let visible: Bool = MainActor.assumeIsolated {
                if action == "hide" {
                    PCodeSettingsController.shared.hide()
                } else {
                    PCodeSettingsController.shared.show()
                }
                return PCodeSettingsController.shared.window?.isVisible ?? false
            }
            finish(["visible": visible])
        case "menus":
            finish(["main": NSApp.mainMenu.map(PCodeMenu.dumpMenu) ?? []])
        default: return false
        }
        return true
    }

    func debugInfo(_ view: Ghostty.SurfaceView) -> [String: Any] {
        guard view.isPCodeOverlay else { return [:] }
        var info: [String: Any] = [
            "overlay": view.pcodeOwner.map { String(describing: type(of: $0)) } ?? "orphaned",
        ]
        if let pane = view.pcodeOwner?.surfaceView { info["overlay_of"] = DebugCommands.shortID(pane) }
        return info
    }

    private static func peekSession(_ args: [String: Any], _ finish: @escaping ([String: Any]) -> Void) throws {
        guard let controller = DebugCommands.terminalWindows().compactMap({ $0.windowController as? PCodeWorkspaceController }).first else {
            throw DebugCommands.Failure("no workspace window")
        }
        let workspace = controller.workspace
        if (args["dismiss"] as? Bool) == true {
            workspace.peek.dismiss()
            finish(["showing": false])
            return
        }
        if (args["open"] as? Bool) == true {
            guard let pane = workspace.panes.first(where: { $0.id != workspace.selected }) else {
                throw DebugCommands.Failure("no non-selected open pane")
            }
            let target = workspace.peekTarget(for: pane)
            workspace.peek.peekFromMenu(target)
            finish(["showing": true, "live": true, "title": target.title])
            return
        }
        let info: PCodeSessionInfo?
        if let id = args["session"] as? String {
            info = workspace.allHistory.first { $0.id == id || $0.id.hasPrefix(id) }
        } else {
            let openIDs = Set(workspace.panes.compactMap(\.sessionID))
            info = workspace.allHistory.first { !openIDs.contains($0.id) } ?? workspace.allHistory.first
        }
        guard let info else { throw DebugCommands.Failure("no session to peek") }
        workspace.peek.peekFromMenu(workspace.peekTarget(for: info))
        let startReplay = (args["replay"] as? Bool) == true
        if let speed = args["speed"] as? Double, speed > 0 {
            workspace.peek.replaySpeed = speed
        }
        workspace.peek.isReplaying = startReplay
        finish([
            "showing": true,
            "live": false,
            "session": info.id,
            "title": info.label,
            "replaying": startReplay,
            "speed": workspace.peek.replaySpeed,
        ])
    }

    private static func sidebarCommand(_ args: [String: Any], _ finish: @escaping ([String: Any]) -> Void) throws {
        guard let controller = DebugCommands.terminalWindows().compactMap({ $0.windowController as? PCodeWorkspaceController }).first else {
            throw DebugCommands.Failure("no workspace window")
        }
        let workspace = controller.workspace
        let totalWidth = controller.window?.contentLayoutRect.width ?? 0
        if (args["reset"] as? Bool) == true {
            workspace.resetSidebarWidth()
        }
        if let width = args["width"] as? Double {
            workspace.sidebarWidth = PCodeSidebarLayout.clampedWidth(CGFloat(width), totalWidth: totalWidth)
        }
        if let autoHide = args["auto_hide"] as? Bool {
            if autoHide != workspace.sidebarAutoHide {
                workspace.toggleSidebarAutoHide()
            }
        }
        if let revealed = args["revealed"] as? Bool, workspace.sidebarAutoHide {
            workspace.sidebarRevealed = revealed
        }
        finish([
            "width": Double(workspace.sidebarWidth),
            "effective_width": Double(workspace.effectiveSidebarWidth(in: totalWidth)),
            "auto_hide": workspace.sidebarAutoHide,
            "revealed": workspace.sidebarRevealed,
        ])
    }
}
