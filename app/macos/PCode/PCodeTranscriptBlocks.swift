import AppKit
import CoreGraphics

/// Common interface for virtualized block views pooled by `PCodeTranscriptDocumentView`.
protocol PCodeTranscriptRowHosting: NSView {
    var itemKind: PCodeTranscriptItem.Kind { get }
    /// Primary text view for selection/link hit-testing (if applicable).
    var primaryTextView: PCodeTextKit2BlockView? { get }
    /// Set selection range in this item's local UTF-16 coordinates.
    func setSelectedRange(_ range: NSRange?)
    /// Current selectable text UTF-16 length.
    var selectableTextLength: Int { get }
    /// Update hover state (managed centrally by the document view's single `NSTrackingArea`).
    func setHovered(_ hovered: Bool)
    /// Reset transient state (scroll offsets, in-flight tasks, copy flash, selection) before pooling.
    func resetForTranscriptReuse()
    /// Apply a compositor-only top-to-bottom curtain reveal mask (`nil` = fully revealed, `0...1` = revealing).
    func setCurtainRevealProgress(_ progress: CGFloat?)
}

extension PCodeTranscriptRowHosting {
    func setHovered(_ hovered: Bool) {}

    func resetForTranscriptReuse() {
        setHovered(false)
        setSelectedRange(nil)
        setCurtainRevealProgress(nil)
        primaryTextView?.inkSweepFrontier = nil
        layer?.removeAllAnimations()
    }

    func setCurtainRevealProgress(_ progress: CGFloat?) {
        guard let layer else { return }
        guard let progress else {
            if layer.mask != nil {
                CATransaction.begin()
                CATransaction.setDisableActions(true)
                layer.mask = nil
                CATransaction.commit()
            }
            return
        }

        let clamped = min(max(0, progress), 1)
        if clamped >= 1 {
            setCurtainRevealProgress(nil)
            return
        }

        let maskLayer: CAGradientLayer
        if let existing = layer.mask as? CAGradientLayer {
            maskLayer = existing
        } else {
            let created = CAGradientLayer()
            created.colors = [
                NSColor.white.withAlphaComponent(1.0).cgColor,
                NSColor.white.withAlphaComponent(1.0).cgColor,
                NSColor.white.withAlphaComponent(0.0).cgColor,
            ]
            created.startPoint = CGPoint(x: 0.5, y: 0.0)
            created.endPoint = CGPoint(x: 0.5, y: 1.0)
            layer.mask = created
            maskLayer = created
        }

        let h = max(1, bounds.height)
        // Soft 1.5-row (~30pt) feather matching Ghostty Smooth's curtain reveal.
        let featherFrac = min(0.45, 30.0 / h)
        let lead = min(1.0, clamped * (1.0 + featherFrac))
        let solidEnd = max(0.0, lead - featherFrac)

        CATransaction.begin()
        CATransaction.setDisableActions(true)
        maskLayer.frame = bounds
        maskLayer.locations = [
            NSNumber(value: 0.0),
            NSNumber(value: Double(solidEnd)),
            NSNumber(value: Double(lead)),
        ]
        CATransaction.commit()
    }
}

/// Inner horizontal `NSScrollView` for code blocks and wide tables.
///
/// - Forces `.overlay` scroller style so legacy scrollers never alter row heights.
/// - Latches gesture axis across finger lift + momentum phase and forwards vertical-dominant
///   scrolls to the outer `PCodeTranscriptScrollView` so vertical scrolling never gets trapped.
/// - Forwards mouse selection drags to `transcriptDocumentView` so cross-item selection
///   survives even if this row recycles offscreen during autoscroll.
final class PCodeHorizontalBlockScrollView: NSScrollView {
    weak var transcriptDocumentView: PCodeTranscriptDocumentView?

    private enum GestureAxis {
        case undecided
        case horizontal
        case vertical
    }

    private var gestureAxis: GestureAxis = .undecided

    override init(frame frameRect: NSRect) {
        super.init(frame: frameRect)
        scrollerStyle = .overlay
        hasHorizontalScroller = true
        hasVerticalScroller = false
        autohidesScrollers = true
        drawsBackground = false
        borderType = .noBorder
    }

    @available(*, unavailable)
    required init?(coder: NSCoder) {
        fatalError("init(coder:) has not been implemented")
    }

    override var scrollerStyle: NSScroller.Style {
        get { .overlay }
        set { super.scrollerStyle = .overlay }
    }

    private var canScrollHorizontally: Bool {
        guard let doc = documentView else { return false }
        return doc.frame.width > contentView.bounds.width + 1
    }

    func resetScrollOrigin() {
        gestureAxis = .undecided
        if contentView.bounds.origin != .zero {
            contentView.scroll(to: .zero)
            reflectScrolledClipView(contentView)
        }
    }

    override func scrollWheel(with event: NSEvent) {
        let phase = event.phase
        let momentum = event.momentumPhase

        if !canScrollHorizontally {
            gestureAxis = .undecided
            forwardScrollToOuter(event)
            return
        }

        // Wait for real deltas on `.mayBegin` instead of prematurely forwarding to outer.
        if phase == .mayBegin {
            gestureAxis = .undecided
            return
        }

        if phase == .began || (phase == [] && momentum == []) {
            gestureAxis = classifyAxis(dx: event.scrollingDeltaX, dy: event.scrollingDeltaY)
        } else if gestureAxis == .undecided {
            gestureAxis = classifyAxis(dx: event.scrollingDeltaX, dy: event.scrollingDeltaY)
        }

        let currentAxis = gestureAxis
        // Keep axis latched across `phase == .ended` so horizontal trackpad momentum stays horizontal.
        if momentum == .ended || momentum == .cancelled || phase == .cancelled {
            gestureAxis = .undecided
        }

        if currentAxis == .horizontal {
            super.scrollWheel(with: event)
        } else if currentAxis == .vertical {
            forwardScrollToOuter(event)
        }
    }

    private func classifyAxis(dx: CGFloat, dy: CGFloat) -> GestureAxis {
        let adx = abs(dx)
        let ady = abs(dy)
        if adx > ady * 1.15 && adx > 0.1 {
            return .horizontal
        }
        if ady > 0.1 {
            return .vertical
        }
        return .undecided
    }

    private func forwardScrollToOuter(_ event: NSEvent) {
        if let outer = transcriptDocumentView?.enclosingScrollView {
            outer.scrollWheel(with: event)
        } else {
            nextResponder?.scrollWheel(with: event)
        }
    }

    override func mouseDown(with event: NSEvent) {
        transcriptDocumentView?.mouseDown(with: event)
    }

    override func mouseDragged(with event: NSEvent) {
        transcriptDocumentView?.mouseDragged(with: event)
    }

    override func mouseUp(with event: NSEvent) {
        transcriptDocumentView?.mouseUp(with: event)
    }

    override func menu(for event: NSEvent) -> NSMenu? {
        transcriptDocumentView?.menu(for: event)
    }
}

/// Small, borderless action button used for "Copy" / "Copy Markdown" inside transcript blocks.
final class PCodeBlockActionButton: NSButton {
    private var resetWork: DispatchWorkItem?
    private var idleTitle: String
    private var idleSymbol: String
    private var idleAccessibilityTitle: String
    private var symbolPointSize: CGFloat
    private var isShowingCopied = false
    var onVisualStateChanged: (() -> Void)?

    init(
        title: String,
        symbol: String,
        accessibilityTitle: String? = nil,
        symbolPointSize: CGFloat = 10
    ) {
        self.idleTitle = title
        self.idleSymbol = symbol
        self.idleAccessibilityTitle = accessibilityTitle ?? title
        self.symbolPointSize = symbolPointSize
        super.init(frame: .zero)
        isBordered = false
        bezelStyle = .inline
        imagePosition = title.isEmpty ? .imageOnly : .imageLeading
        wantsLayer = true
        layer?.cornerRadius = 4
        applyIdleState()
    }

    @available(*, unavailable)
    required init?(coder: NSCoder) {
        fatalError("init(coder:) has not been implemented")
    }

    var isFlashingCopied: Bool { isShowingCopied }

    func applyTheme(foreground: NSColor, background: NSColor) {
        contentTintColor = foreground
        layer?.backgroundColor = background.cgColor
        if !isShowingCopied {
            applyIdleState()
        }
    }

    func flashCopied() {
        resetWork?.cancel()
        isShowingCopied = true
        let flashText = idleTitle.isEmpty ? "" : "Copied"
        setTitle(flashText, symbol: "checkmark", accessibilityTitle: "Copied", color: .systemGreen)
        onVisualStateChanged?()
        let work = DispatchWorkItem { [weak self] in
            self?.isShowingCopied = false
            self?.applyIdleState()
            self?.onVisualStateChanged?()
        }
        resetWork = work
        DispatchQueue.main.asyncAfter(deadline: .now() + 1.2, execute: work)
    }

    func cancelCopiedFlash() {
        resetWork?.cancel()
        resetWork = nil
        if isShowingCopied {
            isShowingCopied = false
            applyIdleState()
            onVisualStateChanged?()
        }
    }

    private func applyIdleState() {
        setTitle(
            idleTitle,
            symbol: idleSymbol,
            accessibilityTitle: idleAccessibilityTitle,
            color: contentTintColor ?? .secondaryLabelColor)
    }

    private func setTitle(_ text: String, symbol: String, accessibilityTitle: String, color: NSColor) {
        let cfg = NSImage.SymbolConfiguration(pointSize: symbolPointSize, weight: .medium)
        imagePosition = text.isEmpty ? .imageOnly : .imageLeading
        image = NSImage(systemSymbolName: symbol, accessibilityDescription: accessibilityTitle)?
            .withSymbolConfiguration(cfg)
        attributedTitle = NSAttributedString(
            string: text.isEmpty ? "" : " " + text,
            attributes: [
                .font: NSFont.systemFont(ofSize: 11, weight: .medium),
                .foregroundColor: color,
            ])
        sizeToFit()
    }
}

/// Position of a settled, collapsed tool row inside a run of consecutive settled tool rows.
/// Consecutive settled rows share one continuous outer silhouette instead of stacking
/// separate rounded pills.
enum PCodeToolStackPosition: Equatable {
    case standalone
    case top
    case middle
    case bottom

    /// Corners to round on a flipped (`isFlipped == true`) NSView's backing CALayer.
    var maskedCorners: CACornerMask {
        switch self {
        case .standalone:
            return [.layerMinXMinYCorner, .layerMaxXMinYCorner, .layerMinXMaxYCorner, .layerMaxXMaxYCorner]
        case .top:
            return [.layerMinXMinYCorner, .layerMaxXMinYCorner]
        case .middle:
            return []
        case .bottom:
            return [.layerMinXMaxYCorner, .layerMaxXMaxYCorner]
        }
    }
}

// MARK: - Prompt Row

final class PCodePromptRowView: NSView, PCodeTranscriptRowHosting {
    let itemKind: PCodeTranscriptItem.Kind = .prompt
    let textView = PCodeTextKit2BlockView()
    private let surfaceLayer = CALayer()
    private let topDivider = CALayer()
    private let bottomDivider = CALayer()
    private let accentBar = NSView()
    private var lastTheme: PCodeTranscriptTheme?
    private var lastIsSide = false
    private var lastStyle: PCodePromptStyle = .rightPill

    var primaryTextView: PCodeTextKit2BlockView? { textView }
    var selectableTextLength: Int { textView.textContentStorage.attributedString?.length ?? 0 }
    var hasVisibleSurface: Bool { !surfaceLayer.isHidden && surfaceLayer.backgroundColor != nil }
    override var isFlipped: Bool { true }

    override init(frame frameRect: NSRect) {
        super.init(frame: frameRect)
        wantsLayer = true
        layer?.addSublayer(surfaceLayer)
        layer?.addSublayer(topDivider)
        layer?.addSublayer(bottomDivider)

        accentBar.wantsLayer = true
        accentBar.layer?.cornerRadius = 1.5
        addSubview(accentBar)

        addSubview(textView)
    }

    @available(*, unavailable)
    required init?(coder: NSCoder) {
        fatalError("init(coder:) has not been implemented")
    }

    func setSelectedRange(_ range: NSRange?) {
        textView.selectedRange = range
    }

    override func viewDidChangeEffectiveAppearance() {
        super.viewDidChangeEffectiveAppearance()
        applyLayerColors()
    }

    func configure(
        attributedText: NSAttributedString,
        isSidePrompt: Bool,
        style: PCodePromptStyle,
        width: CGFloat,
        theme: PCodeTranscriptTheme,
        selectedRange: NSRange?,
        contentInsetX: CGFloat = 0
    ) -> CGFloat {
        lastTheme = theme
        lastIsSide = isSidePrompt
        lastStyle = style

        let totalHeight: CGFloat
        var surfaceRect: CGRect = .zero
        var topRuleRect: CGRect = .zero
        var bottomRuleRect: CGRect = .zero

        switch style {
        case .rightPill:
            let padX: CGFloat = min(15, floor(width * 0.25))
            let padY: CGFloat = 10
            let maxPillWidth = min(width, max(200, floor(width * 0.72)))
            let maxTextWidth = max(1, maxPillWidth - padX * 2)
            let naturalWidth = ceil(attributedText.boundingRect(
                with: CGSize(width: maxTextWidth, height: .greatestFiniteMagnitude),
                options: [.usesLineFragmentOrigin, .usesFontLeading]).width) + 4
            let textWidth = max(1, min(maxTextWidth, naturalWidth))
            let pillWidth = min(width, textWidth + padX * 2)
            let pillX = max(0, width - pillWidth)
            let textHeight = textView.configure(attributedText: attributedText, width: textWidth)
            textView.selectedRange = selectedRange
            textView.frame = CGRect(x: pillX + padX, y: padY, width: textWidth, height: textHeight)
            accentBar.isHidden = true
            accentBar.frame = .zero
            totalHeight = textHeight + padY * 2
            surfaceRect = CGRect(x: pillX, y: 0, width: pillWidth, height: totalHeight)

        case .leftWash:
            let padX: CGFloat = min(15, floor(width * 0.25))
            let padY: CGFloat = 10
            let maxTextWidth = max(1, width - padX * 2)
            let naturalWidth = ceil(attributedText.boundingRect(
                with: CGSize(width: maxTextWidth, height: .greatestFiniteMagnitude),
                options: [.usesLineFragmentOrigin, .usesFontLeading]).width) + 4
            let textWidth = max(1, min(maxTextWidth, naturalWidth))
            let pillWidth = min(width, textWidth + padX * 2)
            let textHeight = textView.configure(attributedText: attributedText, width: textWidth)
            textView.selectedRange = selectedRange
            textView.frame = CGRect(x: padX, y: padY, width: textWidth, height: textHeight)
            accentBar.isHidden = true
            accentBar.frame = .zero
            totalHeight = textHeight + padY * 2
            surfaceRect = CGRect(x: 0, y: 0, width: pillWidth, height: totalHeight)

        case .band:
            let topPad: CGFloat = 11
            let bottomPad: CGFloat = 11
            let columnX = max(0, contentInsetX)
            let columnWidth = max(1, width - columnX * 2)
            let textWidth = max(1, columnWidth)
            let textHeight = textView.configure(attributedText: attributedText, width: textWidth)
            textView.selectedRange = selectedRange
            textView.frame = CGRect(x: columnX, y: topPad, width: textWidth, height: textHeight)
            accentBar.isHidden = true
            accentBar.frame = .zero
            totalHeight = textHeight + topPad + bottomPad
            surfaceRect = CGRect(x: 0, y: 0, width: width, height: totalHeight)
            topRuleRect = CGRect(x: 0, y: 0, width: width, height: 1)
            bottomRuleRect = CGRect(x: 0, y: max(0, totalHeight - 1), width: width, height: 1)

        case .bar:
            let topPad: CGFloat = 3
            let bottomPad: CGFloat = 3
            let textX: CGFloat = min(15, floor(width * 0.25))
            let textWidth = max(1, width - textX - 4)
            let textHeight = textView.configure(attributedText: attributedText, width: textWidth)
            textView.selectedRange = selectedRange
            textView.frame = CGRect(x: textX, y: topPad, width: textWidth, height: textHeight)
            accentBar.isHidden = false
            accentBar.frame = CGRect(x: 2, y: topPad + 1, width: 3, height: max(14, textHeight - 2))
            totalHeight = textHeight + topPad + bottomPad
        }

        CATransaction.begin()
        CATransaction.setDisableActions(true)
        surfaceLayer.frame = surfaceRect
        surfaceLayer.isHidden = surfaceRect.isEmpty
        topDivider.frame = topRuleRect
        topDivider.isHidden = topRuleRect.isEmpty
        bottomDivider.frame = bottomRuleRect
        bottomDivider.isHidden = bottomRuleRect.isEmpty
        CATransaction.commit()

        applyLayerColors()
        return totalHeight
    }

    private func applyLayerColors() {
        guard let theme = lastTheme else { return }
        effectiveAppearance.performAsCurrentDrawingAppearance {
            let accent = lastIsSide ? NSColor.systemOrange : theme.accent
            accentBar.layer?.backgroundColor = accent.cgColor

            CATransaction.begin()
            CATransaction.setDisableActions(true)
            let hairline = theme.foreground.withAlphaComponent(theme.isDark ? 0.09 : 0.08).cgColor
            topDivider.backgroundColor = hairline
            bottomDivider.backgroundColor = hairline

            layer?.backgroundColor = nil
            layer?.cornerRadius = 0
            layer?.borderWidth = 0
            layer?.borderColor = nil

            switch lastStyle {
            case .rightPill:
                let fill: NSColor = lastIsSide
                    ? NSColor.systemOrange.withAlphaComponent(theme.isDark ? 0.14 : 0.09)
                    : theme.foreground.withAlphaComponent(theme.isDark ? 0.085 : 0.058)
                surfaceLayer.backgroundColor = fill.cgColor
                surfaceLayer.cornerRadius = 14
                surfaceLayer.borderWidth = 0
                surfaceLayer.borderColor = nil
            case .leftWash:
                let fill: NSColor = lastIsSide
                    ? NSColor.systemOrange.withAlphaComponent(theme.isDark ? 0.14 : 0.09)
                    : theme.foreground.withAlphaComponent(theme.isDark ? 0.075 : 0.052)
                surfaceLayer.backgroundColor = fill.cgColor
                surfaceLayer.cornerRadius = 11
                surfaceLayer.borderWidth = 0
                surfaceLayer.borderColor = nil
            case .band:
                let fill: NSColor = lastIsSide
                    ? NSColor.systemOrange.withAlphaComponent(theme.isDark ? 0.11 : 0.075)
                    : theme.foreground.withAlphaComponent(theme.isDark ? 0.045 : 0.036)
                surfaceLayer.backgroundColor = fill.cgColor
                surfaceLayer.cornerRadius = 0
                surfaceLayer.borderWidth = 0
                surfaceLayer.borderColor = nil
            case .bar:
                surfaceLayer.backgroundColor = nil
                surfaceLayer.cornerRadius = 0
                surfaceLayer.borderWidth = 0
                surfaceLayer.borderColor = nil
            }
            CATransaction.commit()
        }
    }
}

// MARK: - Prose Row (TextKit 2 Markdown + Turn-End Inline Copy Icon)

final class PCodeProseRowView: NSView, PCodeTranscriptRowHosting {
    private static let idleButtonWidth: CGFloat = 24
    private static let buttonHeight: CGFloat = 20
    private static let buttonRightMargin: CGFloat = 4
    private static let minInlineGap: CGFloat = 10

    let itemKind: PCodeTranscriptItem.Kind = .prose
    let textView = PCodeTextKit2BlockView()
    let copyButton = PCodeBlockActionButton(
        title: "",
        symbol: "doc.on.doc",
        accessibilityTitle: "Copy answer as Markdown",
        symbolPointSize: 11)
    private var copyPayload: String?
    private var showsCopyButton = false
    /// Where the button puts the answer; the document view passes its own.
    var pasteboard: NSPasteboard = .general
    private var lastTheme: PCodeTranscriptTheme?
    private var lastRowWidth: CGFloat = 0
    private var lastButtonY: CGFloat = 0
    private var isButtonHovered = false
    private var buttonTrackingArea: NSTrackingArea?

    var primaryTextView: PCodeTextKit2BlockView? { textView }
    var selectableTextLength: Int { textView.textContentStorage.attributedString?.length ?? 0 }
    override var isFlipped: Bool { true }

    override init(frame frameRect: NSRect) {
        super.init(frame: frameRect)
        wantsLayer = true
        addSubview(textView)
        copyButton.isHidden = true
        copyButton.toolTip = "Copy answer as Markdown"
        copyButton.target = self
        copyButton.action = #selector(copyMarkdownPressed)
        copyButton.layer?.cornerRadius = 5
        copyButton.onVisualStateChanged = { [weak self] in
            self?.layoutCopyButton()
            self?.applyButtonAppearance()
        }
        addSubview(copyButton)
    }

    @available(*, unavailable)
    required init?(coder: NSCoder) {
        fatalError("init(coder:) has not been implemented")
    }

    func setSelectedRange(_ range: NSRange?) {
        textView.selectedRange = range
    }

    func setButtonHovered(_ hovered: Bool) {
        guard isButtonHovered != hovered else { return }
        isButtonHovered = hovered
        applyButtonAppearance()
    }

    override func updateTrackingAreas() {
        super.updateTrackingAreas()
        if let buttonTrackingArea { removeTrackingArea(buttonTrackingArea) }
        buttonTrackingArea = nil
        guard !copyButton.isHidden, !copyButton.frame.isEmpty else { return }
        let area = NSTrackingArea(
            rect: copyButton.frame,
            options: [.activeInKeyWindow, .mouseEnteredAndExited],
            owner: self,
            userInfo: nil)
        addTrackingArea(area)
        buttonTrackingArea = area
    }

    override func mouseEntered(with event: NSEvent) {
        setButtonHovered(true)
    }

    override func mouseExited(with event: NSEvent) {
        setButtonHovered(false)
    }

    func resetForTranscriptReuse() {
        isButtonHovered = false
        showsCopyButton = false
        copyPayload = nil
        copyButton.isHidden = true
        copyButton.frame = .zero
        copyButton.cancelCopiedFlash()
        if let buttonTrackingArea {
            removeTrackingArea(buttonTrackingArea)
            self.buttonTrackingArea = nil
        }
        setSelectedRange(nil)
        setCurtainRevealProgress(nil)
        textView.inkSweepFrontier = nil
        layer?.removeAllAnimations()
    }

    override func viewDidChangeEffectiveAppearance() {
        super.viewDidChangeEffectiveAppearance()
        applyButtonAppearance()
    }

    func configure(
        attributedText: NSAttributedString,
        copyMarkdown: String?,
        showCopyButton: Bool,
        width: CGFloat,
        theme: PCodeTranscriptTheme,
        selectedRange: NSRange?
    ) -> CGFloat {
        let insets = NSEdgeInsets(top: 2, left: 4, bottom: 2, right: 4)
        let textWidth = max(1, width - insets.left - insets.right)
        let textHeight = textView.configure(attributedText: attributedText, width: textWidth)
        textView.selectedRange = selectedRange
        textView.frame = CGRect(x: insets.left, y: insets.top, width: textWidth, height: textHeight)

        copyPayload = copyMarkdown
        lastTheme = theme
        lastRowWidth = width
        let shouldShow = showCopyButton && (copyMarkdown?.isEmpty == false)
        showsCopyButton = shouldShow
        copyButton.isHidden = !shouldShow

        guard shouldShow else {
            copyButton.frame = .zero
            if let buttonTrackingArea {
                removeTrackingArea(buttonTrackingArea)
                self.buttonTrackingArea = nil
            }
            return textHeight + insets.top + insets.bottom
        }

        let lastLine = textView.lastLineFrame
        let lastLineMaxXInRow = insets.left + lastLine.maxX
        let fitsOnLastLine = !lastLine.isEmpty
            && (lastLineMaxXInRow + Self.minInlineGap + Self.idleButtonWidth <= width - Self.buttonRightMargin)

        let extraHeight: CGFloat
        if fitsOnLastLine {
            lastButtonY = max(0, insets.top + lastLine.midY - Self.buttonHeight * 0.5)
            extraHeight = 0
        } else {
            lastButtonY = insets.top + textHeight + 2
            extraHeight = Self.buttonHeight + 2
        }

        applyButtonAppearance()
        layoutCopyButton()
        return textHeight + insets.top + insets.bottom + extraHeight
    }

    private func layoutCopyButton() {
        guard showsCopyButton else { return }
        let btnX = max(4, lastRowWidth - Self.idleButtonWidth - Self.buttonRightMargin)
        let nextFrame = CGRect(x: btnX, y: lastButtonY, width: Self.idleButtonWidth, height: Self.buttonHeight)
        if copyButton.frame != nextFrame || buttonTrackingArea == nil {
            copyButton.frame = nextFrame
            updateTrackingAreas()
        }
    }

    private func applyButtonAppearance() {
        guard showsCopyButton, let theme = lastTheme else { return }
        let active = isButtonHovered || copyButton.isFlashingCopied
        effectiveAppearance.performAsCurrentDrawingAppearance {
            let bg: NSColor = isButtonHovered
                ? theme.foreground.withAlphaComponent(theme.isDark ? 0.08 : 0.06)
                : .clear
            copyButton.applyTheme(foreground: theme.secondaryForeground, background: bg)
        }
        copyButton.alphaValue = active ? 1.0 : 0.55
    }

    @objc private func copyMarkdownPressed() {
        guard let copyPayload, !copyPayload.isEmpty else { return }
        pasteboard.clearContents()
        pasteboard.setString(copyPayload, forType: .string)
        copyButton.flashCopied()
    }
}

// MARK: - Answer Actions Footer Row

final class PCodeAnswerActionsRowView: NSView, PCodeTranscriptRowHosting {
    static let rowHeight: CGFloat = 22
    private static let idleButtonWidth: CGFloat = 24
    private static let buttonHeight: CGFloat = 20

    let itemKind: PCodeTranscriptItem.Kind = .answerActions
    let copyButton = PCodeBlockActionButton(
        title: "",
        symbol: "doc.on.doc",
        accessibilityTitle: "Copy answer as Markdown",
        symbolPointSize: 11)
    private var copyPayload: String = ""
    /// Where the button puts the answer; the document view passes its own.
    var pasteboard: NSPasteboard = .general
    private var lastTheme: PCodeTranscriptTheme?
    private var lastRowWidth: CGFloat = 0
    private var isButtonHovered = false
    private var buttonTrackingArea: NSTrackingArea?

    var primaryTextView: PCodeTextKit2BlockView? { nil }
    var selectableTextLength: Int { 0 }
    override var isFlipped: Bool { true }

    override init(frame frameRect: NSRect) {
        super.init(frame: frameRect)
        wantsLayer = true
        copyButton.toolTip = "Copy answer as Markdown"
        copyButton.target = self
        copyButton.action = #selector(copyMarkdownPressed)
        copyButton.layer?.cornerRadius = 5
        copyButton.onVisualStateChanged = { [weak self] in
            self?.layoutCopyButton()
            self?.applyButtonAppearance()
        }
        addSubview(copyButton)
    }

    @available(*, unavailable)
    required init?(coder: NSCoder) {
        fatalError("init(coder:) has not been implemented")
    }

    func setSelectedRange(_ range: NSRange?) {}

    func setButtonHovered(_ hovered: Bool) {
        guard isButtonHovered != hovered else { return }
        isButtonHovered = hovered
        applyButtonAppearance()
    }

    override func updateTrackingAreas() {
        super.updateTrackingAreas()
        if let buttonTrackingArea { removeTrackingArea(buttonTrackingArea) }
        let area = NSTrackingArea(
            rect: copyButton.frame,
            options: [.activeInKeyWindow, .mouseEnteredAndExited],
            owner: self,
            userInfo: nil)
        addTrackingArea(area)
        buttonTrackingArea = area
    }

    override func mouseEntered(with event: NSEvent) {
        setButtonHovered(true)
    }

    override func mouseExited(with event: NSEvent) {
        setButtonHovered(false)
    }

    func resetForTranscriptReuse() {
        isButtonHovered = false
        setCurtainRevealProgress(nil)
        layer?.removeAllAnimations()
        copyPayload = ""
        copyButton.cancelCopiedFlash()
        applyButtonAppearance()
    }

    override func viewDidChangeEffectiveAppearance() {
        super.viewDidChangeEffectiveAppearance()
        applyButtonAppearance()
    }

    func configure(
        copyMarkdown: String,
        width: CGFloat,
        theme: PCodeTranscriptTheme
    ) -> CGFloat {
        copyPayload = copyMarkdown
        lastTheme = theme
        lastRowWidth = width
        applyButtonAppearance()
        layoutCopyButton()
        return Self.rowHeight
    }

    private func layoutCopyButton() {
        let btnW: CGFloat = copyButton.isFlashingCopied
            ? max(Self.idleButtonWidth, ceil(copyButton.fittingSize.width) + 10)
            : Self.idleButtonWidth
        let btnX = max(2, lastRowWidth - btnW - 4)
        let nextFrame = CGRect(x: btnX, y: 1, width: btnW, height: Self.buttonHeight)
        if copyButton.frame != nextFrame {
            copyButton.frame = nextFrame
            updateTrackingAreas()
        }
    }

    private func applyButtonAppearance() {
        guard let theme = lastTheme else { return }
        let active = isButtonHovered || copyButton.isFlashingCopied
        effectiveAppearance.performAsCurrentDrawingAppearance {
            let bg: NSColor = isButtonHovered
                ? theme.foreground.withAlphaComponent(theme.isDark ? 0.08 : 0.06)
                : .clear
            copyButton.applyTheme(foreground: theme.secondaryForeground, background: bg)
        }
        copyButton.alphaValue = active ? 1.0 : 0.55
    }

    @objc private func copyMarkdownPressed() {
        guard !copyPayload.isEmpty else { return }
        pasteboard.clearContents()
        pasteboard.setString(copyPayload, forType: .string)
        copyButton.flashCopied()
    }
}

// MARK: - Code Block Row (Header + Copy + Horizontal Scroll + Async Prism Highlighting)

final class PCodeCodeBlockView: NSView, PCodeTranscriptRowHosting {
    let itemKind: PCodeTranscriptItem.Kind = .codeBlock
    let scrollView = PCodeHorizontalBlockScrollView()
    let textView = PCodeTextKit2BlockView()
    private let headerView = NSView()
    private let languageLabel = NSTextField(labelWithString: "")
    private let copyButton = PCodeBlockActionButton(title: "Copy", symbol: "doc.on.doc")
    private let dividerLayer = CALayer()

    private var copyCode: String = ""
    private var highlightGeneration: UInt64 = 0
    private var highlightTask: Task<Void, Never>?
    private var lastTheme: PCodeTranscriptTheme?
    private var lastHighlightedCode: String?
    private var lastHighlightedLanguage: String?
    private var lastHighlightedAttr: NSAttributedString?
    private var lastHighlightedPaletteDark: Bool?

    var primaryTextView: PCodeTextKit2BlockView? { textView }
    var selectableTextLength: Int { textView.textContentStorage.attributedString?.length ?? 0 }
    override var isFlipped: Bool { true }

    static let headerHeight: CGFloat = 28

    override init(frame frameRect: NSRect) {
        super.init(frame: frameRect)
        wantsLayer = true
        headerView.wantsLayer = true
        headerView.layer?.addSublayer(dividerLayer)
        languageLabel.isEditable = false
        languageLabel.isSelectable = false
        languageLabel.drawsBackground = false
        languageLabel.isBezeled = false
        languageLabel.lineBreakMode = .byTruncatingTail

        copyButton.target = self
        copyButton.action = #selector(copyCodePressed)

        headerView.addSubview(languageLabel)
        headerView.addSubview(copyButton)
        addSubview(headerView)

        scrollView.documentView = textView
        addSubview(scrollView)
    }

    @available(*, unavailable)
    required init?(coder: NSCoder) {
        fatalError("init(coder:) has not been implemented")
    }

    deinit {
        highlightTask?.cancel()
    }

    func setSelectedRange(_ range: NSRange?) {
        textView.selectedRange = range
    }

    func resetForTranscriptReuse() {
        setHovered(false)
        setSelectedRange(nil)
        setCurtainRevealProgress(nil)
        textView.inkSweepFrontier = nil
        layer?.removeAllAnimations()
        highlightTask?.cancel()
        highlightTask = nil
        highlightGeneration &+= 1
        lastHighlightedCode = nil
        lastHighlightedLanguage = nil
        lastHighlightedAttr = nil
        lastHighlightedPaletteDark = nil
        scrollView.resetScrollOrigin()
        copyButton.cancelCopiedFlash()
    }

    override func viewDidChangeEffectiveAppearance() {
        super.viewDidChangeEffectiveAppearance()
        applyLayerColors()
    }

    func configure(
        code: String,
        fullCopyCode: String,
        language: String?,
        showHeader: Bool,
        width: CGFloat,
        typography: PCodeTypographySnapshot,
        theme: PCodeTranscriptTheme,
        highlighter: any PCodeSyntaxHighlighter,
        documentView: PCodeTranscriptDocumentView?,
        selectedRange: NSRange?
    ) -> CGFloat {
        scrollView.transcriptDocumentView = documentView
        copyCode = fullCopyCode
        lastTheme = theme
        highlightTask?.cancel()
        highlightGeneration &+= 1
        let gen = highlightGeneration

        let codeFont = typography.codeNSFont(scale: typography.codeBlockScale)
        let lineHeight = typography.lineHeight(scale: typography.codeBlockScale)
        let palette = PCodeSyntaxPalette.make(isDark: theme.isDark, foreground: theme.foreground)

        let initialAttr: NSAttributedString
        let needsAsyncHighlight: Bool
        let isStreamingExtension: Bool
        if let cached = highlighter.cachedTokens(code: code, language: language) {
            let styled = PCodePrismSyntaxHighlighter.attributedString(
                from: cached,
                font: codeFont,
                palette: palette,
                lineHeight: lineHeight)
            initialAttr = styled
            lastHighlightedCode = code
            lastHighlightedLanguage = language
            lastHighlightedAttr = styled
            lastHighlightedPaletteDark = theme.isDark
            needsAsyncHighlight = false
            isStreamingExtension = false
        } else if let prevCode = lastHighlightedCode,
                  let prevAttr = lastHighlightedAttr,
                  lastHighlightedLanguage == language,
                  lastHighlightedPaletteDark == theme.isDark,
                  code.hasPrefix(prevCode),
                  code.count > prevCode.count
        {
            // Preserve the already-highlighted prefix while streaming new lines/chars
            // so syntax colors don't flicker back to plain text on every 120Hz frame.
            let suffix = String(code.dropFirst(prevCode.count))
            let combined = NSMutableAttributedString(attributedString: prevAttr)
            combined.append(PCodePrismSyntaxHighlighter.plainAttributedString(
                code: suffix,
                font: codeFont,
                foreground: theme.foreground,
                lineHeight: lineHeight))
            initialAttr = combined
            needsAsyncHighlight = true
            isStreamingExtension = true
        } else {
            initialAttr = PCodePrismSyntaxHighlighter.plainAttributedString(
                code: code,
                font: codeFont,
                foreground: theme.foreground,
                lineHeight: lineHeight)
            needsAsyncHighlight = true
            isStreamingExtension = false
        }

        let horizontalPad: CGFloat = 12
        let verticalPad: CGFloat = 10
        let contentWidth = max(1, width - horizontalPad * 2)
        let maxLineWidth = Self.widestLineWidth(in: code, font: codeFont)
        let layoutWidth = max(contentWidth, ceil(maxLineWidth) + 12)
        let textHeight = textView.configure(attributedText: initialAttr, width: layoutWidth)
        textView.selectedRange = selectedRange
        textView.frame = CGRect(x: 0, y: 0, width: layoutWidth, height: textHeight)

        let headerH: CGFloat = showHeader ? Self.headerHeight : 0
        headerView.isHidden = !showHeader
        if showHeader {
            headerView.frame = CGRect(x: 0, y: 0, width: width, height: headerH)
            languageLabel.stringValue = (language?.isEmpty == false) ? (language ?? "") : "code"
            languageLabel.font = typography.codeNSFont(scale: 0.78, weight: .medium)
            languageLabel.textColor = theme.secondaryForeground
            languageLabel.sizeToFit()
            languageLabel.frame = CGRect(
                x: horizontalPad,
                y: floor((headerH - languageLabel.frame.height) * 0.5),
                width: max(40, width - 90),
                height: languageLabel.frame.height)

            effectiveAppearance.performAsCurrentDrawingAppearance {
                copyButton.applyTheme(foreground: theme.secondaryForeground, background: .clear)
            }
            let btnW = max(54, copyButton.frame.width + 8)
            copyButton.frame = CGRect(
                x: max(horizontalPad, width - btnW - 8),
                y: floor((headerH - 20) * 0.5),
                width: btnW,
                height: 20)
            dividerLayer.frame = CGRect(x: 0, y: headerH - 1, width: width, height: 1)
        }

        scrollView.frame = CGRect(
            x: horizontalPad,
            y: headerH + verticalPad,
            width: contentWidth,
            height: textHeight)

        let totalHeight = headerH + textHeight + verticalPad * 2
        applyLayerColors()

        if needsAsyncHighlight {
            highlightTask = Task { [weak self] in
                if isStreamingExtension {
                    try? await Task.sleep(nanoseconds: 50_000_000)
                    guard !Task.isCancelled else { return }
                }
                let tokens = await highlighter.tokenize(code: code, language: language)
                guard !Task.isCancelled else { return }
                await MainActor.run { [weak self] in
                    guard let self, self.highlightGeneration == gen else { return }
                    let styled = PCodePrismSyntaxHighlighter.attributedString(
                        from: tokens,
                        font: codeFont,
                        palette: palette,
                        lineHeight: lineHeight)
                    self.lastHighlightedCode = code
                    self.lastHighlightedLanguage = language
                    self.lastHighlightedAttr = styled
                    self.lastHighlightedPaletteDark = theme.isDark
                    _ = self.textView.configure(attributedText: styled, width: layoutWidth)
                    self.textView.needsDisplay = true
                }
            }
        }

        return totalHeight
    }

    private func applyLayerColors() {
        guard let theme = lastTheme else { return }
        effectiveAppearance.performAsCurrentDrawingAppearance {
            layer?.backgroundColor = theme.codeBackground.cgColor
            layer?.cornerRadius = 8
            layer?.borderWidth = 1
            layer?.borderColor = theme.foreground.withAlphaComponent(theme.isDark ? 0.10 : 0.12).cgColor
            dividerLayer.backgroundColor = theme.foreground.withAlphaComponent(theme.isDark ? 0.08 : 0.10).cgColor
        }
    }

    /// Measure the widest line accurately (ASCII fast path for pure printable ASCII without tabs,
    /// full `NSString.size(withAttributes:)` whenever tabs or non-ASCII/emoji/CJK are present).
    static func widestLineWidth(in code: String, font: NSFont) -> CGFloat {
        let advance = ("M" as NSString).size(withAttributes: [.font: font]).width
        let attrs: [NSAttributedString.Key: Any] = [.font: font]
        var maxWidth: CGFloat = 0

        for line in code.split(separator: "\n", omittingEmptySubsequences: false) {
            let hasComplex = line.unicodeScalars.contains { scalar in
                scalar.value < 0x20 || scalar.value > 0x7E
            }
            let w: CGFloat
            if hasComplex {
                w = (String(line) as NSString).size(withAttributes: attrs).width
            } else {
                w = CGFloat(line.count) * advance
            }
            if w > maxWidth { maxWidth = w }
        }
        return maxWidth
    }

    @objc private func copyCodePressed() {
        let pb = NSPasteboard.general
        pb.clearContents()
        pb.setString(copyCode, forType: .string)
        copyButton.flashCopied()
    }
}

// MARK: - Table Block Row (Horizontal Scroll + 1px Hairline Grid)

private final class PCodeTableGridView: NSView {
    struct CellLayout {
        let attributed: NSAttributedString
        let alignment: PCodeMarkdownTable.Alignment
    }

    var columnWidths: [CGFloat] = []
    var rowHeights: [CGFloat] = []
    var rows: [[CellLayout]] = []
    var hasHeader = false
    var headerFill: NSColor = .clear
    var gridColor: NSColor = .separatorColor
    var isSelected = false

    static let cellPadX: CGFloat = 10
    static let cellPadY: CGFloat = 7

    override var isFlipped: Bool { true }

    override func draw(_ dirtyRect: NSRect) {
        guard let ctx = NSGraphicsContext.current?.cgContext, !rowHeights.isEmpty else { return }
        ctx.saveGState()
        defer { ctx.restoreGState() }

        let totalWidth = columnWidths.reduce(0, +)
        let totalHeight = rowHeights.reduce(0, +)

        if isSelected {
            ctx.setFillColor(NSColor.selectedTextBackgroundColor.withAlphaComponent(0.35).cgColor)
            ctx.fill(CGRect(x: 0, y: 0, width: totalWidth, height: totalHeight))
        }

        if hasHeader, let firstH = rowHeights.first {
            ctx.setFillColor(headerFill.cgColor)
            ctx.fill(CGRect(x: 0, y: 0, width: totalWidth, height: firstH))
        }

        var y: CGFloat = 0
        for (rowIdx, row) in rows.enumerated() {
            let h = rowHeights[rowIdx]
            var x: CGFloat = 0
            for (colIdx, width) in columnWidths.enumerated() {
                if colIdx < row.count {
                    let cell = row[colIdx]
                    let drawRect = CGRect(
                        x: x + Self.cellPadX,
                        y: y + Self.cellPadY,
                        width: max(1, width - Self.cellPadX * 2),
                        height: max(1, h - Self.cellPadY * 2))
                    if drawRect.intersects(dirtyRect) {
                        cell.attributed.draw(with: drawRect, options: [.usesLineFragmentOrigin, .usesFontLeading])
                    }
                }
                x += width
            }
            y += h
        }

        ctx.setFillColor(gridColor.cgColor)
        let px: CGFloat = 1.0 / (window?.backingScaleFactor ?? 2.0)

        var rowY: CGFloat = 0
        for (idx, h) in rowHeights.enumerated() {
            rowY += h
            if idx < rowHeights.count - 1 {
                ctx.fill(CGRect(x: 0, y: rowY - px, width: totalWidth, height: px))
            }
        }
        var colX: CGFloat = 0
        for (idx, w) in columnWidths.enumerated() {
            colX += w
            if idx < columnWidths.count - 1 {
                ctx.fill(CGRect(x: colX - px, y: 0, width: px, height: totalHeight))
            }
        }
    }
}

final class PCodeTableBlockView: NSView, PCodeTranscriptRowHosting {
    let itemKind: PCodeTranscriptItem.Kind = .table
    let scrollView = PCodeHorizontalBlockScrollView()
    private let gridView = PCodeTableGridView()
    private var plainLength: Int = 0
    private var lastTheme: PCodeTranscriptTheme?

    var primaryTextView: PCodeTextKit2BlockView? { nil }
    var selectableTextLength: Int { plainLength }
    override var isFlipped: Bool { true }

    override init(frame frameRect: NSRect) {
        super.init(frame: frameRect)
        wantsLayer = true
        scrollView.documentView = gridView
        addSubview(scrollView)
    }

    @available(*, unavailable)
    required init?(coder: NSCoder) {
        fatalError("init(coder:) has not been implemented")
    }

    func setSelectedRange(_ range: NSRange?) {
        let selected = (range?.length ?? 0) > 0
        if gridView.isSelected != selected {
            gridView.isSelected = selected
            gridView.needsDisplay = true
        }
    }

    func resetForTranscriptReuse() {
        setHovered(false)
        setSelectedRange(nil)
        setCurtainRevealProgress(nil)
        layer?.removeAllAnimations()
        scrollView.resetScrollOrigin()
    }

    override func viewDidChangeEffectiveAppearance() {
        super.viewDidChangeEffectiveAppearance()
        applyLayerColors()
    }

    func configure(
        table: PCodeMarkdownTable,
        width: CGFloat,
        typography: PCodeTypographySnapshot,
        theme: PCodeTranscriptTheme,
        documentView: PCodeTranscriptDocumentView?,
        selectedRange: NSRange?
    ) -> CGFloat {
        scrollView.transcriptDocumentView = documentView
        lastTheme = theme
        plainLength = (table.plainText as NSString).length

        let colCount = max(table.headers.count, table.rows.map(\.count).max() ?? 0)
        guard colCount > 0 else { return 0 }

        var allRows: [[PCodeTableGridView.CellLayout]] = []
        let hasHeader = !table.headers.isEmpty
        if hasHeader {
            allRows.append(buildRow(
                table.headers,
                alignments: table.alignments,
                colCount: colCount,
                isHeader: true,
                typography: typography,
                theme: theme))
        }
        for row in table.rows {
            allRows.append(buildRow(
                row,
                alignments: table.alignments,
                colCount: colCount,
                isHeader: false,
                typography: typography,
                theme: theme))
        }

        var colWidths = Array(repeating: CGFloat(68), count: colCount)
        for row in allRows {
            for (c, cell) in row.enumerated() {
                let measured = cell.attributed.boundingRect(
                    with: CGSize(width: 320 - PCodeTableGridView.cellPadX * 2, height: .greatestFiniteMagnitude),
                    options: [.usesLineFragmentOrigin, .usesFontLeading]).width
                let w = min(320, max(68, ceil(measured) + PCodeTableGridView.cellPadX * 2 + 4))
                if w > colWidths[c] { colWidths[c] = w }
            }
        }

        let naturalTotal = colWidths.reduce(0, +)
        if naturalTotal < width && naturalTotal > 0 {
            let scale = width / naturalTotal
            colWidths = colWidths.map { floor($0 * scale) }
            if let last = colWidths.indices.last {
                colWidths[last] += width - colWidths.reduce(0, +)
            }
        }

        var rowHeights: [CGFloat] = []
        rowHeights.reserveCapacity(allRows.count)
        for row in allRows {
            var maxH: CGFloat = ceil(typography.lineHeight(scale: 0.92))
            for (c, cell) in row.enumerated() {
                let textW = max(1, colWidths[c] - PCodeTableGridView.cellPadX * 2)
                let h = cell.attributed.boundingRect(
                    with: CGSize(width: textW, height: .greatestFiniteMagnitude),
                    options: [.usesLineFragmentOrigin, .usesFontLeading]).height
                if h > maxH { maxH = ceil(h) }
            }
            rowHeights.append(maxH + PCodeTableGridView.cellPadY * 2)
        }

        let totalW = colWidths.reduce(0, +)
        let totalH = rowHeights.reduce(0, +)

        gridView.columnWidths = colWidths
        gridView.rowHeights = rowHeights
        gridView.rows = allRows
        gridView.hasHeader = hasHeader
        gridView.isSelected = (selectedRange?.length ?? 0) > 0
        gridView.frame = CGRect(x: 0, y: 0, width: totalW, height: totalH)

        applyLayerColors()
        gridView.needsDisplay = true
        scrollView.frame = CGRect(x: 0, y: 0, width: width, height: totalH)
        return totalH
    }

    private func applyLayerColors() {
        guard let theme = lastTheme else { return }
        effectiveAppearance.performAsCurrentDrawingAppearance {
            gridView.headerFill = theme.toolBackground
            gridView.gridColor = theme.foreground.withAlphaComponent(theme.isDark ? 0.14 : 0.16)
            layer?.backgroundColor = theme.background.cgColor
            layer?.cornerRadius = 8
            layer?.borderWidth = 1
            layer?.borderColor = theme.foreground.withAlphaComponent(theme.isDark ? 0.14 : 0.16).cgColor
            layer?.masksToBounds = true
        }
    }

    private func buildRow(
        _ cells: [PCodeMarkdownTableCell],
        alignments: [PCodeMarkdownTable.Alignment],
        colCount: Int,
        isHeader: Bool,
        typography: PCodeTypographySnapshot,
        theme: PCodeTranscriptTheme
    ) -> [PCodeTableGridView.CellLayout] {
        (0..<colCount).map { idx in
            let raw = idx < cells.count ? cells[idx].markdown : ""
            let align = idx < alignments.count ? alignments[idx] : .left
            let attr = NSMutableAttributedString(attributedString: PCodeTranscriptMarkdown.attributedString(
                forInlineCell: raw,
                typography: typography,
                theme: theme,
                scale: 0.92,
                weight: isHeader ? typography.weight.nsEmphasisWeight : nil))
            let style = NSMutableParagraphStyle()
            style.minimumLineHeight = typography.lineHeight(scale: 0.92)
            style.maximumLineHeight = typography.lineHeight(scale: 0.92)
            switch align {
            case .left: style.alignment = .left
            case .center: style.alignment = .center
            case .right: style.alignment = .right
            }
            if attr.length > 0 {
                attr.addAttribute(.paragraphStyle, value: style, range: NSRange(location: 0, length: attr.length))
            }
            return PCodeTableGridView.CellLayout(attributed: attr, alignment: align)
        }
    }
}

// MARK: - Native Swift Running Spinner (CoreAnimation GPU Arc)

/// Crisp 12x12pt CoreAnimation spinning arc indicator for running tool calls and
/// live edit previews. Animates smoothly on the GPU without timers or Kitty spinners.
final class PCodeRunningSpinnerView: NSView {
    private static let animationKey = "pcode.spinner.rotation"
    private let spinnerContainer = CALayer()
    private let trackLayer = CAShapeLayer()
    private let arcLayer = CAShapeLayer()
    private var lastAccentColor: NSColor = .controlAccentColor
    private var isAnimating = false

    override var isFlipped: Bool { true }

    override init(frame frameRect: NSRect) {
        super.init(frame: frameRect)
        wantsLayer = true
        trackLayer.fillColor = nil
        trackLayer.lineWidth = 1.6
        arcLayer.fillColor = nil
        arcLayer.lineWidth = 1.6
        arcLayer.lineCap = .round
        arcLayer.strokeStart = 0
        arcLayer.strokeEnd = 0.68
        spinnerContainer.addSublayer(trackLayer)
        spinnerContainer.addSublayer(arcLayer)
        layer?.addSublayer(spinnerContainer)
    }

    @available(*, unavailable)
    required init?(coder: NSCoder) {
        fatalError("init(coder:) has not been implemented")
    }

    override func layout() {
        super.layout()
        updatePaths()
    }

    override func viewDidChangeEffectiveAppearance() {
        super.viewDidChangeEffectiveAppearance()
        applyColors()
    }

    override func viewDidChangeBackingProperties() {
        super.viewDidChangeBackingProperties()
        updateBackingScale()
    }

    private func updateBackingScale() {
        let scale = window?.backingScaleFactor ?? NSScreen.main?.backingScaleFactor ?? 2.0
        CATransaction.begin()
        CATransaction.setDisableActions(true)
        spinnerContainer.contentsScale = scale
        trackLayer.contentsScale = scale
        arcLayer.contentsScale = scale
        CATransaction.commit()
    }

    func startAnimating(color: NSColor) {
        lastAccentColor = color
        applyColors()
        updatePaths()
        guard !isAnimating else { return }
        isAnimating = true
        let spin = CABasicAnimation(keyPath: "transform.rotation.z")
        spin.fromValue = 0
        spin.toValue = Double.pi * 2
        spin.duration = 0.85
        spin.repeatCount = .infinity
        spin.isRemovedOnCompletion = false
        spinnerContainer.add(spin, forKey: Self.animationKey)
    }

    func stopAnimating() {
        guard isAnimating else { return }
        isAnimating = false
        spinnerContainer.removeAnimation(forKey: Self.animationKey)
    }

    private func updatePaths() {
        updateBackingScale()
        CATransaction.begin()
        CATransaction.setDisableActions(true)
        spinnerContainer.frame = bounds
        let side = min(bounds.width, bounds.height)
        let radius = max(1, (side - 2.0) * 0.5)
        let center = CGPoint(x: bounds.midX, y: bounds.midY)
        let path = CGMutablePath()
        path.addArc(center: center, radius: radius, startAngle: -.pi / 2, endAngle: .pi * 1.5, clockwise: false)
        trackLayer.frame = bounds
        trackLayer.path = path
        arcLayer.frame = bounds
        arcLayer.path = path
        CATransaction.commit()
    }

    private func applyColors() {
        effectiveAppearance.performAsCurrentDrawingAppearance {
            CATransaction.begin()
            CATransaction.setDisableActions(true)
            trackLayer.strokeColor = lastAccentColor.withAlphaComponent(0.20).cgColor
            arcLayer.strokeColor = lastAccentColor.cgColor
            CATransaction.commit()
        }
    }
}

// MARK: - Tool Card Row (Compact Pill + Optional Expandable Detail)

final class PCodeToolCardView: NSView, PCodeTranscriptRowHosting {
    let itemKind: PCodeTranscriptItem.Kind = .tool
    let textView = PCodeTextKit2BlockView()
    private let iconView = NSImageView()
    private let spinnerView = PCodeRunningSpinnerView()
    private let headerTapButton = NSButton()
    private let elapsedLabel = NSTextField(labelWithString: "")
    private let copyButton = PCodeBlockActionButton(title: "Copy", symbol: "doc.on.doc")
    private let inspectButton = NSButton()
    private let toggleButton = NSButton()
    private let bottomCollapseButton = NSButton()
    private let bottomDivider = CALayer()
    private var onToggle: (() -> Void)?
    private var onInspect: (() -> Void)?
    private var lastTheme: PCodeTranscriptTheme?
    private var lastIsRunning = false
    private var lastIsFailed = false
    private var lastIsExpanded = false
    private var lastHovered = false
    private var lastStackPosition: PCodeToolStackPosition = .standalone
    private var currentCall: PCodePeekToolCall?
    private var textToCopy = ""

    var primaryTextView: PCodeTextKit2BlockView? { textView }
    var selectableTextLength: Int { textView.textContentStorage.attributedString?.length ?? 0 }
    override var isFlipped: Bool { true }

    private static let headerTapHeight: CGFloat = 26
    private static let bottomCollapseBarHeight: CGFloat = 24

    override init(frame frameRect: NSRect) {
        super.init(frame: frameRect)
        wantsLayer = true
        iconView.imageScaling = .scaleProportionallyDown
        addSubview(iconView)
        spinnerView.isHidden = true
        addSubview(spinnerView)
        addSubview(textView)

        headerTapButton.isBordered = false
        headerTapButton.title = ""
        headerTapButton.target = self
        headerTapButton.action = #selector(togglePressed)
        addSubview(headerTapButton)

        elapsedLabel.isEditable = false
        elapsedLabel.isSelectable = false
        elapsedLabel.drawsBackground = false
        elapsedLabel.isBezeled = false
        elapsedLabel.alignment = .right
        elapsedLabel.isHidden = true
        addSubview(elapsedLabel)

        copyButton.target = self
        copyButton.action = #selector(copyPressed)
        copyButton.isHidden = true
        addSubview(copyButton)

        inspectButton.isBordered = false
        inspectButton.title = ""
        inspectButton.target = self
        inspectButton.action = #selector(inspectPressed)
        inspectButton.toolTip = "Open in Inspector"
        inspectButton.isHidden = true
        addSubview(inspectButton)

        toggleButton.isBordered = false
        toggleButton.title = ""
        toggleButton.target = self
        toggleButton.action = #selector(togglePressed)
        addSubview(toggleButton)

        layer?.addSublayer(bottomDivider)
        bottomDivider.isHidden = true

        bottomCollapseButton.isBordered = false
        bottomCollapseButton.bezelStyle = .inline
        bottomCollapseButton.imagePosition = .imageLeading
        bottomCollapseButton.target = self
        bottomCollapseButton.action = #selector(togglePressed)
        bottomCollapseButton.isHidden = true
        addSubview(bottomCollapseButton)
    }

    @available(*, unavailable)
    required init?(coder: NSCoder) {
        fatalError("init(coder:) has not been implemented")
    }

    func setSelectedRange(_ range: NSRange?) {
        textView.selectedRange = range
    }

    func setHovered(_ hovered: Bool) {
        guard lastHovered != hovered else { return }
        lastHovered = hovered
        applyLayerColors(animated: true)
    }

    func resetForTranscriptReuse() {
        lastHovered = false
        lastIsFailed = false
        lastStackPosition = .standalone
        setSelectedRange(nil)
        setCurtainRevealProgress(nil)
        primaryTextView?.inkSweepFrontier = nil
        layer?.removeAllAnimations()
        spinnerView.stopAnimating()
        spinnerView.isHidden = true
        iconView.isHidden = false
        elapsedLabel.isHidden = true
        elapsedLabel.stringValue = ""
        copyButton.cancelCopiedFlash()
        copyButton.isHidden = true
        inspectButton.isHidden = true
        onToggle = nil
        onInspect = nil
        currentCall = nil
        lastIsRunning = false
        lastIsExpanded = false
        textToCopy = ""
        bottomCollapseButton.isHidden = true
        bottomDivider.isHidden = true
    }

    override func viewDidChangeEffectiveAppearance() {
        super.viewDidChangeEffectiveAppearance()
        applyLayerColors(animated: false)
    }

    /// Update the live elapsed clock in-place while a command is running (no document relayout).
    func updateLiveElapsed(now: Date = Date()) {
        guard let call = currentCall, call.isRunning else { return }
        if let text = call.liveElapsedText(now: now) {
            elapsedLabel.stringValue = text
            elapsedLabel.isHidden = false
        } else {
            elapsedLabel.stringValue = ""
            elapsedLabel.isHidden = true
        }
    }

    /// Number of lines in the expandable payload (output if present, otherwise multiline summary).
    static func expandableLineCount(for call: PCodePeekToolCall) -> Int {
        func countLines(_ raw: String) -> Int {
            var s = raw[...]
            if s.last?.isNewline == true {
                s = s.dropLast()
            }
            guard !s.isEmpty else { return 0 }
            return s.split(separator: "\n", omittingEmptySubsequences: false).count
        }
        if !call.output.isEmpty {
            return countLines(call.output)
        }
        let summary = call.summaryText
        if summary.contains("\n") {
            return countLines(summary)
        }
        return 0
    }

    /// The last non-blank line a running shell-like call has printed, bounded for one row;
    /// nil for any other call (a sub-agent's report is not a stream) or before output.
    static func latestOutputLine(for call: PCodePeekToolCall) -> String? {
        guard call.isRunning, call.isShellLike, !call.isDelegate else { return nil }
        guard let line = call.output.split(separator: "\n").last(where: {
            !$0.trimmingCharacters(in: .whitespaces).isEmpty
        }) else { return nil }
        let text = String(line).trimmingCharacters(in: .whitespaces)
        return text.count > 200 ? String(text.prefix(200)) + "…" : text
    }

    /// The settled row's mark: a check, a red cross for a hard failure, or a small dot for a
    /// command that ran and exited non-zero in the ordinary way (mirrors pcode's `nonzero` icon).
    static func settledMark(for call: PCodePeekToolCall) -> (symbol: String, label: String, pointSize: CGFloat) {
        if call.failed { return ("xmark.circle.fill", "Failed", 10) }
        if call.exitedNonZero { return ("circle.fill", "Exited \(call.exitCode ?? 0)", 5) }
        return ("checkmark.circle.fill", "Succeeded", 10)
    }

    /// Single source of truth for rendered tool card text (shared by drawing and copy).
    static func attributedText(
        for call: PCodePeekToolCall,
        isExpanded: Bool,
        typography: PCodeTypographySnapshot,
        theme: PCodeTranscriptTheme
    ) -> NSAttributedString {
        let out = NSMutableAttributedString()
        let lineHeight = typography.lineHeight(scale: 0.84)
        let style = NSMutableParagraphStyle()
        style.minimumLineHeight = lineHeight
        style.maximumLineHeight = lineHeight
        style.lineBreakMode = isExpanded ? .byWordWrapping : .byTruncatingTail

        let nameColor: NSColor
        if call.isRunning {
            nameColor = theme.accent
        } else if call.failed {
            nameColor = .systemRed
        } else {
            nameColor = theme.secondaryForeground
        }
        out.append(NSAttributedString(
            string: call.displayName,
            attributes: [
                .font: typography.codeNSFont(scale: 0.84, weight: typography.weight.nsEmphasisWeight),
                .foregroundColor: nameColor,
                .paragraphStyle: style,
            ]))

        if let badge = call.asyncBadgeText {
            let badgeColor = call.isRunning
                ? theme.accent.withAlphaComponent(0.78)
                : theme.secondaryForeground.withAlphaComponent(0.68)
            out.append(NSAttributedString(
                string: "  " + badge,
                attributes: [
                    .font: typography.codeNSFont(scale: 0.78, weight: .medium),
                    .foregroundColor: badgeColor,
                    .paragraphStyle: style,
                ]))
        }

        let summary = call.isJobWait ? call.jobSubjectText : call.summaryText
        if !summary.isEmpty {
            let displaySummary: String
            if isExpanded {
                displaySummary = "\n" + summary
            } else {
                let firstLine = summary.split(separator: "\n", maxSplits: 1).first.map(String.init) ?? summary
                displaySummary = "  " + (firstLine.count > 120 ? String(firstLine.prefix(120)) + "…" : firstLine)
            }
            out.append(NSAttributedString(
                string: displaySummary,
                attributes: [
                    .font: typography.codeNSFont(scale: 0.84),
                    .foregroundColor: theme.secondaryForeground.withAlphaComponent(call.isRunning ? 0.95 : 0.85),
                    .paragraphStyle: style,
                ]))
        }

        if !call.isRunning, let elapsed = call.formattedElapsed {
            out.append(NSAttributedString(
                string: "  (\(elapsed))",
                attributes: [
                    .font: typography.codeNSFont(scale: 0.78),
                    .foregroundColor: theme.secondaryForeground.withAlphaComponent(0.62),
                    .paragraphStyle: style,
                ]))
        }

        if !isExpanded {
            let lineCount = expandableLineCount(for: call)
            if call.isRunning {
                let lines = lineCount == 1 ? "1 line" : "\(lineCount) lines"
                if let latest = latestOutputLine(for: call) {
                    // A running command's newest line, on a line of its own so a long
                    // command on the header can't push it (or the count) out of view:
                    // the stream is visible without expanding the card.
                    let liveStyle = (style.mutableCopy() as? NSMutableParagraphStyle) ?? style
                    liveStyle.paragraphSpacingBefore = 2
                    out.append(NSAttributedString(string: "\n", attributes: [.paragraphStyle: style]))
                    out.append(NSAttributedString(
                        string: "live · \(lines)  ",
                        attributes: [
                            .font: typography.codeNSFont(scale: 0.78),
                            .foregroundColor: theme.accent.withAlphaComponent(0.85),
                            .paragraphStyle: liveStyle,
                        ]))
                    out.append(NSAttributedString(
                        string: latest,
                        attributes: [
                            .font: typography.codeNSFont(scale: 0.80),
                            .foregroundColor: theme.foreground.withAlphaComponent(0.84),
                            .paragraphStyle: liveStyle,
                        ]))
                } else {
                    out.append(NSAttributedString(
                        string: lineCount > 0 ? "  · live · \(lines)" : "  · running",
                        attributes: [
                            .font: typography.codeNSFont(scale: 0.78),
                            .foregroundColor: theme.accent.withAlphaComponent(0.85),
                            .paragraphStyle: style,
                        ]))
                }
            } else if lineCount > 0 {
                let label = lineCount == 1 ? "1 line" : "\(lineCount) lines"
                out.append(NSAttributedString(
                    string: "  · \(label)",
                    attributes: [
                        .font: typography.codeNSFont(scale: 0.78),
                        .foregroundColor: theme.secondaryForeground.withAlphaComponent(0.62),
                        .paragraphStyle: style,
                    ]))
            }
        }

        // Lazy tool output rendering: only format/append output when expanded.
        if isExpanded {
            if !call.output.isEmpty && call.isDelegate {
                // A sub-agent's report is Markdown written for a reader, not tool output.
                out.append(NSAttributedString(string: "\n", attributes: [.paragraphStyle: style]))
                out.append(PCodeTranscriptMarkdown.attributedString(
                    forProse: call.output, typography: typography, theme: theme, scale: 0.92))
            } else if !call.output.isEmpty {
                out.append(NSAttributedString(
                    string: "\n" + call.output,
                    attributes: [
                        .font: typography.codeNSFont(scale: 0.80),
                        .foregroundColor: theme.foreground.withAlphaComponent(0.84),
                        .paragraphStyle: style,
                    ]))
            } else if call.isRunning {
                out.append(NSAttributedString(
                    string: "\nWaiting for output…",
                    attributes: [
                        .font: typography.codeNSFont(scale: 0.80),
                        .foregroundColor: theme.secondaryForeground.withAlphaComponent(0.58),
                        .paragraphStyle: style,
                    ]))
            }
        }
        return out
    }

    func configure(
        call: PCodePeekToolCall,
        isExpanded: Bool,
        attributedText: NSAttributedString,
        width: CGFloat,
        theme: PCodeTranscriptTheme,
        selectedRange: NSRange?,
        stackPosition: PCodeToolStackPosition = .standalone,
        onToggle: @escaping () -> Void,
        onInspect: (() -> Void)? = nil
    ) -> CGFloat {
        self.onToggle = onToggle
        self.onInspect = onInspect
        self.lastTheme = theme
        self.lastIsRunning = call.isRunning
        self.lastIsFailed = call.failed
        self.lastIsExpanded = isExpanded
        self.lastStackPosition = stackPosition
        self.currentCall = call
        self.textToCopy = !call.output.isEmpty ? call.output : call.summaryText

        let canExpand = call.canExpand
        let showCopy = isExpanded && !textToCopy.isEmpty
        let showInspect = onInspect != nil

        if call.isRunning {
            iconView.isHidden = true
            spinnerView.isHidden = false
            spinnerView.startAnimating(color: theme.accent)
        } else {
            spinnerView.stopAnimating()
            spinnerView.isHidden = true
            iconView.isHidden = false
            let mark = Self.settledMark(for: call)
            let cfg = NSImage.SymbolConfiguration(pointSize: mark.pointSize, weight: .medium)
            iconView.image = NSImage(systemSymbolName: mark.symbol, accessibilityDescription: mark.label)?
                .withSymbolConfiguration(cfg)
            iconView.contentTintColor = call.failed ? .systemRed : theme.secondaryForeground.withAlphaComponent(0.8)
        }

        let elapsedWidth: CGFloat = (call.isRunning && call.startedAt != nil) ? 54 : 0
        let copyWidth: CGFloat = showCopy ? max(64, ceil(copyButton.frame.width)) : 0
        let inspectWidth: CGFloat = showInspect ? 20 : 0
        let chevronWidth: CGFloat = canExpand ? 26 : 10
        let rightControlsWidth = chevronWidth
            + (showInspect ? inspectWidth + 2 : 0)
            + (showCopy ? copyWidth + 4 : 0)
            + (elapsedWidth > 0 ? elapsedWidth + 4 : 0)

        let leftPad: CGFloat = 26
        let rightPad: CGFloat = rightControlsWidth
        let topPad: CGFloat = 5
        let bottomPad: CGFloat = 5
        let textWidth = max(1, width - leftPad - rightPad)
        let textHeight = textView.configure(attributedText: attributedText, width: textWidth)
        textView.selectedRange = selectedRange
        textView.frame = CGRect(x: leftPad, y: topPad, width: textWidth, height: textHeight)

        let showBottomCollapse = canExpand && isExpanded
        let contentHeight = max(26, textHeight + topPad + bottomPad)
        let totalHeight = contentHeight + (showBottomCollapse ? Self.bottomCollapseBarHeight : 0)
        let iconFrame = CGRect(x: 9, y: floor((min(contentHeight, 26) - 12) * 0.5), width: 12, height: 12)
        iconView.frame = iconFrame
        spinnerView.frame = iconFrame

        // Clicking anywhere on a collapsed pill (or the first header line of an expanded card)
        // toggles it, while keeping line 2+ (command & output body) free for text selection.
        headerTapButton.isHidden = !canExpand
        if canExpand {
            let tapWidth = max(0, width - rightControlsWidth)
            let tapHeight = isExpanded ? 18 : contentHeight
            headerTapButton.frame = CGRect(x: 0, y: 0, width: tapWidth, height: tapHeight)
        }

        toggleButton.isHidden = !canExpand
        if canExpand {
            let chevron = isExpanded ? "chevron.down" : "chevron.right"
            toggleButton.image = NSImage(systemSymbolName: chevron, accessibilityDescription: isExpanded ? "Collapse" : "Expand")?
                .withSymbolConfiguration(NSImage.SymbolConfiguration(pointSize: 9, weight: .semibold))
            toggleButton.contentTintColor = theme.secondaryForeground
            toggleButton.frame = CGRect(x: width - 22, y: 3, width: 18, height: 20)
        }

        var cursorX = width - chevronWidth
        inspectButton.isHidden = !showInspect
        if showInspect {
            let symCfg = NSImage.SymbolConfiguration(pointSize: 10, weight: .medium)
            inspectButton.image = NSImage(systemSymbolName: "sidebar.right", accessibilityDescription: "Open in Inspector")?
                .withSymbolConfiguration(symCfg)
            inspectButton.toolTip = call.isDelegate
                ? "Open agent in Inspector"
                : (PCodeReadFileReference.isReadFileTool(call.name) ? "Open file in Inspector" : "Open command in Inspector")
            inspectButton.contentTintColor = theme.secondaryForeground
            cursorX -= inspectWidth
            inspectButton.frame = CGRect(x: max(leftPad, cursorX), y: 3, width: inspectWidth, height: 20)
            cursorX -= 2
        }
        copyButton.isHidden = !showCopy
        if showCopy {
            effectiveAppearance.performAsCurrentDrawingAppearance {
                copyButton.applyTheme(foreground: theme.secondaryForeground, background: .clear)
            }
            cursorX -= copyWidth
            copyButton.frame = CGRect(x: max(leftPad, cursorX), y: 3, width: copyWidth, height: 20)
            cursorX -= 4
        }

        if call.isRunning && call.startedAt != nil {
            elapsedLabel.font = NSFont.monospacedDigitSystemFont(ofSize: 10.5, weight: .regular)
            elapsedLabel.textColor = theme.accent.withAlphaComponent(0.85)
            cursorX -= elapsedWidth
            elapsedLabel.frame = CGRect(x: max(leftPad, cursorX), y: 5, width: elapsedWidth, height: 16)
            updateLiveElapsed()
        } else {
            elapsedLabel.isHidden = true
            elapsedLabel.stringValue = ""
        }

        bottomCollapseButton.isHidden = !showBottomCollapse
        bottomDivider.isHidden = !showBottomCollapse
        if showBottomCollapse {
            let lineCount = Self.expandableLineCount(for: call)
            let title: String
            if call.isRunning {
                title = lineCount > 1 ? " Collapse (streaming · \(lineCount) lines)" : " Collapse (streaming)"
            } else {
                title = lineCount > 1 ? " Collapse (\(lineCount) lines)" : " Collapse"
            }
            let symCfg = NSImage.SymbolConfiguration(pointSize: 9, weight: .semibold)
            bottomCollapseButton.image = NSImage(systemSymbolName: "chevron.up", accessibilityDescription: "Collapse")?
                .withSymbolConfiguration(symCfg)
            bottomCollapseButton.contentTintColor = theme.secondaryForeground
            bottomCollapseButton.attributedTitle = NSAttributedString(
                string: title,
                attributes: [
                    .font: NSFont.systemFont(ofSize: 11, weight: .medium),
                    .foregroundColor: theme.secondaryForeground,
                ])
            bottomCollapseButton.frame = CGRect(
                x: 8,
                y: contentHeight + 1,
                width: max(1, width - 16),
                height: Self.bottomCollapseBarHeight - 2)
            CATransaction.begin()
            CATransaction.setDisableActions(true)
            bottomDivider.frame = CGRect(x: 8, y: contentHeight, width: max(0, width - 16), height: 1)
            CATransaction.commit()
        }

        applyLayerColors(animated: false)
        return totalHeight
    }

    private func applyLayerColors(animated: Bool = false) {
        guard let theme = lastTheme else { return }
        let bgColor: NSColor?
        let borderWidth: CGFloat
        let borderColor: NSColor?
        if lastIsRunning {
            bgColor = theme.toolBackground
            borderWidth = 1
            borderColor = theme.accent.withAlphaComponent(theme.isDark ? 0.36 : 0.28)
        } else if lastIsFailed {
            bgColor = NSColor.systemRed.withAlphaComponent(theme.isDark ? 0.08 : 0.055)
            borderWidth = 1
            borderColor = NSColor.systemRed.withAlphaComponent(theme.isDark ? 0.30 : 0.24)
        } else if lastIsExpanded {
            bgColor = theme.toolBackground
            borderWidth = 1
            borderColor = theme.foreground.withAlphaComponent(theme.isDark ? 0.09 : 0.08)
        } else if lastHovered {
            bgColor = theme.foreground.withAlphaComponent(theme.isDark ? 0.088 : 0.068)
            borderWidth = 0
            borderColor = nil
        } else {
            // Settled, collapsed tool rows use a soft borderless fill at rest so they
            // separate cleanly from prose without adding heavy boxed borders.
            bgColor = theme.foreground.withAlphaComponent(theme.isDark ? 0.052 : 0.038)
            borderWidth = 0
            borderColor = nil
        }

        let effectiveStack: PCodeToolStackPosition = (lastIsRunning || lastIsFailed || lastIsExpanded) ? .standalone : lastStackPosition
        effectiveAppearance.performAsCurrentDrawingAppearance {
            CATransaction.begin()
            if animated && !NSWorkspace.shared.accessibilityDisplayShouldReduceMotion {
                CATransaction.setAnimationDuration(0.12)
                CATransaction.setAnimationTimingFunction(CAMediaTimingFunction(name: .easeOut))
            } else {
                CATransaction.setDisableActions(true)
            }
            layer?.backgroundColor = bgColor?.cgColor
            layer?.cornerRadius = 6
            layer?.maskedCorners = effectiveStack.maskedCorners
            layer?.borderWidth = borderWidth
            layer?.borderColor = borderColor?.cgColor
            bottomDivider.backgroundColor = theme.foreground.withAlphaComponent(theme.isDark ? 0.10 : 0.12).cgColor
            CATransaction.commit()
        }
    }

    @objc private func inspectPressed() {
        onInspect?()
    }

    @objc private func togglePressed() {
        onToggle?()
    }

    @objc private func copyPressed() {
        guard !textToCopy.isEmpty else { return }
        let pb = NSPasteboard.general
        pb.clearContents()
        pb.setString(textToCopy, forType: .string)
        copyButton.flashCopied()
    }
}

// MARK: - Tool Group Summary Row (Folded Consecutive Settled Tools & Edits)

/// Full-row disclosure button that forwards right-clicks up to `PCodeTranscriptDocumentView.menu(for:)`
/// so users can right-click a grouped tool pill directly to toggle "Group Tool Calls".
private final class PCodeContextMenuForwardingButton: NSButton {
    override func menu(for event: NSEvent) -> NSMenu? {
        var ancestor = superview
        while let current = ancestor {
            if let doc = current as? PCodeTranscriptDocumentView {
                return doc.menu(for: event)
            }
            ancestor = current.superview
        }
        return super.menu(for: event)
    }
}

final class PCodeToolGroupCardView: NSView, PCodeTranscriptRowHosting {
    let itemKind: PCodeTranscriptItem.Kind = .toolGroup
    let textView = PCodeTextKit2BlockView()
    private let iconView = NSImageView()
    private let elapsedLabel = NSTextField(labelWithString: "")
    private let chevronContainer = NSView()
    private let chevronView = NSImageView()
    private let tapButton = PCodeContextMenuForwardingButton()
    private var onToggle: (() -> Void)?
    private var lastTheme: PCodeTranscriptTheme?
    private var lastIsRunning = false
    private var lastIsExpanded = false
    private var lastHasFailures = false
    private var lastHovered = false
    private var lastStackPosition: PCodeToolStackPosition = .standalone
    private var hasConfigured = false

    var primaryTextView: PCodeTextKit2BlockView? { textView }
    var selectableTextLength: Int { textView.textContentStorage.attributedString?.length ?? 0 }
    override var isFlipped: Bool { true }

    static let rowHeight: CGFloat = 28

    override init(frame frameRect: NSRect) {
        super.init(frame: frameRect)
        wantsLayer = true
        layer?.masksToBounds = true

        iconView.imageScaling = .scaleProportionallyDown
        addSubview(iconView)
        addSubview(textView)

        elapsedLabel.isEditable = false
        elapsedLabel.isSelectable = false
        elapsedLabel.drawsBackground = false
        elapsedLabel.isBezeled = false
        elapsedLabel.alignment = .right
        elapsedLabel.isHidden = true
        addSubview(elapsedLabel)

        chevronContainer.wantsLayer = true
        chevronView.imageScaling = .scaleProportionallyDown
        chevronView.image = NSImage(systemSymbolName: "chevron.right", accessibilityDescription: "Toggle tool group")?
            .withSymbolConfiguration(NSImage.SymbolConfiguration(pointSize: 9, weight: .semibold))
        chevronContainer.addSubview(chevronView)
        addSubview(chevronContainer)

        tapButton.isBordered = false
        tapButton.title = ""
        tapButton.target = self
        tapButton.action = #selector(togglePressed)
        addSubview(tapButton)
    }

    @available(*, unavailable)
    required init?(coder: NSCoder) {
        fatalError("init(coder:) has not been implemented")
    }

    func setSelectedRange(_ range: NSRange?) {
        textView.selectedRange = range
    }

    func setHovered(_ hovered: Bool) {
        guard lastHovered != hovered else { return }
        lastHovered = hovered
        applyLayerColors(animated: true)
    }

    func resetForTranscriptReuse() {
        lastHovered = false
        lastStackPosition = .standalone
        setSelectedRange(nil)
        setCurtainRevealProgress(nil)
        primaryTextView?.inkSweepFrontier = nil
        layer?.removeAllAnimations()
        chevronContainer.layer?.removeAllAnimations()
        chevronContainer.layer?.transform = CATransform3DIdentity
        elapsedLabel.isHidden = true
        elapsedLabel.stringValue = ""
        onToggle = nil
        lastIsRunning = false
        lastIsExpanded = false
        lastHasFailures = false
        hasConfigured = false
    }

    override func viewDidChangeEffectiveAppearance() {
        super.viewDidChangeEffectiveAppearance()
        applyLayerColors(animated: false)
    }

    /// Single source of truth for rendered tool group summary text (shared by drawing, copy, and Find).
    static func attributedText(
        for payload: PCodeToolGroupPayload,
        typography: PCodeTypographySnapshot,
        theme: PCodeTranscriptTheme
    ) -> NSAttributedString {
        let out = NSMutableAttributedString()
        let lineHeight = Self.lineHeight(typography)
        let style = NSMutableParagraphStyle()
        style.minimumLineHeight = lineHeight
        style.maximumLineHeight = lineHeight
        style.lineBreakMode = .byTruncatingTail

        let headFont = typography.codeNSFont(scale: 0.82, weight: typography.weight.nsEmphasisWeight)
        let bodyFont = typography.codeNSFont(scale: 0.80)
        let sepAttrs: [NSAttributedString.Key: Any] = [
            .font: bodyFont,
            .foregroundColor: theme.secondaryForeground.withAlphaComponent(0.42),
            .paragraphStyle: style,
        ]

        if payload.failedCount == 0 {
            let head = payload.totalCount == 1 ? "1 tool" : "\(payload.totalCount) tools"
            out.append(NSAttributedString(
                string: head,
                attributes: [
                    .font: headFont,
                    .foregroundColor: theme.foreground.withAlphaComponent(0.74),
                    .paragraphStyle: style,
                ]))
        } else {
            if payload.succeededCount > 0 {
                out.append(NSAttributedString(
                    string: "\(payload.succeededCount) ok",
                    attributes: [
                        .font: headFont,
                        .foregroundColor: theme.foreground.withAlphaComponent(0.84),
                        .paragraphStyle: style,
                    ]))
                out.append(NSAttributedString(string: " · ", attributes: sepAttrs))
            }
            out.append(NSAttributedString(
                string: "\(payload.failedCount) failed",
                attributes: [
                    .font: headFont,
                    .foregroundColor: NSColor.systemRed,
                    .paragraphStyle: style,
                ]))
        }
        if payload.runningCount > 0 {
            out.append(NSAttributedString(string: " · ", attributes: sepAttrs))
            out.append(NSAttributedString(
                string: "\(payload.runningCount) running",
                attributes: [
                    .font: headFont,
                    .foregroundColor: theme.accent,
                    .paragraphStyle: style,
                ]))
        }

        let categories = payload.categoryCounts
        if !categories.isEmpty {
            out.append(NSAttributedString(string: " · ", attributes: sepAttrs))
            for (idx, cat) in categories.enumerated() {
                if idx > 0 {
                    out.append(NSAttributedString(string: " · ", attributes: sepAttrs))
                }
                out.append(NSAttributedString(
                    string: "\(cat.count) \(cat.label)",
                    attributes: [
                        .font: bodyFont,
                        .foregroundColor: theme.secondaryForeground.withAlphaComponent(0.88),
                        .paragraphStyle: style,
                    ]))
                if cat.failedCount > 0 {
                    out.append(NSAttributedString(
                        string: " (\(cat.failedCount) failed)",
                        attributes: [
                            .font: bodyFont,
                            .foregroundColor: NSColor.systemRed.withAlphaComponent(0.9),
                            .paragraphStyle: style,
                        ]))
                }
            }
        }
        for entry in payload.windowEntries {
            out.append(NSAttributedString(string: "\n", attributes: sepAttrs))
            appendWindowRow(entry, to: out, font: bodyFont, style: style, theme: theme)
        }
        return out
    }

    /// A window row's status glyph: running, failed, ran but exited non-zero, or ok.
    /// Only a hard failure is red; a non-zero exit gets the card's muted dot.
    static func windowGlyph(for entry: PCodeToolGroupEntry, theme: PCodeTranscriptTheme) -> (String, NSColor) {
        if entry.isRunning { return ("◌", theme.accent) }
        if entry.failed { return ("✗", .systemRed) }
        if entry.exitedNonZero { return ("•", theme.secondaryForeground.withAlphaComponent(0.8)) }
        return ("✓", NSColor.systemGreen.withAlphaComponent(theme.isDark ? 0.80 : 0.75))
    }

    /// One window row: a status glyph, then the call on a single line. Every row must stay
    /// exactly one line, or the window's fixed height would clip it.
    private static func appendWindowRow(
        _ entry: PCodeToolGroupEntry,
        to out: NSMutableAttributedString,
        font: NSFont,
        style: NSParagraphStyle,
        theme: PCodeTranscriptTheme
    ) {
        let (glyph, glyphColor) = windowGlyph(for: entry, theme: theme)
        let name: String
        var detail: String
        switch entry.content {
        case .tool(let call):
            name = call.displayName
            detail = call.summaryText
            if let elapsed = call.formattedElapsed { detail += "  \(elapsed)" }
        case .diff(let diff):
            name = diff.displayToolName
            detail = PCodeDiffCardView.summaryLine(for: diff)
            if detail.hasPrefix(name) { detail = String(detail.dropFirst(name.count)) }
        }
        let oneLine = detail
            .split(whereSeparator: \.isNewline)
            .joined(separator: " ")
            .trimmingCharacters(in: .whitespaces)
        out.append(NSAttributedString(
            string: "\(glyph) ",
            attributes: [.font: font, .foregroundColor: glyphColor, .paragraphStyle: style]))
        out.append(NSAttributedString(
            string: name,
            attributes: [
                .font: font,
                .foregroundColor: theme.foreground.withAlphaComponent(0.84),
                .paragraphStyle: style,
            ]))
        if !oneLine.isEmpty {
            out.append(NSAttributedString(
                string: " \(oneLine)",
                attributes: [
                    .font: font,
                    .foregroundColor: theme.secondaryForeground.withAlphaComponent(0.88),
                    .paragraphStyle: style,
                ]))
        }
    }

    /// Height of a collapsed nested-call window: the summary row plus one line per shown
    /// call. It depends only on how many calls are shown, which never decreases.
    static func windowHeight(rowCount: Int, lineHeight: CGFloat) -> CGFloat {
        rowHeight + CGFloat(rowCount) * lineHeight + windowBottomPad
    }

    private static let windowBottomPad: CGFloat = 4

    /// Every line's height, clamped in `attributedText`; the window's fixed height relies on it.
    static func lineHeight(_ typography: PCodeTypographySnapshot) -> CGFloat {
        typography.lineHeight(scale: 0.84)
    }

    func configure(
        payload: PCodeToolGroupPayload,
        attributedText: NSAttributedString,
        width: CGFloat,
        lineHeight: CGFloat,
        theme: PCodeTranscriptTheme,
        selectedRange: NSRange?,
        stackPosition: PCodeToolStackPosition = .standalone,
        onToggle: @escaping () -> Void
    ) -> CGFloat {
        let wasConfigured = hasConfigured
        let expansionChanged = wasConfigured && (lastIsExpanded != payload.isExpanded)

        self.onToggle = onToggle
        self.lastTheme = theme
        self.lastIsRunning = payload.runningCount > 0
        self.lastIsExpanded = payload.isExpanded
        self.lastHasFailures = payload.failedCount > 0
        self.lastStackPosition = stackPosition
        self.hasConfigured = true

        let cfg = NSImage.SymbolConfiguration(pointSize: 10.5, weight: .medium)
        if payload.runningCount > 0 {
            iconView.image = NSImage(systemSymbolName: "circle.dotted", accessibilityDescription: "Tools running")?
                .withSymbolConfiguration(cfg)
            iconView.contentTintColor = theme.accent
        } else if payload.failedCount > 0 {
            iconView.image = NSImage(systemSymbolName: "exclamationmark.circle.fill", accessibilityDescription: "Some tools failed")?
                .withSymbolConfiguration(cfg)
            iconView.contentTintColor = .systemRed
        } else {
            iconView.image = NSImage(systemSymbolName: "checkmark.circle.fill", accessibilityDescription: "Tools completed")?
                .withSymbolConfiguration(cfg)
            iconView.contentTintColor = NSColor.systemGreen.withAlphaComponent(theme.isDark ? 0.80 : 0.75)
        }

        let elapsedText = payload.formattedTotalElapsed
        let elapsedWidth: CGFloat = elapsedText != nil ? 48 : 0
        let chevronWidth: CGFloat = 24
        let rightControlsWidth = chevronWidth + (elapsedWidth > 0 ? elapsedWidth + 4 : 0)

        let leftPad: CGFloat = 26
        let topPad: CGFloat = 5
        let bottomPad: CGFloat = 5
        let textWidth = max(1, width - leftPad - rightControlsWidth)
        let measuredHeight = textView.configure(attributedText: attributedText, width: textWidth)
        textView.selectedRange = selectedRange
        let windowRowCount = payload.windowEntries.count
        let totalHeight: CGFloat
        let textHeight: CGFloat
        let textY: CGFloat
        if windowRowCount > 0 {
            // Fixed by row count, never by measured text, so a long call can't resize it.
            totalHeight = Self.windowHeight(rowCount: windowRowCount, lineHeight: lineHeight)
            textHeight = CGFloat(windowRowCount + 1) * lineHeight
            textY = floor((Self.rowHeight - lineHeight) * 0.5)
        } else {
            totalHeight = max(Self.rowHeight, measuredHeight + topPad + bottomPad)
            textHeight = measuredHeight
            textY = floor((totalHeight - textHeight) * 0.5)
        }
        textView.frame = CGRect(x: leftPad, y: textY, width: textWidth, height: textHeight)
        // Controls line up with the summary row, which is the whole card unless windowed.
        let headerHeight = windowRowCount > 0 ? Self.rowHeight : totalHeight

        iconView.frame = CGRect(x: 9, y: floor((headerHeight - 12) * 0.5), width: 12, height: 12)

        if let elapsedText {
            elapsedLabel.stringValue = elapsedText
            elapsedLabel.font = NSFont.monospacedDigitSystemFont(ofSize: 10.5, weight: .regular)
            elapsedLabel.textColor = theme.secondaryForeground.withAlphaComponent(0.58)
            elapsedLabel.isHidden = false
            elapsedLabel.frame = CGRect(
                x: max(leftPad, width - chevronWidth - elapsedWidth - 4),
                y: floor((headerHeight - 15) * 0.5),
                width: elapsedWidth,
                height: 15)
        } else {
            elapsedLabel.isHidden = true
            elapsedLabel.stringValue = ""
        }

        chevronView.contentTintColor = theme.secondaryForeground.withAlphaComponent(0.85)
        let chevSize: CGFloat = 16
        let chevOrigin = CGPoint(x: width - 22, y: floor((headerHeight - chevSize) * 0.5))
        CATransaction.begin()
        CATransaction.setDisableActions(true)
        chevronContainer.frame = CGRect(origin: chevOrigin, size: CGSize(width: chevSize, height: chevSize))
        chevronView.frame = chevronContainer.bounds
        CATransaction.commit()
        updateChevronRotation(isExpanded: payload.isExpanded, animated: expansionChanged)

        tapButton.frame = CGRect(x: 0, y: 0, width: width, height: totalHeight)
        tapButton.toolTip = payload.isExpanded ? "Collapse tool group" : "Expand \(payload.totalCount) tool calls"

        applyLayerColors(animated: false)
        return totalHeight
    }

    private func updateChevronRotation(isExpanded: Bool, animated: Bool) {
        guard let chevLayer = chevronContainer.layer else { return }
        let targetAngle: CGFloat = isExpanded ? (.pi / 2) : 0
        let targetTransform = CATransform3DMakeRotation(targetAngle, 0, 0, 1)
        let reduceMotion = NSWorkspace.shared.accessibilityDisplayShouldReduceMotion

        // Keep anchorPoint centered while preserving frame origin.
        let oldFrame = chevLayer.frame
        if chevLayer.anchorPoint != CGPoint(x: 0.5, y: 0.5) {
            CATransaction.begin()
            CATransaction.setDisableActions(true)
            chevLayer.anchorPoint = CGPoint(x: 0.5, y: 0.5)
            chevLayer.frame = oldFrame
            CATransaction.commit()
        }

        if animated && !reduceMotion && window != nil {
            let anim = CABasicAnimation(keyPath: "transform.rotation.z")
            anim.fromValue = isExpanded ? 0 : (CGFloat.pi / 2)
            anim.toValue = targetAngle
            anim.duration = 0.18
            anim.timingFunction = CAMediaTimingFunction(controlPoints: 0.22, 1.0, 0.36, 1.0)
            chevLayer.add(anim, forKey: "pcode.group.chevron")
        }
        CATransaction.begin()
        CATransaction.setDisableActions(true)
        chevLayer.transform = targetTransform
        CATransaction.commit()
    }

    private func applyLayerColors(animated: Bool) {
        guard let theme = lastTheme else { return }
        let bgColor: NSColor?
        let borderWidth: CGFloat
        let borderColor: NSColor?
        if lastHasFailures {
            bgColor = NSColor.systemRed.withAlphaComponent(theme.isDark ? 0.08 : 0.055)
            borderWidth = 1
            borderColor = NSColor.systemRed.withAlphaComponent(theme.isDark ? 0.30 : 0.24)
        } else if lastIsRunning {
            bgColor = theme.toolBackground
            borderWidth = 1
            borderColor = theme.accent.withAlphaComponent(theme.isDark ? 0.36 : 0.28)
        } else if lastIsExpanded {
            bgColor = theme.foreground.withAlphaComponent(
                lastHovered ? (theme.isDark ? 0.085 : 0.062) : (theme.isDark ? 0.055 : 0.042))
            borderWidth = 1
            borderColor = theme.foreground.withAlphaComponent(theme.isDark ? 0.10 : 0.09)
        } else if lastHovered {
            bgColor = theme.foreground.withAlphaComponent(theme.isDark ? 0.088 : 0.068)
            borderWidth = 0
            borderColor = nil
        } else {
            // Collapsed, settled tool groups use a soft borderless fill at rest so they
            // separate cleanly from prose without heavy boxed borders.
            bgColor = theme.foreground.withAlphaComponent(theme.isDark ? 0.052 : 0.038)
            borderWidth = 0
            borderColor = nil
        }

        let effectiveStack: PCodeToolStackPosition = (lastIsRunning || lastIsExpanded || lastHasFailures) ? .standalone : lastStackPosition
        effectiveAppearance.performAsCurrentDrawingAppearance {
            CATransaction.begin()
            if animated && !NSWorkspace.shared.accessibilityDisplayShouldReduceMotion {
                CATransaction.setAnimationDuration(0.12)
                CATransaction.setAnimationTimingFunction(CAMediaTimingFunction(name: .easeOut))
            } else {
                CATransaction.setDisableActions(true)
            }
            layer?.backgroundColor = bgColor?.cgColor
            layer?.cornerRadius = 6
            layer?.maskedCorners = effectiveStack.maskedCorners
            layer?.borderWidth = borderWidth
            layer?.borderColor = borderColor?.cgColor
            CATransaction.commit()
        }
    }

    @objc private func togglePressed() {
        onToggle?()
    }
}

// MARK: - System / Housekeeping Note Row

/// Visual severity for system and housekeeping notes emitted by the host
/// (e.g. loaded repo instructions, skill commands, warnings, errors).
enum PCodeNoteTone: String, Equatable {
    case info
    case warning
    case error
}

final class PCodeNoteRowView: NSView, PCodeTranscriptRowHosting {
    static let fontScale: CGFloat = 0.84

    let itemKind: PCodeTranscriptItem.Kind = .note
    let textView = PCodeTextKit2BlockView()

    var primaryTextView: PCodeTextKit2BlockView? { textView }
    var selectableTextLength: Int { textView.textContentStorage.attributedString?.length ?? 0 }
    override var isFlipped: Bool { true }

    override init(frame frameRect: NSRect) {
        super.init(frame: frameRect)
        wantsLayer = true
        addSubview(textView)
    }

    @available(*, unavailable)
    required init?(coder: NSCoder) {
        fatalError("init(coder:) has not been implemented")
    }

    func setSelectedRange(_ range: NSRange?) {
        textView.selectedRange = range
    }

    static func attributedText(
        for text: String,
        tone: PCodeNoteTone,
        typography: PCodeTypographySnapshot,
        theme: PCodeTranscriptTheme
    ) -> NSAttributedString {
        let color: NSColor
        switch tone {
        case .info:
            color = theme.secondaryForeground
        case .warning:
            color = .systemOrange
        case .error:
            color = .systemRed
        }
        let paragraph = NSMutableParagraphStyle()
        let lineHeight = typography.lineHeight(scale: Self.fontScale)
        paragraph.minimumLineHeight = lineHeight
        paragraph.maximumLineHeight = lineHeight
        paragraph.lineBreakMode = .byWordWrapping
        return NSAttributedString(
            string: text,
            attributes: [
                .font: typography.proseNSFont(scale: Self.fontScale),
                .foregroundColor: color,
                .paragraphStyle: paragraph,
            ])
    }

    func configure(
        attributedText: NSAttributedString,
        width: CGFloat,
        selectedRange: NSRange?
    ) -> CGFloat {
        let insets = NSEdgeInsets(top: 1, left: 4, bottom: 1, right: 4)
        let textW = max(1, width - insets.left - insets.right)
        let textH = textView.configure(attributedText: attributedText, width: textW)
        textView.selectedRange = selectedRange
        textView.frame = CGRect(x: insets.left, y: insets.top, width: textW, height: textH)
        return textH + insets.top + insets.bottom
    }
}

// MARK: - Status Badge Row

final class PCodeStatusRowView: NSView, PCodeTranscriptRowHosting {
    let itemKind: PCodeTranscriptItem.Kind = .status
    let textView = PCodeTextKit2BlockView()

    var primaryTextView: PCodeTextKit2BlockView? { textView }
    var selectableTextLength: Int { textView.textContentStorage.attributedString?.length ?? 0 }
    override var isFlipped: Bool { true }

    override init(frame frameRect: NSRect) {
        super.init(frame: frameRect)
        wantsLayer = true
        addSubview(textView)
    }

    @available(*, unavailable)
    required init?(coder: NSCoder) {
        fatalError("init(coder:) has not been implemented")
    }

    func setSelectedRange(_ range: NSRange?) {
        textView.selectedRange = range
    }

    /// Single source of truth for rendered status text (shared by drawing and copy).
    static func displayLabel(for status: PCodePeekTurn.Status) -> String {
        switch status {
        case .running: return "In progress"
        case .cancelled: return "Cancelled"
        case .failed: return "Failed"
        case .complete: return "Complete"
        }
    }

    static func attributedText(
        for status: PCodePeekTurn.Status,
        typography: PCodeTypographySnapshot,
        theme: PCodeTranscriptTheme
    ) -> NSAttributedString {
        let color: NSColor
        switch status {
        case .running: color = theme.accent
        case .cancelled: color = .systemOrange
        case .failed: color = .systemRed
        case .complete: color = theme.secondaryForeground
        }
        return NSAttributedString(
            string: displayLabel(for: status),
            attributes: [
                .font: typography.proseNSFont(scale: 0.80, weight: .medium),
                .foregroundColor: color,
            ])
    }

    func configure(
        attributedText: NSAttributedString,
        width: CGFloat,
        selectedRange: NSRange?
    ) -> CGFloat {
        let textW = min(width, ceil(attributedText.size().width) + 2)
        let textH = textView.configure(attributedText: attributedText, width: textW)
        textView.selectedRange = selectedRange
        textView.frame = CGRect(x: 8, y: 3, width: textW, height: textH)
        return textH + 6
    }
}

// MARK: - Diff Card Row (`EditPreview` / `EditCompleted`)

final class PCodeDiffCardView: NSView, PCodeTranscriptRowHosting {
    let itemKind: PCodeTranscriptItem.Kind = .diff
    let scrollView = PCodeHorizontalBlockScrollView()
    let textView = PCodeTextKit2BlockView()
    private let headerView = NSView()
    private let iconView = NSImageView()
    private let spinnerView = PCodeRunningSpinnerView()
    private let toolLabel = NSTextField(labelWithString: "")
    private let pathLabel = NSTextField(labelWithString: "")
    private let statsLabel = NSTextField(labelWithString: "")
    private let copyButton = PCodeBlockActionButton(title: "Copy", symbol: "doc.on.doc")
    private let inspectButton = NSButton()
    private let toggleButton = NSButton()
    private let headerTapButton = NSButton()
    private let bottomCollapseButton = NSButton()
    private let dividerLayer = CALayer()
    private let bottomDivider = CALayer()

    private var patchToCopy: String = ""
    private var onToggle: (() -> Void)?
    private var onInspect: (() -> Void)?
    private var lastTheme: PCodeTranscriptTheme?
    private var lastIsExpanded: Bool = false

    var primaryTextView: PCodeTextKit2BlockView? { scrollView.isHidden ? nil : textView }
    var selectableTextLength: Int { scrollView.isHidden ? 0 : (textView.textContentStorage.attributedString?.length ?? 0) }
    override var isFlipped: Bool { true }

    static let headerHeight: CGFloat = 28
    private static let bottomCollapseBarHeight: CGFloat = 24

    override init(frame frameRect: NSRect) {
        super.init(frame: frameRect)
        wantsLayer = true
        headerView.wantsLayer = true
        headerView.layer?.addSublayer(dividerLayer)
        iconView.imageScaling = .scaleProportionallyDown

        for label in [toolLabel, pathLabel, statsLabel] {
            label.isEditable = false
            label.isSelectable = false
            label.drawsBackground = false
            label.isBezeled = false
            label.lineBreakMode = .byTruncatingMiddle
        }

        headerTapButton.isBordered = false
        headerTapButton.title = ""
        headerTapButton.target = self
        headerTapButton.action = #selector(togglePressed)

        copyButton.target = self
        copyButton.action = #selector(copyPatchPressed)

        inspectButton.isBordered = false
        inspectButton.title = ""
        inspectButton.target = self
        inspectButton.action = #selector(inspectPressed)
        inspectButton.toolTip = "Open diff in Inspector"
        inspectButton.isHidden = true

        toggleButton.isBordered = false
        toggleButton.title = ""
        toggleButton.target = self
        toggleButton.action = #selector(togglePressed)

        headerView.addSubview(headerTapButton)
        headerView.addSubview(iconView)
        spinnerView.isHidden = true
        headerView.addSubview(spinnerView)
        headerView.addSubview(toolLabel)
        headerView.addSubview(pathLabel)
        headerView.addSubview(statsLabel)
        headerView.addSubview(copyButton)
        headerView.addSubview(inspectButton)
        headerView.addSubview(toggleButton)
        addSubview(headerView)

        scrollView.documentView = textView
        addSubview(scrollView)

        layer?.addSublayer(bottomDivider)
        bottomDivider.isHidden = true

        bottomCollapseButton.isBordered = false
        bottomCollapseButton.bezelStyle = .inline
        bottomCollapseButton.imagePosition = .imageLeading
        bottomCollapseButton.target = self
        bottomCollapseButton.action = #selector(togglePressed)
        bottomCollapseButton.isHidden = true
        addSubview(bottomCollapseButton)
    }

    @available(*, unavailable)
    required init?(coder: NSCoder) {
        fatalError("init(coder:) has not been implemented")
    }

    func setSelectedRange(_ range: NSRange?) {
        textView.selectedRange = range
    }

    func resetForTranscriptReuse() {
        setHovered(false)
        setSelectedRange(nil)
        setCurtainRevealProgress(nil)
        textView.inkSweepFrontier = nil
        layer?.removeAllAnimations()
        scrollView.resetScrollOrigin()
        copyButton.cancelCopiedFlash()
        inspectButton.isHidden = true
        spinnerView.stopAnimating()
        spinnerView.isHidden = true
        iconView.isHidden = false
        onToggle = nil
        onInspect = nil
        bottomCollapseButton.isHidden = true
        bottomDivider.isHidden = true
    }

    override func viewDidChangeEffectiveAppearance() {
        super.viewDidChangeEffectiveAppearance()
        applyLayerColors()
    }

    static func diffLineCount(for payload: PCodeDiffPayload) -> Int {
        var s = payload.patch[...]
        if s.last?.isNewline == true {
            s = s.dropLast()
        }
        guard !s.isEmpty else { return 0 }
        return s.split(separator: "\n", omittingEmptySubsequences: false).count
    }

    static func summaryLine(for payload: PCodeDiffPayload) -> String {
        var parts = [payload.displayToolName, payload.path]
        if payload.added > 0 || payload.removed > 0 {
            parts.append("+\(payload.added) −\(payload.removed)")
        }
        if let elapsed = payload.formattedElapsed {
            parts.append("(\(elapsed))")
        }
        return parts.filter { !$0.isEmpty }.joined(separator: " ")
    }

    static func attributedPatch(
        for payload: PCodeDiffPayload,
        typography: PCodeTypographySnapshot,
        theme: PCodeTranscriptTheme
    ) -> NSAttributedString {
        let font = typography.codeNSFont(scale: typography.codeBlockScale)
        let lh = typography.lineHeight(scale: typography.codeBlockScale)
        let para = NSMutableParagraphStyle()
        para.minimumLineHeight = lh
        para.maximumLineHeight = lh

        let addBg = NSColor.systemGreen.withAlphaComponent(theme.isDark ? 0.16 : 0.12)
        let delBg = NSColor.systemRed.withAlphaComponent(theme.isDark ? 0.16 : 0.12)
        let hunkColor = theme.accent.withAlphaComponent(0.85)
        let addFg = theme.isDark ? NSColor(calibratedRed: 0.56, green: 0.90, blue: 0.62, alpha: 1) : NSColor(calibratedRed: 0.08, green: 0.45, blue: 0.18, alpha: 1)
        let delFg = theme.isDark ? NSColor(calibratedRed: 0.98, green: 0.56, blue: 0.56, alpha: 1) : NSColor(calibratedRed: 0.65, green: 0.12, blue: 0.14, alpha: 1)

        let out = NSMutableAttributedString()
        let rawLines = payload.patch.split(separator: "\n", omittingEmptySubsequences: false)
        let capped = rawLines.prefix(200)
        for (idx, lineSub) in capped.enumerated() {
            let line = String(lineSub) + (idx < capped.count - 1 ? "\n" : "")
            var attrs: [NSAttributedString.Key: Any] = [
                .font: font,
                .foregroundColor: theme.foreground,
                .paragraphStyle: para,
            ]
            if lineSub.hasPrefix("+") && !lineSub.hasPrefix("+++") {
                attrs[.foregroundColor] = addFg
                attrs[.backgroundColor] = addBg
            } else if lineSub.hasPrefix("-") && !lineSub.hasPrefix("---") {
                attrs[.foregroundColor] = delFg
                attrs[.backgroundColor] = delBg
            } else if lineSub.hasPrefix("@@") {
                attrs[.foregroundColor] = hunkColor
            }
            out.append(NSAttributedString(string: line, attributes: attrs))
        }
        if rawLines.count > 200 {
            out.append(NSAttributedString(
                string: "\n… (\(rawLines.count - 200) more diff lines)",
                attributes: [.font: font, .foregroundColor: theme.secondaryForeground, .paragraphStyle: para]))
        }
        return out
    }

    func configure(
        payload: PCodeDiffPayload,
        isExpanded: Bool,
        attributedPatch: NSAttributedString,
        width: CGFloat,
        typography: PCodeTypographySnapshot,
        theme: PCodeTranscriptTheme,
        documentView: PCodeTranscriptDocumentView?,
        selectedRange: NSRange?,
        onToggle: @escaping () -> Void,
        onInspect: (() -> Void)? = nil
    ) -> CGFloat {
        scrollView.transcriptDocumentView = documentView
        patchToCopy = payload.patch
        self.onToggle = onToggle
        self.onInspect = onInspect
        lastTheme = theme

        let canExpand = !payload.patch.isEmpty
        let padX: CGFloat = 12
        let padY: CGFloat = 8

        let iconFrame = CGRect(x: 9, y: floor((Self.headerHeight - 12) * 0.5), width: 12, height: 12)
        iconView.frame = iconFrame
        spinnerView.frame = iconFrame
        if payload.isPreview {
            iconView.isHidden = true
            spinnerView.isHidden = false
            spinnerView.startAnimating(color: theme.accent)
        } else {
            spinnerView.stopAnimating()
            spinnerView.isHidden = true
            iconView.isHidden = false
            let symCfg = NSImage.SymbolConfiguration(pointSize: 10, weight: .medium)
            let symbolName = payload.failed ? "xmark.circle.fill" : "checkmark.circle.fill"
            iconView.image = NSImage(systemSymbolName: symbolName, accessibilityDescription: payload.failed ? "Failed" : "Succeeded")?
                .withSymbolConfiguration(symCfg)
            iconView.contentTintColor = payload.failed ? .systemRed : theme.secondaryForeground.withAlphaComponent(0.8)
        }

        headerView.frame = CGRect(x: 0, y: 0, width: width, height: Self.headerHeight)
        headerTapButton.isHidden = !canExpand
        headerTapButton.frame = headerView.bounds

        toolLabel.stringValue = payload.displayToolName
        toolLabel.font = typography.codeNSFont(scale: 0.84, weight: typography.weight.nsEmphasisWeight)
        toolLabel.textColor = payload.failed ? .systemRed : theme.secondaryForeground
        toolLabel.sizeToFit()

        pathLabel.stringValue = payload.isPreview ? "\(payload.path) (preview)" : payload.path
        pathLabel.font = typography.codeNSFont(scale: 0.82)
        pathLabel.textColor = isExpanded ? theme.foreground : theme.secondaryForeground.withAlphaComponent(0.88)
        pathLabel.sizeToFit()

        let statsAttr = NSMutableAttributedString()
        if payload.added > 0 {
            statsAttr.append(NSAttributedString(
                string: "+\(payload.added)",
                attributes: [.font: typography.codeNSFont(scale: 0.78, weight: .semibold), .foregroundColor: NSColor.systemGreen]))
        }
        if payload.removed > 0 {
            if statsAttr.length > 0 { statsAttr.append(NSAttributedString(string: " ")) }
            statsAttr.append(NSAttributedString(
                string: "−\(payload.removed)",
                attributes: [.font: typography.codeNSFont(scale: 0.78, weight: .semibold), .foregroundColor: NSColor.systemRed]))
        }
        if let elapsed = payload.formattedElapsed {
            let prefix = statsAttr.length > 0 ? "  " : ""
            statsAttr.append(NSAttributedString(
                string: "\(prefix)(\(elapsed))",
                attributes: [
                    .font: typography.codeNSFont(scale: 0.78),
                    .foregroundColor: theme.secondaryForeground.withAlphaComponent(0.62),
                ]))
        }
        statsLabel.attributedStringValue = statsAttr
        statsLabel.sizeToFit()

        toggleButton.isHidden = !canExpand
        if canExpand {
            let chevron = isExpanded ? "chevron.down" : "chevron.right"
            toggleButton.image = NSImage(systemSymbolName: chevron, accessibilityDescription: isExpanded ? "Collapse" : "Expand")?
                .withSymbolConfiguration(NSImage.SymbolConfiguration(pointSize: 9, weight: .semibold))
            toggleButton.contentTintColor = theme.secondaryForeground
            toggleButton.frame = CGRect(x: width - 22, y: floor((Self.headerHeight - 20) * 0.5), width: 18, height: 20)
        }

        let showBody = canExpand && isExpanded
        let showInspect = onInspect != nil
        copyButton.isHidden = !showBody
        if showBody {
            effectiveAppearance.performAsCurrentDrawingAppearance {
                copyButton.applyTheme(foreground: theme.secondaryForeground, background: .clear)
            }
        }
        let toggleReserve: CGFloat = canExpand ? 26 : 10
        let inspectW: CGFloat = showInspect ? 20 : 0
        inspectButton.isHidden = !showInspect
        if showInspect {
            let symCfg = NSImage.SymbolConfiguration(pointSize: 10, weight: .medium)
            inspectButton.image = NSImage(systemSymbolName: "sidebar.right", accessibilityDescription: "Open diff in Inspector")?
                .withSymbolConfiguration(symCfg)
            inspectButton.contentTintColor = theme.secondaryForeground
            inspectButton.frame = CGRect(
                x: max(padX, width - toggleReserve - inspectW - 2),
                y: floor((Self.headerHeight - 20) * 0.5),
                width: inspectW,
                height: 20)
        }
        let inspectReserve: CGFloat = showInspect ? inspectW + 4 : 0
        let copyW: CGFloat = showBody ? max(54, copyButton.frame.width + 8) : 0
        if showBody {
            copyButton.frame = CGRect(
                x: max(padX, width - toggleReserve - inspectReserve - copyW - 4),
                y: floor((Self.headerHeight - 20) * 0.5),
                width: copyW,
                height: 20)
        }

        let toolX: CGFloat = 26
        let toolW = min(toolLabel.frame.width, 120)
        toolLabel.frame = CGRect(
            x: toolX,
            y: floor((Self.headerHeight - toolLabel.frame.height) * 0.5),
            width: toolW,
            height: toolLabel.frame.height)

        let statsW = statsLabel.frame.width
        let rightReserve = toggleReserve + inspectReserve + (showBody ? copyW + 8 : 0)
        let pathX = toolLabel.frame.maxX + 8
        let pathMaxW = max(40, width - pathX - statsW - rightReserve - 12)
        pathLabel.frame = CGRect(
            x: pathX,
            y: floor((Self.headerHeight - pathLabel.frame.height) * 0.5),
            width: min(pathLabel.frame.width, pathMaxW),
            height: pathLabel.frame.height)
        statsLabel.frame = CGRect(
            x: pathLabel.frame.maxX + 8,
            y: floor((Self.headerHeight - statsLabel.frame.height) * 0.5),
            width: statsW,
            height: statsLabel.frame.height)

        dividerLayer.isHidden = !showBody
        dividerLayer.frame = CGRect(x: 0, y: Self.headerHeight - 1, width: width, height: 1)

        if !showBody {
            scrollView.isHidden = true
            bottomCollapseButton.isHidden = true
            bottomDivider.isHidden = true
            applyLayerColors(isExpanded: false)
            return Self.headerHeight
        }

        let contentWidth = max(1, width - padX * 2)
        let codeFont = typography.codeNSFont(scale: typography.codeBlockScale)
        let widest = PCodeCodeBlockView.widestLineWidth(in: attributedPatch.string, font: codeFont)
        let layoutWidth = max(contentWidth, ceil(widest) + 12)
        let textHeight = attributedPatch.length > 0
            ? textView.configure(attributedText: attributedPatch, width: layoutWidth)
            : 0
        textView.selectedRange = selectedRange
        textView.frame = CGRect(x: 0, y: 0, width: layoutWidth, height: textHeight)

        scrollView.isHidden = textHeight <= 0
        scrollView.frame = CGRect(x: padX, y: Self.headerHeight + padY, width: contentWidth, height: textHeight)

        let contentBottom = Self.headerHeight + (textHeight > 0 ? textHeight + padY * 2 : 0)
        let lineCount = Self.diffLineCount(for: payload)
        let collapseTitle = lineCount > 1 ? " Collapse (\(lineCount) lines)" : " Collapse"
        bottomCollapseButton.isHidden = false
        bottomDivider.isHidden = false
        bottomCollapseButton.image = NSImage(systemSymbolName: "chevron.up", accessibilityDescription: "Collapse")?
            .withSymbolConfiguration(NSImage.SymbolConfiguration(pointSize: 9, weight: .semibold))
        bottomCollapseButton.contentTintColor = theme.secondaryForeground
        bottomCollapseButton.attributedTitle = NSAttributedString(
            string: collapseTitle,
            attributes: [
                .font: NSFont.systemFont(ofSize: 11, weight: .medium),
                .foregroundColor: theme.secondaryForeground,
            ])
        bottomCollapseButton.frame = CGRect(
            x: 8,
            y: contentBottom + 1,
            width: max(1, width - 16),
            height: Self.bottomCollapseBarHeight - 2)
        CATransaction.begin()
        CATransaction.setDisableActions(true)
        bottomDivider.frame = CGRect(x: 8, y: contentBottom, width: max(0, width - 16), height: 1)
        CATransaction.commit()

        applyLayerColors(isExpanded: true)
        return contentBottom + Self.bottomCollapseBarHeight
    }

    private func applyLayerColors(isExpanded: Bool? = nil) {
        if let isExpanded { lastIsExpanded = isExpanded }
        guard let theme = lastTheme else { return }
        let expanded = lastIsExpanded
        effectiveAppearance.performAsCurrentDrawingAppearance {
            layer?.backgroundColor = (expanded ? theme.codeBackground : theme.toolBackground).cgColor
            layer?.cornerRadius = expanded ? 8 : 5
            layer?.borderWidth = expanded ? 1 : 0
            layer?.borderColor = theme.foreground.withAlphaComponent(theme.isDark ? 0.10 : 0.12).cgColor
            dividerLayer.backgroundColor = theme.foreground.withAlphaComponent(theme.isDark ? 0.08 : 0.10).cgColor
            bottomDivider.backgroundColor = theme.foreground.withAlphaComponent(theme.isDark ? 0.10 : 0.12).cgColor
        }
    }

    @objc private func inspectPressed() {
        onInspect?()
    }

    @objc private func togglePressed() {
        onToggle?()
    }

    @objc private func copyPatchPressed() {
        let pb = NSPasteboard.general
        pb.clearContents()
        pb.setString(patchToCopy, forType: .string)
        copyButton.flashCopied()
    }
}

// MARK: - Thinking Row (Muted Inline Reasoning)

/// Thinking renders as quiet prose in the answer's column, not a card: the full text,
/// slightly smaller and in the secondary colour, behind a hairline rule that pulses while
/// it streams. No label; the styling says what it is. pcode's `show_thinking` decides
/// whether these rows show at all.
final class PCodeThinkingCardView: NSView, PCodeTranscriptRowHosting {
    static let verticalPadding: CGFloat = 3
    static let bodyScale: CGFloat = 0.9
    /// Matches `PCodeProseRowView`'s left inset so thinking lines up with the answer.
    private static let leadingInset: CGFloat = 4
    /// Body text indent past the hairline rule.
    private static let bodyIndent: CGFloat = 12
    private static let pulseKey = "thinkingPulse"

    let itemKind: PCodeTranscriptItem.Kind = .thinking
    let textView = PCodeTextKit2BlockView()
    private let rule = NSView()
    private var lastTheme: PCodeTranscriptTheme?

    var primaryTextView: PCodeTextKit2BlockView? { textView }
    var selectableTextLength: Int { textView.textContentStorage.attributedString?.length ?? 0 }
    override var isFlipped: Bool { true }

    override init(frame frameRect: NSRect) {
        super.init(frame: frameRect)
        wantsLayer = true
        addSubview(textView)
        rule.wantsLayer = true
        rule.layer?.cornerRadius = 1
        addSubview(rule)
    }

    @available(*, unavailable)
    required init?(coder: NSCoder) {
        fatalError("init(coder:) has not been implemented")
    }

    func setSelectedRange(_ range: NSRange?) {
        textView.selectedRange = range
    }

    override func viewDidChangeEffectiveAppearance() {
        super.viewDidChangeEffectiveAppearance()
        applyLayerColors()
    }

    /// "Thinking" while streaming, then "Thought for 12s" (or "Thought" when no duration was
    /// recorded); the row's accessibility label, since nothing on screen names it.
    static func accessibilityTitle(for thinking: PCodeThinkingPayload) -> String {
        if thinking.isStreaming { return "Thinking" }
        guard let seconds = thinking.elapsedSeconds else { return "Thought" }
        let whole = max(1, Int(seconds.rounded()))
        if whole < 60 { return "Thought for \(whole)s" }
        let minutes = whole / 60
        let rest = whole % 60
        return rest == 0 ? "Thought for \(minutes)m" : "Thought for \(minutes)m \(rest)s"
    }

    /// The selectable body: muted Markdown once settled, plain text while streaming
    /// (re-parsing Markdown on every delta would be quadratic in the block's length).
    static func attributedText(
        for thinking: PCodeThinkingPayload,
        typography: PCodeTypographySnapshot,
        theme: PCodeTranscriptTheme
    ) -> NSAttributedString {
        let trimmed = thinking.text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return NSAttributedString() }
        let muted = mutedTheme(theme)
        if !thinking.isStreaming {
            return PCodeTranscriptMarkdown.attributedString(
                forProse: trimmed,
                typography: typography,
                theme: muted,
                scale: bodyScale)
        }
        let style = NSMutableParagraphStyle()
        let lh = typography.lineHeight(scale: bodyScale)
        style.minimumLineHeight = lh
        style.maximumLineHeight = lh
        style.paragraphSpacing = 6
        return NSAttributedString(
            string: trimmed.replacingOccurrences(of: "\n\n", with: "\n"),
            attributes: [
                .font: typography.proseNSFont(scale: bodyScale),
                .foregroundColor: muted.foreground,
                .paragraphStyle: style,
            ])
    }

    /// The transcript theme with prose pushed back to the secondary colour.
    private static func mutedTheme(_ theme: PCodeTranscriptTheme) -> PCodeTranscriptTheme {
        PCodeTranscriptTheme(
            isDark: theme.isDark,
            background: theme.background,
            foreground: theme.secondaryForeground,
            secondaryForeground: theme.secondaryForeground.withAlphaComponent(0.55),
            accent: theme.accent,
            promptBackground: theme.promptBackground,
            codeBackground: theme.codeBackground,
            inlineCodeFill: theme.inlineCodeFill,
            toolBackground: theme.toolBackground)
    }

    func configure(
        thinking: PCodeThinkingPayload,
        attributedText: NSAttributedString,
        width: CGFloat,
        typography: PCodeTypographySnapshot,
        theme: PCodeTranscriptTheme,
        selectedRange: NSRange?
    ) -> CGFloat {
        lastTheme = theme
        setPulsing(thinking.isStreaming)

        let inset = Self.leadingInset
        let textWidth = max(1, width - inset - Self.bodyIndent - inset)
        let textHeight = attributedText.length > 0
            ? textView.configure(attributedText: attributedText, width: textWidth)
            : 0
        textView.isHidden = attributedText.length == 0
        rule.isHidden = textView.isHidden
        textView.selectedRange = selectedRange
        let top = Self.verticalPadding
        textView.frame = CGRect(x: inset + Self.bodyIndent, y: top, width: textWidth, height: textHeight)
        rule.frame = CGRect(x: inset + 1, y: top + 2, width: 2, height: max(0, textHeight - 4))

        applyLayerColors()
        return textHeight + Self.verticalPadding * 2
    }

    func resetForTranscriptReuse() {
        setSelectedRange(nil)
        setCurtainRevealProgress(nil)
        textView.inkSweepFrontier = nil
        layer?.removeAllAnimations()
        setPulsing(false)
    }

    private func setPulsing(_ pulsing: Bool) {
        guard let layer = rule.layer else { return }
        let animating = layer.animation(forKey: Self.pulseKey) != nil
        if pulsing && !animating && !NSWorkspace.shared.accessibilityDisplayShouldReduceMotion {
            let pulse = CABasicAnimation(keyPath: "opacity")
            pulse.fromValue = 1.0
            pulse.toValue = 0.25
            pulse.duration = 0.9
            pulse.autoreverses = true
            pulse.repeatCount = .infinity
            pulse.timingFunction = CAMediaTimingFunction(name: .easeInEaseOut)
            layer.add(pulse, forKey: Self.pulseKey)
        } else if !pulsing && animating {
            layer.removeAnimation(forKey: Self.pulseKey)
        }
    }

    private func applyLayerColors() {
        guard let theme = lastTheme else { return }
        effectiveAppearance.performAsCurrentDrawingAppearance {
            rule.layer?.backgroundColor = theme.foreground.withAlphaComponent(theme.isDark ? 0.16 : 0.14).cgColor
        }
    }
}
