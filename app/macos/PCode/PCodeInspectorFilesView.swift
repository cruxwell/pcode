import AppKit
import CodeEditLanguages
import CodeEditSourceEditor
import SwiftUI

/// Native Inspector Files tab powered by `CodeEditSourceEditor` (AppKit + Tree-sitter),
/// paired with a collapsible workspace directory tree, open-file tabs, `⌘P` quick-open,
/// live disk-change sync, `⌘L` composer reference injection, and `⌘O` external editor handoff.
struct PCodeInspectorFilesView: View {
    @ObservedObject var state: PCodeInspectorState
    @ObservedObject var store: PCodeInspectorFilesStore
    let actions: PCodeInspectorActions
    let codeFont: NSFont
    let surfaceBackground: Color
    let foregroundColor: NSColor

    @Environment(\.colorScheme) private var colorScheme
    @State private var editorStatesByPath: [String: SourceEditorState] = [:]
    @State private var jumpCoordinator = PCodeSourceEditorJumpCoordinator()
    @State private var lastAppliedJumpToken: UUID?
    @State private var quickOpenQuery: String = ""
    @State private var quickOpenSelectionIndex: Int = 0
    @State private var quickOpenMatches: [String] = []

    private var isDark: Bool {
        colorScheme == .dark
    }

    private var activeBuffer: PCodeOpenFileBuffer? {
        store.activeBuffer
    }

    var body: some View {
        GeometryReader { geometry in
            let showSidebar = store.showFileTree || activeBuffer == nil
            ZStack {
                VStack(spacing: 0) {
                    toolbar
                    Divider().opacity(0.45)

                    if !store.openBuffers.isEmpty {
                        openTabsStrip
                        Divider().opacity(0.35)
                    }

                    if let buffer = activeBuffer, buffer.isMissingOnDisk || buffer.diskChangedConflict {
                        conflictBanner(for: buffer)
                        Divider().opacity(0.35)
                    }

                    HStack(spacing: 0) {
                        if showSidebar {
                            fileTreeSidebar
                                .frame(width: min(210, max(155, geometry.size.width * 0.30)))
                            Divider().opacity(0.45)
                        }

                        editorArea
                            .frame(maxWidth: .infinity, maxHeight: .infinity)
                    }
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
                }
                .background(surfaceBackground)

                if state.isQuickOpenPresented {
                    quickOpenOverlay
                }
            }
            .background(
                PCodeFilesShortcutMonitor(
                    enabled: activeBuffer != nil && !state.isQuickOpenPresented,
                    onSave: { _ = store.saveActiveFile() },
                    onReference: { referenceActiveFileInComposer() },
                    onOpenExternal: { openActiveFileInExternalEditor() }
                )
            )
        }
        .onAppear {
            store.ensureTreeAndIndexLoaded()
            syncJumpIfNeeded()
        }
        .onChange(of: store.activeFilePath) { _ in
            syncJumpIfNeeded()
        }
        .onChange(of: store.openBuffers.map(\.path)) { openPaths in
            let openSet = Set(openPaths)
            editorStatesByPath = editorStatesByPath.filter { openSet.contains($0.key) }
        }
        .onChange(of: activeBuffer?.pendingJumpToken) { _ in
            syncJumpIfNeeded()
        }
        .onChange(of: state.isQuickOpenPresented) { presented in
            if presented {
                quickOpenQuery = ""
                quickOpenSelectionIndex = 0
                store.ensureTreeAndIndexLoaded()
                quickOpenMatches = store.filteredQuickOpenFiles(query: "", limit: 80)
            }
        }
        .onChange(of: store.allProjectFiles.count) { _ in
            if state.isQuickOpenPresented {
                quickOpenMatches = store.filteredQuickOpenFiles(query: quickOpenQuery, limit: 80)
            }
        }
    }

    // MARK: - Toolbar

    private var toolbar: some View {
        HStack(spacing: 6) {
            Button {
                store.showFileTree.toggle()
                if store.showFileTree {
                    store.ensureTreeAndIndexLoaded()
                }
            } label: {
                Image(systemName: "sidebar.left")
                    .font(.system(size: 11.5, weight: .medium))
                    .foregroundStyle(store.showFileTree ? Color.accentColor : .secondary)
                    .frame(width: 22, height: 22)
                    .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .help(store.showFileTree ? "Hide file tree" : "Show file tree")

            if let buffer = activeBuffer {
                HStack(spacing: 5) {
                    Text(buffer.displayPath)
                        .font(.system(size: 11, weight: .medium, design: .monospaced))
                        .foregroundStyle(.primary)
                        .lineLimit(1)
                        .truncationMode(.middle)
                    if buffer.isDirty {
                        Circle()
                            .fill(Color.orange)
                            .frame(width: 6, height: 6)
                            .help("Unsaved changes (⌘S)")
                    }
                }
            } else {
                Text("Files")
                    .font(.system(size: 11, weight: .medium))
                    .foregroundStyle(.secondary)
            }

            Spacer(minLength: 4)

            if let status = store.statusMessage {
                Text(status)
                    .font(.system(size: 10.5))
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
            }

            Button {
                state.isQuickOpenPresented = true
            } label: {
                HStack(spacing: 3) {
                    Image(systemName: "magnifyingglass")
                        .font(.system(size: 10.5, weight: .medium))
                    Text("⌘P")
                        .font(.system(size: 9.5, weight: .medium, design: .monospaced))
                }
                .foregroundStyle(.secondary)
                .padding(.horizontal, 5)
                .padding(.vertical, 3)
                .background(
                    RoundedRectangle(cornerRadius: 4, style: .continuous)
                        .fill(Color.primary.opacity(isDark ? 0.08 : 0.06))
                )
            }
            .buttonStyle(.plain)
            .help("Quick Open file (⌘P)")

            Button {
                store.followReads.toggle()
            } label: {
                Image(systemName: store.followReads ? "eye.fill" : "eye")
                    .font(.system(size: 11, weight: .medium))
                    .foregroundStyle(store.followReads ? Color.accentColor : .secondary)
                    .frame(width: 22, height: 22)
                    .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .help(store.followReads ? "Following read_file calls (click to turn off)" : "Auto-follow read_file tool calls")

            if let buffer = activeBuffer {
                Button {
                    referenceActiveFileInComposer()
                } label: {
                    Image(systemName: "at")
                        .font(.system(size: 11, weight: .medium))
                        .foregroundStyle(.secondary)
                        .frame(width: 22, height: 22)
                        .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
                .help("Reference file/selection in prompt (⌘L)")

                Button {
                    openActiveFileInExternalEditor()
                } label: {
                    Image(systemName: "arrow.up.forward.square")
                        .font(.system(size: 11, weight: .medium))
                        .foregroundStyle(.secondary)
                        .frame(width: 22, height: 22)
                        .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
                .help("Open in External Editor (⌘O)")

                if buffer.canEdit {
                    Button {
                        _ = store.saveFile(path: buffer.path)
                    } label: {
                        Text("Save")
                            .font(.system(size: 10.5, weight: .semibold))
                            .foregroundStyle(buffer.isDirty ? Color.white : Color.secondary)
                            .padding(.horizontal, 7)
                            .padding(.vertical, 3)
                            .background(
                                RoundedRectangle(cornerRadius: 4, style: .continuous)
                                    .fill(buffer.isDirty ? Color.accentColor : Color.primary.opacity(isDark ? 0.08 : 0.06))
                            )
                    }
                    .buttonStyle(.plain)
                    .disabled(!buffer.isDirty)
                    .help("Save file (⌘S)")
                }
            }
        }
        .padding(.horizontal, 8)
        .padding(.vertical, 5)
    }

    // MARK: - Open File Tabs Strip

    private var openTabsStrip: some View {
        ScrollView(.horizontal, showsIndicators: false) {
            HStack(spacing: 2) {
                ForEach(store.openBuffers) { buffer in
                    let isSelected = buffer.path == store.activeFilePath
                    HStack(spacing: 4) {
                        if buffer.isDirty {
                            Circle()
                                .fill(Color.orange)
                                .frame(width: 5, height: 5)
                        }
                        Text(buffer.basename)
                            .font(.system(size: 10.5, weight: isSelected ? .semibold : .regular))
                            .foregroundStyle(isSelected ? .primary : .secondary)
                            .lineLimit(1)

                        Button {
                            store.closeFile(path: buffer.path)
                        } label: {
                            Image(systemName: "xmark")
                                .font(.system(size: 8.5, weight: .semibold))
                                .foregroundStyle(.secondary)
                                .frame(width: 14, height: 14)
                                .contentShape(Rectangle())
                        }
                        .buttonStyle(.plain)
                    }
                    .padding(.horizontal, 7)
                    .padding(.vertical, 4)
                    .background(
                        RoundedRectangle(cornerRadius: 4, style: .continuous)
                            .fill(isSelected ? Color.primary.opacity(isDark ? 0.13 : 0.09) : Color.clear)
                    )
                    .contentShape(Rectangle())
                    .onTapGesture {
                        store.selectFile(path: buffer.path)
                    }
                    .help(buffer.displayPath)
                }
            }
            .padding(.horizontal, 6)
            .padding(.vertical, 3)
        }
    }

    // MARK: - Disk Conflict Banner

    private func conflictBanner(for buffer: PCodeOpenFileBuffer) -> some View {
        HStack(spacing: 8) {
            Image(systemName: "exclamationmark.triangle.fill")
                .font(.system(size: 11))
                .foregroundStyle(.orange)
            Text(
                buffer.isMissingOnDisk
                    ? "File was removed or moved on disk."
                    : "Changed on disk while you have unsaved edits."
            )
            .font(.system(size: 11))
            .foregroundStyle(.primary)
            .lineLimit(1)
            Spacer(minLength: 4)
            if buffer.isMissingOnDisk {
                Button("Save Copy") {
                    _ = store.saveFile(path: buffer.path)
                }
                .buttonStyle(.bordered)
                .controlSize(.small)

                Button("Close") {
                    store.closeFile(path: buffer.path)
                }
                .buttonStyle(.plain)
                .font(.system(size: 11))
                .foregroundStyle(.secondary)
            } else {
                Button("Reload") {
                    store.reloadFile(path: buffer.path)
                }
                .buttonStyle(.bordered)
                .controlSize(.small)

                Button("Keep Mine") {
                    store.dismissConflict(for: buffer.path)
                }
                .buttonStyle(.plain)
                .font(.system(size: 11))
                .foregroundStyle(.secondary)
            }
        }
        .padding(.horizontal, 10)
        .padding(.vertical, 5)
        .background(Color.orange.opacity(isDark ? 0.14 : 0.10))
    }

    // MARK: - File Tree Sidebar

    private var fileTreeSidebar: some View {
        VStack(spacing: 0) {
            HStack(spacing: 4) {
                Text((store.sessionDirectory as NSString).lastPathComponent)
                    .font(.system(size: 10.5, weight: .semibold))
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
                    .truncationMode(.middle)
                Spacer(minLength: 2)
                Button {
                    store.refreshTree()
                    store.refreshFileIndex()
                } label: {
                    Image(systemName: "arrow.clockwise")
                        .font(.system(size: 10, weight: .medium))
                        .foregroundStyle(.secondary)
                        .frame(width: 18, height: 18)
                        .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
                .help("Refresh directory tree")
            }
            .padding(.horizontal, 8)
            .padding(.vertical, 5)

            Divider().opacity(0.35)

            ScrollView(.vertical) {
                LazyVStack(alignment: .leading, spacing: 1) {
                    if !store.recentlyReadPaths.isEmpty {
                        Text("RECENT READS")
                            .font(.system(size: 9, weight: .semibold))
                            .foregroundStyle(.tertiary)
                            .padding(.horizontal, 8)
                            .padding(.top, 6)
                            .padding(.bottom, 2)

                        ForEach(store.recentlyReadPaths.prefix(6), id: \.self) { absPath in
                            let isSelected = absPath == store.activeFilePath
                            HStack(spacing: 5) {
                                Image(systemName: "eye")
                                    .font(.system(size: 9.5))
                                    .foregroundStyle(.secondary)
                                    .frame(width: 12)
                                Text((absPath as NSString).lastPathComponent)
                                    .font(.system(size: 11, weight: isSelected ? .semibold : .regular))
                                    .foregroundStyle(isSelected ? .primary : .secondary)
                                    .lineLimit(1)
                                    .truncationMode(.middle)
                                Spacer(minLength: 0)
                            }
                            .padding(.horizontal, 8)
                            .padding(.vertical, 3)
                            .background(
                                RoundedRectangle(cornerRadius: 4, style: .continuous)
                                    .fill(isSelected ? Color.accentColor.opacity(isDark ? 0.22 : 0.14) : Color.clear)
                            )
                            .contentShape(Rectangle())
                            .onTapGesture {
                                _ = store.openFile(path: absPath)
                            }
                            .help(store.displayPath(for: absPath))
                        }

                        Divider()
                            .opacity(0.3)
                            .padding(.vertical, 4)
                    }

                    ForEach(store.treeEntries) { entry in
                        let isSelected = !entry.isDirectory && entry.path == store.activeFilePath
                        let isExpanded = entry.isDirectory && store.expandedDirectories.contains(entry.path)
                        HStack(spacing: 4) {
                            if entry.isDirectory {
                                Image(systemName: isExpanded ? "chevron.down" : "chevron.right")
                                    .font(.system(size: 8.5, weight: .semibold))
                                    .foregroundStyle(.secondary)
                                    .frame(width: 11)
                                Image(systemName: isExpanded ? "folder.fill" : "folder")
                                    .font(.system(size: 10.5))
                                    .foregroundStyle(.secondary)
                                    .frame(width: 13)
                            } else {
                                Color.clear.frame(width: 11, height: 1)
                                Image(systemName: "doc.text")
                                    .font(.system(size: 10))
                                    .foregroundStyle(isSelected ? Color.accentColor : .secondary)
                                    .frame(width: 13)
                            }

                            Text(entry.name)
                                .font(.system(size: 11, weight: isSelected ? .semibold : .regular))
                                .foregroundStyle(isSelected ? .primary : .secondary)
                                .lineLimit(1)
                                .truncationMode(.middle)
                            Spacer(minLength: 0)
                        }
                        .padding(.leading, CGFloat(entry.depth) * 12 + 6)
                        .padding(.trailing, 6)
                        .padding(.vertical, 3)
                        .background(
                            RoundedRectangle(cornerRadius: 4, style: .continuous)
                                .fill(isSelected ? Color.accentColor.opacity(isDark ? 0.22 : 0.14) : Color.clear)
                        )
                        .contentShape(Rectangle())
                        .onTapGesture {
                            if entry.isDirectory {
                                store.toggleDirectory(entry.path)
                            } else {
                                _ = store.openFile(path: entry.path)
                            }
                        }
                        .help(entry.relativePath)
                    }
                }
                .padding(.vertical, 4)
                .padding(.horizontal, 4)
            }
        }
    }

    // MARK: - Editor Area

    @ViewBuilder
    private var editorArea: some View {
        if let buffer = activeBuffer {
            if let error = buffer.errorMessage {
                unavailableFileView(
                    icon: "exclamationmark.circle",
                    title: buffer.basename,
                    message: error,
                    path: buffer.path
                )
            } else if buffer.isBinary {
                unavailableFileView(
                    icon: "doc.zipper",
                    title: buffer.basename,
                    message: "Binary file (\(ByteCountFormatter.string(fromByteCount: buffer.fileSize, countStyle: .file))).",
                    path: buffer.path
                )
            } else if buffer.isTooLarge {
                unavailableFileView(
                    icon: "doc.badge.ellipsis",
                    title: buffer.basename,
                    message: "File is too large for inline editing (\(ByteCountFormatter.string(fromByteCount: buffer.fileSize, countStyle: .file))).",
                    path: buffer.path
                )
            } else {
                let textBinding = Binding<String>(
                    get: { store.openBuffers.first(where: { $0.path == buffer.path })?.text ?? buffer.text },
                    set: { store.updateText($0, for: buffer.path) }
                )
                let stateBinding = Binding<SourceEditorState>(
                    get: { editorStatesByPath[buffer.path] ?? SourceEditorState() },
                    set: { newValue in
                        editorStatesByPath[buffer.path] = newValue
                        reportSelectionToStore(newValue.cursorPositions, for: buffer.path)
                    }
                )
                let language = CodeLanguage.detectLanguageFrom(url: URL(fileURLWithPath: buffer.path))
                let config = makeEditorConfiguration()

                SourceEditor(
                    textBinding,
                    language: language,
                    configuration: config,
                    state: stateBinding,
                    coordinators: [jumpCoordinator]
                )
                .id("\(buffer.path)#\(store.reloadGeneration)")
                .frame(maxWidth: .infinity, maxHeight: .infinity)
            }
        } else {
            emptyStateView
        }
    }

    private var emptyStateView: some View {
        VStack(spacing: 12) {
            Image(systemName: "doc.text.magnifyingglass")
                .font(.system(size: 28, weight: .light))
                .foregroundStyle(.secondary)

            Text("No file open")
                .font(.system(size: 13, weight: .semibold))
                .foregroundStyle(.primary)

            Text("Pick a file from the directory tree, click any read_file card in the transcript, or press ⌘P to quick-open.")
                .font(.system(size: 11.5))
                .foregroundStyle(.secondary)
                .multilineTextAlignment(.center)
                .frame(maxWidth: 280)

            Button {
                state.isQuickOpenPresented = true
            } label: {
                Label("Open File… (⌘P)", systemImage: "magnifyingglass")
                    .font(.system(size: 11.5, weight: .medium))
            }
            .buttonStyle(.borderedProminent)
            .controlSize(.small)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .padding(20)
    }

    private func unavailableFileView(icon: String, title: String, message: String, path: String) -> some View {
        VStack(spacing: 10) {
            Image(systemName: icon)
                .font(.system(size: 26, weight: .light))
                .foregroundStyle(.secondary)
            Text(title)
                .font(.system(size: 12.5, weight: .semibold))
            Text(message)
                .font(.system(size: 11.5))
                .foregroundStyle(.secondary)
                .multilineTextAlignment(.center)
                .frame(maxWidth: 280)
            Button {
                actions.openInEditor(path, nil)
            } label: {
                Label("Open in External Editor (⌘O)", systemImage: "arrow.up.forward.square")
                    .font(.system(size: 11.5, weight: .medium))
            }
            .buttonStyle(.bordered)
            .controlSize(.small)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .padding(20)
    }

    // MARK: - Quick Open Overlay (⌘P)

    private var quickOpenOverlay: some View {
        let matches = quickOpenMatches
        return ZStack(alignment: .top) {
            Color.black.opacity(isDark ? 0.35 : 0.18)
                .onTapGesture {
                    state.isQuickOpenPresented = false
                }

            VStack(spacing: 0) {
                HStack(spacing: 6) {
                    Image(systemName: "magnifyingglass")
                        .font(.system(size: 12))
                        .foregroundStyle(.secondary)

                    ZStack(alignment: .leading) {
                        if quickOpenQuery.isEmpty {
                            Text("Open file in workspace…")
                                .font(.system(size: 12.5))
                                .foregroundStyle(.secondary)
                                .allowsHitTesting(false)
                        }
                        PCodeQuickOpenSearchField(
                            text: $quickOpenQuery,
                            onMoveUp: {
                                if !matches.isEmpty {
                                    quickOpenSelectionIndex = max(0, quickOpenSelectionIndex - 1)
                                }
                            },
                            onMoveDown: {
                                if !matches.isEmpty {
                                    quickOpenSelectionIndex = min(matches.count - 1, quickOpenSelectionIndex + 1)
                                }
                            },
                            onSubmit: {
                                let idx = min(quickOpenSelectionIndex, max(0, matches.count - 1))
                                if matches.indices.contains(idx) {
                                    _ = store.openFile(path: matches[idx])
                                    state.isQuickOpenPresented = false
                                }
                            },
                            onCancel: {
                                state.isQuickOpenPresented = false
                            }
                        )
                        .frame(height: 20)
                    }

                    if !quickOpenQuery.isEmpty {
                        Button {
                            quickOpenQuery = ""
                        } label: {
                            Image(systemName: "xmark.circle.fill")
                                .font(.system(size: 11))
                                .foregroundStyle(.secondary)
                        }
                        .buttonStyle(.plain)
                    }

                    Button {
                        state.isQuickOpenPresented = false
                    } label: {
                        Text("Esc")
                            .font(.system(size: 9.5, design: .monospaced))
                            .foregroundStyle(.secondary)
                            .padding(.horizontal, 4)
                            .padding(.vertical, 2)
                            .background(
                                RoundedRectangle(cornerRadius: 3)
                                    .fill(Color.primary.opacity(0.08))
                            )
                    }
                    .buttonStyle(.plain)
                }
                .padding(.horizontal, 10)
                .padding(.vertical, 8)

                Divider().opacity(0.4)

                if matches.isEmpty {
                    Text(store.isIndexingFiles ? "Indexing workspace files…" : "No matching files")
                        .font(.system(size: 11.5))
                        .foregroundStyle(.secondary)
                        .padding(16)
                } else {
                    ScrollViewReader { proxy in
                        ScrollView(.vertical) {
                            LazyVStack(alignment: .leading, spacing: 1) {
                                ForEach(Array(matches.enumerated()), id: \.element) { idx, relPath in
                                    let isHighlighted = idx == min(quickOpenSelectionIndex, matches.count - 1)
                                    let basename = (relPath as NSString).lastPathComponent
                                    let dir = (relPath as NSString).deletingLastPathComponent
                                    HStack(spacing: 6) {
                                        Image(systemName: "doc.text")
                                            .font(.system(size: 10.5))
                                            .foregroundStyle(isHighlighted ? Color.accentColor : .secondary)
                                        Text(basename)
                                            .font(.system(size: 11.5, weight: .medium))
                                            .foregroundStyle(.primary)
                                            .lineLimit(1)
                                        if !dir.isEmpty && dir != "." {
                                            Text(dir)
                                                .font(.system(size: 10.5))
                                                .foregroundStyle(.secondary)
                                                .lineLimit(1)
                                                .truncationMode(.head)
                                        }
                                        Spacer(minLength: 0)
                                    }
                                    .padding(.horizontal, 10)
                                    .padding(.vertical, 5)
                                    .background(
                                        RoundedRectangle(cornerRadius: 5, style: .continuous)
                                            .fill(isHighlighted ? Color.accentColor.opacity(isDark ? 0.24 : 0.15) : Color.clear)
                                    )
                                    .contentShape(Rectangle())
                                    .id(idx)
                                    .onTapGesture {
                                        _ = store.openFile(path: relPath)
                                        state.isQuickOpenPresented = false
                                    }
                                }
                            }
                            .padding(4)
                        }
                        .frame(maxHeight: 280)
                        .onChange(of: quickOpenSelectionIndex) { newIndex in
                            if matches.indices.contains(newIndex) {
                                proxy.scrollTo(newIndex, anchor: .center)
                            }
                        }
                    }
                }
            }
            .background(
                RoundedRectangle(cornerRadius: 8, style: .continuous)
                    .fill(surfaceBackground)
                    .overlay(
                        RoundedRectangle(cornerRadius: 8, style: .continuous)
                            .strokeBorder(Color.primary.opacity(0.14), lineWidth: 1)
                    )
                    .shadow(color: Color.black.opacity(0.32), radius: 14, y: 6)
            )
            .padding(.horizontal, 16)
            .padding(.top, 20)
            .frame(maxWidth: 460)
        }
        .onChange(of: quickOpenQuery) { newQuery in
            quickOpenSelectionIndex = 0
            quickOpenMatches = store.filteredQuickOpenFiles(query: newQuery, limit: 80)
        }
    }

    // MARK: - Editor Configuration & Theme

    private func makeEditorConfiguration() -> SourceEditorConfiguration {
        let bgNSColor = NSColor(surfaceBackground)
        let palette = PCodeSyntaxPalette.make(isDark: isDark, foreground: foregroundColor)
        let lineHighlight = (isDark ? NSColor.white : NSColor.black).withAlphaComponent(isDark ? 0.05 : 0.04)
        let selectionColor = NSColor.selectedTextBackgroundColor.withAlphaComponent(isDark ? 0.45 : 0.35)
        let invisiblesColor = foregroundColor.withAlphaComponent(0.28)

        let theme = EditorTheme(
            text: .init(color: palette.plain),
            insertionPoint: foregroundColor,
            invisibles: .init(color: invisiblesColor),
            background: bgNSColor,
            lineHighlight: lineHighlight,
            selection: selectionColor,
            keywords: .init(color: palette.keyword),
            commands: .init(color: palette.function),
            types: .init(color: palette.type),
            attributes: .init(color: palette.attribute),
            variables: .init(color: palette.variable),
            values: .init(color: palette.constant),
            numbers: .init(color: palette.number),
            strings: .init(color: palette.string),
            characters: .init(color: palette.string),
            comments: .init(color: palette.comment)
        )

        let appearance = SourceEditorConfiguration.Appearance(
            theme: theme,
            useThemeBackground: true,
            font: codeFont,
            lineHeightMultiple: 1.32,
            wrapLines: false,
            tabWidth: 4,
            bracketPairEmphasis: .flash
        )

        let peripherals = SourceEditorConfiguration.Peripherals(
            showGutter: true,
            showMinimap: state.isExpanded,
            showReformattingGuide: false,
            showFoldingRibbon: true
        )

        return SourceEditorConfiguration(
            appearance: appearance,
            peripherals: peripherals
        )
    }

    // MARK: - Actions & Selection Sync

    private func syncJumpIfNeeded() {
        guard let buffer = activeBuffer,
              let jumpLine = buffer.pendingJumpLine,
              buffer.pendingJumpToken != lastAppliedJumpToken
        else { return }
        lastAppliedJumpToken = buffer.pendingJumpToken
        let positions: [CursorPosition]
        if let range = buffer.highlightedLineRange, range.upperBound > range.lowerBound {
            positions = [
                CursorPosition(
                    start: CursorPosition.Position(line: range.lowerBound, column: 1),
                    end: CursorPosition.Position(line: range.upperBound, column: 1)
                )
            ]
        } else {
            positions = [CursorPosition(line: jumpLine, column: 1)]
        }
        var currentState = editorStatesByPath[buffer.path] ?? SourceEditorState()
        currentState.cursorPositions = positions
        editorStatesByPath[buffer.path] = currentState
        jumpCoordinator.jump(to: positions)
    }

    private func reportSelectionToStore(_ positions: [CursorPosition]?, for path: String) {
        guard let first = positions?.first,
              first.start.line > 0
        else { return }
        let startLine = first.start.line
        let endLine = max(startLine, first.end?.line ?? startLine)
        store.updateSelection(startLine...endLine, for: path)
    }

    private func referenceActiveFileInComposer() {
        guard let buffer = activeBuffer else { return }
        actions.insertIntoComposer(buffer.composerReference())
    }

    private func openActiveFileInExternalEditor() {
        guard let buffer = activeBuffer else { return }
        actions.openInEditor(buffer.path, buffer.preferredExternalEditorLine)
    }
}

/// AppKit-backed search field for Quick Open (`⌘P`) that immediately grabs first responder
/// from the Ghostty terminal surface and handles ↑/↓/Enter/Esc natively.
private struct PCodeQuickOpenSearchField: NSViewRepresentable {
    @Binding var text: String
    let onMoveUp: () -> Void
    let onMoveDown: () -> Void
    let onSubmit: () -> Void
    let onCancel: () -> Void

    func makeCoordinator() -> Coordinator {
        Coordinator(self)
    }

    func makeNSView(context: Context) -> NSTextField {
        let field = NSTextField(frame: .zero)
        field.isBordered = false
        field.isBezeled = false
        field.drawsBackground = false
        field.focusRingType = .none
        field.font = NSFont.systemFont(ofSize: 12.5)
        field.placeholderString = nil
        field.lineBreakMode = .byTruncatingTail
        field.usesSingleLineMode = true
        field.delegate = context.coordinator
        field.stringValue = text
        DispatchQueue.main.async { [weak field] in
            guard let field, let window = field.window else { return }
            window.makeFirstResponder(field)
        }
        return field
    }

    func updateNSView(_ field: NSTextField, context: Context) {
        context.coordinator.parent = self
        if field.stringValue != text {
            field.stringValue = text
        }
    }

    final class Coordinator: NSObject, NSTextFieldDelegate {
        var parent: PCodeQuickOpenSearchField

        init(_ parent: PCodeQuickOpenSearchField) {
            self.parent = parent
        }

        func controlTextDidChange(_ obj: Notification) {
            guard let field = obj.object as? NSTextField else { return }
            if parent.text != field.stringValue {
                parent.text = field.stringValue
            }
        }

        func control(_ control: NSControl, textView: NSTextView, doCommandBy commandSelector: Selector) -> Bool {
            switch commandSelector {
            case #selector(NSResponder.moveUp(_:)):
                parent.onMoveUp()
                return true
            case #selector(NSResponder.moveDown(_:)):
                parent.onMoveDown()
                return true
            case #selector(NSResponder.insertNewline(_:)):
                parent.onSubmit()
                return true
            case #selector(NSResponder.cancelOperation(_:)):
                parent.onCancel()
                return true
            default:
                return false
            }
        }
    }
}

/// Coordinator that bridges programmatic jump-to-line requests onto `TextViewController`,
/// scrolling the target line range into view immediately on attach or update.
@MainActor
private final class PCodeSourceEditorJumpCoordinator: TextViewCoordinator {
    private weak var controller: TextViewController?
    private var pendingPositions: [CursorPosition]?

    func prepareCoordinator(controller: TextViewController) {
        self.controller = controller
        if let pendingPositions {
            self.pendingPositions = nil
            DispatchQueue.main.async { [weak controller] in
                controller?.setCursorPositions(pendingPositions, scrollToVisible: true)
            }
        }
    }

    func destroy() {
        controller = nil
    }

    func jump(to positions: [CursorPosition]) {
        if let controller {
            controller.setCursorPositions(positions, scrollToVisible: true)
        } else {
            pendingPositions = positions
        }
    }
}

/// Scoped key monitor for `⌘S`, `⌘L`, and `⌘O` that only intercepts events when
/// the window's first responder is inside the Files inspector view hierarchy.
private struct PCodeFilesShortcutMonitor: NSViewRepresentable {
    let enabled: Bool
    let onSave: () -> Void
    let onReference: () -> Void
    let onOpenExternal: () -> Void

    func makeNSView(context: Context) -> MonitorView {
        let view = MonitorView()
        update(view)
        return view
    }

    func updateNSView(_ view: MonitorView, context: Context) {
        update(view)
    }

    static func dismantleNSView(_ view: MonitorView, coordinator: ()) {
        view.teardown()
    }

    private func update(_ view: MonitorView) {
        view.enabled = enabled
        view.onSave = onSave
        view.onReference = onReference
        view.onOpenExternal = onOpenExternal
    }

    final class MonitorView: NSView {
        var enabled = false {
            didSet { syncMonitor() }
        }
        var onSave: (() -> Void)?
        var onReference: (() -> Void)?
        var onOpenExternal: (() -> Void)?
        private var monitor: Any?

        deinit {
            teardown()
        }

        override func viewDidMoveToWindow() {
            super.viewDidMoveToWindow()
            syncMonitor()
        }

        func teardown() {
            if let monitor {
                NSEvent.removeMonitor(monitor)
                self.monitor = nil
            }
        }

        private func isResponderInsideFilesView() -> Bool {
            guard let window,
                  let container = superview,
                  let responder = window.firstResponder as? NSView
            else { return false }
            return responder === container || responder.isDescendant(of: container)
        }

        private func syncMonitor() {
            guard enabled, window != nil else {
                teardown()
                return
            }
            guard monitor == nil else { return }
            monitor = NSEvent.addLocalMonitorForEvents(matching: .keyDown) { [weak self] event in
                guard let self,
                      self.enabled,
                      let window = self.window,
                      event.window === window,
                      event.modifierFlags.intersection([.command, .option, .control, .shift]) == .command,
                      self.isResponderInsideFilesView(),
                      let key = event.charactersIgnoringModifiers?.lowercased()
                else {
                    return event
                }
                switch key {
                case "s":
                    self.onSave?()
                    return nil
                case "l":
                    self.onReference?()
                    return nil
                case "o":
                    self.onOpenExternal?()
                    return nil
                default:
                    return event
                }
            }
        }
    }
}
