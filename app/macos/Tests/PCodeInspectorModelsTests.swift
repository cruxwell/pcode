import Foundation
import SwiftUI
import Testing
@testable import Ghostty

struct PCodeInspectorModelsTests {
    @Test func diffSelectionFormatsSingleAndMultiLineReferences() {
        let single = PCodeDiffSelection(path: "src/auth.swift", startLine: 42, endLine: 42)
        #expect(single.promptReference == "@src/auth.swift:L42")

        let reversed = PCodeDiffSelection(path: "src/auth.swift", startLine: 58, endLine: 42)
        #expect(reversed.startLine == 42)
        #expect(reversed.endLine == 58)
        #expect(reversed.promptReference == "@src/auth.swift:L42-L58")
    }

    @Test func fileDiffInfersLanguageAndFirstChangedLine() {
        let hunk = PCodeDiffHunk(
            id: "h1",
            header: "@@ -10,3 +10,4 @@",
            oldStart: 10,
            oldCount: 3,
            newStart: 10,
            newCount: 4,
            lines: [
                PCodeDiffLine(id: "l1", kind: .context, oldLineNumber: 10, newLineNumber: 10, text: "let a = 1"),
                PCodeDiffLine(id: "l2", kind: .added, oldLineNumber: nil, newLineNumber: 11, text: "let b = 2"),
            ]
        )
        let diff = PCodeFileDiff(path: "app/macos/PCode/Auth.swift", hunks: [hunk])
        #expect(diff.language == "swift")
        #expect(diff.basename == "Auth.swift")
        #expect(diff.parentDirectory == "app/macos/PCode")
        #expect(diff.firstChangedLine == 11)
    }

    @Test @MainActor func inspectorStateResponsiveAndNavigationHelpers() {
        #expect(PCodeInspectorState.shouldUseOverlay(containerWidth: 700))
        #expect(!PCodeInspectorState.shouldUseOverlay(containerWidth: 940))

        let state = PCodeInspectorState()
        #expect(state.isOpen)
        #expect(state.diffScope == .session)

        state.close()
        #expect(!state.isOpen)

        state.toggleCollapsed(filePath: "a.swift")
        #expect(state.collapsedFiles.contains("a.swift"))

        state.openChanges(filePath: "a.swift", scope: .workingTree)
        #expect(state.isOpen)
        #expect(state.activeTab == .changes)
        #expect(state.diffScope == .workingTree)
        #expect(state.focusedFilePath == "a.swift")
        #expect(!state.collapsedFiles.contains("a.swift"))

        state.toggleExpanded()
        #expect(state.isExpanded)
        state.close()
        #expect(!state.isOpen)
        #expect(!state.isExpanded)
    }

    @Test func changeKindMapsOperationStrings() {
        #expect(PCodeFileChangeKind(operation: "write").badge == "A")
        #expect(PCodeFileChangeKind(operation: "create").badge == "A")
        #expect(PCodeFileChangeKind(operation: "delete").badge == "D")
        #expect(PCodeFileChangeKind(operation: "rename").badge == "R")
        #expect(PCodeFileChangeKind(operation: "edit").badge == "M")
    }

    @Test @MainActor func overlayDrawerWidthClampsNarrowAndWideContainers() {
        // Very narrow container (< minSplitWidth 320): never overflows totalWidth.
        #expect(PCodeInspectorSplit<EmptyView>.overlayDrawerWidth(in: 240, fraction: 0.44) == 240)
        // Normal overlay container (700pt): respects minSplitWidth and 0.85 cap.
        #expect(PCodeInspectorSplit<EmptyView>.overlayDrawerWidth(in: 700, fraction: 0.25) == 320)
        #expect(PCodeInspectorSplit<EmptyView>.overlayDrawerWidth(in: 700, fraction: 0.95) == 700 * 0.85)
    }

    @Test @MainActor func diffWrapLinesPersistsAndDefaultsToOff() {
        let key = PCodeInspectorState.diffWrapLinesKey
        let previous = UserDefaults.standard.object(forKey: key)
        defer {
            if let previous {
                UserDefaults.standard.set(previous, forKey: key)
            } else {
                UserDefaults.standard.removeObject(forKey: key)
            }
        }

        UserDefaults.standard.removeObject(forKey: key)
        let fresh = PCodeInspectorState()
        #expect(!fresh.wrapDiffLines)

        fresh.wrapDiffLines = true
        #expect(UserDefaults.standard.bool(forKey: key))

        let reloaded = PCodeInspectorState()
        #expect(reloaded.wrapDiffLines)
    }

    @Test @MainActor func diffLayoutSnapshotWrapsLongLinesAndMatchesCTLineCache() {
        let font = NSFont.monospacedSystemFont(ofSize: 11, weight: .regular)
        let theme = PCodeDiffTheme.make(
            isDark: true,
            surfaceBackground: .black,
            foreground: .white,
            codeFont: font
        )
        let shortLine = "let x = 1"
        let longLine = "let veryLongIdentifier = someFunctionCall(firstArgument: 12345, secondArgument: \"hello world from pcode diff viewer\")"
        let hunk = PCodeDiffHunk(
            id: "h1",
            header: "@@ -1,2 +1,2 @@",
            oldStart: 1,
            oldCount: 2,
            newStart: 1,
            newCount: 2,
            lines: [
                PCodeDiffLine(id: "l1", kind: .deleted, oldLineNumber: 1, newLineNumber: nil, text: shortLine),
                PCodeDiffLine(id: "l2", kind: .added, oldLineNumber: nil, newLineNumber: 1, text: longLine),
            ]
        )
        let diff = PCodeFileDiff(path: "src/example.swift", hunks: [hunk])
        let wrapWidth: CGFloat = 180

        let unwrapped = PCodeDiffLayoutSnapshot.build(
            diffs: [diff],
            layout: .unified,
            collapsedFiles: [],
            expandedGaps: [:],
            focusedFilePath: nil,
            updatedUnseenFiles: [],
            copiedPath: nil,
            lineRowHeight: theme.lineRowHeight,
            charAdvance: theme.charAdvance,
            bottomPadding: 16
        )
        let wrapped = PCodeDiffLayoutSnapshot.build(
            diffs: [diff],
            layout: .unified,
            collapsedFiles: [],
            expandedGaps: [:],
            focusedFilePath: nil,
            updatedUnseenFiles: [],
            copiedPath: nil,
            lineRowHeight: theme.lineRowHeight,
            charAdvance: theme.charAdvance,
            bottomPadding: 16,
            wrapCodeWidth: wrapWidth,
            codeFont: font
        )

        #expect(unwrapped.wrapCodeWidth == nil)
        #expect(wrapped.wrapCodeWidth == wrapWidth)
        // Row indices: 0 = fileHeader, 1 = hunkHeader, 2 = shortLine, 3 = longLine, 4 = spacer
        #expect(unwrapped.heights[2] == theme.lineRowHeight)
        #expect(unwrapped.heights[3] == theme.lineRowHeight)
        #expect(wrapped.heights[2] == theme.lineRowHeight)
        #expect(wrapped.heights[3] > theme.lineRowHeight)
        #expect(wrapped.totalHeight > unwrapped.totalHeight)

        // Verify PCodeDiffCTLineCache produces the exact same visual segment count as layout.
        let cache = PCodeDiffCTLineCache()
        let entry = cache.ctLine(
            text: longLine,
            tokens: nil,
            lineKind: .added,
            hunkKeyHash: 1,
            lineIndex: 1,
            theme: theme,
            wrapWidth: wrapWidth
        )
        #expect(CGFloat(entry.ctLines.count) * theme.lineRowHeight == wrapped.heights[3])

        // Side-by-side pairs shortLine (left) with longLine (right): height matches the taller side.
        let wrappedSbs = PCodeDiffLayoutSnapshot.build(
            diffs: [diff],
            layout: .sideBySide,
            collapsedFiles: [],
            expandedGaps: [:],
            focusedFilePath: nil,
            updatedUnseenFiles: [],
            copiedPath: nil,
            lineRowHeight: theme.lineRowHeight,
            charAdvance: theme.charAdvance,
            bottomPadding: 16,
            wrapCodeWidth: wrapWidth,
            codeFont: font
        )
        #expect(wrappedSbs.heights[2] == wrapped.heights[3])

        // Verify horizontal scroll is disabled and offsets reset while wrapping is enabled.
        let scrollView = PCodeDiffScrollView(theme: theme, syntaxStore: PCodeDiffSyntaxStore())
        scrollView.frame = CGRect(x: 0, y: 0, width: 280, height: 300)
        scrollView.diffDocumentView.apply(
            snapshot: unwrapped,
            layout: .unified,
            selection: nil,
            theme: theme,
            followBottom: false,
            allowSmoothGlide: false
        )
        #expect(scrollView.diffDocumentView.canScrollHorizontally)
        scrollView.diffDocumentView.adjustHorizontalScroll(deltaX: -60, locationInView: NSPoint(x: 100, y: 50))
        #expect(scrollView.diffDocumentView.unifiedHorizontalOffset > 0)

        scrollView.diffDocumentView.apply(
            snapshot: wrapped,
            layout: .unified,
            selection: nil,
            theme: theme,
            followBottom: false,
            allowSmoothGlide: false
        )
        #expect(!scrollView.diffDocumentView.canScrollHorizontally)
        #expect(scrollView.diffDocumentView.unifiedHorizontalOffset == 0)

        // Verify parity across edge cases: tabs, unbroken tokens, Unicode, embedded CR, and syntax tokens.
        let edgeCases: [(String, [PCodeSyntaxToken]?)] = [
            ("\t\tlet tabbed = value", nil),
            (String(repeating: "a", count: 80), nil),
            ("let greeting = \"こんにちは世界 🚀✨ — wrapping unicode text cleanly\"", nil),
            ("first\rsecond", nil),
            ("let keyword = \"highlighted string value that wraps across lines\"", [
                PCodeSyntaxToken(content: "let", kind: .keyword),
                PCodeSyntaxToken(content: " keyword = ", kind: .plain),
                PCodeSyntaxToken(content: "\"highlighted string value that wraps across lines\"", kind: .string),
            ]),
        ]
        for (idx, (sample, tokens)) in edgeCases.enumerated() {
            let expectedLines = PCodeDiffLayoutSnapshot.wrappedLineCount(
                for: sample,
                wrapWidth: wrapWidth,
                codeFont: font,
                charAdvance: theme.charAdvance
            )
            let cached = cache.ctLine(
                text: sample,
                tokens: tokens,
                lineKind: .added,
                hunkKeyHash: 99,
                lineIndex: idx,
                theme: theme,
                wrapWidth: wrapWidth
            )
            #expect(cached.ctLines.count == expectedLines)
        }

        // Verify resizing wrap width updates row heights cleanly.
        let widerWidth: CGFloat = 600
        let wrappedWider = PCodeDiffLayoutSnapshot.build(
            diffs: [diff],
            layout: .unified,
            collapsedFiles: [],
            expandedGaps: [:],
            focusedFilePath: nil,
            updatedUnseenFiles: [],
            copiedPath: nil,
            lineRowHeight: theme.lineRowHeight,
            charAdvance: theme.charAdvance,
            bottomPadding: 16,
            wrapCodeWidth: widerWidth,
            codeFont: font
        )
        #expect(wrappedWider.heights[3] < wrapped.heights[3])
    }

    @Test func diffThemeGutterScalesDynamicallyWithMaxLineNumber() {
        let font = NSFont.monospacedSystemFont(ofSize: 11, weight: .regular)
        let small = PCodeDiffTheme.make(
            isDark: true,
            surfaceBackground: .black,
            foreground: .white,
            codeFont: font,
            maxLineNumber: 42
        )
        let medium = PCodeDiffTheme.make(
            isDark: true,
            surfaceBackground: .black,
            foreground: .white,
            codeFont: font,
            maxLineNumber: 999
        )
        let large = PCodeDiffTheme.make(
            isDark: true,
            surfaceBackground: .black,
            foreground: .white,
            codeFont: font,
            maxLineNumber: 12_345
        )

        // Single-column unified gutter = number column + marker column.
        #expect(small.unifiedGutterWidth == small.unifiedNumWidth + small.unifiedMarkerWidth)
        #expect(small.unifiedNumWidth < medium.unifiedNumWidth)
        #expect(medium.unifiedNumWidth < large.unifiedNumWidth)
        #expect(small.sideBySideNumWidth < large.sideBySideNumWidth)
        // Compact default: 3-digit unified gutter stays well below the old ~90pt dual-column gutter.
        #expect(medium.unifiedGutterWidth < 45)
    }

    @Test func diffThemeMaxLineNumberInspectsHunksAndExpandedGaps() {
        let hunk = PCodeDiffHunk(
            id: "h1",
            header: "@@ -10,5 +12,8 @@",
            oldStart: 10,
            oldCount: 5,
            newStart: 12,
            newCount: 8,
            lines: [
                PCodeDiffLine(id: "l1", kind: .context, oldLineNumber: 10, newLineNumber: 12, text: "a"),
                PCodeDiffLine(id: "l2", kind: .added, oldLineNumber: nil, newLineNumber: 19, text: "b"),
            ]
        )
        let diff = PCodeFileDiff(path: "src/foo.swift", hunks: [hunk])
        #expect(PCodeDiffTheme.maxLineNumber(in: [diff]) == 19)

        let gapKey = PCodeDiffExpandedGapKey(fileID: diff.id, gapID: "gap-1")
        let staleKey = PCodeDiffExpandedGapKey(fileID: "src/removed.swift", gapID: "gap-stale")
        let expanded = [
            gapKey: [
                PCodeDiffLine(id: "g1", kind: .context, oldLineNumber: 1200, newLineNumber: 1205, text: "x"),
            ],
            staleKey: [
                PCodeDiffLine(id: "g2", kind: .context, oldLineNumber: 99_999, newLineNumber: 99_999, text: "stale"),
            ],
        ]
        #expect(PCodeDiffTheme.maxLineNumber(in: [diff], expandedGaps: expanded) == 1205)

        // Pure-deletion hunk where oldLineNumber > newLineNumber still drives the gutter width.
        let deletedHunk = PCodeDiffHunk(
            id: "h-del",
            header: "@@ -1000,5 +0,0 @@",
            oldStart: 1000,
            oldCount: 5,
            newStart: 0,
            newCount: 0,
            lines: [
                PCodeDiffLine(id: "d1", kind: .deleted, oldLineNumber: 1004, newLineNumber: nil, text: "gone"),
            ]
        )
        let deletedDiff = PCodeFileDiff(path: "src/deleted.swift", kind: .deleted, hunks: [deletedHunk])
        #expect(PCodeDiffTheme.maxLineNumber(in: [deletedDiff]) == 1004)
    }
}
