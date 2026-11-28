import AppKit
import Combine
import SwiftUI

/// The native composer's text view: plain text on TextKit 2, with the terminal editor's
/// keys (`pcode.prompt_keys`) for sending; everything else is `NSTextView`'s own
/// macOS and Emacs-style editing.
///
/// | Key | Does |
/// | --- | --- |
/// | Enter | send (`!cmd` runs in the shell, `/cmd` is a command) |
/// | Shift+Enter, Option+Enter, Ctrl+J | newline |
/// | Ctrl+Enter | interrupt the turn and send |
/// | Ctrl+C | clear the draft; with nothing typed, interrupt a busy session |
/// | Ctrl+S | cycle the send mode for the next send (steering, queue, interrupt) |
/// | Tab | open completion (with the menu open: take the selected or first row) |
/// | Cmd+Shift+A | insert a file reference from a picker |
/// | Cmd+Shift+K | show the plan's checklist |
/// | Ctrl+\ | next /btw thread (Escape closes the drawer) |
///
/// Ctrl+D is left to `NSTextView` (delete forward): the terminal's "exit on an empty
/// prompt" has no place in a pane you close with ⌘W, and its busy-cancel is Ctrl+C's.
///
/// Hooks for later phases are the `on…` closures below: each returns whether it handled
/// the event, and the default (nil) leaves it to `NSTextView`.
final class PCodeComposerTextView: NSTextView {
    enum Action: Equatable {
        case submit
        case interruptSubmit
        case newline
        case clearOrCancel
        case cycleSendMode
        /// Up on the first line or Down on the last: history recall.
        case historyPrevious
        case historyNext
        /// While the completion menu is open.
        case menuPrevious
        case menuNext
        case menuAccept
        case menuClose
        /// Cmd+Shift+A: browse the workspace for a file to name.
        case openFilePicker
        /// Tab with the menu closed: open completion, never type a tab.
        case complete
        /// Cmd+Shift+K: show or hide the plan's checklist.
        case toggleTasks
        /// Escape with the menu closed: close the checklist or the /btw drawer, else `NSTextView`'s.
        case escape
        /// Ctrl+\: the next /btw thread, while the drawer shows one.
        case nextThread
    }

    var onAction: ((Action) -> Bool)?
    /// Whether the completion menu is open (it takes Tab, arrows, Escape, Ctrl+L/N/P).
    var isMenuOpen: () -> Bool = { false }
    /// Paste or drop of an image or files (`PCodePastedImage`). True when taken.
    var onPasteImage: ((NSPasteboard) -> Bool)?
    /// Every edit, with the caret: where `@` and `/` completion (phase 3) will listen.
    var onTextChange: ((_ text: String, _ caret: NSRange) -> Void)?
    var placeholder = "" { didSet { needsDisplay = true } }
    var placeholderColor = NSColor.placeholderTextColor { didSet { needsDisplay = true } }
    /// Moved into a window: the session may have asked for focus before it was mounted.
    var onWindowChange: (() -> Void)?

    /// What `event` does in the composer, or nil for `NSTextView`'s own handling.
    static func action(
        keyCode: UInt16, characters: String?, modifiers: NSEvent.ModifierFlags,
        onFirstLine: Bool, onLastLine: Bool, menuOpen: Bool = false
    ) -> Action? {
        let mods = modifiers.intersection([.shift, .control, .option, .command])
        if menuOpen {
            switch (keyCode, mods) {
            case (48, []): return .menuAccept        // Tab
            case (126, []): return .menuPrevious     // Up
            case (125, []): return .menuNext         // Down
            case (53, []): return .menuClose         // Escape
            default: break
            }
            if mods == .control {
                switch characters?.lowercased() {
                case "l": return .menuAccept
                case "p": return .menuPrevious
                case "n": return .menuNext
                default: break
                }
            }
        }
        if mods == [.command, .shift], characters?.lowercased() == "a" { return .openFilePicker }
        if mods == [.command, .shift], characters?.lowercased() == "k" { return .toggleTasks }
        if keyCode == 48, mods.isEmpty { return .complete }
        if keyCode == 53, mods.isEmpty { return .escape }
        let isReturn = keyCode == 36 || keyCode == 76
        if isReturn {
            switch mods {
            case []: return .submit
            case .control: return .interruptSubmit
            case .shift, .option: return .newline
            default: return nil
            }
        }
        if mods == .control {
            switch characters?.lowercased() {
            case "j": return .newline
            case "c": return .clearOrCancel
            case "s": return .cycleSendMode
            case "\\": return .nextThread
            default: return nil
            }
        }
        if mods.isEmpty {
            if keyCode == 126, onFirstLine { return .historyPrevious }
            if keyCode == 125, onLastLine { return .historyNext }
        }
        return nil
    }

    override func keyDown(with event: NSEvent) {
        // An input method composing text gets every key, Return included.
        if !hasMarkedText(),
           let action = Self.action(
               keyCode: event.keyCode,
               characters: event.charactersIgnoringModifiers,
               modifiers: event.modifierFlags,
               onFirstLine: caretOnFirstLine,
               onLastLine: caretOnLastLine,
               menuOpen: isMenuOpen())
        {
            if action == .newline {
                insertNewlineIgnoringFieldEditor(nil)
                return
            }
            if onAction?(action) == true { return }
            if action == .historyPrevious || action == .historyNext || action == .escape || action == .nextThread {
                return super.keyDown(with: event)
            }
            return
        }
        super.keyDown(with: event)
    }

    override func performKeyEquivalent(with event: NSEvent) -> Bool {
        // Ctrl+key chords (and Cmd+Shift+A) arrive here first when a menu item claims them.
        let mods = event.modifierFlags.intersection([.shift, .control, .option, .command])
        if window?.firstResponder === self, !hasMarkedText(),
           mods == .control || mods == [.command, .shift],
           let action = Self.action(
               keyCode: event.keyCode, characters: event.charactersIgnoringModifiers,
               modifiers: event.modifierFlags, onFirstLine: false, onLastLine: false,
               menuOpen: isMenuOpen()),
           action != .newline
        {
            return onAction?(action) == true
        }
        return super.performKeyEquivalent(with: event)
    }

    override func paste(_ sender: Any?) {
        // Text wins whenever there is any (`PCodePastedImage.prefersImage`).
        let board = NSPasteboard.general
        if PCodePastedImage.prefersImage(board), onPasteImage?(board) == true { return }
        pasteAsPlainText(sender)
    }

    /// A plain-text view turns Paste off for an image-only pasteboard, and the menu then
    /// never calls `paste(_:)`: keep it on when the composer can name the image.
    override func validateUserInterfaceItem(_ item: NSValidatedUserInterfaceItem) -> Bool {
        if item.action == #selector(paste(_:)), PCodePastedImage.prefersImage(.general) { return true }
        return super.validateUserInterfaceItem(item)
    }

    /// Images and files can be dropped too (they are named by path).
    override var readablePasteboardTypes: [NSPasteboard.PasteboardType] {
        super.readablePasteboardTypes + [.fileURL, .png, .tiff]
    }

    override func performDragOperation(_ sender: NSDraggingInfo) -> Bool {
        // A dropped file or image is named by path, as the terminal types a dropped file.
        let board = sender.draggingPasteboard
        if PCodePastedImage.canPaste(board), onPasteImage?(board) == true {
            window?.makeFirstResponder(self)
            return true
        }
        return super.performDragOperation(sender)
    }

    override func didChangeText() {
        super.didChangeText()
        needsDisplay = true
        onTextChange?(string, selectedRange())
    }

    /// The caret moved without the text changing (a click, an arrow key).
    var onCaretMove: (() -> Void)?

    override func setSelectedRanges(
        _ ranges: [NSValue], affinity: NSSelectionAffinity, stillSelecting: Bool
    ) {
        super.setSelectedRanges(ranges, affinity: affinity, stillSelecting: stillSelecting)
        if !stillSelecting { onCaretMove?() }
    }

    /// Where the character at `location` starts, in this view's coordinates: the
    /// completion menu hangs from the start of the word it completes.
    func characterOrigin(at location: Int) -> NSPoint? {
        guard let window else { return nil }
        let length = (string as NSString).length
        let range = NSRange(location: max(0, min(location, length)), length: 0)
        let screen = firstRect(forCharacterRange: range, actualRange: nil)
        guard screen != .zero else { return nil }
        let inWindow = window.convertFromScreen(screen)
        return convert(inWindow, from: nil).origin
    }

    override func viewDidMoveToWindow() {
        super.viewDidMoveToWindow()
        onWindowChange?()
    }

    /// The caret's line rect in screen coordinates, for anchoring a completion menu.
    func caretScreenRect() -> NSRect {
        firstRect(forCharacterRange: selectedRange(), actualRange: nil)
    }

    private var caretOnFirstLine: Bool {
        let caret = selectedRange()
        guard caret.length == 0 else { return false }
        return !(string as NSString).substring(to: caret.location).contains("\n") && visualLine(atEnd: false)
    }

    private var caretOnLastLine: Bool {
        let caret = selectedRange()
        guard caret.length == 0 else { return false }
        return !(string as NSString).substring(from: caret.location).contains("\n") && visualLine(atEnd: true)
    }

    /// Whether the caret sits on the first (or last) wrapped line of its paragraph.
    private func visualLine(atEnd: Bool) -> Bool {
        let caret = firstRect(forCharacterRange: selectedRange(), actualRange: nil)
        let edge = NSRange(location: atEnd ? (string as NSString).length : 0, length: 0)
        let target = firstRect(forCharacterRange: edge, actualRange: nil)
        guard caret != .zero, target != .zero else { return true }
        return abs(caret.midY - target.midY) < max(2, caret.height / 2)
    }

    override func draw(_ dirtyRect: NSRect) {
        super.draw(dirtyRect)
        guard string.isEmpty, !placeholder.isEmpty, let font else { return }
        let origin = NSPoint(
            x: textContainerOrigin.x + (textContainer?.lineFragmentPadding ?? 0),
            y: textContainerOrigin.y)
        (placeholder as NSString).draw(at: origin, withAttributes: [.font: font, .foregroundColor: placeholderColor])
    }
}

/// The composer's scroll view and text view, made once per session and kept by it, so the
/// draft survives switching panes (the pane's SwiftUI views are rebuilt each time).
final class PCodeComposerEditorView: NSScrollView {
    let textView: PCodeComposerTextView
    /// The text's own height changed (typing, wrapping, a font change).
    var onContentHeightChange: ((CGFloat) -> Void)?
    private var typographySubscription: AnyCancellable?

    static let verticalInset: CGFloat = 9

    init() {
        textView = PCodeComposerTextView(usingTextLayoutManager: true)
        super.init(frame: NSRect(x: 0, y: 0, width: 400, height: 40))
        drawsBackground = false
        hasVerticalScroller = true
        autohidesScrollers = true
        borderType = .noBorder
        textView.isRichText = false
        textView.importsGraphics = false
        textView.allowsUndo = true
        textView.drawsBackground = false
        textView.isAutomaticQuoteSubstitutionEnabled = false
        textView.isAutomaticDashSubstitutionEnabled = false
        textView.isAutomaticTextReplacementEnabled = false
        textView.isAutomaticSpellingCorrectionEnabled = false
        textView.isContinuousSpellCheckingEnabled = false
        textView.smartInsertDeleteEnabled = false
        textView.isVerticallyResizable = true
        textView.isHorizontallyResizable = false
        textView.autoresizingMask = [.width]
        textView.textContainerInset = NSSize(width: 6, height: Self.verticalInset)
        textView.textContainer?.widthTracksTextView = true
        textView.minSize = NSSize(width: 0, height: 0)
        textView.maxSize = NSSize(width: CGFloat.greatestFiniteMagnitude, height: CGFloat.greatestFiniteMagnitude)
        textView.placeholder = "Message pcode — Enter sends, Shift+Enter adds a line"
        textView.setAccessibilityLabel("Message")
        documentView = textView
        textView.postsFrameChangedNotifications = true
        NotificationCenter.default.addObserver(
            self, selector: #selector(textFrameChanged), name: NSView.frameDidChangeNotification, object: textView)
        applyFont(PCodeTypography.shared.snapshot)
        typographySubscription = PCodeTypography.shared.snapshotPublisher
            .removeDuplicates()
            .receive(on: DispatchQueue.main)
            .sink { [weak self] snapshot in self?.applyFont(snapshot) }
    }

    @available(*, unavailable)
    required init?(coder: NSCoder) { fatalError("init(coder:) is not supported") }

    func applyColors(foreground: NSColor, accent: NSColor) {
        textView.textColor = foreground
        textView.insertionPointColor = accent
        textView.placeholderColor = foreground.withAlphaComponent(0.38)
        textView.typingAttributes[.foregroundColor] = foreground
    }

    private func applyFont(_ snapshot: PCodeTypographySnapshot) {
        let font = snapshot.proseNSFont()
        textView.font = font
        textView.typingAttributes[.font] = font
        reportHeight()
    }

    /// The text's height with its insets: what the composer grows to, up to its cap.
    var contentHeight: CGFloat {
        let lineHeight = (textView.font.map { NSLayoutManager().defaultLineHeight(for: $0) } ?? 18)
        var used = lineHeight
        if let layout = textView.textLayoutManager {
            layout.ensureLayout(for: layout.documentRange)
            used = max(lineHeight, layout.usageBoundsForTextContainer.height)
        }
        return ceil(used + 2 * Self.verticalInset)
    }

    @objc private func textFrameChanged() { reportHeight() }

    func reportHeight() { onContentHeightChange?(contentHeight) }

    override func layout() {
        super.layout()
        reportHeight()
    }
}

/// Mounts a session's editor view in SwiftUI and reports how tall its text wants to be.
struct PCodeComposerEditor: NSViewRepresentable {
    let editor: PCodeComposerEditorView
    let foreground: NSColor
    let accent: NSColor
    @Binding var contentHeight: CGFloat

    func makeNSView(context: Context) -> PCodeComposerEditorView {
        editor.removeFromSuperview()  // Still in the pane view being torn down.
        editor.applyColors(foreground: foreground, accent: accent)
        editor.onContentHeightChange = { height in
            guard abs(height - contentHeight) > 0.5 else { return }
            DispatchQueue.main.async { contentHeight = height }
        }
        DispatchQueue.main.async { editor.reportHeight() }
        return editor
    }

    func updateNSView(_ nsView: PCodeComposerEditorView, context: Context) {
        nsView.applyColors(foreground: foreground, accent: accent)
    }
}

/// A native-composer session pane: the transcript, and under it the composer card with its
/// status strip, trust and failure cards. Mounted on the first frame: nothing here waits
/// for the session host.
struct PCodeNativeSessionPaneContent: View {
    @EnvironmentObject var ghostty: Ghostty.App
    @ObservedObject var pane: PCodeSessionPane
    @ObservedObject var session: PCodeComposerSession
    /// False for a live peek: shows the pane without taking its text view.
    var interactive = true
    @State private var contentHeight: CGFloat = 40
    @State private var footprint: CGFloat = 0

    private static let cornerRadius: CGFloat = 12

    private var theme: PCodeTranscriptTheme {
        PCodeTranscriptTheme.make(
            background: NSColor(ghostty.config.backgroundColor),
            foreground: NSColor(ghostty.config.foregroundColor))
    }

    var body: some View {
        GeometryReader { geometry in
            let horizontal = PCodeDock.horizontalLayout(in: geometry.size.width)
            let maxEditor = max(40, geometry.size.height * 0.4)
            ZStack(alignment: .bottomTrailing) {
                PCodeMainWindowTranscriptView(
                    sessionID: pane.sessionID,
                    hostID: session.hostID,
                    bottomContentInset: footprint,
                    nativePane: pane)
                VStack(spacing: PCodeDock.statusStripSpacing) {
                    cards
                    if let toast = session.toast {
                        Text(toast)
                            .font(.system(size: 11.5, weight: .medium))
                            .foregroundStyle(ghostty.config.foregroundColor.opacity(0.85))
                            .padding(.horizontal, 10)
                            .padding(.vertical, 4)
                            .background(Capsule().fill(ghostty.config.backgroundColor)
                                .shadow(color: .black.opacity(0.25), radius: 6, y: 1))
                            .transition(.opacity)
                    }
                    PCodeComposerStatusStrip(
                        status: session.composerStatus,
                        startedAt: session.phaseStartedAt,
                        onToggleSendMode: interactive ? { session.cycleSendMode() } : nil,
                        onOpenFiles: interactive ? { session.openFilePicker() } : nil,
                        tasks: session.state.plan,
                        tasksShown: interactive ? $session.tasksShown : nil)
                        .padding(.horizontal, PCodeDock.bottomMargin + 4)
                        .overlay(alignment: .bottomLeading) {
                            // Above the strip, outside layout: the transcript never moves.
                            if interactive, session.tasksShown, !session.state.plan.isEmpty {
                                PCodeTaskListCard(items: session.state.plan)
                                    .padding(.leading, PCodeDock.bottomMargin)
                                    .offset(y: -(PCodeDock.statusStripHeight + 6))
                                    .transition(.opacity)
                            }
                        }
                        .zIndex(1)
                    card(height: min(maxEditor, contentHeight))
                        .overlay(alignment: .topLeading) {
                            if interactive {
                                PCodeComposerCompletionLayer(session: session, completion: session.completion)
                            }
                        }
                }
                .padding(.bottom, PCodeDock.bottomMargin)
                .background(GeometryReader { proxy in
                    Color.clear.preference(key: PCodeComposerFootprintKey.self, value: proxy.size.height)
                })
                .frame(width: horizontal.outerWidth)
                .padding(.trailing, horizontal.trailingInset)
            }
            .overlay(alignment: .topTrailing) {
                // The /btw drawer: full height above the composer card, which keeps the keyboard.
                if interactive {
                    PCodeNativeBtwDrawer(
                        drawer: session.btwDrawer,
                        size: CGSize(width: geometry.size.width, height: max(0, geometry.size.height - footprint)))
                }
            }
            .onPreferenceChange(PCodeComposerFootprintKey.self) { footprint = $0 }
            .animation(.easeOut(duration: 0.12), value: contentHeight)
            .animation(.easeOut(duration: 0.15), value: session.toast)
            .overlay {
                if interactive, let picker = session.filePicker {
                    PCodeComposerPickerSheet(picker: picker, size: geometry.size)
                }
            }
        }
    }

    private func card(height: CGFloat) -> some View {
        Group {
            if interactive {
                PCodeComposerEditor(
                    editor: session.editor,
                    foreground: theme.foreground,
                    accent: theme.accent,
                    contentHeight: $contentHeight)
            } else {
                Text(session.editor.textView.string)
                    .font(Font(PCodeTypography.shared.snapshot.proseNSFont()))
                    .foregroundStyle(ghostty.config.foregroundColor)
                    .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
                    .padding(.horizontal, 11)
                    .padding(.vertical, PCodeComposerEditorView.verticalInset)
            }
        }
        .frame(height: height)
        .background(
            RoundedRectangle(cornerRadius: Self.cornerRadius)
                .fill(Color(nsColor: theme.promptBackground))
                .shadow(color: .black.opacity(0.28), radius: 18, x: 0, y: -2))
        .clipShape(RoundedRectangle(cornerRadius: Self.cornerRadius))
        .padding(.horizontal, PCodeDock.bottomMargin)
        .padding(.top, PCodeDock.topMargin)
    }

    /// Startup states the terminal used to print: trust, a failed start, a failed agent.
    @ViewBuilder private var cards: some View {
        switch session.connection {
        case .needsTrust(let key, let code):
            PCodeComposerNotice(
                title: "Trust this repository?",
                detail: "\(key) ships code that runs at launch with your permissions: \(code.joined(separator: ", ")).",
                isError: false,
                actions: [
                    ("Trust", { session.answerTrust(true) }),
                    ("Not now", { session.answerTrust(false) }),
                ])
        case .failed(let message):
            PCodeComposerNotice(
                title: "Session host didn't start",
                detail: message,
                isError: true,
                actions: [("Reopen", { _ = session.requestRelaunch(isManual: true) })])
        default:
            if !session.state.startupError.isEmpty {
                PCodeComposerNotice(
                    title: "Agent startup failed",
                    detail: "\(session.state.startupError)\nChoose another model with /model.",
                    isError: true,
                    actions: [])
            }
        }
    }
}

/// The composer's completion menu, hung above the card from where the completed word
/// starts, in the same rows as the terminal editor's (`PCodeCompletionList`).
private struct PCodeComposerCompletionLayer: View {
    @EnvironmentObject var ghostty: Ghostty.App
    let session: PCodeComposerSession
    @ObservedObject var completion: PCodeComposerCompletion
    @ObservedObject private var typography = PCodeTypography.shared

    var body: some View {
        if completion.isOpen {
            let fonts = PCodePickFonts(typography: typography.snapshot)
            let theme = PCodePickTheme(base: PCodePickTheme.Base(ghostty.config), style: nil, fonts: fonts)
            let rowHeight = ceil(NSLayoutManager().defaultLineHeight(for: fonts.regular)) + 2 * PCodeCompletionLayer.rowPadding
            let size = Self.size(completion.rows, advance: fonts.advance, rowHeight: rowHeight)
            // The card's text starts at its own inset; the list's text lines up with the word.
            // The overlay's origin is the card's outer edge, `bottomMargin` left of the text.
            let x = max(0, PCodeDock.bottomMargin + (session.menuAnchorX() ?? 0)
                - PCodeCompletionLayer.textInset - PCodeCompletionLayer.listPadding)
            PCodeCompletionList(
                rows: completion.rows.map { ($0.display, $0.meta) },
                selected: completion.selected,
                revision: completion.revision,
                theme: theme,
                rowHeight: rowHeight,
                onAccept: { session.takeRow($0) })
                .frame(width: size.width, height: size.height)
                .background(ghostty.config.backgroundColor)
                .clipShape(RoundedRectangle(cornerRadius: PCodeCompletionLayer.cornerRadius))
                .overlay(
                    RoundedRectangle(cornerRadius: PCodeCompletionLayer.cornerRadius)
                        .strokeBorder(ghostty.config.splitDividerColor, lineWidth: 1))
                .shadow(color: .black.opacity(0.35), radius: 10, x: 0, y: 4)
                .environment(\.colorScheme, NSColor(ghostty.config.backgroundColor).isLightColor ? .light : .dark)
                .offset(x: x, y: -size.height - PCodeCompletionLayer.margin)
        }
    }

    /// As `PCodeCompletionLayer.frame`'s width and height, for rows of plain text.
    static func size(_ rows: [PCodeComposerCompletion.Row], advance: CGFloat, rowHeight: CGFloat) -> CGSize {
        let text = PCodeCompletionLayer.columns(rows.map(\.display), max: PCodeCompletionLayer.maxTextColumns) * advance
        let metaColumns = PCodeCompletionLayer.columns(rows.map(\.meta), max: PCodeCompletionLayer.maxMetaColumns)
        let meta = metaColumns > 0 ? 12 + metaColumns * advance * PCodePickFonts.smallScale : 0
        let width = max(text + meta + 2 * (PCodeCompletionLayer.textInset + PCodeCompletionLayer.listPadding) + 4, 160)
        let height = CGFloat(min(rows.count, PCodeCompletionLayer.maxRows)) * rowHeight + 2 * PCodeCompletionLayer.listPadding
        return CGSize(width: width, height: height)
    }
}

/// The `@` file picker, centered over the pane on a dimmed scrim as pcode's popups are.
private struct PCodeComposerPickerSheet: View {
    @EnvironmentObject var ghostty: Ghostty.App
    @ObservedObject var picker: PCodePicker
    let size: CGSize

    var body: some View {
        ZStack {
            Color.black.opacity(0.4)
                .contentShape(Rectangle())
                .onTapGesture { picker.cancel() }
            VStack(spacing: 0) {
                Text("Insert a file reference")
                    .font(.system(size: 12, weight: .medium))
                    .foregroundStyle(ghostty.config.foregroundColor.opacity(PCodePickTheme.mutedOpacity))
                    .padding(.top, 8)
                    .padding(.bottom, 4)
                PCodePickerView(picker: picker, config: ghostty.config, fonts: nil)
            }
            .padding(4)
            .frame(
                width: min(size.width, max(480, size.width * 0.7)),
                height: min(size.height, max(280, size.height * 0.7)))
            .background(ghostty.config.backgroundColor)
            .clipShape(RoundedRectangle(cornerRadius: 10))
            .overlay(RoundedRectangle(cornerRadius: 10).strokeBorder(ghostty.config.splitDividerColor, lineWidth: 1))
            .shadow(color: .black.opacity(0.45), radius: 18, x: 0, y: 6)
            .environment(\.colorScheme, NSColor(ghostty.config.backgroundColor).isLightColor ? .light : .dark)
        }
    }
}

/// A native pane's `/btw` drawer (`PCodeDrawer` driven by the composer), drawn as the
/// terminal path draws it (`PCodeDrawerSurface`) but over the pane above the composer.
private struct PCodeNativeBtwDrawer: View {
    @EnvironmentObject var ghostty: Ghostty.App
    @ObservedObject var drawer: PCodeDrawer
    let size: CGSize
    @State private var dragStart: CGFloat?

    var body: some View {
        ZStack(alignment: .topTrailing) {
            Color.clear.allowsHitTesting(false)
            if let thread = drawer.thread {
                let width = PCodeDrawer.width(in: size.width, fraction: drawer.fraction)
                HStack(spacing: 0) {
                    Rectangle()
                        .fill(ghostty.config.splitDividerColor)
                        .frame(width: 1)
                        .overlay(
                            Color.clear.frame(width: 8).contentShape(Rectangle())
                                .gesture(DragGesture(coordinateSpace: .global)
                                    .onChanged { gesture in
                                        guard size.width > 0 else { return }
                                        let start = dragStart ?? (width / size.width)
                                        dragStart = start
                                        let minFraction = min(1, PCodeDrawer.minWidth / size.width)
                                        drawer.fraction = min(PCodeDrawer.maxFraction,
                                                              max(minFraction, start - gesture.translation.width / size.width))
                                    }
                                    .onEnded { _ in dragStart = nil }))
                    PCodeBtwDrawerContent(
                        model: thread,
                        docked: false,
                        fonts: drawer.fonts,
                        onFocusTerminal: { drawer.focusTerminal() },
                        onClose: { drawer.close() },
                        onContentView: { drawer.contentView = $0 })
                        .id(ObjectIdentifier(thread))
                }
                .frame(width: width, height: size.height)
                .background(ghostty.config.backgroundColor.shadow(color: .black.opacity(0.35), radius: 8, x: -2, y: 2))
                .offset(x: drawer.visible ? 0 : width + 16)
                .opacity(drawer.visible ? 1 : 0)
                .allowsHitTesting(drawer.visible)
            }
        }
        .frame(width: size.width, height: size.height, alignment: .topTrailing)
        .clipped()
        .animation(.easeOut(duration: 0.18), value: drawer.visible)
    }
}

private struct PCodeComposerFootprintKey: PreferenceKey {
    static var defaultValue: CGFloat = 0
    static func reduce(value: inout CGFloat, nextValue: () -> CGFloat) { value = nextValue() }
}

/// A card above the composer for a startup state, with optional buttons.
private struct PCodeComposerNotice: View {
    @EnvironmentObject var ghostty: Ghostty.App
    let title: String
    let detail: String
    let isError: Bool
    let actions: [(String, () -> Void)]

    var body: some View {
        HStack(alignment: .top, spacing: 10) {
            Circle()
                .fill(isError ? Color.red : Color.orange)
                .frame(width: 7, height: 7)
                .padding(.top, 5)
            VStack(alignment: .leading, spacing: 3) {
                Text(title)
                    .font(.system(size: 12, weight: .semibold))
                    .foregroundStyle(ghostty.config.foregroundColor.opacity(0.9))
                Text(detail)
                    .font(.system(size: 11))
                    .foregroundStyle(ghostty.config.foregroundColor.opacity(0.65))
                    .lineLimit(8)
                    .textSelection(.enabled)
            }
            Spacer(minLength: 8)
            ForEach(Array(actions.enumerated()), id: \.offset) { _, action in
                Button(action.0, action: action.1)
                    .buttonStyle(.bordered)
                    .controlSize(.small)
            }
        }
        .padding(.horizontal, 14)
        .padding(.vertical, 9)
        .background(
            RoundedRectangle(cornerRadius: 9)
                .fill(ghostty.config.backgroundColor)
                .shadow(color: .black.opacity(0.22), radius: 10, x: 0, y: 3))
        .overlay(
            RoundedRectangle(cornerRadius: 9)
                .strokeBorder(isError ? Color.red.opacity(0.45) : ghostty.config.splitDividerColor, lineWidth: 1))
        .padding(.horizontal, PCodeDock.bottomMargin)
    }
}
