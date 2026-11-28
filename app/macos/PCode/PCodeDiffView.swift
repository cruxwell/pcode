import AppKit
import Foundation
import SwiftUI

// MARK: - Syntax Highlighting Cache for Diff Views

/// Per-view cache that tokenizes diff lines asynchronously via `PCodeSyntaxHighlighter`
/// and splits token streams cleanly per line so multi-line constructs stay highlighted.
@MainActor
final class PCodeDiffSyntaxStore: ObservableObject {
    static let maxCacheEntries = 256

    private let highlighter: any PCodeSyntaxHighlighter
    private var tokensByKey: [String: [[PCodeSyntaxToken]]] = [:]
    private var keyOrder: [String] = []
    private var inFlight: Set<String> = []
    private var tokensReadyObservers: [UUID: @MainActor (String) -> Void] = [:]

    init(highlighter: any PCodeSyntaxHighlighter = PCodePrismSyntaxHighlighter.shared) {
        self.highlighter = highlighter
    }

    /// Register a targeted callback invoked when a hunk finishes async tokenization.
    /// Native AppKit views use this to redraw only the affected mounted rows without
    /// triggering a full SwiftUI view-tree invalidation.
    @discardableResult
    func addTokensReadyObserver(_ handler: @escaping @MainActor (String) -> Void) -> UUID {
        let id = UUID()
        tokensReadyObservers[id] = handler
        return id
    }

    func removeTokensReadyObserver(_ id: UUID) {
        tokensReadyObservers.removeValue(forKey: id)
    }

    /// Returns cached per-line tokens if available, or kicks off an async tokenization pass.
    func lineTokens(for lines: [String], language: String?) -> [[PCodeSyntaxToken]]? {
        guard !lines.isEmpty else { return [] }
        let lang = PCodePrismSyntaxHighlighter.normalizeLanguage(language)
        guard lang != "plain" else { return nil }
        let joined = lines.joined(separator: "\n")
        let key = "\(lang)\u{0}\(joined)"
        return lineTokens(forPrecomputedKey: key, joinedCode: joined, lineCount: lines.count, normalizedLanguage: lang)
    }

    /// Fast-path lookup using a precomputed `(key, joinedCode)` built once per hunk, avoiding
    /// per-line string joins and hashing during 120fps scrolling.
    func lineTokens(
        forPrecomputedKey key: String,
        joinedCode: String,
        lineCount: Int,
        normalizedLanguage lang: String
    ) -> [[PCodeSyntaxToken]]? {
        guard lineCount > 0, lang != "plain" else { return nil }
        if let cached = tokensByKey[key] {
            return cached
        }

        if let syncTokens = highlighter.cachedTokens(code: joinedCode, language: lang) {
            let split = Self.splitTokensByLine(syncTokens, lineCount: lineCount)
            storeTokens(split, forKey: key)
            return split
        }

        if !inFlight.contains(key) {
            inFlight.insert(key)
            let hl = highlighter
            let expectedCount = lineCount
            Task { [weak self] in
                let raw = await hl.tokenize(code: joinedCode, language: lang)
                let split = Self.splitTokensByLine(raw, lineCount: expectedCount)
                await MainActor.run {
                    guard let self else { return }
                    self.inFlight.remove(key)
                    self.storeTokens(split, forKey: key)
                    if self.tokensReadyObservers.isEmpty {
                        self.objectWillChange.send()
                    } else {
                        for observer in self.tokensReadyObservers.values {
                            observer(key)
                        }
                    }
                }
            }
        }
        return nil
    }

    private func storeTokens(_ tokens: [[PCodeSyntaxToken]], forKey key: String) {
        let isNew = tokensByKey[key] == nil
        tokensByKey[key] = tokens
        if isNew {
            keyOrder.append(key)
        }
        while tokensByKey.count > Self.maxCacheEntries, !keyOrder.isEmpty {
            let oldest = keyOrder.removeFirst()
            tokensByKey.removeValue(forKey: oldest)
        }
    }

    /// Split a flat `[PCodeSyntaxToken]` array (representing `\n`-joined lines) into per-line token arrays.
    static func splitTokensByLine(_ tokens: [PCodeSyntaxToken], lineCount: Int) -> [[PCodeSyntaxToken]] {
        var result: [[PCodeSyntaxToken]] = [[]]
        result.reserveCapacity(max(1, lineCount))

        for token in tokens {
            let pieces = token.content.split(separator: "\n", omittingEmptySubsequences: false)
            for (idx, piece) in pieces.enumerated() {
                if idx > 0 {
                    result.append([])
                }
                if !piece.isEmpty {
                    result[result.count - 1].append(PCodeSyntaxToken(content: String(piece), kind: token.kind))
                }
            }
        }

        while result.count < lineCount {
            result.append([])
        }
        return result
    }

    /// Build a styled SwiftUI `AttributedString` combining syntax tokens with intraline word-diff tints.
    static func renderedLine(
        text: String,
        tokens: [PCodeSyntaxToken]?,
        intralineSpans: [PCodeIntralineSpan],
        lineKind: PCodeDiffLine.Kind,
        font: NSFont,
        palette: PCodeSyntaxPalette,
        isDark: Bool
    ) -> AttributedString {
        let mutable = NSMutableAttributedString()
        mutable.beginEditing()

        if let tokens, !tokens.isEmpty {
            for token in tokens {
                mutable.append(NSAttributedString(
                    string: token.content,
                    attributes: [
                        .font: font,
                        .foregroundColor: palette.color(for: token.kind),
                    ]
                ))
            }
        } else {
            let fallbackColor: NSColor = (lineKind == .meta) ? palette.comment : palette.plain
            mutable.append(NSAttributedString(
                string: text,
                attributes: [
                    .font: font,
                    .foregroundColor: fallbackColor,
                ]
            ))
        }

        // Overlay intraline word-diff background tints using exact UTF-16 ranges.
        if !intralineSpans.isEmpty && (lineKind == .added || lineKind == .deleted) {
            let totalUTF16 = mutable.length
            let tint: NSColor = lineKind == .added
                ? NSColor.systemGreen.withAlphaComponent(isDark ? 0.34 : 0.28)
                : NSColor.systemRed.withAlphaComponent(isDark ? 0.34 : 0.28)

            for span in intralineSpans where span.emphasized {
                let start = max(0, min(span.range.lowerBound, totalUTF16))
                let end = max(start, min(span.range.upperBound, totalUTF16))
                let length = end - start
                if length > 0 {
                    mutable.addAttribute(.backgroundColor, value: tint, range: NSRange(location: start, length: length))
                }
            }
        }

        mutable.endEditing()
        return AttributedString(mutable)
    }
}

// MARK: - Changes Inspector View

/// Right-hand Inspector "Changes" tab rendering live session, working-tree, branch, and single-edit diffs.
struct PCodeChangesInspectorView: View {
    @ObservedObject var state: PCodeInspectorState
    let diffs: [PCodeFileDiff]
    let actions: PCodeInspectorActions
    let singleEditLabel: String?
    let isLoading: Bool
    let emptyMessage: String?
    let codeFont: NSFont?
    let surfaceBackground: Color
    let foregroundColor: NSColor?
    let onScopeChange: ((PCodeDiffScope) -> Void)?
    let onRefresh: (() -> Void)?

    @Environment(\.colorScheme) private var colorScheme
    @StateObject private var syntaxStore: PCodeDiffSyntaxStore

    @State private var selection: PCodeDiffSelection?
    @State private var selectionAnchor: (path: String, line: Int)?
    @State private var expandedGaps: [PCodeDiffExpandedGapKey: [PCodeDiffLine]] = [:]
    @State private var updatedUnseenFiles: Set<String> = []
    @State private var lastModifiedByPath: [String: Date] = [:]
    @State private var copiedPath: String?
    @State private var containerWidth: CGFloat = 460
    @State private var scrollToBottomToken: Int = 0
    @State private var scrollToFileRequest: (id: String, token: Int, animated: Bool)?
    @State private var suppressNextFocusScroll: Bool = false

    /// Minimum pane width before Side-by-Side layout is enabled without expanding the Inspector.
    static let sideBySideMinWidth: CGFloat = 640

    init(
        state: PCodeInspectorState,
        diffs: [PCodeFileDiff],
        actions: PCodeInspectorActions = .noop,
        singleEditLabel: String? = nil,
        isLoading: Bool = false,
        emptyMessage: String? = nil,
        highlighter: any PCodeSyntaxHighlighter = PCodePrismSyntaxHighlighter.shared,
        codeFont: NSFont? = nil,
        surfaceBackground: Color = Color(nsColor: .textBackgroundColor),
        foregroundColor: NSColor? = nil,
        onScopeChange: ((PCodeDiffScope) -> Void)? = nil,
        onRefresh: (() -> Void)? = nil
    ) {
        self.state = state
        self.diffs = diffs
        self.actions = actions
        self.singleEditLabel = singleEditLabel
        self.isLoading = isLoading
        self.emptyMessage = emptyMessage
        self.codeFont = codeFont
        self.surfaceBackground = surfaceBackground
        self.foregroundColor = foregroundColor
        self.onScopeChange = onScopeChange
        self.onRefresh = onRefresh
        self._syntaxStore = StateObject(wrappedValue: PCodeDiffSyntaxStore(highlighter: highlighter))
    }

    private var isDark: Bool { colorScheme == .dark }

    private var resolvedCodeFont: NSFont {
        if let codeFont { return codeFont }
        if let jb = NSFont(name: "JetBrainsMono-Regular", size: 11) {
            return jb
        }
        return NSFont.monospacedSystemFont(ofSize: 11, weight: .regular)
    }

    private var canUseSideBySide: Bool {
        state.isExpanded || containerWidth >= Self.sideBySideMinWidth
    }

    private var effectiveLayout: PCodeDiffLayout {
        (state.diffLayout == .sideBySide && canUseSideBySide) ? .sideBySide : .unified
    }

    private var collidingBasenames: Set<String> {
        var counts: [String: Int] = [:]
        for diff in diffs {
            counts[diff.basename, default: 0] += 1
        }
        return Set(counts.filter { $0.value > 1 }.keys)
    }

    var body: some View {
        GeometryReader { geo in
            VStack(spacing: 0) {
                toolbar
                Divider()
                contentArea
            }
            .onAppear {
                containerWidth = geo.size.width
                seedTrackingState()
            }
            .onChange(of: geo.size.width) { newWidth in
                containerWidth = newWidth
            }
        }
        .background(surfaceBackground)
    }

    // MARK: - Top Toolbar

    private var isCompactToolbar: Bool { containerWidth < 490 }
    private var isVeryCompactToolbar: Bool { containerWidth < 360 }

    private var compactScopeTitle: String {
        switch state.diffScope {
        case .session: return "Session"
        case .workingTree: return "Working"
        case .branch: return "Branch"
        case .singleEdit: return singleEditLabel ?? "Edit"
        }
    }

    private var toolbar: some View {
        HStack(spacing: isCompactToolbar ? 4 : 8) {
            scopeControl

            if state.diffScope == .session {
                sessionViewModeControl
            }

            fileJumpMenu

            Spacer(minLength: 2)

            if isLoading {
                ProgressView()
                    .controlSize(.small)
                    .scaleEffect(0.75)
            } else if let onRefresh {
                Button(action: onRefresh) {
                    Image(systemName: "arrow.clockwise")
                        .font(.system(size: 11, weight: .medium))
                }
                .buttonStyle(.borderless)
                .help("Refresh diffs")
            }

            followAgentButton

            wrapToggleButton

            if canUseSideBySide {
                layoutToggleButton
            }
        }
        .padding(.horizontal, isCompactToolbar ? 6 : 10)
        .padding(.vertical, 4)
        .background(Color.primary.opacity(isDark ? 0.04 : 0.035))
    }

    @ViewBuilder
    private var scopeControl: some View {
        if case .singleEdit = state.diffScope {
            HStack(spacing: 4) {
                Button {
                    setScope(.session)
                } label: {
                    Image(systemName: "chevron.left")
                        .font(.system(size: 10, weight: .semibold))
                }
                .buttonStyle(.borderless)
                .help("Back to This Session")

                HStack(spacing: 4) {
                    Image(systemName: "doc.text")
                        .font(.system(size: 10))
                    Text(singleEditLabel ?? state.diffScope.displayTitle)
                        .font(.system(size: 11, weight: .medium))
                        .lineLimit(1)
                        .truncationMode(.middle)
                }
                .padding(.horizontal, 7)
                .padding(.vertical, 3)
                .background(
                    Capsule()
                        .fill(Color.accentColor.opacity(isDark ? 0.24 : 0.14))
                )
                .foregroundStyle(Color.accentColor)
            }
        } else {
            Menu {
                scopeMenuItem(.session, icon: "sparkles")
                scopeMenuItem(.workingTree, icon: "tray.full")
                scopeMenuItem(.branch, icon: "arrow.triangle.branch")
            } label: {
                HStack(spacing: 3) {
                    Text(isCompactToolbar ? compactScopeTitle : state.diffScope.displayTitle)
                        .font(.system(size: isCompactToolbar ? 10.5 : 11, weight: .medium))
                        .lineLimit(1)
                    Image(systemName: "chevron.down")
                        .font(.system(size: 8.5, weight: .semibold))
                        .foregroundStyle(.secondary)
                }
                .padding(.horizontal, isCompactToolbar ? 6 : 7)
                .padding(.vertical, 2.5)
                .background(
                    RoundedRectangle(cornerRadius: 5, style: .continuous)
                        .fill(Color.primary.opacity(isDark ? 0.08 : 0.06))
                )
            }
            .menuStyle(.borderlessButton)
            .fixedSize()
        }
    }

    private func scopeMenuItem(_ scope: PCodeDiffScope, icon: String) -> some View {
        Button {
            setScope(scope)
        } label: {
            Label(scope.displayTitle, systemImage: state.diffScope == scope ? "checkmark" : icon)
        }
    }

    private func setScope(_ scope: PCodeDiffScope) {
        guard state.diffScope != scope else { return }
        state.diffScope = scope
        resetTransientDiffState()
        onScopeChange?(scope)
    }

    private func resetTransientDiffState() {
        selection = nil
        selectionAnchor = nil
        expandedGaps.removeAll()
        updatedUnseenFiles.removeAll()
        lastModifiedByPath.removeAll()
    }

    private var sessionViewModeControl: some View {
        HStack(spacing: 2) {
            ForEach(PCodeSessionDiffViewMode.allCases, id: \.self) { mode in
                let selected = state.sessionDiffViewMode == mode
                Button {
                    guard state.sessionDiffViewMode != mode else { return }
                    state.sessionDiffViewMode = mode
                    state.focusedFilePath = nil
                    resetTransientDiffState()
                    if state.followAgent {
                        scrollToBottomToken += 1
                    }
                } label: {
                    Text(mode.title)
                        .font(.system(size: 10.5, weight: selected ? .semibold : .medium))
                        .lineLimit(1)
                        .fixedSize(horizontal: true, vertical: false)
                        .foregroundStyle(selected ? .primary : .secondary)
                        .padding(.horizontal, isCompactToolbar ? 5 : 6)
                        .padding(.vertical, 2)
                        .background(
                            RoundedRectangle(cornerRadius: 4, style: .continuous)
                                .fill(selected ? Color.primary.opacity(isDark ? 0.14 : 0.10) : Color.clear)
                        )
                }
                .buttonStyle(.plain)
            }
        }
        .fixedSize(horizontal: true, vertical: false)
        .padding(2)
        .background(
            RoundedRectangle(cornerRadius: 5, style: .continuous)
                .fill(Color.primary.opacity(isDark ? 0.06 : 0.04))
        )
        .help(
            state.sessionDiffViewMode == .stream
                ? "Stream: chronological edits as they arrive (switch to Files to combine per file)"
                : "Files: combined per-file session changes (switch to Stream for live chronological edits)"
        )
    }

    private var fileJumpMenu: some View {
        let isStream = state.diffScope == .session && state.sessionDiffViewMode == .stream
        return Menu {
            if diffs.isEmpty {
                Text(isStream ? "No edits yet" : "No changed files")
            } else {
                ForEach(diffs) { diff in
                    Button {
                        jumpToDiff(diff)
                    } label: {
                        let title = collidingBasenames.contains(diff.basename) && !diff.parentDirectory.isEmpty
                            ? "\(diff.basename) — \(diff.parentDirectory)"
                            : diff.basename
                        Text("[\(diff.kind.badge)] \(title)   +\(diff.added) -\(diff.removed)")
                    }
                }
                Divider()
                Button(isStream ? "Expand All Edits" : "Expand All Files") {
                    state.collapsedFiles.removeAll()
                }
                Button(isStream ? "Collapse All Edits" : "Collapse All Files") {
                    state.collapsedFiles = Set(diffs.map(\.id))
                }
            }
        } label: {
            HStack(spacing: 3) {
                if isVeryCompactToolbar {
                    Image(systemName: "doc.on.doc")
                        .font(.system(size: 9.5, weight: .medium))
                        .foregroundStyle(diffs.isEmpty ? .secondary : .primary)
                    Text("\(diffs.count)")
                        .font(.system(size: 10.5, weight: .medium, design: .monospaced))
                        .lineLimit(1)
                        .foregroundStyle(diffs.isEmpty ? .secondary : .primary)
                } else {
                    Text(
                        isCompactToolbar
                            ? (isStream ? "Edits (\(diffs.count))" : "Files (\(diffs.count))")
                            : (isStream ? "All edits (\(diffs.count))" : "All files (\(diffs.count))")
                    )
                    .font(.system(size: isCompactToolbar ? 10.5 : 11, weight: .medium))
                    .lineLimit(1)
                    .foregroundStyle(diffs.isEmpty ? .secondary : .primary)
                }
                Image(systemName: "chevron.down")
                    .font(.system(size: 8.5, weight: .semibold))
                    .foregroundStyle(.secondary)
            }
            .padding(.horizontal, isCompactToolbar ? 6 : 7)
            .padding(.vertical, 2.5)
            .background(
                RoundedRectangle(cornerRadius: 5, style: .continuous)
                    .fill(Color.primary.opacity(isDark ? 0.06 : 0.04))
            )
        }
        .menuStyle(.borderlessButton)
        .fixedSize()
        .disabled(diffs.isEmpty)
    }

    private var followAgentButton: some View {
        Button {
            state.followAgent.toggle()
            if state.followAgent {
                updatedUnseenFiles.removeAll()
                if state.diffScope == .session {
                    scrollToBottomToken += 1
                } else if let latest = diffs.first {
                    state.focusedFilePath = latest.path
                }
            }
        } label: {
            HStack(spacing: 3) {
                Image(systemName: state.followAgent ? "pin.fill" : "pin")
                    .font(.system(size: 10.5, weight: .medium))
                if !isCompactToolbar {
                    Text("Follow")
                        .font(.system(size: 10.5, weight: .medium))
                        .lineLimit(1)
                        .fixedSize(horizontal: true, vertical: false)
                }
            }
            .padding(.horizontal, 6)
            .padding(.vertical, 3)
            .background(
                RoundedRectangle(cornerRadius: 5, style: .continuous)
                    .fill(state.followAgent ? Color.accentColor.opacity(isDark ? 0.24 : 0.14) : Color.clear)
            )
            .foregroundStyle(state.followAgent ? Color.accentColor : Color.secondary)
        }
        .buttonStyle(.borderless)
        .help(state.followAgent ? "Following incoming edits (click to pause)" : "Paused auto-scroll (click to follow agent edits)")
    }

    private var wrapToggleButton: some View {
        Button {
            state.wrapDiffLines.toggle()
        } label: {
            Image(systemName: "arrow.turn.down.left")
                .font(.system(size: 11, weight: .medium))
                .padding(.horizontal, 5)
                .padding(.vertical, 3)
                .background(
                    RoundedRectangle(cornerRadius: 5, style: .continuous)
                        .fill(state.wrapDiffLines ? Color.accentColor.opacity(isDark ? 0.24 : 0.14) : Color.clear)
                )
                .foregroundStyle(state.wrapDiffLines ? Color.accentColor : Color.secondary)
        }
        .buttonStyle(.borderless)
        .help(state.wrapDiffLines ? "Disable line wrapping" : "Wrap long lines")
        .accessibilityLabel(state.wrapDiffLines ? "Disable line wrapping" : "Wrap long lines")
    }

    private var layoutToggleButton: some View {
        Button {
            guard canUseSideBySide else { return }
            state.diffLayout = (state.diffLayout == .unified) ? .sideBySide : .unified
        } label: {
            Image(systemName: effectiveLayout == .sideBySide ? "rectangle.split.2x1.fill" : "rectangle.split.1x2")
                .font(.system(size: 11, weight: .medium))
                .padding(.horizontal, 5)
                .padding(.vertical, 3)
                .background(
                    RoundedRectangle(cornerRadius: 5, style: .continuous)
                        .fill(effectiveLayout == .sideBySide ? Color.accentColor.opacity(isDark ? 0.24 : 0.14) : Color.clear)
                )
                .foregroundStyle(
                    !canUseSideBySide
                        ? Color.secondary.opacity(0.45)
                        : (effectiveLayout == .sideBySide ? Color.accentColor : Color.secondary)
                )
        }
        .buttonStyle(.borderless)
        .disabled(!canUseSideBySide)
        .help(
            canUseSideBySide
                ? (effectiveLayout == .sideBySide ? "Switch to Unified diff" : "Switch to Side-by-Side diff")
                : "Expand Inspector (⌘⇧F) or widen pane for Side-by-Side diff"
        )
    }

    // MARK: - Main Scrollable Content (Native AppKit NSScrollView)

    private var resolvedDiffTheme: PCodeDiffTheme {
        PCodeDiffTheme.make(
            isDark: isDark,
            surfaceBackground: NSColor(surfaceBackground),
            foreground: foregroundColor,
            codeFont: resolvedCodeFont,
            maxLineNumber: PCodeDiffTheme.maxLineNumber(in: diffs, expandedGaps: expandedGaps)
        )
    }

    private var contentArea: some View {
        Group {
            if diffs.isEmpty {
                emptyStateView
            } else {
                ZStack(alignment: .bottomTrailing) {
                    PCodeNativeDiffScrollView(
                        diffs: diffs,
                        layout: effectiveLayout,
                        wrapLines: state.wrapDiffLines,
                        viewportWidth: containerWidth,
                        collapsedFiles: state.collapsedFiles,
                        expandedGaps: expandedGaps,
                        focusedFilePath: state.focusedFilePath,
                        updatedUnseenFiles: updatedUnseenFiles,
                        copiedPath: copiedPath,
                        selection: selection,
                        followBottom: state.diffScope == .session && state.followAgent,
                        scrollToBottomToken: scrollToBottomToken,
                        scrollToFileRequest: scrollToFileRequest,
                        theme: resolvedDiffTheme,
                        syntaxStore: syntaxStore,
                        onToggleCollapse: { diffID, diffPath in
                            state.toggleCollapsed(filePath: diffID)
                            updatedUnseenFiles.remove(diffPath)
                        },
                        onEditInFilesTab: { diff in
                            let targetPath = diff.absolutePath ?? diff.path
                            let line = (selection?.path == diff.path ? selection?.startLine : nil) ?? diff.firstChangedLine
                            actions.openInFilesTab(targetPath, line, line)
                        },
                        onOpenInEditor: { diff in
                            let targetPath = diff.absolutePath ?? diff.path
                            let line = (selection?.path == diff.path ? selection?.startLine : nil) ?? diff.firstChangedLine
                            actions.openInEditor(targetPath, line)
                        },
                        onCopyPath: { path in
                            actions.copyToClipboard(path)
                            copiedPath = path
                            DispatchQueue.main.asyncAfter(deadline: .now() + 1.4) {
                                if copiedPath == path {
                                    copiedPath = nil
                                }
                            }
                        },
                        onExpandGap: { payload in
                            expandContextGap(payload)
                        },
                        onLineClick: { line, filePath, modifierFlags in
                            handleLineTap(line, filePath: filePath, isShiftHeld: modifierFlags.contains(.shift))
                        },
                        onPrevFile: {
                            navigateFile(direction: -1)
                        },
                        onNextFile: {
                            navigateFile(direction: 1)
                        },
                        onEscape: {
                            guard selection != nil else { return false }
                            clearSelection()
                            return true
                        },
                        onReferenceSelection: {
                            guard selection != nil else { return false }
                            referenceCurrentSelection()
                            return true
                        },
                        onOpenFocusedInEditor: {
                            guard !diffs.isEmpty else { return false }
                            openFocusedOrSelectedInEditor()
                            return true
                        },
                        onUserScrolledAwayFromBottom: {
                            if state.diffScope == .session && state.followAgent {
                                state.followAgent = false
                            }
                        }
                    )
                    .onAppear {
                        if let focused = state.focusedFilePath {
                            requestScrollToFocusedFile(focused, animated: false)
                        } else if state.diffScope == .session && state.followAgent {
                            scrollToBottomToken += 1
                        }
                    }
                    .onChange(of: state.focusedFilePath) { targetPath in
                        if suppressNextFocusScroll {
                            suppressNextFocusScroll = false
                            return
                        }
                        requestScrollToFocusedFile(targetPath, animated: true)
                    }

                    if let selection {
                        floatingSelectionBar(for: selection)
                            .padding(.trailing, 14)
                            .padding(.bottom, 12)
                            .transition(.move(edge: .bottom).combined(with: .opacity))
                    }
                }
                .animation(.easeOut(duration: 0.16), value: selection)
            }
        }
        .onChange(of: state.diffScope) { _ in
            resetTransientDiffState()
        }
        .onChange(of: diffTrackingFingerprint) { _ in
            handleDiffUpdates()
        }
    }

    private func expandContextGap(_ payload: PCodeDiffExpandGapPayload) {
        let key = PCodeDiffExpandedGapKey(fileID: payload.fileID, gapID: payload.gapID)
        let loaded = expandedGaps[key] ?? []
        let newlyLoaded = PCodeDiffParser.expandContext(
            filePath: payload.filePath,
            afterLine: payload.afterLine,
            beforeLine: payload.beforeLine,
            limit: payload.step,
            fromBottom: payload.fromBottom,
            oldLineOffset: payload.oldLineOffset
        )
        guard !newlyLoaded.isEmpty else { return }
        if payload.fromBottom {
            expandedGaps[key] = newlyLoaded + loaded
        } else {
            expandedGaps[key] = loaded + newlyLoaded
        }
    }

    private var emptyStateView: some View {
        VStack(spacing: 8) {
            Image(systemName: isLoading ? "hourglass" : "doc.text.magnifyingglass")
                .font(.system(size: 24, weight: .light))
                .foregroundStyle(.secondary)
            Text(emptyStateTitle)
                .font(.system(size: 12.5, weight: .medium))
                .foregroundStyle(.primary)
            Text(emptyMessage ?? defaultEmptySubtitle)
                .font(.system(size: 11.5))
                .foregroundStyle(.secondary)
                .multilineTextAlignment(.center)
                .frame(maxWidth: 260)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .padding(24)
    }

    private var emptyStateTitle: String {
        if isLoading { return "Loading diffs…" }
        switch state.diffScope {
        case .session: return "No edits in this session yet"
        case .workingTree: return "Working tree is clean"
        case .branch: return "No branch changes"
        case .singleEdit: return "Edit diff unavailable"
        }
    }

    private var defaultEmptySubtitle: String {
        switch state.diffScope {
        case .session: return "File edits made by the agent will stream in here automatically."
        case .workingTree: return "Uncommitted changes and untracked files will appear here."
        case .branch: return "Commits ahead of the merge base and uncommitted edits appear here."
        case .singleEdit: return "Return to This Session to view all session edits."
        }
    }

    // MARK: - Selection & Floating Reference Action

    private func handleLineTap(_ line: PCodeDiffLine, filePath: String, isShiftHeld: Bool) {
        guard let lineNum = line.referenceLineNumber else { return }

        if state.focusedFilePath != filePath {
            suppressNextFocusScroll = true
            state.focusedFilePath = filePath
        }
        updatedUnseenFiles.remove(filePath)

        if isShiftHeld, let anchor = selectionAnchor, anchor.path == filePath {
            selection = PCodeDiffSelection(path: filePath, startLine: anchor.line, endLine: lineNum)
        } else if let current = selection,
                  current.path == filePath,
                  current.startLine == lineNum && current.endLine == lineNum
        {
            clearSelection()
        } else {
            selectionAnchor = (path: filePath, line: lineNum)
            selection = PCodeDiffSelection(path: filePath, startLine: lineNum, endLine: lineNum)
        }
    }

    private func clearSelection() {
        selection = nil
        selectionAnchor = nil
    }

    private func referenceCurrentSelection() {
        guard let selection else { return }
        actions.insertIntoComposer(
            PCodeComposerInsertion.formattedInsertion(reference: selection.promptReference)
        )
    }

    private func openFocusedOrSelectedInEditor() {
        if let selection {
            let match = diffs.first { $0.path == selection.path }
            actions.openInEditor(match?.absolutePath ?? selection.path, selection.startLine)
            return
        }
        if let focused = state.focusedFilePath, let match = diffs.first(where: { $0.path == focused }) {
            actions.openInEditor(match.absolutePath ?? match.path, match.firstChangedLine)
            return
        }
        if let first = diffs.first {
            actions.openInEditor(first.absolutePath ?? first.path, first.firstChangedLine)
        }
    }

    private func floatingSelectionBar(for selection: PCodeDiffSelection) -> some View {
        HStack(spacing: 8) {
            Text(selection.promptReference)
                .font(.system(size: 11, weight: .medium, design: .monospaced))
                .foregroundStyle(.secondary)
                .lineLimit(1)
                .truncationMode(.head)
                .frame(maxWidth: 200, alignment: .leading)

            Button {
                referenceCurrentSelection()
            } label: {
                HStack(spacing: 4) {
                    Image(systemName: "text.quote")
                        .font(.system(size: 10.5))
                    Text("Reference (⌘L)")
                        .font(.system(size: 11, weight: .semibold))
                }
                .padding(.horizontal, 8)
                .padding(.vertical, 4)
                .background(
                    RoundedRectangle(cornerRadius: 6, style: .continuous)
                        .fill(Color.accentColor)
                )
                .foregroundStyle(.white)
            }
            .buttonStyle(.plain)

            Button {
                openFocusedOrSelectedInEditor()
            } label: {
                Image(systemName: "arrow.up.forward.square")
                    .font(.system(size: 11.5))
                    .foregroundStyle(.secondary)
            }
            .buttonStyle(.borderless)
            .help("Open selected line in Editor (⌘O)")

            Button {
                clearSelection()
            } label: {
                Image(systemName: "xmark")
                    .font(.system(size: 10, weight: .semibold))
                    .foregroundStyle(.secondary)
            }
            .buttonStyle(.borderless)
            .help("Clear selection (Esc)")
        }
        .padding(.horizontal, 10)
        .padding(.vertical, 6)
        .background(
            RoundedRectangle(cornerRadius: 9, style: .continuous)
                .fill(.regularMaterial)
                .shadow(color: Color.black.opacity(isDark ? 0.45 : 0.16), radius: 8, y: 3)
        )
        .overlay(
            RoundedRectangle(cornerRadius: 9, style: .continuous)
                .stroke(Color.primary.opacity(isDark ? 0.16 : 0.10), lineWidth: 0.75)
        )
    }

    // MARK: - Navigation & Streaming Update Tracking

    private func requestScrollToFocusedFile(_ targetPath: String?, animated: Bool) {
        guard let targetPath,
              let match = diffs.last(where: { $0.id == targetPath || $0.path == targetPath })
        else { return }
        updatedUnseenFiles.remove(match.path)
        let nextToken = (scrollToFileRequest?.token ?? 0) + 1
        scrollToFileRequest = (id: match.id, token: nextToken, animated: animated)
    }

    private func jumpToDiff(_ diff: PCodeFileDiff) {
        state.collapsedFiles.remove(diff.id)
        updatedUnseenFiles.remove(diff.path)
        if state.focusedFilePath == diff.id {
            requestScrollToFocusedFile(diff.id, animated: true)
        } else {
            state.focusedFilePath = diff.id
        }
    }

    private func navigateFile(direction: Int) {
        let previous = state.focusedFilePath
        let next: String? = direction >= 0
            ? PCodeDiffNavigation.selectNextFile(in: diffs, state: state)
            : PCodeDiffNavigation.selectPreviousFile(in: diffs, state: state)
        guard let next,
              let match = diffs.last(where: { $0.id == next || $0.path == next })
        else { return }
        updatedUnseenFiles.remove(match.path)
        // If focusedFilePath didn't change (e.g. single file), still scroll explicitly;
        // otherwise `.onChange(of: state.focusedFilePath)` triggers the scroll once.
        if previous == state.focusedFilePath {
            requestScrollToFocusedFile(match.id, animated: true)
        }
    }

    /// Lightweight fingerprint capturing incoming hunks, streaming flags, and file modification timestamps.
    private var diffTrackingFingerprint: String {
        diffs.map { diff in
            let hunkCount = diff.hunks.count
            let lineCount = diff.hunks.reduce(0) { $0 + $1.lines.count }
            return "\(diff.id)|\(diff.lastModifiedAt.timeIntervalSinceReferenceDate)|\(diff.isStreaming)|\(hunkCount)|\(lineCount)"
        }.joined(separator: ";")
    }

    private func seedTrackingState() {
        var initialDates: [String: Date] = [:]
        for diff in diffs {
            initialDates[diff.id] = diff.lastModifiedAt
        }
        lastModifiedByPath = initialDates
    }

    private func handleDiffUpdates() {
        var touchedFiles: [PCodeFileDiff] = []

        for diff in diffs {
            let prevDate = lastModifiedByPath[diff.id]
            let fileUpdated = prevDate == nil || (prevDate != nil && diff.lastModifiedAt > prevDate!)
            if fileUpdated || diff.isStreaming {
                touchedFiles.append(diff)
            }
            lastModifiedByPath[diff.id] = diff.lastModifiedAt
        }

        guard let latest = touchedFiles.max(by: { $0.lastModifiedAt < $1.lastModifiedAt }) else {
            return
        }

        if state.followAgent {
            state.collapsedFiles.remove(latest.id)
            updatedUnseenFiles.remove(latest.path)
            if state.diffScope != .session {
                if state.focusedFilePath == latest.path {
                    requestScrollToFocusedFile(latest.id, animated: true)
                } else {
                    state.focusedFilePath = latest.path
                }
            }
        } else {
            // Do NOT yank scroll when Follow is off — mark updated files with a subtle indicator dot.
            for file in touchedFiles {
                updatedUnseenFiles.insert(file.path)
            }
        }
    }
}
