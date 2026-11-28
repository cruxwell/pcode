import AppKit
import CoreText
import Foundation
import QuartzCore
import SwiftUI

// MARK: - Shared Keys & Theme

struct PCodeDiffExpandedGapKey: Hashable, Sendable {
    let fileID: String
    let gapID: String
}

/// Concrete AppKit palette for `PCodeDiffScrollView`, immune to macOS Light Mode
/// resolving dynamic catalog colors against the wrong appearance when Ghostty uses a dark theme.
struct PCodeDiffTheme: Equatable {
    let isDark: Bool
    let background: NSColor
    let foreground: NSColor
    let secondaryForeground: NSColor
    let accent: NSColor
    let codeFont: NSFont
    let gutterFont: NSFont
    let headerTitleFont: NSFont
    let headerPathFont: NSFont
    let hunkHeaderFont: NSFont
    let noticeFont: NSFont
    let syntaxPalette: PCodeSyntaxPalette

    let addedRowFill: NSColor
    let deletedRowFill: NSColor
    let metaRowFill: NSColor
    let selectedRowFill: NSColor
    let addedIntralineFill: NSColor
    let deletedIntralineFill: NSColor
    let addedMarkerColor: NSColor
    let deletedMarkerColor: NSColor
    let mutedMarkerColor: NSColor
    let hunkHeaderFill: NSColor
    let hunkHeaderTextColor: NSColor
    let headerBaseFill: NSColor
    let headerFocusedTint: NSColor
    let headerStreamingTint: NSColor
    let expandBarFill: NSColor
    let expandBarHoverFill: NSColor
    let dividerColor: NSColor
    let fillerCellFill: NSColor

    let lineRowHeight: CGFloat
    let charAdvance: CGFloat
    let unifiedNumWidth: CGFloat
    let unifiedMarkerWidth: CGFloat
    let sideBySideNumWidth: CGFloat
    let sideBySideMarkerWidth: CGFloat

    var unifiedGutterWidth: CGFloat {
        unifiedNumWidth + unifiedMarkerWidth
    }

    var sideBySideHalfGutterWidth: CGFloat {
        sideBySideNumWidth + sideBySideMarkerWidth
    }

    /// Highest line number displayed across `diffs` and any loaded `expandedGaps`,
    /// used to size the line-number gutter dynamically without reserving 5 digits for small files.
    static func maxLineNumber(
        in diffs: [PCodeFileDiff],
        expandedGaps: [PCodeDiffExpandedGapKey: [PCodeDiffLine]] = [:]
    ) -> Int {
        var highest = 0
        for diff in diffs {
            for hunk in diff.hunks {
                if hunk.oldCount > 0 {
                    highest = max(highest, hunk.oldStart + hunk.oldCount - 1)
                } else {
                    highest = max(highest, hunk.oldStart)
                }
                if hunk.newCount > 0 {
                    highest = max(highest, hunk.newStart + hunk.newCount - 1)
                } else {
                    highest = max(highest, hunk.newStart)
                }
                if let last = hunk.lines.last {
                    if let old = last.oldLineNumber { highest = max(highest, old) }
                    if let new = last.newLineNumber { highest = max(highest, new) }
                }
            }
        }
        if !expandedGaps.isEmpty {
            let activeFileIDs = Set(diffs.map(\.id))
            for (key, lines) in expandedGaps where activeFileIDs.contains(key.fileID) {
                if let first = lines.first {
                    if let old = first.oldLineNumber { highest = max(highest, old) }
                    if let new = first.newLineNumber { highest = max(highest, new) }
                }
                if let last = lines.last {
                    if let old = last.oldLineNumber { highest = max(highest, old) }
                    if let new = last.newLineNumber { highest = max(highest, new) }
                }
            }
        }
        return highest
    }

    static func make(
        isDark: Bool,
        surfaceBackground: NSColor,
        foreground: NSColor?,
        codeFont: NSFont,
        maxLineNumber: Int = 999
    ) -> PCodeDiffTheme {
        let concreteFg = foreground ?? (isDark ? NSColor(white: 0.90, alpha: 1.0) : NSColor(white: 0.12, alpha: 1.0))
        let secondaryFg = concreteFg.withAlphaComponent(0.62)
        let accent = NSColor.controlAccentColor
        let palette = PCodeSyntaxPalette.make(isDark: isDark, foreground: concreteFg)

        let gutterFont = NSFont.monospacedDigitSystemFont(ofSize: max(8.5, codeFont.pointSize - 1.2), weight: .regular)
        let headerTitleFont = NSFont.systemFont(ofSize: 11.5, weight: .semibold)
        let headerPathFont = NSFont.systemFont(ofSize: 10.5, weight: .regular)
        let hunkHeaderFont = NSFont.monospacedSystemFont(ofSize: 10, weight: .regular)
        let noticeFont = NSFont.systemFont(ofSize: 11, weight: .regular)

        let rowHeight = ceil(codeFont.pointSize * 1.32) + 1
        let sampleWidth = ("M" as NSString).size(withAttributes: [.font: codeFont]).width
        let charAdvance = max(6.0, sampleWidth)
        let digits = max(2, String(max(1, maxLineNumber)).count)
        let digitsSample = String(repeating: "9", count: digits)
        let digitsWidth = ceil((digitsSample as NSString).size(withAttributes: [.font: gutterFont]).width)
        // 3.5pt left (clears the 2.5pt selection bar) + 3.5pt right before the +/- marker.
        let numColWidth = max(20, digitsWidth + 7)
        let markerWidth = max(11, ceil(("+" as NSString).size(withAttributes: [.font: gutterFont]).width) + 3)

        let primaryTintBase = isDark ? NSColor.white : NSColor.black
        let headerBase = surfaceBackground.blended(
            withFraction: isDark ? 0.06 : 0.045,
            of: primaryTintBase
        ) ?? surfaceBackground

        return PCodeDiffTheme(
            isDark: isDark,
            background: surfaceBackground,
            foreground: concreteFg,
            secondaryForeground: secondaryFg,
            accent: accent,
            codeFont: codeFont,
            gutterFont: gutterFont,
            headerTitleFont: headerTitleFont,
            headerPathFont: headerPathFont,
            hunkHeaderFont: hunkHeaderFont,
            noticeFont: noticeFont,
            syntaxPalette: palette,
            addedRowFill: NSColor.systemGreen.withAlphaComponent(isDark ? 0.14 : 0.10),
            deletedRowFill: NSColor.systemRed.withAlphaComponent(isDark ? 0.14 : 0.10),
            metaRowFill: primaryTintBase.withAlphaComponent(isDark ? 0.04 : 0.03),
            selectedRowFill: accent.withAlphaComponent(isDark ? 0.24 : 0.16),
            addedIntralineFill: NSColor.systemGreen.withAlphaComponent(isDark ? 0.34 : 0.28),
            deletedIntralineFill: NSColor.systemRed.withAlphaComponent(isDark ? 0.34 : 0.28),
            addedMarkerColor: isDark
                ? NSColor(red: 0.52, green: 0.87, blue: 0.56, alpha: 1.0)
                : NSColor(red: 0.11, green: 0.50, blue: 0.22, alpha: 1.0),
            deletedMarkerColor: isDark
                ? NSColor(red: 0.98, green: 0.50, blue: 0.50, alpha: 1.0)
                : NSColor(red: 0.76, green: 0.14, blue: 0.16, alpha: 1.0),
            mutedMarkerColor: secondaryFg.withAlphaComponent(0.55),
            hunkHeaderFill: accent.withAlphaComponent(isDark ? 0.08 : 0.05),
            hunkHeaderTextColor: accent.withAlphaComponent(isDark ? 0.85 : 0.90),
            headerBaseFill: headerBase,
            headerFocusedTint: accent.withAlphaComponent(isDark ? 0.10 : 0.06),
            headerStreamingTint: NSColor.systemGreen.withAlphaComponent(isDark ? 0.08 : 0.05),
            expandBarFill: primaryTintBase.withAlphaComponent(isDark ? 0.04 : 0.03),
            expandBarHoverFill: primaryTintBase.withAlphaComponent(isDark ? 0.09 : 0.07),
            dividerColor: primaryTintBase.withAlphaComponent(isDark ? 0.14 : 0.10),
            fillerCellFill: primaryTintBase.withAlphaComponent(isDark ? 0.035 : 0.025),
            lineRowHeight: rowHeight,
            charAdvance: charAdvance,
            unifiedNumWidth: numColWidth,
            unifiedMarkerWidth: markerWidth,
            sideBySideNumWidth: numColWidth,
            sideBySideMarkerWidth: max(10, markerWidth - 1)
        )
    }
}

// MARK: - Flat Virtualized Row Model & Layout Engine

enum PCodeDiffRowKind: Hashable {
    case fileHeader
    case hunkHeader
    case expandGap
    case unifiedLine
    case sideBySideRow
    case notice
    case spacer
}

struct PCodeDiffExpandGapPayload: Equatable {
    let fileID: String
    let gapID: String
    let filePath: String
    let afterLine: Int
    let beforeLine: Int
    let oldLineOffset: Int
    let fromBottom: Bool
    let hiddenCount: Int
    let step: Int
}

/// Precomputed syntax-highlighting payload shared by all lines in a single hunk or expanded gap,
/// avoiding per-line `joined(separator: "\n")` allocations and multi-KB key hashing during 120fps scrolling.
final class PCodeDiffHunkSyntaxContext {
    let hunkKey: String
    let hunkKeyHash: Int
    let joinedCode: String
    let lineCount: Int
    let normalizedLanguage: String

    init(lines: [String], language: String?) {
        let lang = PCodePrismSyntaxHighlighter.normalizeLanguage(language)
        let joined = lines.joined(separator: "\n")
        let key = "\(lang)\u{0}\(joined)"
        self.hunkKey = key
        self.hunkKeyHash = key.hashValue
        self.joinedCode = joined
        self.lineCount = lines.count
        self.normalizedLanguage = lang
    }
}

struct PCodeDiffUnifiedLinePayload {
    let fileID: String
    let filePath: String
    let syntaxContext: PCodeDiffHunkSyntaxContext
    let lineIndex: Int
    let line: PCodeDiffLine
}

struct PCodeDiffSideBySidePayload {
    let fileID: String
    let filePath: String
    let syntaxContext: PCodeDiffHunkSyntaxContext
    let row: PCodeSideBySideRow
    let leftLineIndex: Int?
    let rightLineIndex: Int?
}

enum PCodeDiffFlatRow {
    case fileHeader(diff: PCodeFileDiff, isCollapsed: Bool, isFocused: Bool, hasUnseenUpdate: Bool, isCopied: Bool)
    case hunkHeader(fileID: String, hunkID: String, header: String)
    case expandGap(PCodeDiffExpandGapPayload)
    case unifiedLine(PCodeDiffUnifiedLinePayload)
    case sideBySideRow(PCodeDiffSideBySidePayload)
    case notice(fileID: String, symbolName: String?, message: String)
    case spacer(fileID: String)

    var kind: PCodeDiffRowKind {
        switch self {
        case .fileHeader: return .fileHeader
        case .hunkHeader: return .hunkHeader
        case .expandGap: return .expandGap
        case .unifiedLine: return .unifiedLine
        case .sideBySideRow: return .sideBySideRow
        case .notice: return .notice
        case .spacer: return .spacer
        }
    }

    var hunkKey: String? {
        switch self {
        case .unifiedLine(let payload): return payload.syntaxContext.hunkKey
        case .sideBySideRow(let payload): return payload.syntaxContext.hunkKey
        default: return nil
        }
    }
}

/// O(log N) flat layout engine for virtualized diff scrolling.
/// Supports fixed-height single-line rows (default) and optional soft-wrapped rows.
struct PCodeDiffLayoutSnapshot {
    static let fileHeaderHeight: CGFloat = 26
    static let hunkHeaderHeight: CGFloat = 20
    static let expandGapHeight: CGFloat = 20
    static let noticeHeight: CGFloat = 28
    static let truncatedNoticeHeight: CGFloat = 22
    static let fileBottomSpacerHeight: CGFloat = 4
    static let wrapRightPadding: CGFloat = 10
    static let minWrapCodeWidth: CGFloat = 40

    static func unifiedWrapCodeWidth(viewportWidth: CGFloat, theme: PCodeDiffTheme) -> CGFloat {
        floor(max(minWrapCodeWidth, viewportWidth - theme.unifiedGutterWidth - wrapRightPadding))
    }

    static func sideBySideWrapCodeWidth(viewportWidth: CGFloat, theme: PCodeDiffTheme) -> CGFloat {
        let halfW = floor((viewportWidth - 1) * 0.5)
        return floor(max(minWrapCodeWidth, halfW - theme.sideBySideHalfGutterWidth - wrapRightPadding))
    }

    /// Compute the number of visual wrapped lines for `text` at `wrapWidth` using CoreText's
    /// `CTTypesetterSuggestLineBreak` (matching `PCodeDiffCTLineCache` exactly).
    static func wrappedLineCount(
        for text: String,
        wrapWidth: CGFloat,
        codeFont: NSFont,
        charAdvance: CGFloat
    ) -> Int {
        guard !text.isEmpty, wrapWidth > 0 else { return 1 }
        var approxWidth: CGFloat = 0
        var canUseFastPath = true
        for byte in text.utf8 {
            if byte == 9 { // tab: CoreText default tab interval is 28pt
                approxWidth += 28.0
            } else if byte < 0x20 || byte >= 0x7F {
                canUseFastPath = false
                break
            } else {
                approxWidth += charAdvance
            }
        }
        if canUseFastPath && approxWidth <= wrapWidth - charAdvance * 2 {
            return 1
        }
        let attr = NSAttributedString(string: text, attributes: [.font: codeFont])
        let typesetter = CTTypesetterCreateWithAttributedString(attr)
        let totalLen = attr.length
        var start = 0
        var count = 0
        while start < totalLen {
            var breakLen = CTTypesetterSuggestLineBreak(typesetter, start, Double(wrapWidth))
            if breakLen <= 0 {
                breakLen = max(1, CTTypesetterSuggestClusterBreak(typesetter, start, Double(wrapWidth)))
            }
            start += breakLen
            count += 1
        }
        return max(1, count)
    }

    let rows: [PCodeDiffFlatRow]
    let yOffsets: [CGFloat]
    let heights: [CGFloat]
    let totalHeight: CGFloat
    /// Row indices of `.fileHeader` rows in ascending order (for O(log F) sticky header lookup).
    let fileHeaderIndices: [Int]
    /// Map from `diff.id` (and `diff.path` for last occurrence) to header Y offset.
    let anchorYByID: [String: CGFloat]
    let anchorYByPath: [String: CGFloat]
    /// Estimated max code text width (excluding gutter) in unified and side-by-side modes.
    let maxUnifiedCodeWidth: CGFloat
    let maxLeftCodeWidth: CGFloat
    let maxRightCodeWidth: CGFloat
    /// Active wrap width (in points) used to size rows when soft-wrap is enabled; `nil` when unwrapped.
    let wrapCodeWidth: CGFloat?

    static let empty = PCodeDiffLayoutSnapshot(
        rows: [],
        yOffsets: [],
        heights: [],
        totalHeight: 0,
        fileHeaderIndices: [],
        anchorYByID: [:],
        anchorYByPath: [:],
        maxUnifiedCodeWidth: 0,
        maxLeftCodeWidth: 0,
        maxRightCodeWidth: 0,
        wrapCodeWidth: nil
    )

    static func build(
        diffs: [PCodeFileDiff],
        layout: PCodeDiffLayout,
        collapsedFiles: Set<String>,
        expandedGaps: [PCodeDiffExpandedGapKey: [PCodeDiffLine]],
        focusedFilePath: String?,
        updatedUnseenFiles: Set<String>,
        copiedPath: String?,
        lineRowHeight: CGFloat,
        charAdvance: CGFloat,
        bottomPadding: CGFloat,
        wrapCodeWidth: CGFloat? = nil,
        codeFont: NSFont? = nil
    ) -> PCodeDiffLayoutSnapshot {
        guard !diffs.isEmpty else { return .empty }

        var rows: [PCodeDiffFlatRow] = []
        var yOffsets: [CGFloat] = []
        var heights: [CGFloat] = []
        var fileHeaderIndices: [Int] = []
        var anchorYByID: [String: CGFloat] = [:]
        var anchorYByPath: [String: CGFloat] = [:]

        var maxUnifiedChars = 0
        var maxLeftChars = 0
        var maxRightChars = 0

        var cursorY: CGFloat = 0

        @inline(__always)
        func appendRow(_ row: PCodeDiffFlatRow, height: CGFloat) {
            rows.append(row)
            yOffsets.append(cursorY)
            heights.append(height)
            cursorY += height
        }

        @inline(__always)
        func visualColumnCount(_ text: String) -> Int {
            var cols = 0
            for ch in text.utf8 {
                if ch == 9 { // tab
                    cols += 4
                } else {
                    cols += 1
                }
            }
            return cols
        }

        @inline(__always)
        func rowHeight(for text: String) -> CGFloat {
            guard let wrapWidth = wrapCodeWidth, let font = codeFont else {
                return lineRowHeight
            }
            let lines = wrappedLineCount(
                for: text,
                wrapWidth: wrapWidth,
                codeFont: font,
                charAdvance: charAdvance
            )
            return CGFloat(lines) * lineRowHeight
        }

        @inline(__always)
        func sideBySideRowHeight(left: PCodeDiffLine?, right: PCodeDiffLine?, isMeta: Bool) -> CGFloat {
            guard !isMeta, let wrapWidth = wrapCodeWidth, let font = codeFont else {
                return lineRowHeight
            }
            if let l = left, let r = right, l.text == r.text {
                let lines = wrappedLineCount(for: l.text, wrapWidth: wrapWidth, codeFont: font, charAdvance: charAdvance)
                return CGFloat(lines) * lineRowHeight
            }
            let leftLines = left.map {
                wrappedLineCount(for: $0.text, wrapWidth: wrapWidth, codeFont: font, charAdvance: charAdvance)
            } ?? 1
            let rightLines = right.map {
                wrappedLineCount(for: $0.text, wrapWidth: wrapWidth, codeFont: font, charAdvance: charAdvance)
            } ?? 1
            return CGFloat(max(leftLines, rightLines)) * lineRowHeight
        }

        for diff in diffs {
            let isCollapsed = collapsedFiles.contains(diff.id)
            let isFocused = (focusedFilePath == diff.path || focusedFilePath == diff.id)
            let hasUnseen = updatedUnseenFiles.contains(diff.path)
            let isCopied = (copiedPath == diff.path)

            fileHeaderIndices.append(rows.count)
            anchorYByID[diff.id] = cursorY
            anchorYByPath[diff.path] = cursorY
            appendRow(
                .fileHeader(
                    diff: diff,
                    isCollapsed: isCollapsed,
                    isFocused: isFocused,
                    hasUnseenUpdate: hasUnseen,
                    isCopied: isCopied
                ),
                height: fileHeaderHeight
            )

            if isCollapsed { continue }

            if diff.hunks.isEmpty {
                let (symbol, message): (String?, String) = {
                    if PCodeDiffParser.isBinaryPatch(diff.rawPatch) {
                        return ("doc.fill", "Binary file changed")
                    } else if diff.kind == .renamed {
                        return ("arrow.right.doc.on.clipboard", "File renamed with no content changes")
                    } else if diff.kind == .added {
                        return ("doc.badge.plus", "Empty file created")
                    } else if diff.kind == .deleted {
                        return ("trash", "File deleted")
                    } else {
                        return (nil, "No textual hunks to display")
                    }
                }()
                appendRow(.notice(fileID: diff.id, symbolName: symbol, message: message), height: noticeHeight)
                continue
            }

            func appendExpandedLines(_ loaded: [PCodeDiffLine], gapKey: String) {
                guard !loaded.isEmpty else { return }
                let syntaxCtx = PCodeDiffHunkSyntaxContext(lines: loaded.map(\.text), language: diff.language)
                for (idx, line) in loaded.enumerated() {
                    let cols = visualColumnCount(line.text)
                    maxUnifiedChars = max(maxUnifiedChars, cols)
                    maxLeftChars = max(maxLeftChars, cols)
                    maxRightChars = max(maxRightChars, cols)
                    if layout == .sideBySide {
                        let sbsRow = PCodeSideBySideRow(id: "exp-\(gapKey)-\(idx)", left: line, right: line, isMeta: false)
                        appendRow(
                            .sideBySideRow(PCodeDiffSideBySidePayload(
                                fileID: diff.id,
                                filePath: diff.path,
                                syntaxContext: syntaxCtx,
                                row: sbsRow,
                                leftLineIndex: idx,
                                rightLineIndex: idx
                            )),
                            height: sideBySideRowHeight(left: line, right: line, isMeta: false)
                        )
                    } else {
                        appendRow(
                            .unifiedLine(PCodeDiffUnifiedLinePayload(
                                fileID: diff.id,
                                filePath: diff.path,
                                syntaxContext: syntaxCtx,
                                lineIndex: idx,
                                line: line
                            )),
                            height: rowHeight(for: line.text)
                        )
                    }
                }
            }

            func appendGap(
                gapID: String,
                filePath: String,
                afterLine: Int,
                beforeLine: Int,
                oldLineOffset: Int,
                fromBottom: Bool
            ) {
                let key = PCodeDiffExpandedGapKey(fileID: diff.id, gapID: gapID)
                let loaded = expandedGaps[key] ?? []
                let remainingAfter = fromBottom ? afterLine : (loaded.last?.newLineNumber ?? afterLine)
                let remainingBefore = fromBottom ? (loaded.first?.newLineNumber ?? beforeLine) : beforeLine
                let hiddenCount = max(0, remainingBefore - remainingAfter - 1)

                if !fromBottom && !loaded.isEmpty {
                    appendExpandedLines(loaded, gapKey: gapID)
                }
                if hiddenCount > 0 {
                    let step = min(20, hiddenCount)
                    appendRow(
                        .expandGap(PCodeDiffExpandGapPayload(
                            fileID: diff.id,
                            gapID: gapID,
                            filePath: filePath,
                            afterLine: remainingAfter,
                            beforeLine: remainingBefore,
                            oldLineOffset: oldLineOffset,
                            fromBottom: fromBottom,
                            hiddenCount: hiddenCount,
                            step: step
                        )),
                        height: expandGapHeight
                    )
                }
                if fromBottom && !loaded.isEmpty {
                    appendExpandedLines(loaded, gapKey: gapID)
                }
            }

            for (hunkIndex, hunk) in diff.hunks.enumerated() {
                if hunkIndex == 0 && hunk.newStart > 1, let absPath = diff.absolutePath {
                    appendGap(
                        gapID: "top-\(hunk.id)",
                        filePath: absPath,
                        afterLine: 0,
                        beforeLine: hunk.newStart,
                        oldLineOffset: hunk.oldStart - hunk.newStart,
                        fromBottom: true
                    )
                } else if hunkIndex > 0, let absPath = diff.absolutePath {
                    let prev = diff.hunks[hunkIndex - 1]
                    let prevEndNew = prev.newStart + max(0, prev.newCount - 1)
                    let prevEndOld = prev.oldStart + max(0, prev.oldCount - 1)
                    if hunk.newStart - prevEndNew > 1 {
                        appendGap(
                            gapID: "between-\(prev.id)-\(hunk.id)",
                            filePath: absPath,
                            afterLine: prevEndNew,
                            beforeLine: hunk.newStart,
                            oldLineOffset: prevEndOld - prevEndNew,
                            fromBottom: false
                        )
                    }
                }

                appendRow(.hunkHeader(fileID: diff.id, hunkID: hunk.id, header: hunk.header), height: hunkHeaderHeight)

                let syntaxCtx = PCodeDiffHunkSyntaxContext(lines: hunk.lines.map(\.text), language: diff.language)

                if layout == .sideBySide {
                    let pairedRows = PCodeDiffParser.sideBySideRows(for: hunk)
                    var indexByLineID: [String: Int] = [:]
                    indexByLineID.reserveCapacity(hunk.lines.count)
                    for (idx, line) in hunk.lines.enumerated() {
                        indexByLineID[line.id] = idx
                    }
                    for sbsRow in pairedRows {
                        if let l = sbsRow.left { maxLeftChars = max(maxLeftChars, visualColumnCount(l.text)) }
                        if let r = sbsRow.right { maxRightChars = max(maxRightChars, visualColumnCount(r.text)) }
                        appendRow(
                            .sideBySideRow(PCodeDiffSideBySidePayload(
                                fileID: diff.id,
                                filePath: diff.path,
                                syntaxContext: syntaxCtx,
                                row: sbsRow,
                                leftLineIndex: sbsRow.left.flatMap { indexByLineID[$0.id] },
                                rightLineIndex: sbsRow.right.flatMap { indexByLineID[$0.id] }
                            )),
                            height: sideBySideRowHeight(left: sbsRow.left, right: sbsRow.right, isMeta: sbsRow.isMeta)
                        )
                    }
                } else {
                    for (idx, line) in hunk.lines.enumerated() {
                        maxUnifiedChars = max(maxUnifiedChars, visualColumnCount(line.text))
                        appendRow(
                            .unifiedLine(PCodeDiffUnifiedLinePayload(
                                fileID: diff.id,
                                filePath: diff.path,
                                syntaxContext: syntaxCtx,
                                lineIndex: idx,
                                line: line
                            )),
                            height: rowHeight(for: line.text)
                        )
                    }
                }
            }

            if diff.truncated {
                appendRow(
                    .notice(fileID: diff.id, symbolName: "ellipsis.circle", message: "Diff output was truncated"),
                    height: truncatedNoticeHeight
                )
            }

            appendRow(.spacer(fileID: diff.id), height: fileBottomSpacerHeight)
        }

        let totalHeight = cursorY + bottomPadding
        return PCodeDiffLayoutSnapshot(
            rows: rows,
            yOffsets: yOffsets,
            heights: heights,
            totalHeight: totalHeight,
            fileHeaderIndices: fileHeaderIndices,
            anchorYByID: anchorYByID,
            anchorYByPath: anchorYByPath,
            maxUnifiedCodeWidth: CGFloat(maxUnifiedChars) * charAdvance + 24,
            maxLeftCodeWidth: CGFloat(maxLeftChars) * charAdvance + 20,
            maxRightCodeWidth: CGFloat(maxRightChars) * charAdvance + 20,
            wrapCodeWidth: wrapCodeWidth
        )
    }

    /// O(log N) binary search returning the contiguous index range intersecting `rect`.
    func visibleRange(in rect: CGRect) -> Range<Int> {
        let count = rows.count
        guard count > 0, rect.height > 0 else { return 0..<0 }

        let minY = rect.minY
        let maxY = rect.maxY

        // First row whose bottom (yOffset + height) > minY
        var lo = 0
        var hi = count
        while lo < hi {
            let mid = (lo + hi) >> 1
            if yOffsets[mid] + heights[mid] <= minY {
                lo = mid + 1
            } else {
                hi = mid
            }
        }
        let first = lo
        if first >= count || yOffsets[first] >= maxY {
            return first..<first
        }

        // First row whose top (yOffset) >= maxY
        lo = first
        hi = count
        while lo < hi {
            let mid = (lo + hi) >> 1
            if yOffsets[mid] < maxY {
                lo = mid + 1
            } else {
                hi = mid
            }
        }
        return first..<lo
    }

    /// O(log F) lookup of the sticky file header at `scrollY`, plus its push-up offset
    /// when the next file's header approaches the top edge.
    func stickyHeaderState(for scrollY: CGFloat) -> (rowIndex: Int, yOffset: CGFloat)? {
        guard !fileHeaderIndices.isEmpty else { return nil }
        // Find last file header whose yOffset <= scrollY.
        var lo = 0
        var hi = fileHeaderIndices.count
        while lo < hi {
            let mid = (lo + hi) >> 1
            if yOffsets[fileHeaderIndices[mid]] <= scrollY {
                lo = mid + 1
            } else {
                hi = mid
            }
        }
        let pos = lo - 1
        guard pos >= 0 else { return nil }
        let headerRowIdx = fileHeaderIndices[pos]
        var stickyY: CGFloat = 0
        if pos + 1 < fileHeaderIndices.count {
            let nextHeaderY = yOffsets[fileHeaderIndices[pos + 1]]
            let distanceToNext = nextHeaderY - scrollY
            if distanceToNext < Self.fileHeaderHeight {
                stickyY = distanceToNext - Self.fileHeaderHeight
            }
        }
        return (headerRowIdx, stickyY)
    }
}

// MARK: - CoreText Line Cache

/// Bounded cache of shaped `CTLine` objects so scrolling never allocates `NSAttributedString`
/// or re-shapes CoreText glyphs for visible diff lines.
@MainActor
final class PCodeDiffCTLineCache {
    struct Entry {
        let ctLines: [CTLine]
        let width: CGFloat
    }

    private struct Key: Hashable {
        let text: String
        let lineKind: PCodeDiffLine.Kind
        let hunkKeyHash: Int
        let lineIndex: Int
        let hasTokens: Bool
        let isDark: Bool
        let fontName: String
        let fontSizeTenths: Int
        let wrapWidthTenths: Int
    }

    private static let maxEntries = 4096
    private var entries: [Key: Entry] = [:]
    private var keyOrder: [Key] = []

    func clear() {
        entries.removeAll(keepingCapacity: true)
        keyOrder.removeAll(keepingCapacity: true)
    }

    func ctLine(
        text: String,
        tokens: [PCodeSyntaxToken]?,
        lineKind: PCodeDiffLine.Kind,
        hunkKeyHash: Int,
        lineIndex: Int,
        theme: PCodeDiffTheme,
        wrapWidth: CGFloat? = nil
    ) -> Entry {
        let wrapWidthTenths = wrapWidth.map { max(1, Int(($0 * 10).rounded())) } ?? 0
        let key = Key(
            text: text,
            lineKind: lineKind,
            hunkKeyHash: hunkKeyHash,
            lineIndex: lineIndex,
            hasTokens: tokens != nil && !(tokens?.isEmpty ?? true),
            isDark: theme.isDark,
            fontName: theme.codeFont.fontName,
            fontSizeTenths: Int((theme.codeFont.pointSize * 10).rounded()),
            wrapWidthTenths: wrapWidthTenths
        )
        if let cached = entries[key] {
            return cached
        }

        let attr = NSMutableAttributedString()
        attr.beginEditing()
        if let tokens, !tokens.isEmpty {
            for token in tokens {
                attr.append(NSAttributedString(
                    string: token.content,
                    attributes: [
                        .font: theme.codeFont,
                        .foregroundColor: theme.syntaxPalette.color(for: token.kind),
                    ]
                ))
            }
        } else {
            let color = (lineKind == .meta) ? theme.syntaxPalette.comment : theme.syntaxPalette.plain
            attr.append(NSAttributedString(
                string: text,
                attributes: [
                    .font: theme.codeFont,
                    .foregroundColor: color,
                ]
            ))
        }
        attr.endEditing()

        let ctLines: [CTLine]
        let maxWidth: CGFloat
        if let wrapWidth, wrapWidth > 0, attr.length > 0 {
            let typesetter = CTTypesetterCreateWithAttributedString(attr)
            let totalLen = attr.length
            var start = 0
            var segments: [CTLine] = []
            var widest: CGFloat = 0
            while start < totalLen {
                var breakLen = CTTypesetterSuggestLineBreak(typesetter, start, Double(wrapWidth))
                if breakLen <= 0 {
                    breakLen = max(1, CTTypesetterSuggestClusterBreak(typesetter, start, Double(wrapWidth)))
                }
                let line = CTTypesetterCreateLine(typesetter, CFRange(location: start, length: breakLen))
                segments.append(line)
                widest = max(widest, CGFloat(CTLineGetTypographicBounds(line, nil, nil, nil)))
                start += breakLen
            }
            ctLines = segments
            maxWidth = widest
        } else {
            let line = CTLineCreateWithAttributedString(attr)
            ctLines = [line]
            maxWidth = CGFloat(CTLineGetTypographicBounds(line, nil, nil, nil))
        }
        let entry = Entry(ctLines: ctLines, width: maxWidth)

        entries[key] = entry
        keyOrder.append(key)
        while entries.count > Self.maxEntries, !keyOrder.isEmpty {
            let oldest = keyOrder.removeFirst()
            entries.removeValue(forKey: oldest)
        }
        return entry
    }
}

// MARK: - Weak CADisplayLink Proxy

private final class PCodeDiffDisplayLinkProxy: NSObject {
    weak var target: PCodeDiffDocumentView?
    init(target: PCodeDiffDocumentView) { self.target = target }
    @objc func tick(_ sender: CADisplayLink) {
        MainActor.assumeIsolated {
            target?.handleDisplayLinkTick(sender)
        }
    }
}

// MARK: - Pooled AppKit Row Views

@MainActor
private protocol PCodeDiffReusableRowView: NSView {
    var rowKind: PCodeDiffRowKind { get }
    func prepareForReuse()
}

/// GPU-animated pulsing dot using CoreAnimation (zero SwiftUI view invalidations).
private final class PCodeDiffPulsingDotLayerView: NSView {
    private let haloLayer = CALayer()
    private let coreLayer = CALayer()
    private var currentColor: NSColor?

    override init(frame frameRect: NSRect) {
        super.init(frame: frameRect)
        wantsLayer = true
        layer?.addSublayer(haloLayer)
        layer?.addSublayer(coreLayer)
    }

    @available(*, unavailable)
    required init?(coder: NSCoder) { fatalError() }

    override func layout() {
        super.layout()
        let cx = bounds.midX
        let cy = bounds.midY
        haloLayer.bounds = CGRect(x: 0, y: 0, width: 11, height: 11)
        haloLayer.position = CGPoint(x: cx, y: cy)
        haloLayer.cornerRadius = 5.5
        coreLayer.bounds = CGRect(x: 0, y: 0, width: 6, height: 6)
        coreLayer.position = CGPoint(x: cx, y: cy)
        coreLayer.cornerRadius = 3
    }

    func configure(color: NSColor, active: Bool) {
        isHidden = !active
        guard active else {
            haloLayer.removeAllAnimations()
            return
        }
        if currentColor != color {
            currentColor = color
            coreLayer.backgroundColor = color.cgColor
            haloLayer.backgroundColor = color.withAlphaComponent(0.35).cgColor
        }
        if haloLayer.animation(forKey: "pulse") == nil {
            let scale = CABasicAnimation(keyPath: "transform.scale")
            scale.fromValue = 0.75
            scale.toValue = 1.35
            let opacity = CABasicAnimation(keyPath: "opacity")
            opacity.fromValue = 1.0
            opacity.toValue = 0.0
            let group = CAAnimationGroup()
            group.animations = [scale, opacity]
            group.duration = 1.1
            group.repeatCount = .infinity
            group.timingFunction = CAMediaTimingFunction(name: .easeInEaseOut)
            haloLayer.add(group, forKey: "pulse")
        }
    }
}

/// Native AppKit file header row, reused both inline in the document and as the sticky pinned header.
final class PCodeDiffFileHeaderRowView: NSView, PCodeDiffReusableRowView {
    var rowKind: PCodeDiffRowKind { .fileHeader }
    override var isFlipped: Bool { true }

    private var diff: PCodeFileDiff?
    private var theme: PCodeDiffTheme?
    private var isCollapsed = false
    private var isFocused = false
    private var hasUnseenUpdate = false
    private var isCopied = false

    var onToggleCollapse: ((String, String) -> Void)?
    var onEditInFilesTab: ((PCodeFileDiff) -> Void)?
    var onOpenInEditor: ((PCodeFileDiff) -> Void)?
    var onCopyPath: ((String) -> Void)?

    private let dotView = PCodeDiffPulsingDotLayerView(frame: .zero)
    private let editButton = NSButton(frame: .zero)
    private let openButton = NSButton(frame: .zero)
    private let copyButton = NSButton(frame: .zero)

    override init(frame frameRect: NSRect) {
        super.init(frame: frameRect)
        wantsLayer = true
        addSubview(dotView)

        configureIconButton(editButton, symbol: "pencil.line", tooltip: "Edit in Files tab")
        editButton.target = self
        editButton.action = #selector(handleEditClick)
        addSubview(editButton)

        configureIconButton(openButton, symbol: "arrow.up.forward.square", tooltip: "Open in External Editor (⌘O)")
        openButton.target = self
        openButton.action = #selector(handleOpenClick)
        addSubview(openButton)

        configureIconButton(copyButton, symbol: "doc.on.doc", tooltip: "Copy path")
        copyButton.target = self
        copyButton.action = #selector(handleCopyClick)
        addSubview(copyButton)
    }

    @available(*, unavailable)
    required init?(coder: NSCoder) { fatalError() }

    private func configureIconButton(_ button: NSButton, symbol: String, tooltip: String) {
        button.isBordered = false
        button.bezelStyle = .regularSquare
        button.imagePosition = .imageOnly
        button.image = NSImage(systemSymbolName: symbol, accessibilityDescription: tooltip)
        button.symbolConfiguration = NSImage.SymbolConfiguration(pointSize: 10.5, weight: .medium)
        button.toolTip = tooltip
    }

    override func prepareForReuse() {
        super.prepareForReuse()
        diff = nil
        onToggleCollapse = nil
        onEditInFilesTab = nil
        onOpenInEditor = nil
        onCopyPath = nil
        dotView.configure(color: .clear, active: false)
    }

    func configure(
        diff: PCodeFileDiff,
        isCollapsed: Bool,
        isFocused: Bool,
        hasUnseenUpdate: Bool,
        isCopied: Bool,
        theme: PCodeDiffTheme
    ) {
        self.diff = diff
        self.isCollapsed = isCollapsed
        self.isFocused = isFocused
        self.hasUnseenUpdate = hasUnseenUpdate
        self.isCopied = isCopied
        self.theme = theme

        editButton.contentTintColor = theme.secondaryForeground
        editButton.isHidden = onEditInFilesTab == nil
        openButton.contentTintColor = theme.secondaryForeground
        copyButton.image = NSImage(
            systemSymbolName: isCopied ? "checkmark" : "doc.on.doc",
            accessibilityDescription: "Copy path"
        )
        copyButton.contentTintColor = isCopied ? .systemGreen : theme.secondaryForeground

        if diff.isStreaming {
            dotView.configure(color: .systemGreen, active: true)
        } else if hasUnseenUpdate {
            dotView.configure(color: theme.accent, active: true)
        } else {
            dotView.configure(color: .clear, active: false)
        }

        needsLayout = true
        needsDisplay = true
    }

    override func layout() {
        super.layout()
        let h = bounds.height
        let w = bounds.width
        let btnSize: CGFloat = 20
        let copyX = max(0, w - 10 - btnSize)
        let openX = max(0, copyX - 4 - btnSize)
        let editX = max(0, openX - 4 - btnSize)
        copyButton.frame = CGRect(x: copyX, y: (h - btnSize) * 0.5, width: btnSize, height: btnSize)
        openButton.frame = CGRect(x: openX, y: (h - btnSize) * 0.5, width: btnSize, height: btnSize)
        editButton.frame = CGRect(x: editX, y: (h - btnSize) * 0.5, width: btnSize, height: btnSize)

        let leftmostButtonX = editButton.isHidden ? openX : editX
        if !dotView.isHidden, let diff, let theme {
            let dotX = computeDotOriginX(diff: diff, theme: theme, openButtonMinX: leftmostButtonX)
            dotView.frame = CGRect(x: dotX, y: (h - 12) * 0.5, width: 12, height: 12)
        }
    }

    private func computeDotOriginX(diff: PCodeFileDiff, theme: PCodeDiffTheme, openButtonMinX: CGFloat) -> CGFloat {
        let statFont = NSFont.monospacedSystemFont(ofSize: 11, weight: .medium)
        var rightCursor = openButtonMinX - 6
        if diff.removed > 0 {
            rightCursor -= ("-\(diff.removed)" as NSString).size(withAttributes: [.font: statFont]).width + 5
        }
        if diff.added > 0 {
            rightCursor -= ("+\(diff.added)" as NSString).size(withAttributes: [.font: statFont]).width + 8
        }
        let maxTextX = max(47, rightCursor - 16)
        var x: CGFloat = 47 // 10 margin + 16 chevron + 21 badge
        let baseW = min((diff.basename as NSString).size(withAttributes: [.font: theme.headerTitleFont]).width + 1, max(0, maxTextX - x))
        x += baseW + 6
        if !diff.parentDirectory.isEmpty && x < maxTextX - 16 {
            let dirW = min((diff.parentDirectory as NSString).size(withAttributes: [.font: theme.headerPathFont]).width + 1, max(0, maxTextX - x))
            x += dirW + 4
        }
        return min(x, max(47, rightCursor - 12))
    }

    override func mouseDown(with event: NSEvent) {
        guard let diff else { return }
        let pt = convert(event.locationInWindow, from: nil)
        // Ignore clicks on the trailing action buttons.
        let actionMinX = editButton.isHidden ? openButton.frame.minX : editButton.frame.minX
        if pt.x >= actionMinX - 4 { return }
        onToggleCollapse?(diff.id, diff.path)
    }

    @objc private func handleEditClick() {
        guard let diff else { return }
        onEditInFilesTab?(diff)
    }

    @objc private func handleOpenClick() {
        guard let diff else { return }
        onOpenInEditor?(diff)
    }

    @objc private func handleCopyClick() {
        guard let diff else { return }
        onCopyPath?(diff.path)
    }

    override func draw(_ dirtyRect: NSRect) {
        guard let diff, let theme, let ctx = NSGraphicsContext.current?.cgContext else { return }

        // Opaque base background so sticky header cleanly covers scrolling content beneath it.
        ctx.setFillColor(theme.headerBaseFill.cgColor)
        ctx.fill(bounds)

        if isFocused {
            ctx.setFillColor(theme.headerFocusedTint.cgColor)
            ctx.fill(bounds)
        } else if diff.isStreaming {
            ctx.setFillColor(theme.headerStreamingTint.cgColor)
            ctx.fill(bounds)
        }

        // Top & bottom hairlines
        ctx.setFillColor(theme.dividerColor.withAlphaComponent(0.6).cgColor)
        ctx.fill(CGRect(x: 0, y: 0, width: bounds.width, height: 1))
        ctx.setFillColor(theme.dividerColor.cgColor)
        ctx.fill(CGRect(x: 0, y: bounds.height - 1, width: bounds.width, height: 1))

        let midY = bounds.height * 0.5
        var x: CGFloat = 10

        // Collapse chevron
        let chevronName = isCollapsed ? "chevron.right" : "chevron.down"
        let chevronConfig = NSImage.SymbolConfiguration(pointSize: 9.5, weight: .semibold)
            .applying(.init(paletteColors: [theme.secondaryForeground]))
        if let img = NSImage(systemSymbolName: chevronName, accessibilityDescription: nil)?
            .withSymbolConfiguration(chevronConfig)
        {
            let sz = img.size
            img.draw(in: CGRect(x: x, y: midY - sz.height * 0.5, width: min(10, sz.width), height: sz.height))
        }
        x += 16

        // Change-kind badge (A/M/D/R)
        let badgeColor: NSColor
        switch diff.kind {
        case .added: badgeColor = .systemGreen
        case .modified: badgeColor = .systemOrange
        case .deleted: badgeColor = .systemRed
        case .renamed: badgeColor = .systemPurple
        }
        let badgeRect = CGRect(x: x, y: midY - 7.5, width: 15, height: 15)
        let badgePath = CGPath(roundedRect: badgeRect, cornerWidth: 3, cornerHeight: 3, transform: nil)
        ctx.setFillColor(badgeColor.withAlphaComponent(theme.isDark ? 0.22 : 0.15).cgColor)
        ctx.addPath(badgePath)
        ctx.fillPath()

        let badgeAttrs: [NSAttributedString.Key: Any] = [
            .font: NSFont.monospacedSystemFont(ofSize: 9.5, weight: .bold),
            .foregroundColor: badgeColor,
        ]
        let badgeStr = diff.kind.badge as NSString
        let badgeSz = badgeStr.size(withAttributes: badgeAttrs)
        badgeStr.draw(
            at: CGPoint(x: badgeRect.midX - badgeSz.width * 0.5, y: badgeRect.midY - badgeSz.height * 0.5),
            withAttributes: badgeAttrs
        )
        x += 21

        // Compute right-hand stats area so title/path truncate cleanly before stats + buttons.
        let statFont = NSFont.monospacedSystemFont(ofSize: 11, weight: .medium)
        var rightCursor = (editButton.isHidden ? openButton.frame.minX : editButton.frame.minX) - 6

        if diff.removed > 0 {
            let remStr = "-\(diff.removed)" as NSString
            let remAttrs: [NSAttributedString.Key: Any] = [.font: statFont, .foregroundColor: theme.deletedMarkerColor]
            let sz = remStr.size(withAttributes: remAttrs)
            rightCursor -= sz.width
            remStr.draw(at: CGPoint(x: rightCursor, y: midY - sz.height * 0.5), withAttributes: remAttrs)
            rightCursor -= 5
        }
        if diff.added > 0 {
            let addStr = "+\(diff.added)" as NSString
            let addAttrs: [NSAttributedString.Key: Any] = [.font: statFont, .foregroundColor: theme.addedMarkerColor]
            let sz = addStr.size(withAttributes: addAttrs)
            rightCursor -= sz.width
            addStr.draw(at: CGPoint(x: rightCursor, y: midY - sz.height * 0.5), withAttributes: addAttrs)
            rightCursor -= 8
        }

        let dotReserve: CGFloat = (!dotView.isHidden) ? 16 : 0
        let maxTextX = max(x, rightCursor - dotReserve)

        // Basename
        let baseParagraph = NSMutableParagraphStyle()
        baseParagraph.lineBreakMode = .byTruncatingMiddle
        let baseAttrs: [NSAttributedString.Key: Any] = [
            .font: theme.headerTitleFont,
            .foregroundColor: theme.foreground,
            .paragraphStyle: baseParagraph,
        ]
        let baseNS = diff.basename as NSString
        let baseIdeal = baseNS.size(withAttributes: baseAttrs)
        let baseWidth = min(baseIdeal.width + 1, max(0, maxTextX - x))
        if baseWidth > 0 {
            baseNS.draw(
                in: CGRect(x: x, y: midY - baseIdeal.height * 0.5, width: baseWidth, height: baseIdeal.height),
                withAttributes: baseAttrs
            )
            x += baseWidth + 6
        }

        // Parent directory
        if !diff.parentDirectory.isEmpty && x < maxTextX - 16 {
            let dirParagraph = NSMutableParagraphStyle()
            dirParagraph.lineBreakMode = .byTruncatingHead
            let dirAttrs: [NSAttributedString.Key: Any] = [
                .font: theme.headerPathFont,
                .foregroundColor: theme.secondaryForeground,
                .paragraphStyle: dirParagraph,
            ]
            let dirNS = diff.parentDirectory as NSString
            let dirIdeal = dirNS.size(withAttributes: dirAttrs)
            let dirWidth = min(dirIdeal.width + 1, max(0, maxTextX - x))
            if dirWidth > 0 {
                dirNS.draw(
                    in: CGRect(x: x, y: midY - dirIdeal.height * 0.5, width: dirWidth, height: dirIdeal.height),
                    withAttributes: dirAttrs
                )
                x += dirWidth + 4
            }
        }
    }
}

/// Native AppKit hunk header bar (`@@ -10,4 +10,5 @@ ...`).
private final class PCodeDiffHunkHeaderRowView: NSView, PCodeDiffReusableRowView {
    var rowKind: PCodeDiffRowKind { .hunkHeader }
    override var isFlipped: Bool { true }

    private var headerText = ""
    private var theme: PCodeDiffTheme?

    override init(frame frameRect: NSRect) {
        super.init(frame: frameRect)
        wantsLayer = true
    }

    @available(*, unavailable)
    required init?(coder: NSCoder) { fatalError() }

    override func prepareForReuse() {
        super.prepareForReuse()
        headerText = ""
    }

    func configure(header: String, theme: PCodeDiffTheme) {
        self.headerText = header
        self.theme = theme
        needsDisplay = true
    }

    override func draw(_ dirtyRect: NSRect) {
        guard let theme, let ctx = NSGraphicsContext.current?.cgContext else { return }
        ctx.setFillColor(theme.hunkHeaderFill.cgColor)
        ctx.fill(bounds)

        let paragraph = NSMutableParagraphStyle()
        paragraph.lineBreakMode = .byTruncatingTail
        let attrs: [NSAttributedString.Key: Any] = [
            .font: theme.hunkHeaderFont,
            .foregroundColor: theme.hunkHeaderTextColor,
            .paragraphStyle: paragraph,
        ]
        let ns = headerText as NSString
        let sz = ns.size(withAttributes: attrs)
        ns.draw(
            in: CGRect(x: 10, y: (bounds.height - sz.height) * 0.5, width: max(0, bounds.width - 20), height: sz.height),
            withAttributes: attrs
        )
    }
}

/// Native AppKit clickable expandable context gap bar.
private final class PCodeDiffExpandGapRowView: NSView, PCodeDiffReusableRowView {
    var rowKind: PCodeDiffRowKind { .expandGap }
    override var isFlipped: Bool { true }

    private var payload: PCodeDiffExpandGapPayload?
    private var theme: PCodeDiffTheme?
    private var isHovered = false
    private var trackingAreaRef: NSTrackingArea?
    var onExpand: ((PCodeDiffExpandGapPayload) -> Void)?

    override init(frame frameRect: NSRect) {
        super.init(frame: frameRect)
        wantsLayer = true
    }

    @available(*, unavailable)
    required init?(coder: NSCoder) { fatalError() }

    override func prepareForReuse() {
        super.prepareForReuse()
        payload = nil
        onExpand = nil
        isHovered = false
    }

    func configure(payload: PCodeDiffExpandGapPayload, theme: PCodeDiffTheme) {
        self.payload = payload
        self.theme = theme
        needsDisplay = true
    }

    override func updateTrackingAreas() {
        super.updateTrackingAreas()
        if let existing = trackingAreaRef { removeTrackingArea(existing) }
        let area = NSTrackingArea(
            rect: bounds,
            options: [.mouseEnteredAndExited, .activeInKeyWindow, .inVisibleRect],
            owner: self,
            userInfo: nil
        )
        addTrackingArea(area)
        trackingAreaRef = area
    }

    override func mouseEntered(with event: NSEvent) {
        isHovered = true
        needsDisplay = true
    }

    override func mouseExited(with event: NSEvent) {
        isHovered = false
        needsDisplay = true
    }

    override func mouseDown(with event: NSEvent) {
        guard let payload else { return }
        onExpand?(payload)
    }

    override func draw(_ dirtyRect: NSRect) {
        guard let payload, let theme, let ctx = NSGraphicsContext.current?.cgContext else { return }
        ctx.setFillColor((isHovered ? theme.expandBarHoverFill : theme.expandBarFill).cgColor)
        ctx.fill(bounds)

        let midY = bounds.height * 0.5
        var x: CGFloat = 12
        let symbol = payload.fromBottom ? "arrow.up.to.line" : "chevron.up.chevron.down"
        let iconConfig = NSImage.SymbolConfiguration(pointSize: 10, weight: .medium)
            .applying(.init(paletteColors: [theme.secondaryForeground]))
        if let img = NSImage(systemSymbolName: symbol, accessibilityDescription: nil)?
            .withSymbolConfiguration(iconConfig)
        {
            let sz = img.size
            img.draw(in: CGRect(x: x, y: midY - sz.height * 0.5, width: sz.width, height: sz.height))
            x += sz.width + 6
        }

        let label = payload.hiddenCount > payload.step
            ? "Show \(payload.step) more lines (\(payload.hiddenCount) hidden)"
            : "Show \(payload.hiddenCount) hidden lines"
        let attrs: [NSAttributedString.Key: Any] = [
            .font: NSFont.systemFont(ofSize: 10.5, weight: .medium),
            .foregroundColor: theme.secondaryForeground,
        ]
        let ns = label as NSString
        let sz = ns.size(withAttributes: attrs)
        ns.draw(at: CGPoint(x: x, y: midY - sz.height * 0.5), withAttributes: attrs)
    }
}

/// Native AppKit notice row (binary / renamed / empty / truncated).
private final class PCodeDiffNoticeRowView: NSView, PCodeDiffReusableRowView {
    var rowKind: PCodeDiffRowKind { .notice }
    override var isFlipped: Bool { true }

    private var symbolName: String?
    private var message = ""
    private var theme: PCodeDiffTheme?

    override init(frame frameRect: NSRect) {
        super.init(frame: frameRect)
        wantsLayer = true
    }

    @available(*, unavailable)
    required init?(coder: NSCoder) { fatalError() }

    override func prepareForReuse() {
        super.prepareForReuse()
        symbolName = nil
        message = ""
    }

    func configure(symbolName: String?, message: String, theme: PCodeDiffTheme) {
        self.symbolName = symbolName
        self.message = message
        self.theme = theme
        needsDisplay = true
    }

    override func draw(_ dirtyRect: NSRect) {
        guard let theme else { return }
        let midY = bounds.height * 0.5
        var x: CGFloat = 14
        let iconConfig = NSImage.SymbolConfiguration(pointSize: 11, weight: .regular)
            .applying(.init(paletteColors: [theme.secondaryForeground]))
        if let symbolName,
           let img = NSImage(systemSymbolName: symbolName, accessibilityDescription: nil)?
            .withSymbolConfiguration(iconConfig)
        {
            let sz = img.size
            img.draw(in: CGRect(x: x, y: midY - sz.height * 0.5, width: sz.width, height: sz.height))
            x += sz.width + 6
        }
        let attrs: [NSAttributedString.Key: Any] = [
            .font: theme.noticeFont,
            .foregroundColor: theme.secondaryForeground,
        ]
        let ns = message as NSString
        let sz = ns.size(withAttributes: attrs)
        ns.draw(at: CGPoint(x: x, y: midY - sz.height * 0.5), withAttributes: attrs)
    }
}

/// Lightweight spacer row between files.
private final class PCodeDiffSpacerRowView: NSView, PCodeDiffReusableRowView {
    var rowKind: PCodeDiffRowKind { .spacer }
    override func prepareForReuse() {
        super.prepareForReuse()
    }
}

// MARK: - CoreText Diff Line Drawing Helper

@MainActor
private func drawCoreTextDiffLine(
    in ctx: CGContext,
    ctLines: [CTLine],
    intralineSpans: [PCodeIntralineSpan],
    lineKind: PCodeDiffLine.Kind,
    codeRect: CGRect,
    horizontalOffset: CGFloat,
    theme: PCodeDiffTheme
) {
    ctx.saveGState()
    ctx.clip(to: codeRect)

    // Compute baseline inside flipped coordinate system.
    let ascender = theme.codeFont.ascender
    let descender = theme.codeFont.descender // negative
    let textHeight = ascender - descender
    let segmentHeight = theme.lineRowHeight
    let originX = codeRect.minX - horizontalOffset
    let drawIntraline = !intralineSpans.isEmpty && (lineKind == .added || lineKind == .deleted)
    let intralineTint = (lineKind == .added) ? theme.addedIntralineFill : theme.deletedIntralineFill

    for (segmentIndex, ctLine) in ctLines.enumerated() {
        let segmentY = codeRect.minY + CGFloat(segmentIndex) * segmentHeight
        let baselineY = segmentY + (segmentHeight - textHeight) * 0.5 + ascender

        // Draw intraline word-diff highlight rects behind glyphs (CoreText ignores NSAttributedString .backgroundColor).
        if drawIntraline {
            let segRange = CTLineGetStringRange(ctLine)
            let segStart = segRange.location
            let segEnd = segRange.location + segRange.length
            ctx.setFillColor(intralineTint.cgColor)
            for span in intralineSpans where span.emphasized {
                let start = max(segStart, span.range.lowerBound)
                let end = min(segEnd, span.range.upperBound)
                guard end > start else { continue }
                let x1 = CTLineGetOffsetForStringIndex(ctLine, start, nil)
                let x2 = CTLineGetOffsetForStringIndex(ctLine, end, nil)
                let rect = CGRect(
                    x: originX + x1,
                    y: segmentY + 1,
                    width: max(2, x2 - x1),
                    height: max(0, segmentHeight - 2)
                )
                ctx.fill(rect)
            }
        }

        // CoreText expects an unflipped Y-up text matrix; flip locally around baselineY.
        ctx.textMatrix = CGAffineTransform(scaleX: 1, y: -1)
        ctx.textPosition = CGPoint(x: originX, y: baselineY)
        CTLineDraw(ctLine, ctx)
    }

    ctx.restoreGState()
}

/// Native AppKit unified diff line row rendered via CoreGraphics + cached CoreText `CTLine`.
private final class PCodeDiffUnifiedLineRowView: NSView, PCodeDiffReusableRowView {
    var rowKind: PCodeDiffRowKind { .unifiedLine }
    override var isFlipped: Bool { true }

    private var payload: PCodeDiffUnifiedLinePayload?
    private var theme: PCodeDiffTheme?
    private var isSelected = false
    private var horizontalOffset: CGFloat = 0
    private var wrapCodeWidth: CGFloat?
    private weak var syntaxStore: PCodeDiffSyntaxStore?
    private weak var lineCache: PCodeDiffCTLineCache?
    var onLineClick: ((PCodeDiffLine, String, NSEvent.ModifierFlags) -> Void)?
    var onMeasuredWidth: ((CGFloat) -> Void)?

    var currentHunkKey: String? { payload?.syntaxContext.hunkKey }

    override init(frame frameRect: NSRect) {
        super.init(frame: frameRect)
        wantsLayer = true
    }

    @available(*, unavailable)
    required init?(coder: NSCoder) { fatalError() }

    override func prepareForReuse() {
        super.prepareForReuse()
        payload = nil
        onLineClick = nil
        onMeasuredWidth = nil
        isSelected = false
        horizontalOffset = 0
        wrapCodeWidth = nil
    }

    func configure(
        payload: PCodeDiffUnifiedLinePayload,
        isSelected: Bool,
        horizontalOffset: CGFloat,
        wrapCodeWidth: CGFloat?,
        theme: PCodeDiffTheme,
        syntaxStore: PCodeDiffSyntaxStore,
        lineCache: PCodeDiffCTLineCache
    ) {
        self.payload = payload
        self.isSelected = isSelected
        self.horizontalOffset = horizontalOffset
        self.wrapCodeWidth = wrapCodeWidth
        self.theme = theme
        self.syntaxStore = syntaxStore
        self.lineCache = lineCache
        needsDisplay = true
    }

    func updateHorizontalOffset(_ offset: CGFloat) {
        guard abs(horizontalOffset - offset) > 0.25 else { return }
        horizontalOffset = offset
        needsDisplay = true
    }

    override func mouseDown(with event: NSEvent) {
        guard let payload else { return }
        onLineClick?(payload.line, payload.filePath, event.modifierFlags)
    }

    override func draw(_ dirtyRect: NSRect) {
        guard let payload, let theme, let syntaxStore, let lineCache,
              let ctx = NSGraphicsContext.current?.cgContext
        else { return }

        let line = payload.line

        // Row background fill
        if isSelected {
            ctx.setFillColor(theme.selectedRowFill.cgColor)
            ctx.fill(bounds)
        } else {
            switch line.kind {
            case .added:
                ctx.setFillColor(theme.addedRowFill.cgColor)
                ctx.fill(bounds)
            case .deleted:
                ctx.setFillColor(theme.deletedRowFill.cgColor)
                ctx.fill(bounds)
            case .meta:
                ctx.setFillColor(theme.metaRowFill.cgColor)
                ctx.fill(bounds)
            case .context:
                break
            }
        }

        // Left selection accent bar
        if isSelected {
            ctx.setFillColor(theme.accent.cgColor)
            ctx.fill(CGRect(x: 0, y: 0, width: 2.5, height: bounds.height))
        }

        // Pinned single-column line-number gutter (aligned to the first visual line when wrapped).
        // Uses newLineNumber when present, falling back to oldLineNumber (slightly softer) for deletions.
        let midY = theme.lineRowHeight * 0.5
        if let displayNum = line.referenceLineNumber {
            let isOldOnly = (line.newLineNumber == nil)
            let gutterAttrs: [NSAttributedString.Key: Any] = [
                .font: theme.gutterFont,
                .foregroundColor: theme.secondaryForeground.withAlphaComponent(isOldOnly ? 0.50 : 0.75),
            ]
            let s = String(displayNum) as NSString
            let sz = s.size(withAttributes: gutterAttrs)
            s.draw(
                at: CGPoint(x: theme.unifiedNumWidth - 3.5 - sz.width, y: midY - sz.height * 0.5),
                withAttributes: gutterAttrs
            )
        }

        // Sign marker (+ / - / space / ·)
        let (marker, markerColor): (String, NSColor) = {
            switch line.kind {
            case .added: return ("+", theme.addedMarkerColor)
            case .deleted: return ("-", theme.deletedMarkerColor)
            case .context: return (" ", theme.mutedMarkerColor)
            case .meta: return ("·", theme.mutedMarkerColor)
            }
        }()
        if marker != " " {
            let markerAttrs: [NSAttributedString.Key: Any] = [
                .font: theme.gutterFont,
                .foregroundColor: markerColor,
            ]
            let s = marker as NSString
            let sz = s.size(withAttributes: markerAttrs)
            let mx = theme.unifiedNumWidth + (theme.unifiedMarkerWidth - sz.width) * 0.5
            s.draw(at: CGPoint(x: mx, y: midY - sz.height * 0.5), withAttributes: markerAttrs)
        }

        // Syntax-highlighted CoreText line (clipped to code region, horizontally scrolled)
        let syn = payload.syntaxContext
        let perLineTokens = syntaxStore.lineTokens(
            forPrecomputedKey: syn.hunkKey,
            joinedCode: syn.joinedCode,
            lineCount: syn.lineCount,
            normalizedLanguage: syn.normalizedLanguage
        )
        let tokens = perLineTokens.flatMap { payload.lineIndex < $0.count ? $0[payload.lineIndex] : nil }
        let entry = lineCache.ctLine(
            text: line.text,
            tokens: tokens,
            lineKind: line.kind,
            hunkKeyHash: syn.hunkKeyHash,
            lineIndex: payload.lineIndex,
            theme: theme,
            wrapWidth: wrapCodeWidth
        )
        if wrapCodeWidth == nil {
            onMeasuredWidth?(entry.width + 24)
        }
        let codeRect = CGRect(
            x: theme.unifiedGutterWidth,
            y: 0,
            width: max(0, bounds.width - theme.unifiedGutterWidth),
            height: bounds.height
        )
        drawCoreTextDiffLine(
            in: ctx,
            ctLines: entry.ctLines,
            intralineSpans: line.intralineSpans,
            lineKind: line.kind,
            codeRect: codeRect,
            horizontalOffset: wrapCodeWidth == nil ? horizontalOffset : 0,
            theme: theme
        )
    }
}

/// Native AppKit side-by-side diff row rendered via CoreGraphics + cached CoreText `CTLine`.
private final class PCodeDiffSideBySideRowView: NSView, PCodeDiffReusableRowView {
    var rowKind: PCodeDiffRowKind { .sideBySideRow }
    override var isFlipped: Bool { true }

    private var payload: PCodeDiffSideBySidePayload?
    private var theme: PCodeDiffTheme?
    private var leftSelected = false
    private var rightSelected = false
    private var leftHorizontalOffset: CGFloat = 0
    private var rightHorizontalOffset: CGFloat = 0
    private var wrapCodeWidth: CGFloat?
    private weak var syntaxStore: PCodeDiffSyntaxStore?
    private weak var lineCache: PCodeDiffCTLineCache?
    var onLineClick: ((PCodeDiffLine, String, NSEvent.ModifierFlags) -> Void)?
    var onMeasuredSideWidths: ((CGFloat, CGFloat) -> Void)?

    var currentHunkKey: String? { payload?.syntaxContext.hunkKey }

    override init(frame frameRect: NSRect) {
        super.init(frame: frameRect)
        wantsLayer = true
    }

    @available(*, unavailable)
    required init?(coder: NSCoder) { fatalError() }

    override func prepareForReuse() {
        super.prepareForReuse()
        payload = nil
        onLineClick = nil
        onMeasuredSideWidths = nil
        leftSelected = false
        rightSelected = false
        leftHorizontalOffset = 0
        rightHorizontalOffset = 0
        wrapCodeWidth = nil
    }

    func configure(
        payload: PCodeDiffSideBySidePayload,
        leftSelected: Bool,
        rightSelected: Bool,
        leftHorizontalOffset: CGFloat,
        rightHorizontalOffset: CGFloat,
        wrapCodeWidth: CGFloat?,
        theme: PCodeDiffTheme,
        syntaxStore: PCodeDiffSyntaxStore,
        lineCache: PCodeDiffCTLineCache
    ) {
        self.payload = payload
        self.leftSelected = leftSelected
        self.rightSelected = rightSelected
        self.leftHorizontalOffset = leftHorizontalOffset
        self.rightHorizontalOffset = rightHorizontalOffset
        self.wrapCodeWidth = wrapCodeWidth
        self.theme = theme
        self.syntaxStore = syntaxStore
        self.lineCache = lineCache
        needsDisplay = true
    }

    func updateHorizontalOffsets(left: CGFloat, right: CGFloat) {
        guard abs(leftHorizontalOffset - left) > 0.25 || abs(rightHorizontalOffset - right) > 0.25 else { return }
        leftHorizontalOffset = left
        rightHorizontalOffset = right
        needsDisplay = true
    }

    override func mouseDown(with event: NSEvent) {
        guard let payload else { return }
        let pt = convert(event.locationInWindow, from: nil)
        let halfW = floor((bounds.width - 1) * 0.5)
        if pt.x <= halfW {
            if let left = payload.row.left {
                onLineClick?(left, payload.filePath, event.modifierFlags)
            }
        } else {
            if let right = payload.row.right {
                onLineClick?(right, payload.filePath, event.modifierFlags)
            }
        }
    }

    override func draw(_ dirtyRect: NSRect) {
        guard let payload, let theme, let syntaxStore, let lineCache,
              let ctx = NSGraphicsContext.current?.cgContext
        else { return }

        let halfW = floor((bounds.width - 1) * 0.5)
        let leftRect = CGRect(x: 0, y: 0, width: halfW, height: bounds.height)
        let rightRect = CGRect(x: halfW + 1, y: 0, width: max(0, bounds.width - halfW - 1), height: bounds.height)

        let syn = payload.syntaxContext
        let perLineTokens = syntaxStore.lineTokens(
            forPrecomputedKey: syn.hunkKey,
            joinedCode: syn.joinedCode,
            lineCount: syn.lineCount,
            normalizedLanguage: syn.normalizedLanguage
        )

        if payload.row.isMeta {
            ctx.setFillColor(theme.metaRowFill.cgColor)
            ctx.fill(bounds)
            if let meta = payload.row.left {
                let attrs: [NSAttributedString.Key: Any] = [
                    .font: theme.gutterFont,
                    .foregroundColor: theme.secondaryForeground,
                ]
                let ns = meta.text as NSString
                let sz = ns.size(withAttributes: attrs)
                ns.draw(
                    at: CGPoint(x: 10, y: (bounds.height - sz.height) * 0.5),
                    withAttributes: attrs
                )
            }
        } else {
            let leftW = drawHalf(
                ctx: ctx,
                rect: leftRect,
                line: payload.row.left,
                lineNumber: payload.row.left?.oldLineNumber,
                lineIndex: payload.leftLineIndex,
                isSelected: leftSelected,
                horizontalOffset: leftHorizontalOffset,
                perLineTokens: perLineTokens,
                hunkKeyHash: syn.hunkKeyHash,
                theme: theme,
                lineCache: lineCache
            )
            let rightW = drawHalf(
                ctx: ctx,
                rect: rightRect,
                line: payload.row.right,
                lineNumber: payload.row.right?.newLineNumber,
                lineIndex: payload.rightLineIndex,
                isSelected: rightSelected,
                horizontalOffset: rightHorizontalOffset,
                perLineTokens: perLineTokens,
                hunkKeyHash: syn.hunkKeyHash,
                theme: theme,
                lineCache: lineCache
            )
            if wrapCodeWidth == nil {
                onMeasuredSideWidths?(leftW + 20, rightW + 20)
            }
        }

        // Center divider
        ctx.setFillColor(theme.dividerColor.cgColor)
        ctx.fill(CGRect(x: halfW, y: 0, width: 1, height: bounds.height))
    }

    @discardableResult
    private func drawHalf(
        ctx: CGContext,
        rect: CGRect,
        line: PCodeDiffLine?,
        lineNumber: Int?,
        lineIndex: Int?,
        isSelected: Bool,
        horizontalOffset: CGFloat,
        perLineTokens: [[PCodeSyntaxToken]]?,
        hunkKeyHash: Int,
        theme: PCodeDiffTheme,
        lineCache: PCodeDiffCTLineCache
    ) -> CGFloat {
        guard let line else {
            ctx.setFillColor(theme.fillerCellFill.cgColor)
            ctx.fill(rect)
            return 0
        }

        if isSelected {
            ctx.setFillColor(theme.selectedRowFill.cgColor)
            ctx.fill(rect)
            ctx.setFillColor(theme.accent.cgColor)
            ctx.fill(CGRect(x: rect.minX, y: rect.minY, width: 2.5, height: rect.height))
        } else {
            switch line.kind {
            case .added:
                ctx.setFillColor(theme.addedRowFill.cgColor)
                ctx.fill(rect)
            case .deleted:
                ctx.setFillColor(theme.deletedRowFill.cgColor)
                ctx.fill(rect)
            case .meta:
                ctx.setFillColor(theme.metaRowFill.cgColor)
                ctx.fill(rect)
            case .context:
                break
            }
        }

        let midY = rect.minY + theme.lineRowHeight * 0.5
        if let num = lineNumber {
            let attrs: [NSAttributedString.Key: Any] = [
                .font: theme.gutterFont,
                .foregroundColor: theme.secondaryForeground.withAlphaComponent(0.75),
            ]
            let s = String(num) as NSString
            let sz = s.size(withAttributes: attrs)
            s.draw(
                at: CGPoint(x: rect.minX + theme.sideBySideNumWidth - 3.5 - sz.width, y: midY - sz.height * 0.5),
                withAttributes: attrs
            )
        }

        let (marker, markerColor): (String, NSColor) = {
            switch line.kind {
            case .added: return ("+", theme.addedMarkerColor)
            case .deleted: return ("-", theme.deletedMarkerColor)
            case .context: return (" ", theme.mutedMarkerColor)
            case .meta: return ("·", theme.mutedMarkerColor)
            }
        }()
        if marker != " " {
            let attrs: [NSAttributedString.Key: Any] = [
                .font: theme.gutterFont,
                .foregroundColor: markerColor,
            ]
            let s = marker as NSString
            let sz = s.size(withAttributes: attrs)
            s.draw(
                at: CGPoint(x: rect.minX + theme.sideBySideNumWidth + (theme.sideBySideMarkerWidth - sz.width) * 0.5, y: midY - sz.height * 0.5),
                withAttributes: attrs
            )
        }

        let tokens: [PCodeSyntaxToken]? = {
            guard let lineIndex, let perLineTokens, lineIndex < perLineTokens.count else { return nil }
            return perLineTokens[lineIndex]
        }()
        let entry = lineCache.ctLine(
            text: line.text,
            tokens: tokens,
            lineKind: line.kind,
            hunkKeyHash: hunkKeyHash,
            lineIndex: lineIndex ?? 0,
            theme: theme,
            wrapWidth: wrapCodeWidth
        )
        let codeRect = CGRect(
            x: rect.minX + theme.sideBySideHalfGutterWidth,
            y: rect.minY,
            width: max(0, rect.width - theme.sideBySideHalfGutterWidth),
            height: rect.height
        )
        drawCoreTextDiffLine(
            in: ctx,
            ctLines: entry.ctLines,
            intralineSpans: line.intralineSpans,
            lineKind: line.kind,
            codeRect: codeRect,
            horizontalOffset: wrapCodeWidth == nil ? horizontalOffset : 0,
            theme: theme
        )
        return entry.width
    }
}

// MARK: - Virtualized Document View

/// Native AppKit virtualized diff document view with responsive scrolling (`prepareContent(in:)`),
/// view pooling, targeted syntax-highlight redraws, and 120Hz `CADisplayLink` follow glide.
@MainActor
final class PCodeDiffDocumentView: NSView {
    override var isFlipped: Bool { true }
    override var acceptsFirstResponder: Bool { true }
    override class var isCompatibleWithResponsiveScrolling: Bool { true }

    private(set) var snapshot: PCodeDiffLayoutSnapshot = .empty
    private var theme: PCodeDiffTheme
    private let syntaxStore: PCodeDiffSyntaxStore
    private let lineCache = PCodeDiffCTLineCache()

    private var mountedViews: [Int: PCodeDiffReusableRowView] = [:]
    private var reusePools: [PCodeDiffRowKind: [PCodeDiffReusableRowView]] = [:]

    private var currentLayout: PCodeDiffLayout = .unified
    private var currentSelection: PCodeDiffSelection?
    private var wrapLines: Bool { snapshot.wrapCodeWidth != nil }

    // Horizontal scroll offsets (in points, >= 0) and dynamically measured max code widths
    private(set) var unifiedHorizontalOffset: CGFloat = 0
    private(set) var leftHorizontalOffset: CGFloat = 0
    private(set) var rightHorizontalOffset: CGFloat = 0
    private var measuredUnifiedCodeWidth: CGFloat = 0
    private var measuredLeftCodeWidth: CGFloat = 0
    private var measuredRightCodeWidth: CGFloat = 0

    // 120Hz smooth follow-agent glide (reusing PCodeSmoothScrollGlide from transcript)
    static func makeStreamingGlide() -> PCodeSmoothScrollGlide {
        PCodeSmoothScrollGlide()
    }

    /// Snappy catch-up glide used when the user explicitly clicks Follow from scrolled-up content,
    /// zipping to the bottom quickly before settling back into normal streaming conveyor speed.
    static func makeCatchUpGlide() -> PCodeSmoothScrollGlide {
        PCodeSmoothScrollGlide(
            minSpeed: 900.0,
            maxSpeed: 8500.0,
            catchUpSeconds: 0.28,
            maxOffset: 100_000.0
        )
    }

    private var smoothGlide = PCodeDiffDocumentView.makeStreamingGlide()
    private var isCatchUpGlide = false
    private var displayLink: CADisplayLink?
    private var lastDisplayTick: CFTimeInterval = 0
    private var programmaticScrollDepth = 0
    private var scrollAnimationGeneration = 0
    private var isAnimatingScrollToFile = false
    private var isFollowPinnedToBottom = false
    private var lastDocumentHeight: CGFloat = 0

    // Callbacks wired by Coordinator
    var onToggleCollapse: ((String, String) -> Void)?
    var onEditInFilesTab: ((PCodeFileDiff) -> Void)?
    var onOpenInEditor: ((PCodeFileDiff) -> Void)?
    var onCopyPath: ((String) -> Void)?
    var onExpandGap: ((PCodeDiffExpandGapPayload) -> Void)?
    var onLineClick: ((PCodeDiffLine, String, NSEvent.ModifierFlags) -> Void)?
    var onPrevFile: (() -> Void)?
    var onNextFile: (() -> Void)?
    var onEscape: (() -> Bool)?
    var onReferenceSelection: (() -> Bool)?
    var onOpenFocusedInEditor: (() -> Bool)?
    var onUserScrolledAwayFromBottom: (() -> Void)?

    init(theme: PCodeDiffTheme, syntaxStore: PCodeDiffSyntaxStore) {
        self.theme = theme
        self.syntaxStore = syntaxStore
        super.init(frame: .zero)
        wantsLayer = true
    }

    @available(*, unavailable)
    required init?(coder: NSCoder) { fatalError() }

    deinit {
        displayLink?.invalidate()
    }

    override func viewDidMoveToWindow() {
        super.viewDidMoveToWindow()
        if window == nil {
            stopDisplayLink()
        } else if smoothGlide.isActive {
            ensureDisplayLinkRunning()
        }
    }

    /// Responsive-scrolling hook: AppKit passes the visible rect plus its own overscan band.
    override func prepareContent(in rect: NSRect) {
        super.prepareContent(in: rect)
        mountViews(in: rect)
    }

    override func mouseDown(with event: NSEvent) {
        window?.makeFirstResponder(self)
        super.mouseDown(with: event)
    }

    private func withProgrammaticScroll(_ body: () -> Void) {
        programmaticScrollDepth += 1
        defer { programmaticScrollDepth = max(0, programmaticScrollDepth - 1) }
        body()
    }

    private var effectiveMaxUnifiedCodeWidth: CGFloat {
        max(snapshot.maxUnifiedCodeWidth, measuredUnifiedCodeWidth)
    }

    private var effectiveMaxLeftCodeWidth: CGFloat {
        max(snapshot.maxLeftCodeWidth, measuredLeftCodeWidth)
    }

    private var effectiveMaxRightCodeWidth: CGFloat {
        max(snapshot.maxRightCodeWidth, measuredRightCodeWidth)
    }

    // MARK: - Snapshot & Theme Application

    func apply(
        snapshot newSnapshot: PCodeDiffLayoutSnapshot,
        layout: PCodeDiffLayout,
        selection: PCodeDiffSelection?,
        theme newTheme: PCodeDiffTheme,
        followBottom: Bool,
        allowSmoothGlide: Bool
    ) {
        let themeChanged = (newTheme != theme)
        let layoutChanged = (layout != currentLayout)
        let wrapChanged = ((newSnapshot.wrapCodeWidth != nil) != (snapshot.wrapCodeWidth != nil))
        let wrapWidthChanged = (newSnapshot.wrapCodeWidth != snapshot.wrapCodeWidth)
        if themeChanged || wrapWidthChanged {
            theme = newTheme
            lineCache.clear()
        }
        if layoutChanged || wrapChanged || wrapWidthChanged {
            currentLayout = layout
            unifiedHorizontalOffset = 0
            leftHorizontalOffset = 0
            rightHorizontalOffset = 0
        }
        let effectiveAllowSmoothGlide = allowSmoothGlide && !layoutChanged && !wrapChanged && !wrapWidthChanged
        currentSelection = selection

        let oldHeight = lastDocumentHeight
        let previousClipY = enclosingScrollView?.contentView.bounds.origin.y ?? 0
        let wasNearBottom: Bool = {
            guard let sv = enclosingScrollView else { return true }
            let viewportH = sv.contentView.bounds.height
            let maxOriginY = max(0, oldHeight - viewportH)
            return previousClipY + smoothGlide.remainingOffset >= maxOriginY - 24
        }()

        snapshot = newSnapshot
        measuredUnifiedCodeWidth = 0
        measuredLeftCodeWidth = 0
        measuredRightCodeWidth = 0

        let viewportW = max(1, enclosingScrollView?.contentSize.width ?? bounds.width)
        let viewportH = enclosingScrollView?.contentView.bounds.height ?? 0
        let targetDocHeight = max(newSnapshot.totalHeight, viewportH)
        lastDocumentHeight = targetDocHeight

        withProgrammaticScroll {
            frame = CGRect(x: 0, y: 0, width: viewportW, height: targetDocHeight)
        }
        // Clamp horizontal offsets *after* resizing frame to the new viewport width.
        clampHorizontalOffsets()

        if followBottom, let scrollView = enclosingScrollView {
            let maxOriginY = max(0, targetDocHeight - scrollView.contentView.bounds.height)
            let growth = targetDocHeight - oldHeight
            if effectiveAllowSmoothGlide && window != nil && wasNearBottom && (growth > 0.5 || smoothGlide.isActive) {
                isFollowPinnedToBottom = true
                if growth > 0.5 {
                    smoothGlide.addOffset(growth)
                } else if growth < -0.5 {
                    smoothGlide.absorbShrink(-growth)
                }
                let scale = window?.backingScaleFactor ?? 2.0
                let rawY = round((maxOriginY - smoothGlide.remainingOffset) * scale) / scale
                setClipOriginY(max(0, min(maxOriginY, rawY)), in: scrollView)
                if smoothGlide.isActive {
                    ensureDisplayLinkRunning()
                }
            } else if wasNearBottom || oldHeight == 0 {
                smoothGlide.reset()
                stopDisplayLink()
                isFollowPinnedToBottom = true
                setClipOriginY(maxOriginY, in: scrollView)
            }
        } else if !followBottom {
            isFollowPinnedToBottom = false
            resetToStreamingGlide()
            stopDisplayLink()
        }

        // Reconcile mounted views in-place instead of wholesale unmount/remount,
        // preserving hover state and CoreAnimation pulsing dots across updates.
        mountViews(in: visibleRect, reconfigureExisting: true)
        (enclosingScrollView as? PCodeDiffScrollView)?.updateStickyHeader(forceReconfigure: true)
    }

    /// Targeted redraw when async Prism tokenization finishes for one hunk key.
    func handleTokensReady(forHunkKey hunkKey: String) {
        for (_, view) in mountedViews {
            if let unified = view as? PCodeDiffUnifiedLineRowView, unified.currentHunkKey == hunkKey {
                unified.needsDisplay = true
            } else if let sbs = view as? PCodeDiffSideBySideRowView, sbs.currentHunkKey == hunkKey {
                sbs.needsDisplay = true
            }
        }
    }

    // MARK: - Programmatic Scrolling

    private func resetToStreamingGlide() {
        isCatchUpGlide = false
        smoothGlide = Self.makeStreamingGlide()
    }

    func scrollToBottom(animated: Bool) {
        guard let scrollView = enclosingScrollView else { return }
        let maxOriginY = max(0, bounds.height - scrollView.contentView.bounds.height)
        let currentY = scrollView.contentView.bounds.origin.y
        isFollowPinnedToBottom = true

        if animated && window != nil && maxOriginY - currentY > 1 {
            isCatchUpGlide = true
            smoothGlide = Self.makeCatchUpGlide()
            smoothGlide.addOffset(maxOriginY - currentY)
            ensureDisplayLinkRunning()
        } else {
            resetToStreamingGlide()
            stopDisplayLink()
            setClipOriginY(maxOriginY, in: scrollView)
            mountViews(in: visibleRect)
            (scrollView as? PCodeDiffScrollView)?.updateStickyHeader()
        }
    }

    @discardableResult
    func scrollToFile(idOrPath: String, animated: Bool) -> Bool {
        guard let scrollView = enclosingScrollView else { return false }
        guard let targetY = snapshot.anchorYByID[idOrPath] ?? snapshot.anchorYByPath[idOrPath] else {
            return false
        }
        resetToStreamingGlide()
        stopDisplayLink()
        let maxOriginY = max(0, bounds.height - scrollView.contentView.bounds.height)
        let clampedY = max(0, min(maxOriginY, targetY))
        if animated && window != nil {
            scrollAnimationGeneration += 1
            let generation = scrollAnimationGeneration
            isAnimatingScrollToFile = true
            NSAnimationContext.runAnimationGroup { ctx in
                ctx.duration = 0.18
                ctx.timingFunction = CAMediaTimingFunction(name: .easeInEaseOut)
                scrollView.contentView.animator().setBoundsOrigin(NSPoint(x: 0, y: clampedY))
            } completionHandler: { [weak self] in
                Task { @MainActor in
                    guard let self else { return }
                    if self.scrollAnimationGeneration == generation {
                        self.isAnimatingScrollToFile = false
                    }
                    guard let sv = self.enclosingScrollView else { return }
                    sv.reflectScrolledClipView(sv.contentView)
                    self.mountViews(in: self.visibleRect)
                    (sv as? PCodeDiffScrollView)?.updateStickyHeader()
                }
            }
        } else {
            scrollAnimationGeneration += 1
            isAnimatingScrollToFile = false
            setClipOriginY(clampedY, in: scrollView)
            mountViews(in: visibleRect)
            (scrollView as? PCodeDiffScrollView)?.updateStickyHeader()
        }
        return true
    }

    // MARK: - Horizontal Scrolling (Axis-Latched from PCodeDiffScrollView)

    var canScrollHorizontally: Bool {
        guard !wrapLines else { return false }
        if currentLayout == .sideBySide {
            let halfCodeW = max(0, floor((bounds.width - 1) * 0.5) - theme.sideBySideHalfGutterWidth)
            return effectiveMaxLeftCodeWidth > halfCodeW + 1 || effectiveMaxRightCodeWidth > halfCodeW + 1
        }
        let codeW = max(0, bounds.width - theme.unifiedGutterWidth)
        return effectiveMaxUnifiedCodeWidth > codeW + 1
    }

    func adjustHorizontalScroll(deltaX: CGFloat, locationInView: NSPoint) {
        if currentLayout == .sideBySide {
            let halfW = floor((bounds.width - 1) * 0.5)
            let halfCodeW = max(0, halfW - theme.sideBySideHalfGutterWidth)
            if locationInView.x <= halfW {
                let maxOff = max(0, effectiveMaxLeftCodeWidth - halfCodeW)
                leftHorizontalOffset = max(0, min(maxOff, leftHorizontalOffset - deltaX))
            } else {
                let maxOff = max(0, effectiveMaxRightCodeWidth - halfCodeW)
                rightHorizontalOffset = max(0, min(maxOff, rightHorizontalOffset - deltaX))
            }
            pushHorizontalOffsetsToMountedRows()
        } else {
            let codeW = max(0, bounds.width - theme.unifiedGutterWidth)
            let maxOff = max(0, effectiveMaxUnifiedCodeWidth - codeW)
            unifiedHorizontalOffset = max(0, min(maxOff, unifiedHorizontalOffset - deltaX))
            pushHorizontalOffsetsToMountedRows()
        }
    }

    private func pushHorizontalOffsetsToMountedRows() {
        for (_, view) in mountedViews {
            if let unified = view as? PCodeDiffUnifiedLineRowView {
                unified.updateHorizontalOffset(unifiedHorizontalOffset)
            } else if let sbs = view as? PCodeDiffSideBySideRowView {
                sbs.updateHorizontalOffsets(left: leftHorizontalOffset, right: rightHorizontalOffset)
            }
        }
    }

    private func clampHorizontalOffsets() {
        if currentLayout == .sideBySide {
            let halfW = floor((bounds.width - 1) * 0.5)
            let halfCodeW = max(0, halfW - theme.sideBySideHalfGutterWidth)
            leftHorizontalOffset = min(leftHorizontalOffset, max(0, effectiveMaxLeftCodeWidth - halfCodeW))
            rightHorizontalOffset = min(rightHorizontalOffset, max(0, effectiveMaxRightCodeWidth - halfCodeW))
        } else {
            let codeW = max(0, bounds.width - theme.unifiedGutterWidth)
            unifiedHorizontalOffset = min(unifiedHorizontalOffset, max(0, effectiveMaxUnifiedCodeWidth - codeW))
        }
    }

    // MARK: - Scroll Notifications & 120Hz CADisplayLink

    func handleLiveScrollBegan() {
        if smoothGlide.isActive {
            // Let user take over immediately without fighting their fingers.
            resetToStreamingGlide()
            stopDisplayLink()
        }
    }

    func handleClipBoundsDidChange() {
        guard programmaticScrollDepth == 0 else { return }
        mountViews(in: visibleRect)
        (enclosingScrollView as? PCodeDiffScrollView)?.updateStickyHeader()

        guard !isAnimatingScrollToFile else { return }

        if let scrollView = enclosingScrollView {
            let maxOriginY = max(0, bounds.height - scrollView.contentView.bounds.height)
            let currentY = scrollView.contentView.bounds.origin.y
            let distFromBottom = max(0, maxOriginY - currentY)
            if smoothGlide.isActive {
                smoothGlide.clampToRemaining(distFromBottom)
            }
            if isFollowPinnedToBottom && distFromBottom > 36 {
                isFollowPinnedToBottom = false
                onUserScrolledAwayFromBottom?()
            }
        }
    }

    func handleViewportSizeChanged(newSize: NSSize) {
        let newWidth = newSize.width
        let targetH = max(snapshot.totalHeight, newSize.height)
        let widthChanged = newWidth > 1 && abs(frame.width - newWidth) > 0.5
        let heightChanged = abs(frame.height - targetH) > 0.5
        guard widthChanged || heightChanged else { return }

        let resolvedW = newWidth > 1 ? newWidth : frame.width
        lastDocumentHeight = targetH
        withProgrammaticScroll {
            frame = CGRect(x: 0, y: 0, width: resolvedW, height: targetH)
        }
        if widthChanged {
            clampHorizontalOffsets()
            for (idx, view) in mountedViews {
                guard idx < snapshot.rows.count else { continue }
                view.frame = CGRect(x: 0, y: snapshot.yOffsets[idx], width: resolvedW, height: snapshot.heights[idx])
                view.needsDisplay = true
            }
            pushHorizontalOffsetsToMountedRows()
        }
        mountViews(in: visibleRect)
        (enclosingScrollView as? PCodeDiffScrollView)?.updateStickyHeader()
    }

    private func ensureDisplayLinkRunning() {
        guard displayLink == nil, window != nil else { return }
        let proxy = PCodeDiffDisplayLinkProxy(target: self)
        let link = self.displayLink(target: proxy, selector: #selector(PCodeDiffDisplayLinkProxy.tick(_:)))
        link.add(to: .main, forMode: .common)
        lastDisplayTick = CACurrentMediaTime()
        displayLink = link
    }

    private func stopDisplayLink() {
        displayLink?.invalidate()
        displayLink = nil
        lastDisplayTick = 0
    }

    fileprivate func handleDisplayLinkTick(_ sender: CADisplayLink) {
        guard smoothGlide.isActive, let scrollView = enclosingScrollView else {
            if isCatchUpGlide {
                resetToStreamingGlide()
            }
            stopDisplayLink()
            return
        }
        let now = CACurrentMediaTime()
        let dt = lastDisplayTick > 0 ? (now - lastDisplayTick) : (1.0 / 120.0)
        lastDisplayTick = now

        let remaining = smoothGlide.step(dt: dt)
        let maxOriginY = max(0, bounds.height - scrollView.contentView.bounds.height)
        let scale = window?.backingScaleFactor ?? 2.0
        let targetY = max(0, min(maxOriginY, round((maxOriginY - remaining) * scale) / scale))

        setClipOriginY(targetY, in: scrollView)
        mountViews(in: visibleRect)
        (scrollView as? PCodeDiffScrollView)?.updateStickyHeader()

        if !smoothGlide.isActive {
            if isCatchUpGlide {
                resetToStreamingGlide()
            }
            stopDisplayLink()
        }
    }

    private func setClipOriginY(_ y: CGFloat, in scrollView: NSScrollView) {
        withProgrammaticScroll {
            scrollView.contentView.setBoundsOrigin(NSPoint(x: 0, y: y))
            scrollView.reflectScrolledClipView(scrollView.contentView)
        }
    }

    // MARK: - View Mounting & Pooling

    func mountViews(in rect: CGRect, reconfigureExisting: Bool = false) {
        let range = snapshot.visibleRange(in: rect)

        // Recycle rows that scrolled out of range.
        for (idx, view) in mountedViews where !range.contains(idx) {
            recycle(view)
            mountedViews.removeValue(forKey: idx)
        }

        let width = max(1, bounds.width)
        for idx in range {
            let row = snapshot.rows[idx]
            let rowFrame = CGRect(x: 0, y: snapshot.yOffsets[idx], width: width, height: snapshot.heights[idx])
            if let existing = mountedViews[idx] {
                if existing.rowKind == row.kind {
                    if existing.frame != rowFrame {
                        existing.frame = rowFrame
                    }
                    if reconfigureExisting {
                        configure(view: existing, for: row)
                    }
                    continue
                }
                recycle(existing)
                mountedViews.removeValue(forKey: idx)
            }
            let view = dequeueView(for: row.kind)
            view.frame = rowFrame
            configure(view: view, for: row)
            addSubview(view)
            mountedViews[idx] = view
        }
    }

    private func dequeueView(for kind: PCodeDiffRowKind) -> PCodeDiffReusableRowView {
        if var pool = reusePools[kind], let recycled = pool.popLast() {
            reusePools[kind] = pool
            return recycled
        }
        switch kind {
        case .fileHeader: return PCodeDiffFileHeaderRowView(frame: .zero)
        case .hunkHeader: return PCodeDiffHunkHeaderRowView(frame: .zero)
        case .expandGap: return PCodeDiffExpandGapRowView(frame: .zero)
        case .unifiedLine: return PCodeDiffUnifiedLineRowView(frame: .zero)
        case .sideBySideRow: return PCodeDiffSideBySideRowView(frame: .zero)
        case .notice: return PCodeDiffNoticeRowView(frame: .zero)
        case .spacer: return PCodeDiffSpacerRowView(frame: .zero)
        }
    }

    private func recycle(_ view: PCodeDiffReusableRowView) {
        view.prepareForReuse()
        view.removeFromSuperview()
        var pool = reusePools[view.rowKind] ?? []
        if pool.count < 96 {
            pool.append(view)
            reusePools[view.rowKind] = pool
        }
    }

    private func recycleAllMountedViews() {
        for (_, view) in mountedViews {
            recycle(view)
        }
        mountedViews.removeAll(keepingCapacity: true)
    }

    private func isLineSelected(_ line: PCodeDiffLine?, filePath: String) -> Bool {
        guard let line,
              let sel = currentSelection,
              sel.path == filePath,
              let num = line.referenceLineNumber
        else { return false }
        return num >= sel.startLine && num <= sel.endLine
    }

    private func configure(view: PCodeDiffReusableRowView, for row: PCodeDiffFlatRow) {
        switch row {
        case .fileHeader(let diff, let isCollapsed, let isFocused, let hasUnseen, let isCopied):
            guard let headerView = view as? PCodeDiffFileHeaderRowView else { return }
            headerView.onToggleCollapse = { [weak self] id, path in
                self?.window?.makeFirstResponder(self)
                self?.onToggleCollapse?(id, path)
            }
            headerView.onEditInFilesTab = onEditInFilesTab
            headerView.onOpenInEditor = onOpenInEditor
            headerView.onCopyPath = onCopyPath
            headerView.configure(
                diff: diff,
                isCollapsed: isCollapsed,
                isFocused: isFocused,
                hasUnseenUpdate: hasUnseen,
                isCopied: isCopied,
                theme: theme
            )

        case .hunkHeader(_, _, let header):
            guard let hunkView = view as? PCodeDiffHunkHeaderRowView else { return }
            hunkView.configure(header: header, theme: theme)

        case .expandGap(let payload):
            guard let gapView = view as? PCodeDiffExpandGapRowView else { return }
            gapView.onExpand = { [weak self] gap in
                self?.window?.makeFirstResponder(self)
                self?.onExpandGap?(gap)
            }
            gapView.configure(payload: payload, theme: theme)

        case .unifiedLine(let payload):
            guard let lineView = view as? PCodeDiffUnifiedLineRowView else { return }
            lineView.onLineClick = { [weak self] line, path, flags in
                self?.window?.makeFirstResponder(self)
                self?.onLineClick?(line, path, flags)
            }
            lineView.onMeasuredWidth = { [weak self] w in
                guard let self, w > self.measuredUnifiedCodeWidth else { return }
                self.measuredUnifiedCodeWidth = w
            }
            lineView.configure(
                payload: payload,
                isSelected: isLineSelected(payload.line, filePath: payload.filePath),
                horizontalOffset: unifiedHorizontalOffset,
                wrapCodeWidth: snapshot.wrapCodeWidth,
                theme: theme,
                syntaxStore: syntaxStore,
                lineCache: lineCache
            )

        case .sideBySideRow(let payload):
            guard let sbsView = view as? PCodeDiffSideBySideRowView else { return }
            sbsView.onLineClick = { [weak self] line, path, flags in
                self?.window?.makeFirstResponder(self)
                self?.onLineClick?(line, path, flags)
            }
            sbsView.onMeasuredSideWidths = { [weak self] leftW, rightW in
                guard let self else { return }
                if leftW > self.measuredLeftCodeWidth { self.measuredLeftCodeWidth = leftW }
                if rightW > self.measuredRightCodeWidth { self.measuredRightCodeWidth = rightW }
            }
            sbsView.configure(
                payload: payload,
                leftSelected: isLineSelected(payload.row.left, filePath: payload.filePath),
                rightSelected: isLineSelected(payload.row.right, filePath: payload.filePath),
                leftHorizontalOffset: leftHorizontalOffset,
                rightHorizontalOffset: rightHorizontalOffset,
                wrapCodeWidth: snapshot.wrapCodeWidth,
                theme: theme,
                syntaxStore: syntaxStore,
                lineCache: lineCache
            )

        case .notice(_, let symbolName, let message):
            guard let noticeView = view as? PCodeDiffNoticeRowView else { return }
            noticeView.configure(symbolName: symbolName, message: message, theme: theme)

        case .spacer:
            break
        }
    }

    func configureStickyHeader(_ headerView: PCodeDiffFileHeaderRowView, rowIndex: Int) {
        guard rowIndex < snapshot.rows.count,
              case .fileHeader(let diff, let isCollapsed, let isFocused, let hasUnseen, let isCopied) = snapshot.rows[rowIndex]
        else { return }
        headerView.onToggleCollapse = onToggleCollapse
        headerView.onEditInFilesTab = onEditInFilesTab
        headerView.onOpenInEditor = onOpenInEditor
        headerView.onCopyPath = onCopyPath
        headerView.configure(
            diff: diff,
            isCollapsed: isCollapsed,
            isFocused: isFocused,
            hasUnseenUpdate: hasUnseen,
            isCopied: isCopied,
            theme: theme
        )
    }

    // MARK: - Keyboard Shortcuts ([ / ] / ⌘L / ⌘O / Esc)

    override func keyDown(with event: NSEvent) {
        let flags = event.modifierFlags.intersection(.deviceIndependentFlagsMask)
        if flags.isEmpty {
            if event.keyCode == 53 { // Escape
                if onEscape?() == true { return }
            } else if event.charactersIgnoringModifiers == "[" {
                onPrevFile?()
                return
            } else if event.charactersIgnoringModifiers == "]" {
                onNextFile?()
                return
            }
        } else if flags == .command {
            let lower = event.charactersIgnoringModifiers?.lowercased()
            if lower == "l", onReferenceSelection?() == true { return }
            if lower == "o", onOpenFocusedInEditor?() == true { return }
        }
        super.keyDown(with: event)
    }

    override func performKeyEquivalent(with event: NSEvent) -> Bool {
        guard window?.firstResponder === self else { return super.performKeyEquivalent(with: event) }
        let flags = event.modifierFlags.intersection(.deviceIndependentFlagsMask)
        if flags == .command {
            let lower = event.charactersIgnoringModifiers?.lowercased()
            if lower == "l", onReferenceSelection?() == true { return true }
            if lower == "o", onOpenFocusedInEditor?() == true { return true }
        }
        return super.performKeyEquivalent(with: event)
    }
}

// MARK: - Native NSScrollView with Sticky File Header & Axis-Latched Horizontal Scroll

/// Native `NSScrollView` hosting `PCodeDiffDocumentView`, with:
/// - Pure native vertical momentum physics (no nested horizontal scroll views trapping vertical flicks)
/// - Axis-latched horizontal trackpad scrolling (matching `PCodeHorizontalBlockScrollView`)
/// - O(log F) floating sticky file header with smooth push-up transitions
@MainActor
final class PCodeDiffScrollView: NSScrollView {
    let diffDocumentView: PCodeDiffDocumentView
    let stickyHeaderView = PCodeDiffFileHeaderRowView(frame: .zero)

    private enum GestureAxis {
        case undecided
        case horizontal
        case vertical
    }

    private var gestureAxis: GestureAxis = .undecided
    private var lastStickyRowIndex: Int?
    private var observers: [NSObjectProtocol] = []

    init(theme: PCodeDiffTheme, syntaxStore: PCodeDiffSyntaxStore) {
        self.diffDocumentView = PCodeDiffDocumentView(theme: theme, syntaxStore: syntaxStore)
        super.init(frame: .zero)

        hasVerticalScroller = true
        hasHorizontalScroller = false
        autohidesScrollers = true
        scrollerStyle = .overlay
        drawsBackground = true
        backgroundColor = theme.background
        borderType = .noBorder
        clipsToBounds = true

        contentView.postsBoundsChangedNotifications = true
        documentView = diffDocumentView

        stickyHeaderView.isHidden = true
        addSubview(stickyHeaderView)

        let nc = NotificationCenter.default
        observers.append(nc.addObserver(
            forName: NSView.boundsDidChangeNotification,
            object: contentView,
            queue: .main
        ) { [weak self] _ in
            MainActor.assumeIsolated {
                self?.diffDocumentView.handleClipBoundsDidChange()
            }
        })
        observers.append(nc.addObserver(
            forName: NSScrollView.willStartLiveScrollNotification,
            object: self,
            queue: .main
        ) { [weak self] _ in
            MainActor.assumeIsolated {
                self?.diffDocumentView.handleLiveScrollBegan()
            }
        })
    }

    @available(*, unavailable)
    required init?(coder: NSCoder) { fatalError() }

    deinit {
        for obs in observers {
            NotificationCenter.default.removeObserver(obs)
        }
    }

    override func layout() {
        super.layout()
        diffDocumentView.handleViewportSizeChanged(newSize: contentSize)
        updateStickyHeader()
    }

    func updateStickyHeader(forceReconfigure: Bool = false) {
        let scrollY = contentView.bounds.origin.y
        guard scrollY > 0.5,
              let state = diffDocumentView.snapshot.stickyHeaderState(for: scrollY)
        else {
            if !stickyHeaderView.isHidden {
                stickyHeaderView.isHidden = true
                lastStickyRowIndex = nil
            }
            return
        }

        stickyHeaderView.isHidden = false
        let width = contentSize.width
        let h = PCodeDiffLayoutSnapshot.fileHeaderHeight
        // NSScrollView is flipped on macOS (top-left origin), clipped via `clipsToBounds = true`.
        let topY = isFlipped ? state.yOffset : (bounds.height - h - state.yOffset)
        stickyHeaderView.frame = CGRect(x: 0, y: topY, width: width, height: h)

        if forceReconfigure || lastStickyRowIndex != state.rowIndex {
            lastStickyRowIndex = state.rowIndex
            diffDocumentView.configureStickyHeader(stickyHeaderView, rowIndex: state.rowIndex)
        }
    }

    // MARK: - Axis-Latched Horizontal / Vertical Scroll Routing

    override func scrollWheel(with event: NSEvent) {
        let phase = event.phase
        let momentum = event.momentumPhase

        if !diffDocumentView.canScrollHorizontally {
            gestureAxis = .undecided
            super.scrollWheel(with: event)
            return
        }

        // Wait for real deltas on `.mayBegin` instead of leaking an unterminated gesture to NSScrollView.
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
        if momentum == .ended || momentum == .cancelled || phase == .cancelled {
            gestureAxis = .undecided
        }

        if currentAxis == .horizontal {
            let scale: CGFloat = event.hasPreciseScrollingDeltas ? 1.0 : 12.0
            let pt = diffDocumentView.convert(event.locationInWindow, from: nil)
            diffDocumentView.adjustHorizontalScroll(deltaX: event.scrollingDeltaX * scale, locationInView: pt)
        } else if currentAxis == .vertical {
            super.scrollWheel(with: event)
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
}

// MARK: - SwiftUI Bridge

/// `NSViewRepresentable` bridging `PCodeDiffScrollView` into `PCodeChangesInspectorView`.
struct PCodeNativeDiffScrollView: NSViewRepresentable {
    let diffs: [PCodeFileDiff]
    let layout: PCodeDiffLayout
    let wrapLines: Bool
    let viewportWidth: CGFloat
    let collapsedFiles: Set<String>
    let expandedGaps: [PCodeDiffExpandedGapKey: [PCodeDiffLine]]
    let focusedFilePath: String?
    let updatedUnseenFiles: Set<String>
    let copiedPath: String?
    let selection: PCodeDiffSelection?
    let followBottom: Bool
    let scrollToBottomToken: Int
    let scrollToFileRequest: (id: String, token: Int, animated: Bool)?
    let theme: PCodeDiffTheme
    let syntaxStore: PCodeDiffSyntaxStore

    let onToggleCollapse: (String, String) -> Void
    let onEditInFilesTab: (PCodeFileDiff) -> Void
    let onOpenInEditor: (PCodeFileDiff) -> Void
    let onCopyPath: (String) -> Void
    let onExpandGap: (PCodeDiffExpandGapPayload) -> Void
    let onLineClick: (PCodeDiffLine, String, NSEvent.ModifierFlags) -> Void
    let onPrevFile: () -> Void
    let onNextFile: () -> Void
    let onEscape: () -> Bool
    let onReferenceSelection: () -> Bool
    let onOpenFocusedInEditor: () -> Bool
    let onUserScrolledAwayFromBottom: () -> Void

    func makeCoordinator() -> Coordinator {
        Coordinator()
    }

    func makeNSView(context: Context) -> PCodeDiffScrollView {
        let scrollView = PCodeDiffScrollView(theme: theme, syntaxStore: syntaxStore)
        scrollView.appearance = NSAppearance(named: theme.isDark ? .darkAqua : .aqua)
        context.coordinator.bind(scrollView: scrollView, syntaxStore: syntaxStore)
        return scrollView
    }

    func updateNSView(_ scrollView: PCodeDiffScrollView, context: Context) {
        let doc = scrollView.diffDocumentView
        let coord = context.coordinator

        let nsAppearance = NSAppearance(named: theme.isDark ? .darkAqua : .aqua)
        if scrollView.appearance != nsAppearance {
            scrollView.appearance = nsAppearance
        }
        scrollView.backgroundColor = theme.background

        doc.onToggleCollapse = onToggleCollapse
        doc.onEditInFilesTab = onEditInFilesTab
        doc.onOpenInEditor = onOpenInEditor
        doc.onCopyPath = onCopyPath
        doc.onExpandGap = onExpandGap
        doc.onLineClick = onLineClick
        doc.onPrevFile = onPrevFile
        doc.onNextFile = onNextFile
        doc.onEscape = onEscape
        doc.onReferenceSelection = onReferenceSelection
        doc.onOpenFocusedInEditor = onOpenFocusedInEditor
        doc.onUserScrolledAwayFromBottom = onUserScrolledAwayFromBottom

        let bottomPadding: CGFloat = selection != nil ? 56 : 16
        let effectiveViewportWidth = viewportWidth > 1 ? viewportWidth : scrollView.contentSize.width
        let wrapCodeWidth: CGFloat? = {
            guard wrapLines else { return nil }
            switch layout {
            case .unified:
                return PCodeDiffLayoutSnapshot.unifiedWrapCodeWidth(viewportWidth: effectiveViewportWidth, theme: theme)
            case .sideBySide:
                return PCodeDiffLayoutSnapshot.sideBySideWrapCodeWidth(viewportWidth: effectiveViewportWidth, theme: theme)
            }
        }()
        let wrapWidthPoints = wrapCodeWidth.map { Int($0) } ?? 0
        let inputKey = Coordinator.SnapshotKey(
            diffs: diffs,
            layout: layout,
            wrapLines: wrapLines,
            wrapWidthPoints: wrapWidthPoints,
            collapsedFiles: collapsedFiles,
            expandedGapCounts: expandedGaps.mapValues(\.count),
            focusedFilePath: focusedFilePath,
            updatedUnseenFiles: updatedUnseenFiles,
            copiedPath: copiedPath,
            lineRowHeight: theme.lineRowHeight,
            charAdvance: theme.charAdvance,
            fontName: theme.codeFont.fontName,
            fontSizeTenths: Int((theme.codeFont.pointSize * 10).rounded()),
            bottomPadding: bottomPadding
        )

        let isFirstApply = !coord.hasAppliedOnce
        let needsApply = isFirstApply
            || coord.lastSnapshotKey != inputKey
            || coord.lastSelection != selection
            || coord.lastTheme != theme
            || coord.lastFollowBottom != followBottom

        if needsApply {
            let snapshot: PCodeDiffLayoutSnapshot
            if coord.lastSnapshotKey == inputKey, let cached = coord.cachedSnapshot {
                snapshot = cached
            } else {
                snapshot = PCodeDiffLayoutSnapshot.build(
                    diffs: diffs,
                    layout: layout,
                    collapsedFiles: collapsedFiles,
                    expandedGaps: expandedGaps,
                    focusedFilePath: focusedFilePath,
                    updatedUnseenFiles: updatedUnseenFiles,
                    copiedPath: copiedPath,
                    lineRowHeight: theme.lineRowHeight,
                    charAdvance: theme.charAdvance,
                    bottomPadding: bottomPadding,
                    wrapCodeWidth: wrapCodeWidth,
                    codeFont: theme.codeFont
                )
                coord.lastSnapshotKey = inputKey
                coord.cachedSnapshot = snapshot
            }

            coord.hasAppliedOnce = true
            coord.lastSelection = selection
            coord.lastTheme = theme
            coord.lastFollowBottom = followBottom

            doc.apply(
                snapshot: snapshot,
                layout: layout,
                selection: selection,
                theme: theme,
                followBottom: followBottom,
                allowSmoothGlide: !isFirstApply
            )
        }

        if scrollToBottomToken != coord.lastScrollToBottomToken {
            coord.lastScrollToBottomToken = scrollToBottomToken
            doc.scrollToBottom(animated: !isFirstApply)
        }

        if let req = scrollToFileRequest, req.token != coord.lastScrollToFileToken {
            coord.lastScrollToFileToken = req.token
            _ = doc.scrollToFile(idOrPath: req.id, animated: req.animated)
        }
    }

    static func dismantleNSView(_ scrollView: PCodeDiffScrollView, coordinator: Coordinator) {
        coordinator.unbind()
    }

    @MainActor
    final class Coordinator {
        struct SnapshotKey: Equatable {
            let diffs: [PCodeFileDiff]
            let layout: PCodeDiffLayout
            let wrapLines: Bool
            let wrapWidthPoints: Int
            let collapsedFiles: Set<String>
            let expandedGapCounts: [PCodeDiffExpandedGapKey: Int]
            let focusedFilePath: String?
            let updatedUnseenFiles: Set<String>
            let copiedPath: String?
            let lineRowHeight: CGFloat
            let charAdvance: CGFloat
            let fontName: String
            let fontSizeTenths: Int
            let bottomPadding: CGFloat
        }

        var hasAppliedOnce = false
        var lastScrollToBottomToken = 0
        var lastScrollToFileToken = 0
        var lastSnapshotKey: SnapshotKey?
        var cachedSnapshot: PCodeDiffLayoutSnapshot?
        var lastSelection: PCodeDiffSelection?
        var lastTheme: PCodeDiffTheme?
        var lastFollowBottom: Bool?
        private var tokenObserverID: UUID?
        private weak var syntaxStore: PCodeDiffSyntaxStore?

        deinit {
            if let id = tokenObserverID, let store = syntaxStore {
                Task { @MainActor in
                    store.removeTokensReadyObserver(id)
                }
            }
        }

        func bind(scrollView: PCodeDiffScrollView, syntaxStore: PCodeDiffSyntaxStore) {
            self.syntaxStore = syntaxStore
            tokenObserverID = syntaxStore.addTokensReadyObserver { [weak scrollView] hunkKey in
                scrollView?.diffDocumentView.handleTokensReady(forHunkKey: hunkKey)
            }
        }

        func unbind() {
            if let id = tokenObserverID {
                syntaxStore?.removeTokensReadyObserver(id)
                tokenObserverID = nil
            }
        }
    }
}
