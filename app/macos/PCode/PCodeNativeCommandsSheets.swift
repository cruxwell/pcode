import AppKit
import Combine
import SwiftUI

// The small sheets the native command routes show (dev/native-app-plan.md, 3b,
// "Native command routes"), and how they go over the window.

// MARK: - Presenting

/// Puts a route's sheet or picker over a window as an AppKit sheet, so nothing in
/// the pane's own view tree has to know about it.
enum PCodeNativeSheetPresenter {
    /// `presentation` as a sheet on `window`; with no window, a no-op (a picker is
    /// cancelled so its caller still gets an answer).
    static func present(_ presentation: PCodeNativeCommands.Presentation, in window: NSWindow?, ghostty: Ghostty.App?) {
        guard let window, let ghostty else {
            if case .picker(_, let picker) = presentation { picker.cancel() }
            return
        }
        // Another route's sheet is up: replace it rather than queueing behind it.
        if let open = window.attachedSheet, open.identifier == sheetIdentifier {
            window.endSheet(open)
        }
        let sheet = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 560, height: 420),
            styleMask: [.titled, .resizable, .fullSizeContentView], backing: .buffered, defer: false)
        sheet.identifier = sheetIdentifier
        sheet.titlebarAppearsTransparent = true
        sheet.titleVisibility = .hidden
        sheet.isReleasedWhenClosed = false
        let dismiss: () -> Void = { [weak window, weak sheet] in
            guard let sheet else { return }
            if let window, window.attachedSheet === sheet { window.endSheet(sheet) } else { sheet.orderOut(nil) }
        }
        let content: AnyView
        switch presentation {
        case .sheet(let title, let build):
            content = AnyView(PCodeNativeSheetFrame(title: title, dismiss: dismiss) { build(dismiss) })
        case .picker(let title, let picker):
            let previous = picker.onClosed
            picker.onClosed = {
                previous?()
                dismiss()
            }
            content = AnyView(PCodeNativePickerSheet(title: title, picker: picker))
        }
        sheet.contentViewController = NSHostingController(rootView: content.environmentObject(ghostty))
        sheet.setContentSize(NSSize(width: min(640, max(480, window.frame.width * 0.6)), height: min(480, max(300, window.frame.height * 0.6))))
        window.beginSheet(sheet) { _ in
            // Closed by the window going away: a picker's caller still gets its nil.
            if case .picker(_, let picker) = presentation { picker.cancel() }
        }
    }

    static let sheetIdentifier = NSUserInterfaceItemIdentifier("PCodeNativeCommandSheet")
}

/// A sheet's title bar and Done button around its content; Esc closes it.
struct PCodeNativeSheetFrame<Content: View>: View {
    let title: String
    let dismiss: () -> Void
    @ViewBuilder let content: () -> Content

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack {
                Text(title).font(.headline)
                Spacer()
                Button("Done", action: dismiss).keyboardShortcut(.cancelAction)
            }
            .padding(.horizontal, 16)
            .padding(.top, 14)
            .padding(.bottom, 10)
            Divider()
            content()
        }
        .frame(minWidth: 420, minHeight: 240)
    }
}

/// A picker in a sheet, styled from the terminal's config when there is one.
private struct PCodeNativePickerSheet: View {
    @EnvironmentObject var ghostty: Ghostty.App
    let title: String
    @ObservedObject var picker: PCodePicker

    var body: some View {
        VStack(spacing: 0) {
            ZStack(alignment: .trailing) {
                Text(title)
                    .font(.system(size: 12, weight: .medium))
                    .foregroundStyle(.secondary)
                    .frame(maxWidth: .infinity)
                // The picker takes Esc itself; this is for the mouse.
                Button("Cancel") { picker.cancel() }
                    .buttonStyle(.borderless)
                    .font(.system(size: 12))
                    .padding(.trailing, 8)
            }
            .padding(.top, 10)
            .padding(.bottom, 4)
            PCodePickerView(picker: picker, config: ghostty.config, fonts: nil)
        }
        .padding(4)
        .background(ghostty.config.backgroundColor)
        .frame(minWidth: 420, minHeight: 260)
    }
}

// MARK: - Jobs

/// The Jobs sheet's state: the host's running background jobs (`peek().jobs`).
final class PCodeJobsModel: ObservableObject {
    @Published private(set) var jobs: [PCodeNativeCommands.Job] = []
    @Published private(set) var loading = true
    @Published private(set) var error = ""
    /// The job this sheet asked the host to watch. The host's own `watched_job`
    /// isn't mirrored, so the footer's Unwatch is always offered too.
    @Published private(set) var watched: String?
    private let context: PCodeNativeCommands.Context

    init(context: PCodeNativeCommands.Context) {
        self.context = context
    }

    func refresh() {
        loading = true
        context.request("peek", []) { [weak self] result, error in
            guard let self else { return }
            self.loading = false
            self.error = error ?? ""
            self.jobs = PCodeNativeCommands.jobs(fromPeek: result)
        }
    }

    /// `stop_jobs([id])`, or every running job for nil.
    func stop(_ id: String?) {
        context.notify("stop_jobs", [id.map { [$0] as Any } ?? NSNull()])
        // The host stops them now; give the registry a moment, then reread.
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.4) { [weak self] in self?.refresh() }
    }

    /// `watch_job(id)`: pin its output tail in the status; nil unpins.
    func watch(_ id: String?) {
        context.notify("watch_job", [id ?? NSNull()])
        watched = id
    }
}

struct PCodeJobsSheet: View {
    @ObservedObject var model: PCodeJobsModel
    let dismiss: () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            if model.loading && model.jobs.isEmpty {
                ProgressView().frame(maxWidth: .infinity, maxHeight: .infinity)
            } else if !model.error.isEmpty {
                placeholder("Could not read jobs: \(model.error)")
            } else if model.jobs.isEmpty {
                placeholder("No background jobs are running.")
            } else {
                List(model.jobs) { job in
                    HStack(alignment: .firstTextBaseline, spacing: 10) {
                        Text(job.id).font(.system(.body, design: .monospaced)).foregroundStyle(.secondary)
                        VStack(alignment: .leading, spacing: 2) {
                            Text(job.purpose.isEmpty ? job.command : job.purpose).lineLimit(1)
                            if !job.purpose.isEmpty {
                                Text(job.command).font(.system(.caption, design: .monospaced))
                                    .foregroundStyle(.secondary).lineLimit(1).truncationMode(.middle)
                            }
                        }
                        Spacer()
                        if model.watched == job.id {
                            Button("Unwatch") { model.watch(nil) }
                        } else {
                            Button("Watch") {
                                model.watch(job.id)
                                dismiss()
                            }
                        }
                        Button("Stop") { model.stop(job.id) }
                    }
                    .padding(.vertical, 2)
                }
            }
            Divider()
            HStack {
                Button("Refresh") { model.refresh() }
                Button("Unwatch") { model.watch(nil) }
                    .help("Stop pinning a job's output in the status")
                Spacer()
                Button("Stop All") { model.stop(nil) }.disabled(model.jobs.isEmpty)
            }
            .padding(12)
        }
    }

    private func placeholder(_ text: String) -> some View {
        Text(text).foregroundStyle(.secondary).frame(maxWidth: .infinity, maxHeight: .infinity)
    }
}

// MARK: - Session info

final class PCodeSessionInfoModel: ObservableObject {
    @Published var rows: [PCodeNativeCommands.InfoRow]

    init(rows: [PCodeNativeCommands.InfoRow]) {
        self.rows = rows
    }
}

/// `/status`: the terminal's "Session" sheet (`session_info_dialog`) as label/value rows.
struct PCodeSessionInfoSheet: View {
    @ObservedObject var model: PCodeSessionInfoModel
    let dismiss: () -> Void

    var body: some View {
        ScrollView {
            Grid(alignment: .leadingFirstTextBaseline, horizontalSpacing: 14, verticalSpacing: 6) {
                ForEach(model.rows) { row in
                    GridRow {
                        Text(row.label).foregroundStyle(.secondary).gridColumnAlignment(.trailing)
                        Text(row.value).textSelection(.enabled).frame(maxWidth: .infinity, alignment: .leading)
                    }
                }
            }
            .padding(16)
        }
        .overlay {
            if model.rows.isEmpty { Text("No session yet.").foregroundStyle(.secondary) }
        }
    }
}

// MARK: - Help

/// `/help`, `/commands`: every command the composer offers, by group.
struct PCodeHelpSheet: View {
    let groups: [PCodeNativeCommands.HelpGroup]
    let dismiss: () -> Void
    @State private var filter = ""

    var body: some View {
        VStack(spacing: 0) {
            TextField("Filter", text: $filter)
                .textFieldStyle(.roundedBorder)
                .padding(12)
            List {
                ForEach(shown) { group in
                    Section(group.name) {
                        ForEach(group.commands, id: \.name) { command in
                            HStack(alignment: .firstTextBaseline, spacing: 12) {
                                Text(([command.name] + command.aliases).joined(separator: ", "))
                                    .font(.system(.body, design: .monospaced))
                                    .frame(minWidth: 150, alignment: .leading)
                                Text(command.description).foregroundStyle(.secondary)
                            }
                        }
                    }
                }
            }
        }
    }

    private var shown: [PCodeNativeCommands.HelpGroup] {
        let needle = PCodePickMatch.casefold(filter.trimmingCharacters(in: .whitespaces))
        guard !needle.isEmpty else { return groups }
        return groups.compactMap { group in
            let commands = group.commands.filter { command in
                ([command.name, command.description] + command.aliases).contains { PCodePickMatch.casefold($0).contains(needle) }
            }
            return commands.isEmpty ? nil : PCodeNativeCommands.HelpGroup(name: group.name, commands: commands)
        }
    }
}

// MARK: - Text

/// Plain, selectable output (`/config list`, `/config diff`).
struct PCodeTextSheet: View {
    let text: String
    let dismiss: () -> Void

    var body: some View {
        ScrollView {
            Text(text)
                .font(.system(.body, design: .monospaced))
                .textSelection(.enabled)
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(16)
        }
    }
}
