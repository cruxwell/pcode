import SwiftUI

/// The model's plan in the native composer's status strip: a one-line summary
/// (`2/7 · Adding the migration` and a progress bar) that opens the full checklist in a
/// popover, so the list never pushes the transcript around. Fed by the host's
/// `plan_changed` (`PCodeHostClientState.plan`); hidden while there is no plan.
///
/// Counts, weights and glyphs follow the terminal's task list: `panel_title` (completed
/// over every step), `plan_sizes.plan_progress`, and `tool_panel.PLAN_ICONS`.
enum PCodePlanProgress {
    /// `plan_sizes.SIZE_WEIGHTS`; an unsized step counts as M.
    static let weights: [String: Double] = ["S": 1, "M": 3, "L": 8]

    /// `plan_progress`: 0...1 weighted by size, cancelled steps left out, the running
    /// step counted half. Nil when nothing counts.
    static func fraction(_ items: [PCodePlanItem]) -> Double? {
        var total = 0.0
        var done = 0.0
        for item in items where item.status != "cancelled" {
            let weight = weights[item.size ?? "M"] ?? weights["M"]!
            total += weight
            if item.status == "completed" {
                done += weight
            } else if item.status == "in_progress" {
                done += weight / 2
            }
        }
        return total > 0 ? done / total : nil
    }

    /// `Tasks 2/7`: completed steps over all of them.
    static func count(_ items: [PCodePlanItem]) -> (completed: Int, total: Int) {
        (items.filter { $0.status == "completed" }.count, items.count)
    }

    /// The step in flight, as the terminal names it: its active form, else its content.
    static func current(_ items: [PCodePlanItem]) -> String? {
        guard let step = items.first(where: { $0.status == "in_progress" }) else { return nil }
        return step.activeForm.isEmpty ? step.content : step.activeForm
    }

    /// `tool_panel.PLAN_ICONS`, with `ACTIVE_TASK_ICON` for the running step.
    static func glyph(_ status: String) -> String {
        switch status {
        case "completed": return "✔"
        case "cancelled": return "–"
        case "in_progress": return "↺"
        case "blocked": return "!"
        default: return "○"
        }
    }

    /// The steps in reading order with their depth: each step's children (by `parent_id`)
    /// right after it. A step whose parent is not in the plan stays at the top level.
    static func rows(_ items: [PCodePlanItem]) -> [(item: PCodePlanItem, depth: Int)] {
        let ids = Set(items.map(\.id))
        var children: [String: [PCodePlanItem]] = [:]
        var roots: [PCodePlanItem] = []
        for item in items {
            if let parent = item.parentID, parent != item.id, ids.contains(parent) {
                children[parent, default: []].append(item)
            } else {
                roots.append(item)
            }
        }
        var result: [(PCodePlanItem, Int)] = []
        var seen: Set<String> = []
        func visit(_ item: PCodePlanItem, _ depth: Int) {
            // A cycle in `parent_id` would otherwise recurse forever.
            guard seen.insert(item.id).inserted || item.id.isEmpty else { return }
            result.append((item, depth))
            for child in children[item.id] ?? [] { visit(child, depth + 1) }
        }
        roots.forEach { visit($0, 0) }
        // Anything only reachable through a cycle still gets listed.
        for item in items where !item.id.isEmpty && !seen.contains(item.id) { visit(item, 0) }
        return result
    }
}

/// The strip's plan summary: a button that opens the checklist popover.
struct PCodeTaskStripSummary: View {
    @EnvironmentObject var ghostty: Ghostty.App
    let items: [PCodePlanItem]
    @Binding var isPresented: Bool

    var body: some View {
        let count = PCodePlanProgress.count(items)
        Button { isPresented.toggle() } label: {
            HStack(spacing: 6) {
                PCodePlanProgressBar(fraction: PCodePlanProgress.fraction(items) ?? 0)
                    .frame(width: 34, height: 4)
                Text("\(count.completed)/\(count.total)")
                    .font(.system(size: 10.5, weight: .semibold).monospacedDigit())
                    .foregroundStyle(.secondary)
                if let current = PCodePlanProgress.current(items) {
                    Text(current)
                        .font(.system(size: 11, weight: .medium))
                        .foregroundStyle(.primary.opacity(0.8))
                        .lineLimit(1)
                        .truncationMode(.tail)
                }
            }
            .padding(.horizontal, 7)
            .padding(.vertical, 1.5)
            .background(Capsule(style: .continuous).fill(Color.primary.opacity(isPresented ? 0.12 : 0.06)))
            .contentShape(Capsule())
        }
        .buttonStyle(.plain)
        .help("Tasks (Cmd+Shift+K)")
    }
}

struct PCodePlanProgressBar: View {
    let fraction: Double

    var body: some View {
        GeometryReader { geometry in
            ZStack(alignment: .leading) {
                Capsule().fill(Color.primary.opacity(0.12))
                Capsule().fill(Color.accentColor)
                    .frame(width: max(0, min(1, fraction)) * geometry.size.width)
            }
        }
        .animation(.easeOut(duration: 0.25), value: fraction)
    }
}

/// The checklist as a card hung above the status strip. An overlay, not an `NSPopover`:
/// it takes no layout space, so the transcript never moves, and it stays in the window
/// (a popover's own window is missed by in-app snapshots). Escape or the summary closes it.
struct PCodeTaskListCard: View {
    @EnvironmentObject var ghostty: Ghostty.App
    let items: [PCodePlanItem]

    var body: some View {
        PCodeTaskListView(items: items)
            .background(
                RoundedRectangle(cornerRadius: 10)
                    .fill(ghostty.config.backgroundColor)
                    .shadow(color: .black.opacity(0.35), radius: 14, x: 0, y: 4))
            .overlay(
                RoundedRectangle(cornerRadius: 10)
                    .strokeBorder(ghostty.config.splitDividerColor, lineWidth: 1))
            .environment(\.colorScheme, NSColor(ghostty.config.backgroundColor).isLightColor ? .light : .dark)
    }
}

/// The full checklist, as the terminal's task list draws it.
struct PCodeTaskListView: View {
    @EnvironmentObject var ghostty: Ghostty.App
    let items: [PCodePlanItem]
    @ObservedObject private var typography = PCodeTypography.shared
    static let maxUnscrolledRows = 14

    var body: some View {
        let count = PCodePlanProgress.count(items)
        VStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 8) {
                Text("Tasks \(count.completed)/\(count.total)")
                    .font(.system(size: 12, weight: .semibold))
                Spacer(minLength: 12)
                PCodePlanProgressBar(fraction: PCodePlanProgress.fraction(items) ?? 0)
                    .frame(width: 80, height: 5)
            }
            let rows = PCodePlanProgress.rows(items)
            let list = VStack(alignment: .leading, spacing: 4) {
                ForEach(Array(rows.enumerated()), id: \.offset) { _, row in
                    line(row.item, depth: row.depth)
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            // A scroll view has no height of its own in an overlay: only a long plan gets one,
            // at a fixed height; a short one sizes to its rows.
            if rows.count > Self.maxUnscrolledRows {
                ScrollView { list }.frame(height: 360)
            } else {
                list.fixedSize(horizontal: false, vertical: true)
            }
        }
        .padding(12)
        .frame(width: 380)
    }

    private func line(_ item: PCodePlanItem, depth: Int) -> some View {
        let running = item.status == "in_progress"
        let finished = item.status == "completed" || item.status == "cancelled"
        let font = Font(typography.snapshot.proseNSFont(scale: 0.92, weight: running ? .semibold : nil))
        return HStack(alignment: .firstTextBaseline, spacing: 7) {
            Text(PCodePlanProgress.glyph(item.status))
                .font(font)
                .foregroundStyle(running ? Color.accentColor : Color.secondary)
                .frame(width: 14)
            Text(running && !item.activeForm.isEmpty ? item.activeForm : item.content)
                .font(font)
                .strikethrough(item.status == "cancelled")
                .foregroundStyle(finished ? Color.secondary : Color.primary)
                .fixedSize(horizontal: false, vertical: true)
            Spacer(minLength: 0)
            if let size = item.size {
                Text(size)
                    .font(.system(size: 9.5, weight: .semibold))
                    .foregroundStyle(.secondary.opacity(0.7))
            }
        }
        .padding(.leading, CGFloat(depth) * 16)
        .padding(.vertical, 2)
        .padding(.horizontal, 4)
        .background(
            RoundedRectangle(cornerRadius: 5)
                .fill(running ? Color.accentColor.opacity(0.12) : Color.clear))
    }
}

extension PCodeComposerSession {
    /// Test hook, off unless `PCODE_DEBUG_PLAN_FILE` is set: while attached, that file's
    /// JSON plan items are fed in as the host's `plan_changed` whenever the file changes,
    /// so the strip can be driven without a model that plans (the preview runtime doesn't).
    func startDebugPlanFeed() {
        guard debugPlanTimer == nil,
              let path = ProcessInfo.processInfo.environment["PCODE_DEBUG_PLAN_FILE"], !path.isEmpty
        else { return }
        var last: Data?
        let timer = Timer(timeInterval: 0.5, repeats: true) { [weak self] _ in
            guard let self, let data = try? Data(contentsOf: URL(fileURLWithPath: path)), data != last,
                  let items = try? JSONSerialization.jsonObject(with: data) as? [Any] else { return }
            last = data
            self.receive("plan_changed", [items], [:])
        }
        RunLoop.main.add(timer, forMode: .common)
        debugPlanTimer = timer
    }
}
