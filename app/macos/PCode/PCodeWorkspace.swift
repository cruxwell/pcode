import AppKit
import Combine
import GhosttyKit
import SwiftUI
import UniformTypeIdentifiers

/// SPIKE: pcode's own window, in place of Ghostty's terminal windows, tabs and splits.
///
/// A sidebar lists the sessions open here and the recent ones pcode saved;
/// picking one shows its pane: pcode's terminal (with its docked editor,
/// popups, drawer and toasts, as in a Ghostty window) and, toggled with ⌘J,
/// a shell along the bottom in the session's directory. Sessions are not
/// tabs or windows: every one lives in this window, and switching only swaps
/// which pane is shown, so a hidden session keeps running.
///
/// On by default (see `PCodeWorkspaceMode.enabled` to turn it off). pcode runs
/// as `$PCODE_COMMAND` (default `pcode`) through the user's login shell,
/// resumed with `-c <id>` in the session's own directory.
enum PCodeWorkspaceMode {
    /// On unless turned off: `PCODE_WORKSPACE=0`, or
    /// `defaults write com.pcode.terminal PCodeWorkspace -bool false` for
    /// Ghostty's own terminal windows, tabs and splits.
    static let enabled: Bool = {
        if let env = ProcessInfo.processInfo.environment["PCODE_WORKSPACE"], !env.isEmpty {
            return env != "0"
        }
        return UserDefaults.standard.object(forKey: "PCodeWorkspace") as? Bool ?? true
    }()

    static var command: String {
        let value = ProcessInfo.processInfo.environment["PCODE_COMMAND"] ?? ""
        return value.isEmpty ? "pcode" : value
    }

    /// The user's login shell: `$SHELL`, else their account's (a Dock or
    /// Finder launch may have no `SHELL`).
    static var loginShell: String {
        if let shell = ProcessInfo.processInfo.environment["SHELL"], !shell.isEmpty { return shell }
        if let entry = getpwuid(getuid()), let shell = entry.pointee.pw_shell {
            let path = String(cString: shell)
            if !path.isEmpty { return path }
        }
        return "/bin/zsh"
    }

    /// True when `path` exists as a non-directory executable file.
    private static func isExecutableRegularFile(
        _ path: String,
        fileManager: FileManager = .default
    ) -> Bool {
        var isDir: ObjCBool = false
        return fileManager.fileExists(atPath: path, isDirectory: &isDir)
            && !isDir.boolValue
            && fileManager.isExecutableFile(atPath: path)
    }

    /// Single-quote `text` for `fish`, where `\` and `'` are escaped inside `'...'`.
    private static func fishQuoted(_ text: String) -> String {
        let escaped = text
            .replacingOccurrences(of: "\\", with: "\\\\")
            .replacingOccurrences(of: "'", with: "\\'")
        return "'\(escaped)'"
    }

    /// True when `directory` or any ancestor contains a `.envrc` file.
    static func hasEnvrc(in directory: String, fileManager: FileManager = .default) -> Bool {
        guard !directory.isEmpty else { return false }
        var current = URL(fileURLWithPath: directory).standardizedFileURL
        while true {
            let envrc = current.appendingPathComponent(".envrc").path
            var isDir: ObjCBool = false
            if fileManager.fileExists(atPath: envrc, isDirectory: &isDir), !isDir.boolValue {
                return true
            }
            let parent = current.deletingLastPathComponent()
            if parent.path == current.path {
                return false
            }
            current = parent
        }
    }

    /// Locate an executable named `name` on a colon-separated `PATH`.
    static func findExecutable(
        _ name: String,
        inPath path: String,
        fileManager: FileManager = .default
    ) -> String? {
        for dir in path.split(separator: ":") where dir.hasPrefix("/") {
            let candidate = URL(fileURLWithPath: String(dir))
                .appendingPathComponent(name)
                .path
            if isExecutableRegularFile(candidate, fileManager: fileManager) {
                return candidate
            }
        }
        return nil
    }

    /// `pcode` with `args`, through a login shell: an app launched from the
    /// Dock gets launchd's bare PATH, where a Homebrew or `uv tool` pcode is
    /// not, so only the user's own shell setup finds it.
    ///
    /// When `useDirenv` is true, goes through `direnv exec <directory>` if
    /// `direnv` is available on the login shell's PATH: direnv's shell hook
    /// only fires at an interactive prompt, so a direct command would miss
    /// `.envrc` (or keep a stale `DIRENV_DIR`).
    static func shellCommand(
        _ args: [String],
        directory: String? = nil,
        useDirenv: Bool = false,
        shell: String = loginShell,
        command: String = command
    ) -> String {
        let isFish = URL(fileURLWithPath: shell).lastPathComponent == "fish"
        let quote: (String) -> String = isFish ? fishQuoted : PCode.shellQuoted
        let commandToken = command.hasPrefix("/") ? quote(command) : command
        let cmdParts = ([commandToken] + args.map(quote)).joined(separator: " ")
        let inner: String
        if useDirenv, let directory {
            let direnvCmd = "exec direnv exec \(quote(directory)) \(cmdParts)"
            let plainCmd = "exec \(cmdParts)"
            if isFish {
                inner = "if type -q direnv; \(direnvCmd); else; \(plainCmd); end"
            } else {
                inner = "if command -v direnv >/dev/null 2>&1; then \(direnvCmd); else \(plainCmd); fi"
            }
        } else {
            inner = "exec \(cmdParts)"
        }
        return "\(PCode.shellQuoted(shell)) -l -c \(PCode.shellQuoted(inner))"
    }

    /// Cached login-shell environment and resolved `pcode` executable so
    /// subsequent panes skip spawning `zsh -l` (~450ms per pane).
    struct CachedShellEnv {
        let commandPath: String
        let direnvPath: String?
        let env: [String: String]
    }

    private static let cacheLock = NSLock()
    private static var cachedEnv: CachedShellEnv?
    private static var cacheStarted = false

    private static let skipCachedEnvKeys: Set<String> = [
        "PWD", "OLDPWD", "SHLVL", "_", "TERM", "TERM_PROGRAM",
        "TERM_PROGRAM_VERSION", "COLORTERM", "TERM_SESSION_ID",
        "TERMINFO", "XDG_DATA_DIRS", "MANPATH", "VTE_VERSION", "LANGUAGE",
        "PCODE_DOCK", "PCODE_EDITOR_ONLY", "PCODE_NATIVE_TRANSCRIPT", "PCODE_HUSH_LOGIN",
        "PCODE_HOST_ID",
    ]

    private static let envSentinel = "__PCODE_ENV_START__"

    private static func isSkippedEnvKey(_ key: String) -> Bool {
        skipCachedEnvKeys.contains(key)
            || key.hasPrefix("GHOSTTY_")
            || key.hasPrefix("DYLD_")
            || key.hasPrefix("__XCODE_")
    }

    /// The `-c` script that prints the sentinel, `command`'s resolved path and
    /// the login shell's environment. It is POSIX sh; fish can't parse it, so
    /// under fish it runs in `/bin/sh`, which inherits fish's exported setup.
    static func loginShellProbe(shell: String, command: String) -> String {
        let probe = "p=$(command -v \(PCode.shellQuoted(command)) 2>/dev/null) || exit 1; printf '\\0%s\\0%s\\0' \(PCode.shellQuoted(envSentinel)) \"$p\"; /usr/bin/env -0"
        guard URL(fileURLWithPath: shell).lastPathComponent == "fish" else { return probe }
        return "exec /bin/sh -c \(fishQuoted(probe))"
    }

    /// Warm the login-shell cache once in the background. Safe to call repeatedly.
    static func warmLoginShellCache() {
        cacheLock.lock()
        if cacheStarted {
            cacheLock.unlock()
            return
        }
        cacheStarted = true
        // Entered under the lock, so `awaitLoginShellCache` never sees the
        // warm-up started but the group still empty.
        cacheWarming.enter()
        cacheLock.unlock()

        let shell = loginShell
        let cmd = command
        let baseEnv = ProcessInfo.processInfo.environment.filter { !isSkippedEnvKey($0.key) }
        PCodeStartupTrace.mark("login-shell env warm start")

        DispatchQueue.global(qos: .userInitiated).async {
            defer { cacheWarming.leave() }
            let probe = loginShellProbe(shell: shell, command: cmd)
            let proc = Process()
            proc.executableURL = URL(fileURLWithPath: shell)
            proc.arguments = ["-l", "-c", probe]
            proc.currentDirectoryURL = URL(fileURLWithPath: NSHomeDirectory())
            proc.environment = baseEnv
            let out = Pipe()
            proc.standardOutput = out
            proc.standardError = FileHandle.nullDevice
            proc.standardInput = FileHandle.nullDevice

            let timeout = DispatchWorkItem {
                if proc.isRunning { proc.terminate() }
            }
            DispatchQueue.global(qos: .utility).asyncAfter(deadline: .now() + 5.0, execute: timeout)
            defer { timeout.cancel() }

            do {
                try proc.run()
            } catch {
                return
            }

            let data = (try? out.fileHandleForReading.readToEnd()) ?? Data()
            proc.waitUntilExit()
            guard proc.terminationStatus == 0 else { return }

            let sentinelData = Data(envSentinel.utf8)
            let chunks = data.split(separator: 0, omittingEmptySubsequences: false)
            guard let sentinelIndex = chunks.firstIndex(where: { $0 == sentinelData }),
                  sentinelIndex + 1 < chunks.count,
                  let resolvedPath = String(data: chunks[sentinelIndex + 1], encoding: .utf8),
                  resolvedPath.hasPrefix("/"),
                  FileManager.default.isExecutableFile(atPath: resolvedPath) else { return }

            // Only record vars the login shell added or modified relative to
            // baseEnv so Ghostty's own env setup and stripped vars stay intact.
            var env: [String: String] = [:]
            for chunk in chunks[(sentinelIndex + 2)...] where !chunk.isEmpty {
                guard let entry = String(data: chunk, encoding: .utf8),
                      let eq = entry.firstIndex(of: "=") else { continue }
                let key = String(entry[..<eq])
                if isSkippedEnvKey(key) { continue }
                let value = String(entry[entry.index(after: eq)...])
                if baseEnv[key] != value {
                    env[key] = value
                }
            }

            // Preserve Ghostty's bundle binary dir on PATH since env_override
            // applies after Exec.zig appends exe_dir.
            if let path = env["PATH"],
               let exeDir = Bundle.main.executableURL?.deletingLastPathComponent().path,
               !path.split(separator: ":").contains(Substring(exeDir)) {
                env["PATH"] = "\(path):\(exeDir)"
            }

            let effectivePath = env["PATH"] ?? baseEnv["PATH"] ?? ""
            let direnvPath = findExecutable("direnv", inPath: effectivePath)

            cacheLock.lock()
            cachedEnv = CachedShellEnv(commandPath: resolvedPath, direnvPath: direnvPath, env: env)
            cacheLock.unlock()
            PCodeStartupTrace.mark("login-shell env cached")
        }
    }

    /// Settles when the warm-up started by `warmLoginShellCache` ends, ready or failed.
    private static let cacheWarming = DispatchGroup()

    /// Block (off the main thread) up to `timeout` for the login-shell cache,
    /// so panes made right after launch take the direct command rather than
    /// each paying for its own login shell. Starts the warm-up if needed.
    static func awaitLoginShellCache(timeout: TimeInterval) {
        warmLoginShellCache()
        _ = cacheWarming.wait(timeout: .now() + timeout)
    }

    /// Direct command + cached login-shell env once ready, else the login-shell fallback.
    static func launchConfig(
        _ args: [String],
        directory: String
    ) -> (command: String, env: [String: String]) {
        warmLoginShellCache()
        cacheLock.lock()
        let cached = cachedEnv
        cacheLock.unlock()
        return buildLaunchConfig(
            args,
            directory: directory,
            cached: cached,
            baseEnvironment: ProcessInfo.processInfo.environment
        )
    }

    static func buildLaunchConfig(
        _ args: [String],
        directory: String,
        cached: CachedShellEnv?,
        baseEnvironment: [String: String],
        shell: String = loginShell,
        command: String = command,
        fileManager: FileManager = .default
    ) -> (command: String, env: [String: String]) {
        guard let cached,
              isExecutableRegularFile(cached.commandPath, fileManager: fileManager) else {
            let hasDirenvDir = !(baseEnvironment["DIRENV_DIR"] ?? "").isEmpty
            let shouldUseDirenv = hasDirenvDir || hasEnvrc(in: directory, fileManager: fileManager)
            return (
                shellCommand(
                    args,
                    directory: directory,
                    useDirenv: shouldUseDirenv,
                    shell: shell,
                    command: command
                ),
                [:]
            )
        }
        let hasDirenvDir = !(cached.env["DIRENV_DIR"] ?? baseEnvironment["DIRENV_DIR"] ?? "").isEmpty
        let shouldUseDirenv = hasDirenvDir || hasEnvrc(in: directory, fileManager: fileManager)
        // Pass through `env --` so `exec -l` doesn't prepend `-` to argv[0] of
        // a venv-backed Python entry point, and paths with `=` aren't parsed as
        // variable assignments.
        var parts = ["/usr/bin/env", "--"]
        if shouldUseDirenv,
           let direnvPath = cached.direnvPath,
           isExecutableRegularFile(direnvPath, fileManager: fileManager) {
            parts += [PCode.shellQuoted(direnvPath), "exec", PCode.shellQuoted(directory)]
        }
        parts += [PCode.shellQuoted(cached.commandPath)] + args.map(PCode.shellQuoted)
        return (parts.joined(separator: " "), cached.env)
    }
}

/// A saved session, read from pcode's `sessions/<id>/session.json`.
struct PCodeSessionInfo: Identifiable, Hashable, Decodable {
    let id: String
    let workspace: String
    let created: String?
    let updated: String
    let name: String?
    let title: String?
    let turns: Int
    /// The main checkout, for a session in one of its worktrees; absent in old sessions.
    let project: String?
    /// Precomputed at decode time off the main thread.
    let normalizedProjectPath: String
    let worktreeName: String?
    let createdDate: Date?
    let updatedDate: Date?

    private enum CodingKeys: String, CodingKey {
        case id, workspace, created, updated, name, title, turns, project
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        id = try c.decode(String.self, forKey: .id)
        workspace = try c.decode(String.self, forKey: .workspace)
        created = try c.decodeIfPresent(String.self, forKey: .created)
        updated = try c.decode(String.self, forKey: .updated)
        name = try c.decodeIfPresent(String.self, forKey: .name)
        title = try c.decodeIfPresent(String.self, forKey: .title)
        turns = try c.decode(Int.self, forKey: .turns)
        project = try c.decodeIfPresent(String.self, forKey: .project)
        let rawProject = project.flatMap { $0.isEmpty ? nil : $0 } ?? workspace
        normalizedProjectPath = PCodeWorkspace.normalizePath(rawProject)
        let normalizedWorkspace = PCodeWorkspace.normalizePath(workspace)
        worktreeName = normalizedWorkspace != normalizedProjectPath
            ? (normalizedWorkspace as NSString).lastPathComponent
            : nil
        createdDate = created.flatMap(PCodeWorkspace.parseISO8601)
        updatedDate = PCodeWorkspace.parseISO8601(updated)
    }

    /// Rebuild a session from its persisted index row, derived fields included
    /// (parsing dates again for every session would cost a cold launch).
    fileprivate init(_ row: LoadCache.Summary) {
        id = row.id
        workspace = row.workspace
        created = row.created
        updated = row.updated
        name = row.name
        title = row.title
        turns = row.turns
        project = row.project
        normalizedProjectPath = row.normalizedProjectPath
        worktreeName = row.worktreeName
        createdDate = row.createdReference.map(Date.init(timeIntervalSinceReferenceDate:))
        updatedDate = row.updatedReference.map(Date.init(timeIntervalSinceReferenceDate:))
    }

    /// What the sidebar groups by (main checkout for worktrees, else workspace).
    var projectPath: String { project.flatMap { $0.isEmpty ? nil : $0 } ?? workspace }

    /// Whether a pcode process holds this session now: pcode keeps an
    /// exclusive `flock` on its `.lock` while it is open (`sessions.is_open`).
    /// Reopening one of those would only continue a copy of it.
    static func isOpen(_ id: String) -> Bool {
        let path = root.appendingPathComponent(id).appendingPathComponent(".lock").path
        let fd = Darwin.open(path, O_RDONLY | O_CLOEXEC | O_NOFOLLOW)
        guard fd >= 0 else { return false }
        defer { Darwin.close(fd) }
        guard flock(fd, LOCK_SH | LOCK_NB) == 0 else { return errno == EWOULDBLOCK }
        flock(fd, LOCK_UN)
        return false
    }

    var label: String {
        if let name, !name.isEmpty { return name }
        if let title, !title.isEmpty { return title }
        return "Untitled session"
    }

    var directoryName: String { (workspace as NSString).lastPathComponent }

    /// Running background hosts (`hosts/<id>.json`), including brand-new ones
    /// that do not have a `session_id` yet.
    static func liveHostEntries(
        in directory: URL = hostRoot,
        isAlive: (Int32) -> Bool = { kill($0, 0) == 0 && PCodeProcess.isPCodeOrHost($0) }
    ) -> [(id: String, pid: Int32, sessionID: String)] {
        struct Entry: Decodable {
            let id: String
            let pid: Int32
            let session_id: String?
            let state: String?
        }
        let fm = FileManager.default
        guard let files = try? fm.contentsOfDirectory(at: directory, includingPropertiesForKeys: nil) else {
            return []
        }
        let uid = Darwin.getuid()
        let decoder = JSONDecoder()
        var entries: [(id: String, pid: Int32, sessionID: String)] = []
        for file in files where file.pathExtension == "json" {
            var jsonStat = stat()
            guard Darwin.lstat(file.path, &jsonStat) == 0,
                  (jsonStat.st_mode & S_IFMT) == S_IFREG,
                  jsonStat.st_uid == uid,
                  let data = try? Data(contentsOf: file),
                  let entry = try? decoder.decode(Entry.self, from: data),
                  PCode.valid(entry.id, length: 1...64),
                  entry.id == file.deletingPathExtension().lastPathComponent,
                  (entry.state ?? "") != "stopped",
                  entry.pid > 0,
                  isAlive(entry.pid)
            else { continue }
            entries.append((id: entry.id, pid: entry.pid, sessionID: entry.session_id ?? ""))
        }
        return entries
    }

    /// Sessions a running background host holds (pcode's `hosts/<id>.json`
    /// entries, as `host_protocol.list_hosts` reads them).
    static func hosted() -> Set<String> {
        Set(liveHostEntries().map(\.sessionID).filter { !$0.isEmpty })
    }

    /// Return the live Unix socket path (`hosts/<host_id>.sock`) for a running hosted session, if any.
    static func hostSocketPath(
        forSessionID sessionID: String?,
        hostID: String? = nil,
        in directory: URL = hostRoot,
        isAlive: (Int32) -> Bool = { kill($0, 0) == 0 && PCodeProcess.isPCodeOrHost($0) }
    ) -> String? {
        let validHost = (hostID?.isEmpty == false) ? hostID : nil
        let validSession = (sessionID?.isEmpty == false) ? sessionID : nil
        guard validHost != nil || validSession != nil else { return nil }
        let uid = Darwin.getuid()
        let entries = liveHostEntries(in: directory, isAlive: isAlive)
        // Prefer an exact host ID match, then fall back to matching by session ID
        // (e.g. after `/restart` replaces the host with a successor for the same session).
        let ordered = entries.filter { $0.id == validHost }
            + entries.filter { $0.id != validHost && validSession != nil && !$0.sessionID.isEmpty && $0.sessionID == validSession }
        for entry in ordered {
            let sockURL = directory.appendingPathComponent("\(entry.id).sock")
            var sockStat = stat()
            if Darwin.lstat(sockURL.path, &sockStat) == 0,
               (sockStat.st_mode & S_IFMT) == S_IFSOCK,
               sockStat.st_uid == uid
            {
                return sockURL.path
            }
        }
        return nil
    }

    /// Last meaningful lines of a host's log file (`hosts/<hostID>.log`), for the
    /// startup failure card when a host crashes before opening its socket.
    static func hostLogTail(
        hostID: String?,
        in directory: URL = hostRoot,
        maxLines: Int = 4,
        maxChars: Int = 240
    ) -> String? {
        guard let hostID, PCode.valid(hostID, length: 1...64) else { return nil }
        let path = directory.appendingPathComponent("\(hostID).log").path
        let fd = Darwin.open(path, O_RDONLY | O_NOFOLLOW | O_CLOEXEC)
        guard fd >= 0 else { return nil }
        defer { Darwin.close(fd) }

        var st = stat()
        guard Darwin.fstat(fd, &st) == 0,
              (st.st_mode & S_IFMT) == S_IFREG,
              st.st_uid == Darwin.getuid(),
              st.st_size > 0
        else { return nil }

        let window = min(Int(st.st_size), 8192)
        let sought = Int(st.st_size) > window
        if sought {
            _ = Darwin.lseek(fd, off_t(Int(st.st_size) - window), SEEK_SET)
        }
        var data = Data(count: window)
        var total = 0
        data.withUnsafeMutableBytes { raw in
            guard let base = raw.baseAddress else { return }
            while total < window {
                let n = Darwin.read(fd, base.advanced(by: total), window - total)
                if n < 0 {
                    if errno == EINTR { continue }
                    break
                }
                if n == 0 { break }
                total += n
            }
        }
        data.count = total
        // Seeking into the middle of a large log can split a line or multi-byte UTF-8 sequence.
        let slice: Data.SubSequence
        if sought, let nl = data.firstIndex(of: 0x0A) {
            slice = data[data.index(after: nl)...]
        } else {
            slice = data[...]
        }
        let text = String(decoding: slice, as: UTF8.self)
        let lines = text
            .split(whereSeparator: \.isNewline)
            .map { $0.trimmingCharacters(in: .whitespacesAndNewlines) }
            .filter { !$0.isEmpty && !($0.hasPrefix("serving ") && $0.hasSuffix(".sock")) }
        guard !lines.isEmpty else { return nil }
        let joined = lines.suffix(maxLines).joined(separator: "\n")
        guard joined.count > maxChars else { return joined }
        return "…" + String(joined.suffix(maxChars - 1))
    }

    /// Of `ids`, sessions a Restart Pcode moved, those ready to reopen: held
    /// by a host, or unlocked on two checks half a second apart (not in the
    /// gap between the old pcode letting go and its host taking over).
    /// Waits up to `timeout` for the rest; one still locked then, with no
    /// host, is held by some other process and left out. Blocks: call it
    /// off the main thread.
    static func awaitRestarted(_ ids: [String], timeout: TimeInterval = 5) -> Set<String> {
        guard !ids.isEmpty else { return [] }
        let deadline = Date().addingTimeInterval(timeout)
        var ready = Set<String>()
        var unlocked = Set<String>()
        while true {
            let hosted = hosted()
            for id in ids where !ready.contains(id) {
                if hosted.contains(id) {
                    ready.insert(id)
                } else if isOpen(id) {
                    unlocked.remove(id)
                } else if !unlocked.insert(id).inserted {
                    ready.insert(id)
                }
            }
            if ready.count == Set(ids).count || Date() >= deadline { break }
            Thread.sleep(forTimeInterval: 0.5)
        }
        // Unlocked at the last check, with no time left for a second.
        return ready.union(unlocked)
    }

    static var hostRoot: URL {
        let env = ProcessInfo.processInfo.environment
        if let dir = env["PCODE_HOST_DIR"], !dir.isEmpty {
            return URL(fileURLWithPath: (dir as NSString).expandingTildeInPath)
        }
        return stateRoot.appendingPathComponent("pcode/hosts")
    }

    private static var stateRoot: URL {
        let state = ProcessInfo.processInfo.environment["XDG_STATE_HOME"].flatMap { $0.isEmpty ? nil : $0 }
            ?? (NSHomeDirectory() as NSString).appendingPathComponent(".local/state")
        return URL(fileURLWithPath: state)
    }

    static var root: URL {
        let env = ProcessInfo.processInfo.environment
        if let dir = env["PCODE_SESSION_DIR"], !dir.isEmpty {
            return URL(fileURLWithPath: (dir as NSString).expandingTildeInPath)
        }
        return stateRoot.appendingPathComponent("pcode/sessions")
    }

    /// Saved sessions with at least one turn (or held by a live host), newest
    /// first by `updated`: every one, for the sidebar to slice per project.
    static func load(
        root: URL = root,
        hosted: Set<String> = hosted(),
        cache: LoadCache = .shared
    ) -> [PCodeSessionInfo] {
        cache.infos(root: root).filter { $0.turns > 0 || hosted.contains($0.id) }
    }

    /// An index of every saved session's summary, keyed by directory name and
    /// reused while its `session.json`'s mtime and size are unchanged: a reload
    /// lists and stats every session but parses only the few that changed.
    /// It is persisted (`session-index.json` in the app's caches folder), so a
    /// cold launch parses only what changed while the app was closed too.
    /// `session.json` stays the source of truth; the index is disposable.
    final class LoadCache {
        /// Bump whenever `Summary` or what it derives (paths, dates) changes.
        static let indexVersion = 2

        /// The app's instance: persisted only for the session root it was made
        /// for, so a test (or caller) loading another root never touches it.
        static let shared = LoadCache(indexURL: defaultIndexURL, persistingFor: PCodeSessionInfo.root)

        static var defaultIndexURL: URL? {
            guard let caches = FileManager.default.urls(for: .cachesDirectory, in: .userDomainMask).first else {
                return nil
            }
            return caches
                .appendingPathComponent(Bundle.main.bundleIdentifier ?? "pcode", isDirectory: true)
                .appendingPathComponent("session-index.json")
        }

        /// A session's sidebar fields, decoded and derived once.
        struct Summary: Codable, Equatable {
            let id: String
            let workspace: String
            let created: String?
            let updated: String
            let name: String?
            let title: String?
            let turns: Int
            let project: String?
            let normalizedProjectPath: String
            let worktreeName: String?
            let createdReference: Double?
            let updatedReference: Double?

            init(_ info: PCodeSessionInfo) {
                id = info.id
                workspace = info.workspace
                created = info.created
                updated = info.updated
                name = info.name
                title = info.title
                turns = info.turns
                project = info.project
                normalizedProjectPath = info.normalizedProjectPath
                worktreeName = info.worktreeName
                createdReference = info.createdDate?.timeIntervalSinceReferenceDate
                updatedReference = info.updatedDate?.timeIntervalSinceReferenceDate
            }
        }

        /// One session directory: its `session.json`'s stamp and summary, or no
        /// summary when the file didn't parse or names another id (so it isn't
        /// reread until it changes).
        struct Row: Codable, Equatable {
            let dir: String
            let sec: Int
            let nsec: Int
            let size: Int64
            let summary: Summary?
        }

        private struct IndexFile: Codable {
            let version: Int
            let root: String
            let rows: [Row]
        }

        private struct Entry {
            let row: Row
            let info: PCodeSessionInfo?
        }

        let indexURL: URL?
        private let persistRoot: String?
        private let lock = NSLock()
        private var root: String?
        private var entries: [String: Entry] = [:]
        /// What the last load returned, reused while nothing changed.
        private var sorted: [PCodeSessionInfo] = []
        private var indexRead = false
        /// Whether the index file on disk matches `entries`.
        private var persisted = false
        private let writer = DispatchQueue(label: "com.pcode.session-index", qos: .utility)
        /// The newest snapshot not yet written (guarded by `pendingLock`): a
        /// burst of reloads writes once, the latest.
        private var pendingWrite: IndexFile?
        private let pendingLock = NSLock()
        private var decodeCount = 0
        /// `session.json`s parsed since this cache was made (not counting the index).
        var decodes: Int {
            lock.lock()
            defer { lock.unlock() }
            return decodeCount
        }

        /// `indexURL` persists the index for loads of `root` (defaults to
        /// `PCodeSessionInfo.root`); nil keeps it in memory only.
        init(indexURL: URL? = nil, persistingFor root: URL? = nil) {
            self.indexURL = indexURL
            persistRoot = indexURL == nil ? nil : (root ?? PCodeSessionInfo.root).standardizedFileURL.path
        }

        /// Every session under `root` that parsed, newest first by `updated`.
        /// Blocks on disk: call it off the main thread.
        func infos(root rootURL: URL) -> [PCodeSessionInfo] {
            lock.lock()
            defer { lock.unlock() }
            let rootPath = rootURL.standardizedFileURL.path
            let persisting = indexURL != nil && rootPath == persistRoot
            if root != rootPath {
                root = rootPath
                entries = [:]
                sorted = []
                indexRead = false
                persisted = false
            }
            if !indexRead {
                indexRead = true
                if persisting, let rows = readIndex(root: rootPath) {
                    entries.reserveCapacity(rows.count)
                    for row in rows {
                        entries[row.dir] = Entry(row: row, info: row.summary.map(PCodeSessionInfo.init))
                    }
                    sorted = Self.sortedInfos(entries.values)
                    persisted = true
                }
            }

            // List and stat every session; keep entries whose stamp still matches.
            var slots: [(dir: String, path: String, stat: Darwin.stat, entry: Entry?)] = []
            slots.reserveCapacity(entries.count + 16)
            for (dir, st) in Self.sessionFiles(rootPath) {
                let path = rootPath + "/" + dir + "/session.json"
                var cached: Entry?
                if let old = entries[dir], old.row.size == Int64(st.st_size),
                   old.row.sec == Int(st.st_mtimespec.tv_sec), old.row.nsec == Int(st.st_mtimespec.tv_nsec) {
                    cached = old
                }
                slots.append((dir, path, st, cached))
            }
            // Parse the rest in parallel: with no index (first launch) that's
            // every saved session.
            let misses = slots.indices.filter { slots[$0].entry == nil }
            decodeCount += misses.count
            if !misses.isEmpty {
                var parsed = [PCodeSessionInfo?](repeating: nil, count: misses.count)
                let chunk = 32
                parsed.withUnsafeMutableBufferPointer { out in
                    DispatchQueue.concurrentPerform(iterations: (misses.count + chunk - 1) / chunk) { c in
                        let decoder = JSONDecoder()
                        for k in (c * chunk)..<min(misses.count, (c + 1) * chunk) {
                            let slot = slots[misses[k]]
                            out[k] = (try? Data(contentsOf: URL(fileURLWithPath: slot.path)))
                                .flatMap { try? decoder.decode(PCodeSessionInfo.self, from: $0) }
                                .flatMap { $0.id == slot.dir ? $0 : nil }
                        }
                    }
                }
                for (k, index) in misses.enumerated() {
                    let st = slots[index].stat
                    let row = Row(
                        dir: slots[index].dir,
                        sec: Int(st.st_mtimespec.tv_sec), nsec: Int(st.st_mtimespec.tv_nsec),
                        size: Int64(st.st_size), summary: parsed[k].map(Summary.init))
                    slots[index].entry = Entry(row: row, info: parsed[k])
                }
            }
            let changed = !misses.isEmpty || slots.count != entries.count
            if changed {
                var fresh: [String: Entry] = [:]
                fresh.reserveCapacity(slots.count)
                for slot in slots {
                    if let entry = slot.entry { fresh[slot.dir] = entry }
                }
                entries = fresh  // Drops deleted sessions.
                sorted = Self.sortedInfos(entries.values)
                persisted = false
            }
            if persisting && !persisted {
                persisted = true
                write(IndexFile(version: Self.indexVersion, root: rootPath, rows: entries.values.map(\.row)))
            }
            return sorted
        }

        /// Wait for a pending index write (tests).
        func flush() { writer.sync {} }

        private static func sortedInfos(_ entries: Dictionary<String, Entry>.Values) -> [PCodeSessionInfo] {
            entries.compactMap(\.info).sorted { $0.updated != $1.updated ? $0.updated > $1.updated : $0.id < $1.id }
        }

        /// Each real directory in `root` (not a symlink to one) holding a
        /// regular `session.json`, with that file's stat. Stats relative to the
        /// open directory: lstat on full paths was several times slower.
        private static func sessionFiles(_ root: String) -> [(dir: String, stat: Darwin.stat)] {
            guard let dirp = opendir(root) else { return [] }
            defer { closedir(dirp) }
            let fd = dirfd(dirp)
            var files: [(dir: String, stat: Darwin.stat)] = []
            while let ent = readdir(dirp) {
                let type = ent.pointee.d_type
                let name = withUnsafeBytes(of: ent.pointee.d_name) { raw in
                    String(cString: raw.bindMemory(to: CChar.self).baseAddress!)
                }
                if name == "." || name == ".." { continue }
                var st = Darwin.stat()
                if type == DT_UNKNOWN {
                    guard fstatat(fd, name, &st, AT_SYMLINK_NOFOLLOW) == 0, (st.st_mode & S_IFMT) == S_IFDIR else { continue }
                } else if type != DT_DIR {
                    continue
                }
                guard fstatat(fd, name + "/session.json", &st, AT_SYMLINK_NOFOLLOW) == 0,
                      (st.st_mode & S_IFMT) == S_IFREG
                else { continue }
                files.append((name, st))
            }
            return files
        }

        /// The persisted rows for `root`, or nil if the file is missing,
        /// unreadable, from another version, or for another root.
        private func readIndex(root: String) -> [Row]? {
            guard let indexURL,
                  let data = try? Data(contentsOf: indexURL),
                  let file = try? JSONDecoder().decode(IndexFile.self, from: data),
                  file.version == Self.indexVersion, file.root == root
            else { return nil }
            return file.rows
        }

        private func write(_ file: IndexFile) {
            guard let indexURL else { return }
            pendingLock.lock()
            pendingWrite = file
            pendingLock.unlock()
            writer.async { [self] in
                pendingLock.lock()
                let latest = pendingWrite
                pendingWrite = nil
                pendingLock.unlock()
                guard let latest, let data = try? JSONEncoder().encode(latest) else { return }
                try? FileManager.default.createDirectory(
                    at: indexURL.deletingLastPathComponent(), withIntermediateDirectories: true)
                // `.atomic` writes a temporary file and renames it over the old one.
                try? data.write(to: indexURL, options: .atomic)
            }
        }
    }
}

/// One session shown in the workspace: pcode's terminal and its optional bottom shell.
final class PCodeSessionPane: ObservableObject, Identifiable {
    let id = UUID()
    /// The terminal running pcode's editor-only client, or nil in a native-composer pane.
    ///
    /// Native-composer panes (`native_composer`, step 3b) run no terminal for the session:
    /// the host is spawned headless and `composer` talks to it over its socket. Rather than
    /// keep a hidden terminal so every caller still has a surface, the surface is optional
    /// and the few things that need "the input" go through `focusInput()` and `composer`.
    /// The bottom shell split stays a Ghostty surface either way.
    let surface: Ghostty.SurfaceView?
    /// The native composer's connection to the session host, when this pane has one.
    let composer: PCodeComposerSession?
    /// The most recently mounted transcript of a native pane (a terminal pane's is on its
    /// surface, `pcodeTranscriptScrollView`), so a live peek scrolls this pane's.
    weak var transcriptScrollView: PCodeTranscriptScrollView?
    let directory: String
    /// The project folder this pane is grouped under (main checkout for worktrees).
    let projectPath: String
    /// Whether this pane was opened to resume an existing saved session.
    let resuming: Bool
    /// The session pcode reports (`pcode;session;<id>`), once it has.
    @Published var sessionID: String?
    /// The saved label of the session this pane was opened to resume, so the
    /// sidebar names it straight away rather than as a new session until pcode
    /// boots and reports a title or the saved-session list catches up.
    var openedLabel: String?
    /// The session host pcode attaches to (`pcode;host;<id>`), even before turn 1 creates a session id.
    @Published var hostID: String?
    /// Last non-empty host id seen for this pane, kept across client teardown so closing stops the host.
    var lastHostID: String?
    /// The id the app asked pcode to start this pane's host with (`PCODE_HOST_ID`),
    /// so closing the pane stops that host even before it has reported itself.
    var launchHostID: String?
    /// Automatic host relaunch attempts already made for this pane without a successful connect.
    var hostRelaunchAttempts = 0
    /// Whether the previous automatic recovery check saw `.lock` held without a host entry
    /// (requiring two consecutive sightings avoids misclassifying a host's own startup/shutdown).
    var sawOpenElsewhere = false
    /// True once pcode has painted its ready UI (dock ready, or fallback timeout).
    @Published private(set) var isReady = false
    @Published private(set) var shell: Ghostty.SurfaceView?
    @Published var shellVisible = false
    /// The session's title, for the sidebar: its host's while one holds the session,
    /// else the terminal's (`PCodePaneStateResolver`). Copied out so the sidebar does
    /// not observe the whole surface, which publishes on every resize and focus change.
    @Published private(set) var title = ""
    /// Active model (`provider:name`), effort, and context usage (`used/limit`)
    /// streamed from the session host for the native window titlebar.
    @Published private(set) var modelName = ""
    @Published private(set) var modelEffort = ""
    @Published private(set) var contextUsage = ""
    /// The session host's entry while one holds this pane's session.
    @Published private(set) var hostStatus: PCodeHostStatus?
    private var resolver = PCodePaneStateResolver()
    static let surfaceKeyPressed = Notification.Name("PCodeSessionPaneSurfaceKeyPressed")

    private var titleSubscription: AnyCancellable?
    private var hostSubscription: AnyCancellable?
    private var keySubscription: AnyCancellable?
    private var dockReadySubscription: AnyCancellable?
    private var inputSubscription: AnyCancellable?
    private var progressSubscription: AnyCancellable?
    private var readyFallbackWork: DispatchWorkItem?
    private var readySafetyWork: DispatchWorkItem?

    /// What the session is doing, from its host while hosted and the progress
    /// pcode reports to its terminal (OSC 9;4): a turn running, a failed one
    /// not yet seen, or idle.
    enum Activity { case idle, running, failed }
    @Published private(set) var activity: Activity = .idle
    /// Called when the pane's activity state transitions, so collapsed folder headers refresh.
    var onActivityChange: (() -> Void)?
    /// Called when a turn ends (running to idle or failed), so the workspace
    /// can mark its response unseen if nobody is looking at it.
    var onTurnFinished: (() -> Void)?
    /// A finished turn's response that hasn't been looked at here yet. Kept
    /// by session ID in the workspace, so it survives restarts.
    @Published var unseen = false
    /// When this pane was opened; used by `reconcilePanes` for its startup grace period.
    let createdAt: Date = Date()
    /// Fallback sort key before a saved session's creation time is loaded; refreshed on `/new`.
    private(set) var sessionStartedAt: Date = Date()
    /// When the most recent turn finished in this pane during this app session.
    @Published private(set) var lastResponseDate: Date?
    /// Per-session right-hand Inspector state and live stores.
    let inspectorState: PCodeInspectorState
    let changesStore: PCodeSessionChangesStore
    let agentsStore: PCodeInspectorAgentsStore
    let commandsStore: PCodeInspectorCommandsStore
    let treeStore: PCodeConversationTreeStore
    /// Wired by `PCodeLiveTranscriptPane` so the Tree tab can scroll the active transcript to a turn.
    var onScrollToTurn: ((String) -> Void)?

    @MainActor
    func applyInspectorEvent(_ event: PCodeHostEvent) {
        if case .sessionMetadata(let model, let effort, let context) = event {
            if modelName != model { modelName = model }
            if modelEffort != effort { modelEffort = effort }
            if contextUsage != context { contextUsage = context }
            return
        }
        changesStore.apply(event)
        agentsStore.apply(event)
        commandsStore.apply(event)
        treeStore.apply(event)
    }

    convenience init(surface: Ghostty.SurfaceView, directory: String, projectPath: String? = nil, sessionID: String?) {
        self.init(surface: surface, composer: nil, directory: directory, projectPath: projectPath, sessionID: sessionID)
    }

    convenience init(composer: PCodeComposerSession, directory: String, projectPath: String? = nil, sessionID: String?) {
        self.init(surface: nil, composer: composer, directory: directory, projectPath: projectPath, sessionID: sessionID)
    }

    private init(
        surface: Ghostty.SurfaceView?,
        composer: PCodeComposerSession?,
        directory: String,
        projectPath: String?,
        sessionID: String?
    ) {
        self.surface = surface
        self.composer = composer
        surface?.pcodeIsSessionPane = true
        self.directory = directory
        self.projectPath = PCodeWorkspace.normalizePath(projectPath ?? directory)
        self.resuming = sessionID != nil
        self.sessionID = sessionID
        let inspectorBundle = MainActor.assumeIsolated {
            (
                PCodeInspectorState(),
                PCodeSessionChangesStore(sessionDirectory: directory),
                PCodeInspectorAgentsStore(),
                PCodeInspectorCommandsStore(),
                PCodeConversationTreeStore(sessionID: sessionID)
            )
        }
        self.inspectorState = inspectorBundle.0
        self.changesStore = inspectorBundle.1
        self.agentsStore = inspectorBundle.2
        self.commandsStore = inspectorBundle.3
        self.treeStore = inspectorBundle.4
        hostSubscription = $sessionID
            .combineLatest(PCodeHostWatcher.shared.$statuses)
            .map { sessionID, statuses in sessionID.flatMap { statuses[$0] } }
            .removeDuplicates()
            .receive(on: DispatchQueue.main)
            .sink { [weak self] status in
                guard let self else { return }
                self.hostStatus = status
                if let hostID = status?.hostID { self.lastHostID = hostID }
                self.resolver.setHost(status)
                self.resolve()
            }
        if let composer {
            observeComposer(composer)
        } else if let surface {
            observeSurface(surface)
        }
    }

    /// A native pane: title and activity come from the composer's host state, and it is
    /// ready at once, since its composer takes typing from the first frame.
    private func observeComposer(_ composer: PCodeComposerSession) {
        isReady = true
        titleSubscription = composer.$state
            .map(\.sessionTitle)
            .removeDuplicates()
            .sink { [weak self] title in
                guard let self else { return }
                self.resolver.oscTitle = title
                self.resolve()
            }
        progressSubscription = composer.$state
            .map { state -> Activity in
                if state.busy || state.promptState == "running" { return .running }
                return state.turnOutcome == "failed" ? .failed : .idle
            }
            .removeDuplicates()
            .sink { [weak self] activity in
                guard let self else { return }
                self.resolver.setOSCActivity(activity)
                self.resolve()
            }
    }

    private func observeSurface(_ surface: Ghostty.SurfaceView) {
        titleSubscription = surface.$title.removeDuplicates()
            .receive(on: DispatchQueue.main)
            .sink { [weak self] title in
                guard let self else { return }
                self.resolver.oscTitle = title
                self.resolve()
            }
        progressSubscription = surface.$progressReport
            .map { report -> Activity in
                switch report?.state {
                case .set?, .indeterminate?, .pause?: return .running
                case .error?: return .failed
                default: return .idle
                }
            }
            .removeDuplicates()
            .receive(on: DispatchQueue.main)
            .sink { [weak self] activity in
                guard let self else { return }
                self.resolver.setOSCActivity(activity)
                self.resolve()
            }
        keySubscription = NotificationCenter.default.publisher(for: Self.surfaceKeyPressed, object: surface)
            .receive(on: DispatchQueue.main)
            .sink { [weak self] _ in
                guard let self else { return }
                self.resolver.dismissFailure()
                self.resolve()
            }
        dockReadySubscription = surface.pcodeDock.$ready
            .filter { $0 }
            .first()
            .receive(on: DispatchQueue.main)
            .sink { [weak self] _ in self?.markReady(via: "dock") }
        // pcode asks the user something before its editor starts: show it now.
        inputSubscription = surface.pcodeDock.$needsInput
            .filter { $0 }
            .first()
            .receive(on: DispatchQueue.main)
            .sink { [weak self] _ in self?.markReady(via: "input") }

        // Safety escape hatch: if pcode errors out or never enables the dock,
        // drop the boot overlay so the terminal output is always visible.
        let safety = DispatchWorkItem { [weak self] in self?.markReady(via: "safety timeout") }
        readySafetyWork = safety
        // A resumed pane shows its transcript meanwhile, and pcode reports a trust
        // prompt or held startup error itself (`startup-input`), so it waits longer
        // before giving up on a slow start and showing the bare terminal.
        let patience: TimeInterval = surface.pcodeDock.bootCovered ? 15 : 4.5
        DispatchQueue.main.asyncAfter(deadline: .now() + patience, execute: safety)
    }

    deinit {
        readyFallbackWork?.cancel()
        readySafetyWork?.cancel()
    }

    /// Publish the title and activity the resolver settles on, and announce a finished turn.
    private func resolve() {
        let title = resolver.title
        if title != self.title { self.title = title }
        let activity = resolver.activity
        guard activity != self.activity else { return }
        let finished = self.activity == .running
        self.activity = activity
        if finished {
            self.lastResponseDate = Date()
            onTurnFinished?()
        }
        onActivityChange?()
    }

    /// Dismiss the startup overlay once pcode is live.
    func markReady(via reason: String = "direct") {
        guard !isReady else { return }
        PCodeStartupTrace.paneReady(via: reason)
        readyFallbackWork?.cancel()
        readyFallbackWork = nil
        readySafetyWork?.cancel()
        readySafetyWork = nil
        isReady = true
        if let surface, surface.pcodeDock.bootCovered { surface.pcodeDock.bootCovered = false }
    }

    /// Called when pcode reports its session or host ID: if `pcode;ready` hasn't
    /// arrived shortly after, dismiss the boot overlay anyway.
    func noteSessionReported() {
        // A resumed pane already shows its transcript under a compact boot card:
        // dropping that early would swap in the full-pane terminal for a moment
        // before the dock card. It waits for the dock (or the safety timeout).
        guard !isReady, readyFallbackWork == nil, let surface, !surface.pcodeDock.bootCovered else { return }
        let work = DispatchWorkItem { [weak self] in
            self?.markReady(via: "session fallback")
        }
        readyFallbackWork = work
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.8, execute: work)
    }

    /// Every terminal this pane shows: pcode's editor and the optional bottom shell.
    var surfaces: [Ghostty.SurfaceView] {
        [surface, shell].compactMap { $0 }
    }

    /// The surface keys should go to: pcode's editor terminal (none in a native pane).
    var keyboardSurface: Ghostty.SurfaceView? {
        surface
    }

    /// Give the keyboard to whatever takes this pane's typing: the editor terminal or the
    /// native composer.
    func focusInput() {
        if let surface {
            Ghostty.moveFocus(to: surface)
        } else {
            composer?.focus()
        }
    }

    /// Whether `view` is (inside) one of the views that take this pane's typing.
    func ownsInput(_ view: NSView) -> Bool {
        if surfaces.contains(where: { view === $0 || view.isDescendant(of: $0) }) { return true }
        guard let text = composer?.textView else { return false }
        return view === text || view.isDescendant(of: text)
    }

    /// Worktree directory name when this pane runs in a worktree under `projectPath`.
    var worktreeName: String? {
        let dir = PCodeWorkspace.normalizePath(directory)
        return dir != projectPath ? (dir as NSString).lastPathComponent : nil
    }

    func clearLastResponseDate() { lastResponseDate = nil }
    func resetSessionStartedAt() { sessionStartedAt = Date() }

    /// The saved label to fall back on while the terminal has no title yet.
    func savedLabel(saved: PCodeSessionInfo?) -> String? {
        saved?.label ?? openedLabel
    }

    /// Timestamp of the last completed response for this pane (nil for brand-new 0-turn panes).
    func lastResponseDate(saved: PCodeSessionInfo?) -> Date? {
        let savedDate = (saved?.turns ?? 0) > 0 ? saved?.updatedDate : nil
        // No session yet means no assistant turn has run (e.g. a `!command` at a fresh prompt).
        let live = sessionID == nil ? nil : lastResponseDate
        switch (live, savedDate) {
        case (let live?, let disk?): return max(live, disk)
        case (let live?, nil): return live
        case (nil, let disk?): return disk
        case (nil, nil): return nil
        }
    }

    /// Sort key for ordering open panes by session creation time (falls back to `sessionStartedAt` for brand-new panes).
    static func sortDate(saved: PCodeSessionInfo?, createdAt: Date) -> Date {
        saved?.createdDate ?? createdAt
    }

    func sortDate(saved: PCodeSessionInfo?) -> Date {
        Self.sortDate(saved: saved, createdAt: sessionStartedAt)
    }

    func setShell(_ view: Ghostty.SurfaceView?) { shell = view }
}

/// A detached session currently held by a live `pcode.host` process.
struct PCodeHostedSession: Identifiable, Equatable {
    let info: PCodeSessionInfo
    let host: PCodeHostStatus

    var id: String { info.id }

    var title: String {
        PCodeWorkspace.displayTitle(paneTitle: host.title, savedLabel: info.label)
    }

    var updatedDate: Date? {
        host.updated > 0 ? Date(timeIntervalSince1970: host.updated) : info.updatedDate
    }

    var activity: PCodeSessionPane.Activity {
        if host.isWorking { return .running }
        if host.outcome == "failed" { return .failed }
        return .idle
    }
}

/// One folder group in the sidebar: starred sessions float to the top, followed by open panes, detached hosts, and recent saved sessions.
struct PCodeProjectGroup: Identifiable {
    let path: String
    let openPanes: [PCodeSessionPane]
    let hostedSessions: [PCodeHostedSession]
    let recentSessions: [PCodeSessionInfo]
    /// Recent saved sessions in the folder beyond `recentSessions` ("Show more…").
    let remainingRecentCount: Int
    let starredSessionIDs: Set<String>

    init(
        path: String,
        openPanes: [PCodeSessionPane],
        hostedSessions: [PCodeHostedSession] = [],
        recentSessions: [PCodeSessionInfo],
        remainingRecentCount: Int = 0,
        starredSessionIDs: Set<String> = []
    ) {
        self.path = path
        self.openPanes = openPanes
        self.hostedSessions = hostedSessions
        self.recentSessions = recentSessions
        self.remainingRecentCount = remainingRecentCount
        self.starredSessionIDs = starredSessionIDs
    }

    /// Every recent saved session in the folder, shown or not.
    var recentCount: Int { recentSessions.count + remainingRecentCount }

    var id: String { path }

    var name: String {
        if path == PCodeWorkspace.normalizePath(NSHomeDirectory()) { return "~" }
        let last = (path as NSString).lastPathComponent
        return last.isEmpty ? path : last
    }

    var abbreviatedPath: String { PCodeWorkspace.abbreviatePath(path) }

    var starredOpenPanes: [PCodeSessionPane] {
        openPanes.filter { pane in pane.sessionID.map(starredSessionIDs.contains) == true }
    }

    var unstarredOpenPanes: [PCodeSessionPane] {
        openPanes.filter { pane in pane.sessionID.map(starredSessionIDs.contains) != true }
    }

    var starredHostedSessions: [PCodeHostedSession] {
        hostedSessions.filter { starredSessionIDs.contains($0.id) }
    }

    var unstarredHostedSessions: [PCodeHostedSession] {
        hostedSessions.filter { !starredSessionIDs.contains($0.id) }
    }

    var starredRecentSessions: [PCodeSessionInfo] {
        recentSessions.filter { starredSessionIDs.contains($0.id) }
    }

    var unstarredRecentSessions: [PCodeSessionInfo] {
        recentSessions.filter { !starredSessionIDs.contains($0.id) }
    }

    var hasRunning: Bool {
        openPanes.contains(where: { $0.activity == .running })
            || hostedSessions.contains(where: { $0.activity == .running })
    }

    var hasFailed: Bool {
        openPanes.contains(where: { $0.activity == .failed })
            || hostedSessions.contains(where: { $0.activity == .failed })
    }

    /// Aggregate activity across open panes and detached live hosts.
    var activity: PCodeSessionPane.Activity? {
        if hasRunning { return .running }
        if hasFailed { return .failed }
        if !openPanes.isEmpty || !hostedSessions.isEmpty { return .idle }
        return nil
    }

    /// Whether any session in the folder, open, hosted, or saved, has an unseen response.
    func hasUnseen(_ unseenIDs: Set<String>) -> Bool {
        openPanes.contains(where: \.unseen)
            || hostedSessions.contains { $0.host.unseen || unseenIDs.contains($0.id) }
            || recentSessions.contains { unseenIDs.contains($0.id) }
    }
}

/// One candidate project/directory in the unified Command Center / launcher.
struct PCodeDirectoryCandidate: Identifiable, Hashable {
    let path: String
    let name: String
    let abbreviatedPath: String
    let openCount: Int
    let recentCount: Int
    let isCurrent: Bool
    let isPinned: Bool
    let isRawPath: Bool

    var id: String { isRawPath ? "raw:\(path)" : path }
}

final class PCodeWorkspace: ObservableObject {
    @Published private(set) var panes: [PCodeSessionPane] = [] {
        didSet {
            syncDetachedHostStatuses()
            publishHistory()
        }
    }
    @Published var selected: UUID?
    /// What the sidebar shows of `allHistory`, newest first: every starred,
    /// unseen, open, and hosted session, plus each project's newest `recentLimit`
    /// others (`remainingRecentCounts` counts the rest).
    @Published private(set) var history: [PCodeSessionInfo] = []
    /// Per project, how many recent sessions `history` leaves out.
    @Published private(set) var remainingRecentCounts: [String: Int] = [:]
    /// Every saved session with a turn (or a live host), newest first. Not
    /// published: the sidebar redraws from its slice, `history`.
    private(set) var allHistory: [PCodeSessionInfo] = [] {
        didSet {
            historyByID = Dictionary(allHistory.map { ($0.id, $0) }, uniquingKeysWith: { a, _ in a })
        }
    }
    /// Sessions a live host held at the last history load.
    private var hostedAtLastLoad: Set<String> = []
    /// Live background hosts not currently open in a pane, published only when the detached subset changes.
    @Published private(set) var detachedHostStatuses: [String: PCodeHostStatus] = [:] {
        didSet {
            if Set(detachedHostStatuses.keys) != Set(oldValue.keys) { publishHistory() }
        }
    }
    private var allHostStatuses: [String: PCodeHostStatus] = [:]
    private var historyByID: [String: PCodeSessionInfo] = [:]
    private var hostWatcherSubscription: AnyCancellable?

    let ghostty: Ghostty.App
    let peek = PCodePeekController()
    let usageStore = PCodeUsageStore()
    /// Whether the workspace window is on screen; set by its controller.
    var windowVisible: () -> Bool = { false }
    /// The same, for SwiftUI views (`pcodeWindowOnScreen`); kept current by `syncOcclusion`.
    let windowVisibility = PCodeWindowVisibility()
    /// The workspace window, for confirmation sheets; set by its controller.
    weak var window: NSWindow?
    private var observers: [NSObjectProtocol] = []
    private var typographySizeSubscription: AnyCancellable?
    private var typographyCodeFamilySubscription: AnyCancellable?
    private var lastTypographySize: Double = PCodeTypography.shared.size
    private static let openKey = "PCodeWorkspaceOpenSessions"
    private static let selectedKey = "PCodeWorkspaceSelectedSession"
    private static let unseenKey = "PCodeWorkspaceUnseenSessions"

    /// Sessions whose latest response hasn't been looked at in this app.
    @Published private(set) var unseenIDs: Set<String> = Set(
        UserDefaults.standard.stringArray(forKey: PCodeWorkspace.unseenKey) ?? []
    ) {
        didSet {
            if unseenIDs != oldValue {
                UserDefaults.standard.set(Array(unseenIDs), forKey: Self.unseenKey)
                publishHistory()
            }
        }
    }

    init(ghostty: Ghostty.App) {
        self.ghostty = ghostty
        PCodeStartupTrace.mark("workspace init")
        PCodeWorkspaceMode.warmLoginShellCache()
        typographySizeSubscription = PCodeTypography.shared.$size
            .map(PCodeTypography.clampSize)
            .removeDuplicates()
            .dropFirst()
            .sink { [weak self] newSize in
                guard let self else { return }
                let apply = {
                    let delta = Int((newSize - self.lastTypographySize).rounded())
                    self.lastTypographySize = newSize
                    guard delta != 0 else { return }
                    self.applyFontSizeDelta(delta, reset: newSize == PCodeTypography.defaultSize)
                }
                if Thread.isMainThread {
                    apply()
                } else {
                    DispatchQueue.main.async(execute: apply)
                }
            }
        typographyCodeFamilySubscription = PCodeTypography.shared.$codeFamily
            .map(PCodeTypographySnapshot.normalizedFamily)
            .removeDuplicates()
            .dropFirst()
            .debounce(for: .zero, scheduler: DispatchQueue.main)
            .sink { [weak self] _ in
                self?.ghostty.reloadConfig()
            }
        peek.onTargetChange = { [weak self] in
            guard let self else { return }
            self.syncOcclusion()
            if !self.peek.isShowing {
                self.scheduleSidebarHideIfPointerOutside()
            }
        }
        peek.windowProvider = { [weak self] in self?.window }
        peek.onRestoreFocus = { [weak self] in
            guard let self, !self.showingDirectoryPicker,
                  self.window?.isKeyWindow == true,
                  let pane = self.selectedPane
            else { return }
            DispatchQueue.main.async { pane.focusInput() }
        }
        let center = NotificationCenter.default
        observers.append(center.addObserver(
            forName: NSMenu.didBeginTrackingNotification, object: nil, queue: .main
        ) { [weak self] _ in
            guard let self else { return }
            self.menuTrackingDepth += 1
            self.cancelSidebarHide()
        })
        observers.append(center.addObserver(
            forName: NSMenu.didEndTrackingNotification, object: nil, queue: .main
        ) { [weak self] _ in
            guard let self else { return }
            self.menuTrackingDepth = max(0, self.menuTrackingDepth - 1)
            self.scheduleSidebarHideIfPointerOutside()
        })
        observers.append(center.addObserver(
            forName: NSWindow.didEndSheetNotification, object: nil, queue: .main
        ) { [weak self] note in
            guard let self, let sheetWindow = note.object as? NSWindow,
                  sheetWindow === self.window else { return }
            self.scheduleSidebarHideIfPointerOutside()
        })
        observers.append(center.addObserver(
            forName: Ghostty.Notification.ghosttyCloseSurface, object: nil, queue: .main
        ) { [weak self] note in
            guard let view = note.object as? Ghostty.SurfaceView else { return }
            let processAlive = note.userInfo?["process_alive"] as? Bool ?? false
            // Posted from inside the surface's own close callback, which is
            // still using it: free it (or ask about it) on the next turn.
            DispatchQueue.main.async { [weak self, weak view] in
                guard let self, let view else { return }
                self.surfaceClosed(view, processAlive: processAlive)
            }
        })
        observers.append(center.addObserver(
            forName: PCodeWorkspace.sessionChanged, object: nil, queue: .main
        ) { [weak self] note in
            guard let self, let view = note.object as? Ghostty.SurfaceView,
                  let pane = self.panes.first(where: { $0.surface === view }) else { return }
            self.sessionReported(pane, sessionID: view.pcodeSession, hostID: view.pcodeHost)
        })
        hostWatcherSubscription = PCodeHostWatcher.shared.$statuses
            .receive(on: DispatchQueue.main)
            .sink { [weak self] statuses in
                self?.handleHostStatusesUpdate(statuses)
            }
        reconcileTimer = Timer.scheduledTimer(
            withTimeInterval: Self.reconcileInterval, repeats: true
        ) { [weak self] _ in
            // Scheduled on the main run loop.
            MainActor.assumeIsolated { self?.reconcilePanes() }
        }
        // A backstop, not a deadline: let the system batch its wakeups.
        reconcileTimer?.tolerance = 1
    }

    /// pcode reported the session and host a pane holds: from its terminal
    /// (`pcode;session;...`), or from a native composer's host.
    func sessionReported(_ pane: PCodeSessionPane, sessionID: String?, hostID: String?) {
        let old = pane.sessionID
        pane.sessionID = sessionID
        // `/new` or `/resume` swapped sessions: the previous one's response time and label no longer apply.
        if let old, old != sessionID {
            pane.clearLastResponseDate()
            pane.openedLabel = nil
            if sessionID == nil { pane.resetSessionStartedAt() }
        }
        pane.hostID = hostID
        if let hostID { pane.lastHostID = hostID }
        pane.noteSessionReported()
        sessionIDChanged(pane, from: old)
        syncDetachedHostStatuses()
        saveOpen()
        if pane.id == selected { saveSelected() }
        scheduleHistoryReload()
    }

    private var reconcileTimer: Timer?
    private static let reconcileInterval: TimeInterval = 5
    /// How often the reconcile tick rereads saved sessions. History otherwise
    /// reloads only on this window's own events, so sessions updated elsewhere
    /// (a terminal, another window, a worker) went missing or kept a stale age.
    private static let historyReconcileInterval: TimeInterval = 30
    private var lastHistoryReload = Date.distantPast
    /// How long a new pane's pcode gets to start before its terminal is judged.
    private static let reconcileGrace: TimeInterval = 10

    /// Close panes whose pcode has exited when their close never arrived.
    ///
    /// A pane normally closes on Ghostty's close-surface notification, and the
    /// sidebar lists every pane as open. When that notification is lost, the
    /// pane outlives its process: an empty transcript, listed as open forever
    /// and reopened on the next launch. This backstop checks each terminal
    /// directly rather than trusting the event.
    @MainActor
    func reconcilePanes(now: Date = Date()) {
        let dead = panes.filter { pane in
            // A native pane has no terminal process to judge; its composer watches the host.
            guard let surface = pane.surface else { return false }
            return now.timeIntervalSince(pane.createdAt) > Self.reconcileGrace && Self.processGone(surface)
        }
        dead.forEach(close)
        // Nobody sees the sidebar while the window is hidden; `syncOcclusion`
        // reloads when it shows again.
        if windowVisibility.onScreen, now.timeIntervalSince(lastHistoryReload) >= Self.historyReconcileInterval {
            reloadHistory()
        }
    }

    /// Whether `view`'s command has exited: Ghostty saw it exit, or no process
    /// has its terminal as controlling terminal any more.
    @MainActor
    static func processGone(
        _ view: Ghostty.SurfaceView,
        ttyInUse: (String) -> Bool? = PCodeWorkspace.ttyInUse
    ) -> Bool {
        guard view.surface != nil else { return false }  // Already freed: closing.
        if view.processExited { return true }
        guard let tty = view.surfaceModel?.ttyName, !tty.isEmpty else { return false }
        return ttyInUse(tty) == false  // nil (could not tell): leave it to the notification.
    }

    /// Whether any process has the terminal at `path` as its controlling
    /// terminal; nil when that can't be read.
    static func ttyInUse(_ path: String) -> Bool? {
        let device = path.hasPrefix("/") ? path : "/dev/\(path)"
        var info = Darwin.stat()
        guard Darwin.lstat(device, &info) == 0, (info.st_mode & S_IFMT) == S_IFCHR else { return nil }
        var mib: [Int32] = [CTL_KERN, KERN_PROC, KERN_PROC_TTY, Int32(info.st_rdev)]
        var size = 0
        // Asked with no buffer, the kernel answers with room to spare.
        guard sysctl(&mib, u_int(mib.count), nil, &size, nil, 0) == 0 else { return nil }
        var buffer = [UInt8](repeating: 0, count: size)
        guard sysctl(&mib, u_int(mib.count), &buffer, &size, nil, 0) == 0 else { return nil }
        return size >= MemoryLayout<kinfo_proc>.stride
    }

    private func syncDetachedHostStatuses() {
        let openIDs = Set(panes.compactMap(\.sessionID))
        let detached = allHostStatuses.filter { !openIDs.contains($0.key) }
        if detached != detachedHostStatuses {
            detachedHostStatuses = detached
        }
    }

    private func handleHostStatusesUpdate(_ statuses: [String: PCodeHostStatus]) {
        let previous = allHostStatuses
        allHostStatuses = statuses
        let openIDs = Set(panes.compactMap(\.sessionID))
        let prevDetached = previous.filter { !openIDs.contains($0.key) }
        let newDetached = statuses.filter { !openIDs.contains($0.key) }

        if newDetached != detachedHostStatuses {
            detachedHostStatuses = newDetached
        }

        // Persist unseen responses from detached hosts so the badge survives host idle-stop
        // and non-selected pane opens (`reopen()`).
        for (id, host) in newDetached where host.unseen && !unseenIDs.contains(id) {
            unseenIDs.insert(id)
        }

        // Reload history when a newly-seen detached host isn't in history yet, a detached host
        // disappears (cleaning up zero-turn stopped hosts / refreshing finished metadata), or a
        // detached host finishes a turn.
        let appearedUnknown = newDetached.keys.contains { previous[$0] == nil && historyByID[$0] == nil }
        let disappeared = prevDetached.keys.contains { newDetached[$0] == nil && !openIDs.contains($0) }
        let turnFinished = newDetached.contains { id, host in
            guard let prev = prevDetached[id] else { return false }
            return host.turns != prev.turns
        }
        if appearedUnknown || disappeared || turnFinished {
            scheduleHistoryReload()
        }
    }

    deinit {
        reconcileTimer?.invalidate()
        stopSidebarMonitoring()
        observers.forEach(NotificationCenter.default.removeObserver)
    }

    private var appearanceObserver: NSKeyValueObservation?

    /// Follow macOS light/dark: each surface needs the scheme set on it, as
    /// Ghostty's own windows do for their surface trees, for a
    /// `theme = light:…,dark:…` config to switch it (and for pcode in it to
    /// hear the change, as a mode 2031 report).
    func followAppearance() {
        appearanceObserver = NSApplication.shared.observe(\.effectiveAppearance, options: [.new]) { [weak self] _, _ in
            DispatchQueue.main.async { self?.syncColorScheme() }
        }
    }

    func syncColorScheme() {
        for pane in panes { pane.surfaces.forEach(PCode.applyColorScheme) }
    }

    /// Posted by a surface whose `pcodeSession` changed.
    static let sessionChanged = Notification.Name("com.pcode.workspace.sessionChanged")

    var selectedPane: PCodeSessionPane? { panes.first { $0.id == selected } }

    /// Reopen the sessions open at last quit, and every session a background
    /// host holds; if none reopen, stays on the inline project launcher.
    func restore() {
        // Not into a window that is quitting to restart (a Dock click while
        // its panes close): the relaunched app reopens them. Nor twice.
        guard !restarting, !restoring else { return }
        restoring = true
        hasRestored = true
        PCodeStartupTrace.mark("restore start")
        let saved = UserDefaults.standard.stringArray(forKey: Self.openKey) ?? []
        let marked = Self.restartMarker()
        let selectedID = UserDefaults.standard.string(forKey: Self.selectedKey)
        // Two things run at once: the history load (which fills the sidebar
        // as soon as it lands) and, off the main thread, the waits below.
        // The panes reopen when both are done.
        let steps = DispatchGroup()
        var loaded: [PCodeSessionInfo] = []
        steps.enter()
        reloadHistory { all in
            loaded = all
            steps.leave()
        }
        var ready: Set<String> = []
        var hosted: Set<String> = []
        var openElsewhere: Set<String> = []
        var liveHosts: [(id: String, pid: Int32, sessionID: String)] = []
        let savedHosts = Self.savedHostPanes()
        steps.enter()
        DispatchQueue.global(qos: .userInitiated).async {
            // Sessions a Restart Pcode just moved may still be on their way
            // into their hosts.
            ready = PCodeSessionInfo.awaitRestarted(marked)
            PCodeStartupTrace.mark("restarted sessions awaited (\(marked.count))")
            // The panes launch next: without the login shell's env each runs
            // its own login shell to start pcode. Bounded; a slow shell
            // config falls back to that as before.
            PCodeWorkspaceMode.awaitLoginShellCache(timeout: 2)
            PCodeStartupTrace.mark("login-shell env awaited")
            // What `reopen` checks, read here rather than on the main thread.
            hosted = PCodeSessionInfo.hosted()
            openElsewhere = Set(saved.filter { !hosted.contains($0) && PCodeSessionInfo.isOpen($0) })
            if !savedHosts.isEmpty { liveHosts = PCodeSessionInfo.liveHostEntries() }
            steps.leave()
        }
        steps.notify(queue: .main) { [weak self] in
            guard let self else { return }
            self.restoring = false
            Self.clearRestartMarker()
            let reopen: ([PCodeSessionInfo]) -> Void = { [weak self] all in
                PCodeStartupTrace.mark("history loaded (\(all.count) sessions)")
                // Even when a session was opened meanwhile (restore waits on the
                // login shell): `open` skips one already here, and `reopen`
                // leaves the user's choice selected.
                guard let self else { return }
                self.reopen(
                    saved: saved, marked: Set(marked), ready: ready, all: all, selectedID: selectedID,
                    hosted: hosted, openElsewhere: openElsewhere)
                self.reopenHostPanes(savedHosts, live: liveHosts)
                PCodeStartupTrace.mark("reopen done (\(self.panes.count) panes)")
            }
            // Restarted sessions' hosts registered during the wait: reload
            // to list them. Otherwise the load above is current enough.
            if marked.isEmpty {
                reopen(loaded)
            } else {
                self.reloadHistory(then: reopen)
            }
        }
    }

    /// Native-composer panes whose session had no id yet (no turn had run) are saved by
    /// their host instead, so a restart reattaches them rather than orphaning the host.
    private static let openHostsKey = "PCodeWorkspaceOpenHosts"

    struct SavedHostPane: Codable, Equatable {
        let hostID: String
        let directory: String
        let projectPath: String
    }

    static func savedHostPanes() -> [SavedHostPane] {
        guard let data = UserDefaults.standard.data(forKey: openHostsKey) else { return [] }
        return (try? JSONDecoder().decode([SavedHostPane].self, from: data)) ?? []
    }

    /// The saved host panes still worth reopening: native panes are on, their host is
    /// alive, and it holds no session that is open already.
    static func reopenableHostPanes(
        _ saved: [SavedHostPane], live: [(id: String, pid: Int32, sessionID: String)],
        openSessions: Set<String>, nativeEnabled: Bool
    ) -> [(pane: SavedHostPane, sessionID: String?)] {
        guard nativeEnabled else { return [] }
        return saved.compactMap { pane in
            guard PCode.valid(pane.hostID, length: 8...8, hex: true),
                  let entry = live.first(where: { $0.id == pane.hostID }) else { return nil }
            if !entry.sessionID.isEmpty, openSessions.contains(entry.sessionID) { return nil }
            return (pane, entry.sessionID.isEmpty ? nil : entry.sessionID)
        }
    }

    private func reopenHostPanes(_ saved: [SavedHostPane], live: [(id: String, pid: Int32, sessionID: String)]) {
        let reopenable = Self.reopenableHostPanes(
            saved, live: live, openSessions: Set(panes.compactMap(\.sessionID)),
            nativeEnabled: PCodeNativeComposer.enabled)
        for (savedPane, sessionID) in reopenable {
            let pane = makeNativePane(
                directory: savedPane.directory, projectPath: savedPane.projectPath,
                sessionID: sessionID, existingHostID: savedPane.hostID)
            adopt(pane)
            panes.append(pane)
        }
        if !reopenable.isEmpty {
            saveOpen()
            if selected == nil, let first = panes.first { select(first) }
        }
    }

    private var restoring = false
    /// True once initial startup restore has run (so later Dock clicks don't force-reopen closed hosts).
    private(set) var hasRestored = false

    private func reopen(
        saved: [String], marked: Set<String>, ready: Set<String>,
        all: [PCodeSessionInfo], selectedID: String?,
        hosted: Set<String>, openElsewhere: Set<String>
    ) {
        let infos = Dictionary(all.map { ($0.id, $0) }, uniquingKeysWith: { a, _ in a })
        let openedMeanwhile = !panes.isEmpty
        // Any session a host holds (`hosted`), even one a terminal is attached
        // to as well: `pcode -c` attaches to the host, and the window shows them all.
        let extra = all.map(\.id).filter { hosted.contains($0) && !saved.contains($0) }
        for id in saved + extra {
            guard let info = infos[id] else { continue }
            // Not one open in another process (a terminal, another window)
            // without a host: that would start a copy of it here instead.
            // A restarted one may have been seen ready before its host
            // registered.
            let openable = hosted.contains(id) || (marked.contains(id) ? ready.contains(id) : !openElsewhere.contains(id))
            if openable { open(info, select: false) }
        }
        if openedMeanwhile {
            return
        } else if !panes.isEmpty {
            select(panes.first { $0.sessionID != nil && $0.sessionID == selectedID } ?? panes[0])
        } else {
            // Nothing reopened: stay on the inline project launcher instead of spawning a blank session.
            launcherFocusTrigger += 1
        }
    }

    /// Reload the saved sessions off the main thread; only the newest load lands,
    /// and any callbacks queued by superseded loads still run once it completes.
    func reloadHistory(then done: (([PCodeSessionInfo]) -> Void)? = nil) {
        if let done { pendingHistoryCallbacks.append(done) }
        lastHistoryReload = Date()
        historyGeneration += 1
        let generation = historyGeneration
        let unseen = unseenIDs
        let starred = starredSessionIDs
        DispatchQueue.global(qos: .userInitiated).async {
            let hosted = PCodeSessionInfo.hosted()
            let all = PCodeSessionInfo.load(hosted: hosted)
            // Sessions deleted outside the app (no directory left at all).
            let gone = unseen.union(starred).filter {
                !FileManager.default.fileExists(atPath: PCodeSessionInfo.root.appendingPathComponent($0).path)
            }
            DispatchQueue.main.async { [weak self] in
                guard let self, generation == self.historyGeneration else { return }
                if all != self.allHistory { self.allHistory = all }
                self.hostedAtLastLoad = hosted
                if !gone.isEmpty {
                    self.unseenIDs.subtract(gone)
                    self.starredSessionIDs.subtract(gone)
                }
                self.publishHistory()
                PCodeStartupTrace.mark("sidebar history set", once: true)
                let callbacks = self.pendingHistoryCallbacks
                self.pendingHistoryCallbacks.removeAll()
                callbacks.forEach { $0(all) }
            }
        }
    }

    /// Reload history shortly, folding in every request until then: at launch
    /// each restored pane reports its session and host, one reload apiece.
    func scheduleHistoryReload() {
        guard historyReloadWork == nil else { return }
        let work = DispatchWorkItem { [weak self] in
            guard let self else { return }
            self.historyReloadWork = nil
            self.reloadHistory()
        }
        historyReloadWork = work
        DispatchQueue.main.asyncAfter(deadline: .now() + Self.historyReloadCoalesce, execute: work)
    }

    private var historyReloadWork: DispatchWorkItem?
    private static let historyReloadCoalesce: TimeInterval = 0.25

    private var historyGeneration = 0
    private var pendingHistoryCallbacks: [([PCodeSessionInfo]) -> Void] = []

    /// Recent sessions a folder shows before "Show more…".
    static let initialRecentLimit = 5
    /// How many more each "Show more…" click reveals.
    static let recentPageSize = 50

    /// Re-slice `allHistory` into `history` (in memory, no disk), publishing
    /// only what changed: the usual periodic reload doesn't redraw the sidebar.
    /// Unseen sessions always show, so their folder's dot has a row to point at.
    private func publishHistory() {
        // A hosted session with no turn is listed only because it is hosted;
        // others are open, detached (`detachedHostStatuses`), or plain recents.
        let hostedOnly = allHistory.lazy.filter { $0.turns == 0 && self.hostedAtLastLoad.contains($0.id) }.map(\.id)
        let keep = starredSessionIDs
            .union(unseenIDs)
            .union(panes.compactMap(\.sessionID))
            .union(detachedHostStatuses.keys)
            .union(hostedOnly)
        let slice = Self.sliceHistory(
            allHistory, keep: keep, recentLimits: recentLimits, defaultLimit: Self.initialRecentLimit)
        if slice.sessions != history { history = slice.sessions }
        if slice.remaining != remainingRecentCounts { remainingRecentCounts = slice.remaining }
    }

    /// Of `all` (newest first), every session in `keep` plus each project's
    /// newest `recentLimits[project]` (else `defaultLimit`) others, in order,
    /// and per project the exact count of the others left out.
    static func sliceHistory(
        _ all: [PCodeSessionInfo], keep: Set<String>, recentLimits: [String: Int], defaultLimit: Int
    ) -> (sessions: [PCodeSessionInfo], remaining: [String: Int]) {
        var sessions: [PCodeSessionInfo] = []
        var shown: [String: Int] = [:]
        var remaining: [String: Int] = [:]
        for info in all {
            if keep.contains(info.id) {
                sessions.append(info)
                continue
            }
            let project = info.normalizedProjectPath
            let count = shown[project, default: 0]
            if count < recentLimits[project] ?? defaultLimit {
                shown[project] = count + 1
                sessions.append(info)
            } else {
                remaining[project, default: 0] += 1
            }
        }
        return (sessions, remaining)
    }

    /// "Show more…": reveal the next page of a folder's recent sessions.
    func showMoreRecent(_ path: String) {
        recentLimits[path] = (recentLimits[path] ?? Self.initialRecentLimit) + Self.recentPageSize
    }

    /// Folders explicitly added to the sidebar (may have no sessions yet).
    @Published var pinnedFolders: [String] = (UserDefaults.standard.stringArray(forKey: PCodeWorkspace.pinnedKey) ?? [])
        .map(PCodeWorkspace.normalizePath) {
        didSet { UserDefaults.standard.set(pinnedFolders, forKey: Self.pinnedKey) }
    }
    /// Folders the user hid from the sidebar (never deletes sessions on disk).
    @Published var hiddenFolders: Set<String> = Set(
        (UserDefaults.standard.stringArray(forKey: PCodeWorkspace.hiddenKey) ?? []).map(PCodeWorkspace.normalizePath)
    ) {
        didSet { UserDefaults.standard.set(Array(hiddenFolders), forKey: Self.hiddenKey) }
    }
    /// Collapsed project folders in the sidebar.
    @Published var collapsedFolders: Set<String> = Set(
        (UserDefaults.standard.stringArray(forKey: PCodeWorkspace.collapsedKey) ?? []).map(PCodeWorkspace.normalizePath)
    ) {
        didSet { UserDefaults.standard.set(Array(collapsedFolders), forKey: Self.collapsedKey) }
    }
    /// Per folder, how many recent sessions to show once the user clicked
    /// "Show more…" (`initialRecentLimit` otherwise); collapsing resets it.
    @Published private(set) var recentLimits: [String: Int] = [:] {
        didSet {
            if recentLimits != oldValue { publishHistory() }
        }
    }
    /// Starred session IDs (always float to the top of their folder).
    @Published private(set) var starredSessionIDs: Set<String> = Set(
        UserDefaults.standard.stringArray(forKey: PCodeWorkspace.starredKey) ?? []
    ) {
        didSet {
            if starredSessionIDs != oldValue {
                UserDefaults.standard.set(Array(starredSessionIDs), forKey: Self.starredKey)
                publishHistory()
            }
        }
    }
    /// User-dragged heights for folder sections (nil = auto-fit).
    @Published private(set) var folderHeights: [String: CGFloat] = {
        guard let stored = UserDefaults.standard.dictionary(forKey: PCodeWorkspace.folderHeightsKey) as? [String: Double] else {
            return [:]
        }
        var result: [String: CGFloat] = [:]
        for (path, value) in stored where value > 0 {
            result[PCodeWorkspace.normalizePath(path)] = PCodeSidebarLayout.clampedFolderHeight(CGFloat(value))
        }
        return result
    }() {
        didSet {
            if folderHeights != oldValue {
                let raw = folderHeights.mapValues { Double($0) }
                UserDefaults.standard.set(raw, forKey: Self.folderHeightsKey)
            }
        }
    }
    /// Whether the ⌘⇧N floating Command Center overlay is open (used when a session pane is active).
    @Published var showingDirectoryPicker = false
    /// Incremented to focus the inline project launcher when no session pane is open.
    @Published var launcherFocusTrigger = 0

    /// Saved sidebar width in points (clamped at layout time to the current window width).
    @Published var sidebarWidth: CGFloat = {
        let stored = UserDefaults.standard.double(forKey: PCodeWorkspace.sidebarWidthKey)
        return stored > 0
            ? PCodeSidebarLayout.clampedWidth(CGFloat(stored), totalWidth: 0)
            : PCodeSidebarLayout.defaultWidth
    }() {
        didSet { UserDefaults.standard.set(Double(sidebarWidth), forKey: Self.sidebarWidthKey) }
    }

    /// When true, the sidebar hides off the left edge and slides the layout over on hover.
    @Published var sidebarAutoHide: Bool = UserDefaults.standard.bool(forKey: PCodeWorkspace.sidebarAutoHideKey) {
        didSet {
            UserDefaults.standard.set(sidebarAutoHide, forKey: Self.sidebarAutoHideKey)
            if !sidebarAutoHide {
                cancelSidebarReveal()
                cancelSidebarHide()
                sidebarRevealed = false
            }
            syncSidebarMouseMonitor()
        }
    }

    /// When true, open sessions in the sidebar show nested file changes from `This Session`.
    @Published var showSidebarFileChanges: Bool = {
        if let stored = UserDefaults.standard.object(forKey: PCodeWorkspace.showSidebarFileChangesKey) as? Bool {
            return stored
        }
        return true
    }() {
        didSet { UserDefaults.standard.set(showSidebarFileChanges, forKey: Self.showSidebarFileChangesKey) }
    }

    /// Whether the auto-hiding sidebar is currently slid open.
    @Published var sidebarRevealed = false

    /// True while the user is dragging the sidebar's right resize handle.
    var isResizingSidebar = false {
        didSet {
            if isResizingSidebar { cancelSidebarHide() }
        }
    }

    /// True while a Finder folder drag is hovering over the sidebar.
    var isSidebarDropTargeted = false {
        didSet {
            if isSidebarDropTargeted {
                cancelSidebarHide()
            } else {
                scheduleSidebarHideIfPointerOutside()
            }
        }
    }

    private var sidebarMonitoringActive = false
    private var sidebarMouseMonitor: Any?
    private var sidebarRevealWork: DispatchWorkItem?
    private var sidebarHideWork: DispatchWorkItem?
    private var menuTrackingDepth = 0
    private static let sidebarRevealDelay: TimeInterval = 0.05
    private static let sidebarHideDelay: TimeInterval = 0.22

    private static let pinnedKey = "PCodeWorkspacePinnedFolders"
    private static let hiddenKey = "PCodeWorkspaceHiddenFolders"
    private static let collapsedKey = "PCodeWorkspaceCollapsedFolders"
    private static let starredKey = "PCodeWorkspaceStarredSessions"
    private static let folderHeightsKey = "PCodeWorkspaceFolderHeights"
    private static let sidebarWidthKey = "PCodeWorkspaceSidebarWidth"
    private static let sidebarAutoHideKey = "PCodeWorkspaceSidebarAutoHide"
    private static let showSidebarFileChangesKey = "PCodeWorkspaceShowSidebarFileChanges"

    func isStarred(_ sessionID: String?) -> Bool {
        guard let sessionID, !sessionID.isEmpty else { return false }
        return starredSessionIDs.contains(sessionID)
    }

    func toggleStar(_ sessionID: String) {
        guard !sessionID.isEmpty else { return }
        // The row moves between ForEach blocks, so SwiftUI may never send it hover(false).
        peek.clearHover(sessionID: sessionID)
        if starredSessionIDs.contains(sessionID) {
            starredSessionIDs.remove(sessionID)
        } else {
            starredSessionIDs.insert(sessionID)
        }
    }

    func setFolderHeight(_ height: CGFloat, for path: String) {
        let normalized = Self.normalizePath(path)
        folderHeights[normalized] = PCodeSidebarLayout.clampedFolderHeight(height)
    }

    func resetFolderHeight(for path: String) {
        let normalized = Self.normalizePath(path)
        folderHeights.removeValue(forKey: normalized)
    }

    func effectiveSidebarWidth(in totalWidth: CGFloat) -> CGFloat {
        PCodeSidebarLayout.clampedWidth(sidebarWidth, totalWidth: totalWidth)
    }

    func resetSidebarWidth() {
        sidebarWidth = PCodeSidebarLayout.defaultWidth
    }

    func toggleSidebarAutoHide() {
        cancelSidebarReveal()
        cancelSidebarHide()
        isResizingSidebar = false
        if sidebarAutoHide {
            sidebarAutoHide = false
        } else {
            sidebarRevealed = false
            sidebarAutoHide = true
            restoreFocusAfterSidebarHide()
        }
    }

    func startSidebarMonitoring() {
        sidebarMonitoringActive = true
        syncSidebarMouseMonitor()
    }

    func stopSidebarMonitoring() {
        sidebarMonitoringActive = false
        cancelSidebarReveal()
        cancelSidebarHide()
        if let sidebarMouseMonitor {
            NSEvent.removeMonitor(sidebarMouseMonitor)
            self.sidebarMouseMonitor = nil
        }
    }

    private func syncSidebarMouseMonitor() {
        if sidebarMonitoringActive && sidebarAutoHide {
            guard sidebarMouseMonitor == nil else { return }
            sidebarMouseMonitor = NSEvent.addLocalMonitorForEvents(matching: [.mouseMoved, .leftMouseDragged]) { [weak self] event in
                self?.handleSidebarMouseMoved(event)
                return event
            }
        } else if let sidebarMouseMonitor {
            NSEvent.removeMonitor(sidebarMouseMonitor)
            self.sidebarMouseMonitor = nil
        }
    }

    private func handleSidebarMouseMoved(_ event: NSEvent) {
        guard sidebarAutoHide, let window, event.window === window,
              window.isKeyWindow, window.attachedSheet == nil,
              !showingDirectoryPicker
        else { return }
        let bounds = window.contentLayoutRect
        let point = event.locationInWindow
        if !sidebarRevealed {
            if event.type == .mouseMoved && NSEvent.pressedMouseButtons == 0
                && PCodeSidebarLayout.shouldReveal(at: point, in: bounds) {
                scheduleSidebarReveal()
            } else {
                cancelSidebarReveal()
            }
        } else {
            cancelSidebarReveal()
            let width = effectiveSidebarWidth(in: bounds.width)
            if PCodeSidebarLayout.isInsideRevealedZone(at: point, in: bounds, sidebarWidth: width) {
                cancelSidebarHide()
            } else {
                scheduleSidebarHide()
            }
        }
    }

    func sidebarHoverChanged(_ hovering: Bool) {
        guard sidebarAutoHide else { return }
        if hovering {
            cancelSidebarHide()
        } else {
            scheduleSidebarHideIfPointerOutside()
        }
    }

    private var canAutoHideSidebar: Bool {
        sidebarAutoHide && sidebarRevealed
            && !isResizingSidebar
            && !isSidebarDropTargeted
            && menuTrackingDepth == 0
            && !peek.isShowing
            && window?.attachedSheet == nil
    }

    private func isPointerInsideRevealedSidebar() -> Bool {
        guard let window else { return false }
        let bounds = window.contentLayoutRect
        let width = effectiveSidebarWidth(in: bounds.width)
        let point = window.mouseLocationOutsideOfEventStream
        return PCodeSidebarLayout.isInsideRevealedZone(at: point, in: bounds, sidebarWidth: width)
    }

    private func cancelSidebarReveal() {
        sidebarRevealWork?.cancel()
        sidebarRevealWork = nil
    }

    private func scheduleSidebarReveal() {
        guard sidebarRevealWork == nil else { return }
        let work = DispatchWorkItem { [weak self] in
            guard let self else { return }
            self.sidebarRevealWork = nil
            guard self.sidebarAutoHide, !self.sidebarRevealed,
                  let window = self.window, window.isKeyWindow,
                  window.attachedSheet == nil, !self.showingDirectoryPicker,
                  NSEvent.pressedMouseButtons == 0
            else { return }
            let point = window.mouseLocationOutsideOfEventStream
            if PCodeSidebarLayout.shouldReveal(at: point, in: window.contentLayoutRect) {
                self.cancelSidebarHide()
                self.sidebarRevealed = true
            }
        }
        sidebarRevealWork = work
        DispatchQueue.main.asyncAfter(deadline: .now() + Self.sidebarRevealDelay, execute: work)
    }

    private func cancelSidebarHide() {
        sidebarHideWork?.cancel()
        sidebarHideWork = nil
    }

    func scheduleSidebarHide() {
        guard canAutoHideSidebar else { return }
        cancelSidebarHide()
        let work = DispatchWorkItem { [weak self] in
            guard let self, self.canAutoHideSidebar, !self.isPointerInsideRevealedSidebar() else { return }
            self.hideRevealedSidebar()
        }
        sidebarHideWork = work
        DispatchQueue.main.asyncAfter(deadline: .now() + Self.sidebarHideDelay, execute: work)
    }

    func scheduleSidebarHideIfPointerOutside() {
        guard canAutoHideSidebar else { return }
        if !isPointerInsideRevealedSidebar() {
            scheduleSidebarHide()
        }
    }

    private func hideRevealedSidebar() {
        guard sidebarRevealed else { return }
        cancelSidebarHide()
        sidebarRevealed = false
        restoreFocusAfterSidebarHide()
    }

    private func restoreFocusAfterSidebarHide() {
        guard let window, window.isKeyWindow,
              window.attachedSheet == nil, !showingDirectoryPicker
        else { return }
        guard let pane = selectedPane else {
            launcherFocusTrigger += 1
            return
        }
        if let responder = window.firstResponder as? NSView, pane.ownsInput(responder) {
            return
        }
        DispatchQueue.main.async { pane.focusInput() }
    }

    func sidebarWindowDidResignKey() {
        cancelSidebarReveal()
        guard canAutoHideSidebar else { return }
        cancelSidebarHide()
        sidebarRevealed = false
    }

    func sidebarWindowWillClose() {
        cancelSidebarReveal()
        cancelSidebarHide()
        isResizingSidebar = false
        isSidebarDropTargeted = false
        sidebarRevealed = false
    }

    /// Canonical project path for an open pane (uses `session.json`'s `project` once loaded).
    func projectPath(for pane: PCodeSessionPane) -> String {
        info(for: pane)?.normalizedProjectPath ?? pane.projectPath
    }

    /// Pure grouping helper for sidebar folders: open panes first, then detached live hosts,
    /// pinned folders, and recent saved sessions (excluding hidden folders unless active or starred).
    /// Within each folder, starred sessions sort first, open panes sort by session creation time
    /// descending, and hosted/recent sessions sort by last active descending.
    static func buildProjectGroups(
        panes: [PCodeSessionPane],
        projectPathForPane: (PCodeSessionPane) -> String,
        history: [PCodeSessionInfo],
        detachedHostStatuses: [String: PCodeHostStatus],
        pinnedFolders: [String],
        hiddenFolders: Set<String>,
        starredSessionIDs: Set<String> = [],
        remainingRecentCounts: [String: Int] = [:],
        sortDateForPane: ((PCodeSessionPane) -> Date)? = nil
    ) -> [PCodeProjectGroup] {
        let openIDs = Set(panes.compactMap(\.sessionID))
        var order: [String] = []
        var seen: Set<String> = []
        func register(_ path: String) {
            if seen.insert(path).inserted { order.append(path) }
        }

        var openByProject: [String: [PCodeSessionPane]] = [:]
        for pane in panes {
            let project = projectPathForPane(pane)
            register(project)
            openByProject[project, default: []].append(pane)
        }
        var hostedByProject: [String: [PCodeHostedSession]] = [:]
        for info in history where !openIDs.contains(info.id) {
            guard let host = detachedHostStatuses[info.id] else { continue }
            let project = info.normalizedProjectPath
            register(project)
            hostedByProject[project, default: []].append(PCodeHostedSession(info: info, host: host))
        }
        for path in pinnedFolders where !hiddenFolders.contains(path) {
            register(path)
        }
        var recentByProject: [String: [PCodeSessionInfo]] = [:]
        for info in history where !openIDs.contains(info.id) && detachedHostStatuses[info.id] == nil {
            let project = info.normalizedProjectPath
            let hasStarred = starredSessionIDs.contains(info.id)
            guard hasStarred || !hiddenFolders.contains(project) || openByProject[project] != nil || hostedByProject[project] != nil else { continue }
            register(project)
            recentByProject[project, default: []].append(info)
        }
        return order.map { path in
            let sortedOpen = (openByProject[path] ?? []).sorted { lhs, rhs in
                let lhsStarred = lhs.sessionID.map(starredSessionIDs.contains) == true
                let rhsStarred = rhs.sessionID.map(starredSessionIDs.contains) == true
                if lhsStarred != rhsStarred { return lhsStarred }
                let lhsDate = sortDateForPane?(lhs) ?? lhs.createdAt
                let rhsDate = sortDateForPane?(rhs) ?? rhs.createdAt
                if lhsDate != rhsDate { return lhsDate > rhsDate }
                return lhs.id.uuidString < rhs.id.uuidString
            }
            let sortedHosted = (hostedByProject[path] ?? []).sorted { lhs, rhs in
                let lhsStarred = starredSessionIDs.contains(lhs.id)
                let rhsStarred = starredSessionIDs.contains(rhs.id)
                if lhsStarred != rhsStarred { return lhsStarred }
                let lhsDate = lhs.updatedDate ?? .distantPast
                let rhsDate = rhs.updatedDate ?? .distantPast
                if lhsDate != rhsDate { return lhsDate > rhsDate }
                return lhs.id < rhs.id
            }
            let sortedRecent = (recentByProject[path] ?? []).sorted { lhs, rhs in
                let lhsStarred = starredSessionIDs.contains(lhs.id)
                let rhsStarred = starredSessionIDs.contains(rhs.id)
                if lhsStarred != rhsStarred { return lhsStarred }
                let lhsDate = lhs.updatedDate ?? .distantPast
                let rhsDate = rhs.updatedDate ?? .distantPast
                if lhsDate != rhsDate { return lhsDate > rhsDate }
                return lhs.id < rhs.id
            }
            // A hidden folder shown only for a starred session lists no others.
            let showsUnstarred = !hiddenFolders.contains(path)
                || openByProject[path] != nil || hostedByProject[path] != nil
            return PCodeProjectGroup(
                path: path,
                openPanes: sortedOpen,
                hostedSessions: sortedHosted,
                recentSessions: sortedRecent,
                remainingRecentCount: showsUnstarred ? remainingRecentCounts[path] ?? 0 : 0,
                starredSessionIDs: starredSessionIDs)
        }
    }

    /// Sidebar groups: pinned folders, open-pane folders, detached-host folders, and recent-session folders (excluding hidden).
    var projectGroups: [PCodeProjectGroup] {
        Self.buildProjectGroups(
            panes: panes,
            projectPathForPane: { projectPath(for: $0) },
            history: history,
            detachedHostStatuses: detachedHostStatuses,
            pinnedFolders: pinnedFolders,
            hiddenFolders: hiddenFolders,
            starredSessionIDs: starredSessionIDs,
            remainingRecentCounts: remainingRecentCounts,
            sortDateForPane: { [self] pane in pane.sortDate(saved: info(for: pane)) })
    }

    /// Projects offered by the Command Center: current project first, then all known projects.
    var pickerCandidates: [PCodeDirectoryCandidate] {
        let currentProject = selectedPane.map { projectPath(for: $0) }
        let pinnedSet = Set(pinnedFolders).subtracting(hiddenFolders)
        let groups = projectGroups
        var seen: Set<String> = []
        var result: [PCodeDirectoryCandidate] = []
        // Saved sessions per project from the full index (the sidebar shows a slice).
        let openIDs = Set(panes.compactMap(\.sessionID))
        var recentCounts: [String: Int] = [:]
        var recentOrder: [String] = []
        for info in allHistory where !openIDs.contains(info.id) && detachedHostStatuses[info.id] == nil {
            let path = info.normalizedProjectPath
            if recentCounts[path] == nil { recentOrder.append(path) }
            recentCounts[path, default: 0] += 1
        }

        func append(_ group: PCodeProjectGroup, isCurrent: Bool) {
            guard seen.insert(group.path).inserted else { return }
            result.append(PCodeDirectoryCandidate(
                path: group.path,
                name: group.name,
                abbreviatedPath: group.abbreviatedPath,
                openCount: group.openPanes.count + group.hostedSessions.count,
                recentCount: recentCounts[group.path] ?? 0,
                isCurrent: isCurrent,
                isPinned: pinnedSet.contains(group.path),
                isRawPath: false))
        }

        if let currentProject, let currentGroup = groups.first(where: { $0.path == currentProject }) {
            append(currentGroup, isCurrent: true)
        }
        for group in groups {
            append(group, isCurrent: group.path == currentProject)
        }
        // Also offer projects hidden from the sidebar so ⌘⇧N can still find and reopen them.
        for path in recentOrder where seen.insert(path).inserted {
            let last = (path as NSString).lastPathComponent
            result.append(PCodeDirectoryCandidate(
                path: path,
                name: path == Self.normalizePath(NSHomeDirectory()) ? "~" : (last.isEmpty ? path : last),
                abbreviatedPath: Self.abbreviatePath(path),
                openCount: 0,
                recentCount: recentCounts[path] ?? 0,
                isCurrent: currentProject == path,
                isPinned: false,
                isRawPath: false))
        }
        let home = Self.normalizePath(NSHomeDirectory())
        if seen.insert(home).inserted {
            result.append(PCodeDirectoryCandidate(
                path: home,
                name: "~",
                abbreviatedPath: home,
                openCount: 0,
                recentCount: 0,
                isCurrent: currentProject == home,
                isPinned: pinnedSet.contains(home),
                isRawPath: false))
        }
        return result
    }

    func info(for pane: PCodeSessionPane) -> PCodeSessionInfo? {
        guard let id = pane.sessionID else { return nil }
        return historyByID[id]
    }

    /// ⌘N: new session in the current session's project, or open the picker if none is open.
    func newSession(directory: String? = nil) {
        let dir: String
        if let directory {
            dir = Self.normalizePath(directory)
        } else if let pane = selectedPane {
            dir = projectPath(for: pane)
        } else {
            openDirectoryPicker()
            return
        }
        hiddenFolders.remove(dir)
        collapsedFolders.remove(dir)
        showingDirectoryPicker = false
        guard let pane = makePane(args: ["--host"], directory: dir, projectPath: dir, sessionID: nil) else { return }
        adopt(pane)
        panes.insert(pane, at: 0)
        select(pane)
    }

    func openDirectoryPicker() {
        peek.dismiss(restoreFocus: false)
        if selectedPane == nil {
            launcherFocusTrigger += 1
            return
        }
        showingDirectoryPicker = true
    }

    func peekTarget(for info: PCodeSessionInfo) -> PCodePeekTarget {
        PCodePeekTarget(
            sessionID: info.id,
            title: info.label,
            subtitle: info.worktreeName ?? Self.relativeTime(info.updatedDate),
            directory: info.workspace,
            turnsHint: info.turns,
            updatedDate: info.updatedDate,
            isOpen: false,
            paneID: nil,
            info: info)
    }

    func peekTarget(for hosted: PCodeHostedSession) -> PCodePeekTarget {
        PCodePeekTarget(
            sessionID: hosted.id,
            title: hosted.title,
            subtitle: hosted.info.worktreeName ?? Self.relativeTime(hosted.updatedDate),
            directory: hosted.info.workspace,
            turnsHint: max(hosted.info.turns, hosted.host.turns),
            updatedDate: hosted.updatedDate,
            isOpen: false,
            paneID: nil,
            info: hosted.info)
    }

    /// Whether the workspace window is currently in native fullscreen (no traffic lights in the title bar).
    @Published var isFullScreen = false
    /// Height of the window's titlebar region in points (matches AppKit's traffic-light bar).
    @Published var titlebarHeight: CGFloat = 28
    /// Leading clearance needed in windowed mode to clear the traffic-light buttons.
    @Published var trafficLightClearance: CGFloat = 78

    /// Collapse runs of whitespace and newlines to single spaces, matching `terminal_notify.title`.
    private static func normalizeTitle(_ text: String) -> String {
        text.components(separatedBy: .whitespacesAndNewlines)
            .filter { !$0.isEmpty }
            .joined(separator: " ")
    }

    /// Resolve the best display title for an open pane, preferring live terminal
    /// titles (e.g. after `/rename` or auto-titling) while falling back to the
    /// saved `session.json` label when the terminal still has Ghostty's placeholder
    /// or an OSC 0 120-char truncation prefix of a longer saved label.
    static func displayTitle(paneTitle: String, savedLabel: String?) -> String {
        let normalizedLive = normalizeTitle(paneTitle)
        let isPlaceholder = normalizedLive.isEmpty || normalizedLive == "👻"
        let normalizedSaved = savedLabel.map(normalizeTitle)
        let validSaved = (normalizedSaved?.isEmpty == false) ? normalizedSaved : nil

        if !isPlaceholder {
            // `TabTitle` truncates OSC 0 to 120 Python chars (Unicode scalars); keep the
            // full saved label only when the live title actually hit that 120-char boundary,
            // so `/rename` to a shorter prefix still takes effect immediately.
            if let validSaved,
               validSaved.unicodeScalars.count > 120,
               normalizedLive.unicodeScalars.count >= 120,
               validSaved.hasPrefix(normalizedLive) {
                return validSaved
            }
            return normalizedLive
        }
        return validSaved ?? "New session"
    }

    func displayTitle(for pane: PCodeSessionPane) -> String {
        Self.displayTitle(paneTitle: pane.title, savedLabel: pane.savedLabel(saved: info(for: pane)))
    }

    func projectName(for pane: PCodeSessionPane) -> String {
        let path = projectPath(for: pane)
        if path == Self.normalizePath(NSHomeDirectory()) { return "~" }
        let last = (path as NSString).lastPathComponent
        return last.isEmpty ? path : last
    }

    func peekTarget(for pane: PCodeSessionPane) -> PCodePeekTarget {
        let saved = info(for: pane)
        let title = Self.displayTitle(paneTitle: pane.title, savedLabel: pane.savedLabel(saved: saved))
        let responseDate = pane.lastResponseDate(saved: saved)
        return PCodePeekTarget(
            sessionID: pane.sessionID,
            title: title,
            subtitle: Self.sessionSubtitle(worktreeName: saved?.worktreeName ?? pane.worktreeName, updatedDate: responseDate),
            directory: pane.directory,
            turnsHint: saved?.turns,
            updatedDate: responseDate,
            isOpen: true,
            paneID: pane.id,
            info: saved)
    }

    func openPeekTarget(_ target: PCodePeekTarget) {
        peek.dismiss(restoreFocus: false)
        if let paneID = target.paneID, let pane = panes.first(where: { $0.id == paneID }) {
            select(pane)
        } else if let info = target.info ?? target.sessionID.flatMap({ historyByID[$0] }) {
            open(info)
        }
    }

    func dismissDirectoryPicker() {
        showingDirectoryPicker = false
        if let pane = selectedPane {
            DispatchQueue.main.async { pane.focusInput() }
        }
    }

    func toggleCollapsed(_ path: String) {
        peek.clearHoverAndUnpinned()
        let normalized = Self.normalizePath(path)
        if collapsedFolders.contains(normalized) {
            collapsedFolders.remove(normalized)
        } else {
            collapsedFolders.insert(normalized)
            recentLimits[normalized] = nil
        }
    }

    @discardableResult
    func addFolder(_ path: String) -> Bool {
        let normalized = Self.normalizePath(path)
        var isDir: ObjCBool = false
        guard FileManager.default.fileExists(atPath: normalized, isDirectory: &isDir), isDir.boolValue else { return false }
        hiddenFolders.remove(normalized)
        collapsedFolders.remove(normalized)
        if !pinnedFolders.contains(normalized) {
            pinnedFolders.insert(normalized, at: 0)
        }
        return true
    }

    func removeFolder(_ path: String) {
        peek.clearHoverAndUnpinned()
        let normalized = Self.normalizePath(path)
        pinnedFolders.removeAll { $0 == normalized }
        hiddenFolders.insert(normalized)
        recentLimits[normalized] = nil
    }

    /// Present a native macOS folder chooser (`NSOpenPanel`).
    func browseForFolder(startSession: Bool, onCancel: (() -> Void)? = nil) {
        if let window, window.attachedSheet != nil { return }
        let panel = NSOpenPanel()
        panel.canChooseDirectories = true
        panel.canChooseFiles = false
        panel.allowsMultipleSelection = false
        panel.canCreateDirectories = true
        panel.prompt = startSession ? "New Session" : "Add Project"
        panel.message = startSession
            ? "Choose a project folder to start a new pcode session in."
            : "Choose a project folder to add to the sidebar."
        if let current = selectedPane?.directory {
            panel.directoryURL = URL(fileURLWithPath: current)
        }
        let handle: (NSApplication.ModalResponse) -> Void = { [weak self] response in
            guard let self else { return }
            guard response == .OK, let url = panel.url else {
                DispatchQueue.main.async { onCancel?() }
                return
            }
            let path = url.standardizedFileURL.path
            self.addFolder(path)
            if startSession {
                // Next turn so AppKit finishes ordering out the sheet before moving focus to the terminal.
                DispatchQueue.main.async {
                    self.newSession(directory: path)
                }
            }
        }
        if let window {
            panel.beginSheetModal(for: window, completionHandler: handle)
        } else {
            handle(panel.runModal())
        }
    }

    func open(_ info: PCodeSessionInfo, select shouldSelect: Bool = true) {
        if shouldSelect {
            peek.dismiss(restoreFocus: false)
            showingDirectoryPicker = false
        }
        if let pane = panes.first(where: { $0.sessionID == info.id }) {
            if shouldSelect { select(pane) }
            return
        }
        guard PCode.valid(info.id, length: 1...64) else { return }
        let project = info.normalizedProjectPath
        hiddenFolders.remove(project)
        collapsedFolders.remove(project)
        guard let pane = makePane(
            args: ["--host", "-c", info.id],
            directory: info.workspace,
            projectPath: project,
            sessionID: info.id
        ) else { return }
        pane.openedLabel = info.label
        adopt(pane)
        panes.append(pane)
        schedulePresize()
        saveOpen()
        if shouldSelect { select(pane) }
    }

    // MARK: Path & time helpers

    static func normalizePath(_ path: String) -> String {
        let expanded = (path as NSString).expandingTildeInPath
        let standardized = (expanded as NSString).standardizingPath
        if standardized.count > 1, standardized.hasSuffix("/") {
            return String(standardized.dropLast())
        }
        return standardized
    }

    static func abbreviatePath(_ path: String) -> String {
        let home = normalizePath(NSHomeDirectory())
        if path == home { return "~" }
        if path.hasPrefix(home + "/") {
            return "~" + path.dropFirst(home.count)
        }
        return path
    }

    private static let isoWithFractional: ISO8601DateFormatter = {
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        return formatter
    }()

    private static let isoBasic: ISO8601DateFormatter = {
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime]
        return formatter
    }()

    static func parseISO8601(_ iso: String) -> Date? {
        isoWithFractional.date(from: iso) ?? isoBasic.date(from: iso)
    }

    static func relativeTime(_ date: Date?, now: Date = Date()) -> String? {
        guard let date else { return nil }
        let seconds = max(0, Int(now.timeIntervalSince(date)))
        if seconds < 60 { return "now" }
        if seconds < 3600 { return "\(seconds / 60)m" }
        if seconds < 86_400 { return "\(seconds / 3600)h" }
        if seconds < 86_400 * 14 { return "\(seconds / 86_400)d" }
        return "\(seconds / (86_400 * 7))w"
    }

    static func sessionSubtitle(worktreeName: String?, updatedDate: Date?, now: Date = Date()) -> String? {
        let rel = relativeTime(updatedDate, now: now)
        switch (worktreeName?.isEmpty == false ? worktreeName : nil, rel) {
        case (let wt?, let age?): return "\(wt) · \(age)"
        case (let wt?, nil): return wt
        case (nil, let age?): return age
        case (nil, nil): return nil
        }
    }

    // MARK: Sidebar actions

    static func reveal(_ directory: String) {
        NSWorkspace.shared.activateFileViewerSelecting([URL(fileURLWithPath: directory)])
    }

    static func copy(_ text: String) {
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(text, forType: .string)
    }

    func confirmDelete(info: PCodeSessionInfo) {
        confirm("Delete “\(info.label)”?") { [weak self] in self?.delete(id: info.id) }
    }

    func confirmDelete(pane: PCodeSessionPane) {
        guard let id = pane.sessionID else { return }
        let title = displayTitle(for: pane)
        confirm("Delete “\(title)”?", detail: "It is closed first, ending pcode in it.") { [weak self, weak pane] in
            guard let self, let pane else { return }
            self.close(pane)
            // pcode lets go of the session a moment after its terminal closes.
            self.delete(id: id, retries: 10)
        }
    }

    private func confirm(_ message: String, detail: String? = nil, then action: @escaping () -> Void) {
        let alert = NSAlert()
        alert.messageText = message
        alert.informativeText = [detail, "Its transcript and history are removed. This can't be undone."]
            .compactMap { $0 }.joined(separator: " ")
        alert.alertStyle = .warning
        alert.addButton(withTitle: "Delete").hasDestructiveAction = true
        alert.addButton(withTitle: "Cancel")
        guard let window else { return }
        alert.beginSheetModal(for: window) { if $0 == .alertFirstButtonReturn { action() } }
    }

    /// `pcode --delete-session <id>`, which refuses a session still open in
    /// a process: retried while one is letting go of it.
    private func delete(id: String, retries: Int = 0) {
        guard PCode.valid(id, length: 1...64) else { return }
        let command = "\(PCodeWorkspaceMode.command) --delete-session \(PCode.shellQuoted(id))"
        let shell = PCodeWorkspaceMode.loginShell
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            let process = Process()
            process.executableURL = URL(fileURLWithPath: shell)
            // A login shell, as the session terminals get: `pcode` is on its PATH.
            process.arguments = ["-l", "-c", command]
            let errors = Pipe()
            process.standardError = errors
            process.standardOutput = FileHandle.nullDevice
            var status: Int32 = -1
            if (try? process.run()) != nil {
                process.waitUntilExit()
                status = process.terminationStatus
            }
            let message = String(data: errors.fileHandleForReading.readDataToEndOfFile(), encoding: .utf8) ?? ""
            DispatchQueue.main.async {
                guard let self else { return }
                if status != 0, retries > 0, message.contains("open in another process") {
                    DispatchQueue.main.asyncAfter(deadline: .now() + 0.5) { self.delete(id: id, retries: retries - 1) }
                    return
                }
                if status == 0 {
                    self.unseenIDs.remove(id)
                    self.starredSessionIDs.remove(id)
                    self.peek.clearHover(sessionID: id)
                }
                self.reloadHistory()
                guard status != 0 else { return }
                let alert = NSAlert()
                alert.messageText = "Couldn't delete the session"
                alert.informativeText = message.isEmpty ? "pcode exited with status \(status)." : message
                if let window = self.window { alert.beginSheetModal(for: window) } else { alert.runModal() }
            }
        }
    }

    /// Tell each terminal's renderer whether it is on screen, as Ghostty's own
    /// windows do: a hidden session must not keep drawing frames that
    /// compete with the one being watched.
    func syncOcclusion() {
        let windowVisible = windowVisible()
        if windowVisibility.onScreen != windowVisible {
            windowVisibility.onScreen = windowVisible
            // The periodic history reload skips a hidden window: catch up now.
            if windowVisible, hasRestored, !restoring,
               Date().timeIntervalSince(lastHistoryReload) >= Self.historyReconcileInterval {
                reloadHistory()
            }
        }
        let peekedPaneID = peek.target?.paneID
        for pane in panes {
            let visible = windowVisible && (pane.id == selected || pane.id == peekedPaneID)
            for view in pane.surfaces {
                // Always sent: a new surface's flag starts false while its
                // renderer starts visible, so comparing would skip hiding it.
                guard let surface = view.surface else { continue }
                ghostty_surface_set_occlusion(surface, visible)
                view.isWindowVisible = visible
            }
        }
    }

    // MARK: Hidden panes' sizes

    /// The session area's size, measured by the view; hidden panes are sized to it.
    var detailSize: CGSize = .zero {
        didSet { if detailSize != oldValue { schedulePresize() } }
    }

    private var presizeWork: DispatchWorkItem?

    /// Presize once things settle: not on every frame of a live window resize
    /// (each one reflows every hidden terminal and signals every hidden pcode),
    /// and after a newly shown pane has been laid out, so it can be measured.
    func schedulePresize() {
        presizeWork?.cancel()
        let work = DispatchWorkItem { [weak self] in self?.presizeHidden() }
        presizeWork = work
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.2, execute: work)
    }

    /// Give every hidden pane's terminals the size they will have when shown.
    ///
    /// A hidden pane is in no window, so nothing lays its terminals out: they
    /// keep the size they were made with (a few dozen columns), pcode in them
    /// draws to that, and showing one reflows everything at once. Sized to
    /// match the shown pane, showing it changes nothing.
    func presizeHidden() {
        guard let window, detailSize.width > 0, detailSize.height > 0 else { return }
        let scale = window.backingScaleFactor
        let shown = selectedPane
        let shownCard = shown?.surface?.existingPCodeDrawer?.isOpen == true
            ? nil
            : Self.pixels(shown?.surface)
        for pane in panes where pane.id != selected {
            guard let surface = pane.surface else { continue }  // Native: no terminal to size.
            let cell = surface.cellSize.height > 0 ? surface.cellSize.height : 18
            let heightPx = UInt32(max(1, PCodeDock.cardHeight(rows: PCodeDock.gridRows, cell: cell) * scale))
            let card: (UInt32, UInt32)
            if let drawer = surface.existingPCodeDrawer, drawer.isOpen {
                let h = PCodeDock.horizontalLayout(
                    in: detailSize.width,
                    drawerOpen: true,
                    drawerFraction: drawer.fraction)
                card = (UInt32(max(1, h.cardWidth * scale)), heightPx)
            } else {
                let h = PCodeDock.horizontalLayout(in: detailSize.width)
                card = shownCard ?? (UInt32(max(1, h.cardWidth * scale)), heightPx)
            }
            Self.setSize(surface, card, scale: scale)
        }
    }

    private static func pixels(_ view: Ghostty.SurfaceView?) -> (UInt32, UInt32)? {
        guard let view, view.window != nil, let surface = view.surface else { return nil }
        let size = ghostty_surface_size(surface)
        guard size.width_px > 0, size.height_px > 0 else { return nil }
        return (size.width_px, size.height_px)
    }

    private static func setSize(_ view: Ghostty.SurfaceView, _ pixels: (UInt32, UInt32), scale: CGFloat) {
        guard let surface = view.surface, view.window == nil else { return }
        let current = ghostty_surface_size(surface)
        guard current.width_px != pixels.0 || current.height_px != pixels.1 else { return }
        ghostty_surface_set_content_scale(surface, scale, scale)
        ghostty_surface_set_size(surface, pixels.0, pixels.1)
    }

    private func adopt(_ pane: PCodeSessionPane) {
        if let id = pane.sessionID, unseenIDs.contains(id) { pane.unseen = true }
        pane.onTurnFinished = { [weak self, weak pane] in
            guard let self, let pane else { return }
            if self.isViewed(pane) { self.markSeen(pane) } else { self.markUnseen(pane) }
        }
        pane.onActivityChange = { [weak self] in
            // Folder headers always surface live running/failed dots (collapsed or expanded).
            self?.objectWillChange.send()
        }
    }

    // MARK: Unseen responses

    /// Whether the user is looking at `pane`: it is the one shown, in the key
    /// window (no window is key while the app is in the background), and that
    /// window is on screen (a locked screen or asleep display leaves it key).
    private func isViewed(_ pane: PCodeSessionPane) -> Bool {
        pane.id == selected && (window?.isKeyWindow ?? false) && windowVisible()
    }

    private func markUnseen(_ pane: PCodeSessionPane) {
        if !pane.unseen {
            pane.unseen = true
            objectWillChange.send()  // collapsed folder headers
        }
        if let id = pane.sessionID, !unseenIDs.contains(id) { unseenIDs.insert(id) }
    }

    private func markSeen(_ pane: PCodeSessionPane) {
        if pane.unseen {
            pane.unseen = false
            objectWillChange.send()
        }
        if let id = pane.sessionID, unseenIDs.contains(id) { unseenIDs.remove(id) }
    }

    /// pcode switched the pane to another session (`/resume`, `/new`, or its
    /// first report): carry an unseen response over to the new ID.
    private func sessionIDChanged(_ pane: PCodeSessionPane, from old: String?) {
        guard old != pane.sessionID else { return }
        publishHistory()  // The open set changed without `panes` changing.
        // Recompute sidebar grouping/sorting even when `history` itself didn't change.
        objectWillChange.send()
        // A response that ended before the new ID arrived belongs to it, not the old one.
        let carried = pane.unseen && (old.map { unseenIDs.contains($0) } ?? true)
        if let old, carried { unseenIDs.remove(old) }
        if isViewed(pane) {
            markSeen(pane)
        } else if carried || pane.sessionID.map(unseenIDs.contains) == true {
            markUnseen(pane)
        } else if pane.unseen {
            pane.unseen = false
            objectWillChange.send()
        }
    }

    /// The window became key or came on screen: the session it shows has now been seen.
    func selectedMaybeSeen() {
        if let pane = selectedPane, isViewed(pane) { markSeen(pane) }
    }

    func select(_ pane: PCodeSessionPane) {
        peek.dismiss(restoreFocus: false)
        // The composer being left keeps its unsent text as a draft (`drafts.py`).
        if selected != pane.id { selectedPane?.composer?.saveDrafts() }
        selected = pane.id
        saveSelected()
        if isViewed(pane) { markSeen(pane) }
        syncOcclusion()
        // The pane just hidden keeps its size; the others follow the shown one.
        schedulePresize()
        // After SwiftUI has put the pane's views in the window.
        DispatchQueue.main.async { pane.focusInput() }
    }

    /// Maximum automatic host relaunches before showing the fallback error card with Reopen.
    static let maxHostRelaunchAttempts = 2
    /// How long a pane with a live host entry is allowed to boot before being treated as stuck.
    private static let bootingHostGrace: TimeInterval = 30

    /// Reset automatic relaunch state once a pane's transcript connects to its host.
    func noteHostConnected(for surface: Ghostty.SurfaceView) {
        surface.pcodeHostRelaunchAttempt = 0
        if let pane = panes.first(where: { $0.surface === surface }) {
            pane.hostRelaunchAttempts = 0
            pane.sawOpenElsewhere = false
        }
    }

    /// Decide whether a pane whose host never appeared (or stopped) should relaunch in place,
    /// keep waiting, or show the fallback failure card. Pure decision helper for unit tests.
    static func hostRecoveryAction(
        attempts: Int,
        maxAttempts: Int = maxHostRelaunchAttempts,
        isManual: Bool,
        needsInput: Bool,
        hostEntryAlive: Bool,
        unavailableDuration: TimeInterval,
        openElsewhere: Bool,
        sawOpenElsewhere: Bool = false
    ) -> PCodeLiveTranscriptController.RelaunchOutcome? {
        if !isManual {
            if needsInput { return .keepWaiting }
            if hostEntryAlive && unavailableDuration < bootingHostGrace { return .keepWaiting }
        }
        if openElsewhere {
            if !isManual && !sawOpenElsewhere {
                return .keepWaiting
            }
            return .failed("This session is open in another terminal.")
        }
        let effectiveAttempts = isManual ? 0 : attempts
        if effectiveAttempts >= maxAttempts {
            return nil // Caller attaches log tail and returns `.failed(...)`.
        }
        return .relaunched(attempt: effectiveAttempts + 1, maxAttempts: maxAttempts)
    }

    /// Relaunch a session pane's host in place when its host failed to start or died unexpectedly.
    func handleHostUnavailable(
        for surface: Ghostty.SurfaceView,
        isManual: Bool,
        unavailableDuration: TimeInterval = 0
    ) -> PCodeLiveTranscriptController.RelaunchOutcome {
        guard !restarting,
              let index = panes.firstIndex(where: { $0.surface === surface })
        else {
            return .keepWaiting
        }
        let pane = panes[index]
        let sessionID = pane.sessionID.flatMap { $0.isEmpty ? nil : $0 }
        let entries = PCodeSessionInfo.liveHostEntries()
        let hostEntryAlive = entries.contains { entry in
            if let sid = sessionID, entry.sessionID == sid { return true }
            if let hid = pane.hostID ?? pane.launchHostID, !hid.isEmpty, entry.id == hid { return true }
            return false
        }
        let openElsewhere: Bool = {
            guard let sid = sessionID else { return false }
            let isHosted = entries.contains { $0.sessionID == sid }
            return !isHosted && PCodeSessionInfo.isOpen(sid)
        }()
        let decision = Self.hostRecoveryAction(
            attempts: pane.hostRelaunchAttempts,
            maxAttempts: Self.maxHostRelaunchAttempts,
            isManual: isManual,
            needsInput: surface.existingPCodeDock?.needsInput == true,
            hostEntryAlive: hostEntryAlive,
            unavailableDuration: unavailableDuration,
            openElsewhere: openElsewhere,
            sawOpenElsewhere: pane.sawOpenElsewhere)
        pane.sawOpenElsewhere = openElsewhere
        guard let decision else {
            let tail = PCodeSessionInfo.hostLogTail(hostID: pane.launchHostID)
                ?? PCodeSessionInfo.hostLogTail(hostID: pane.hostID ?? pane.lastHostID)
            return .failed(tail ?? "The session host didn't start in time.")
        }
        guard case .relaunched(let attempt, let maxAttempts) = decision else {
            return decision
        }
        // Stop any stuck or broken host for this pane before spawning its replacement,
        // so the new pane doesn't reattach to the same host or leak an orphan.
        let oldHostID = pane.hostID ?? pane.lastHostID ?? pane.hostStatus?.hostID ?? pane.launchHostID
        if Self.stopHost(hostID: oldHostID, sessionID: sessionID).isEmpty,
           let launchHostID = pane.launchHostID {
            for delay in [2.0, 6.0] {
                DispatchQueue.global(qos: .utility).asyncAfter(deadline: .now() + delay) {
                    _ = Self.stopHost(hostID: launchHostID, sessionID: nil)
                }
            }
        }
        let args = sessionID.map { ["--host", "-c", $0] } ?? ["--host"]
        guard let fresh = makePane(
            args: args,
            directory: pane.directory,
            projectPath: pane.projectPath,
            sessionID: sessionID
        ) else {
            return .failed("Could not start a new session pane.")
        }
        fresh.openedLabel = pane.openedLabel
        fresh.unseen = pane.unseen
        fresh.hostRelaunchAttempts = attempt
        fresh.surface?.pcodeHostRelaunchAttempt = attempt
        MainActor.assumeIsolated {
            fresh.inspectorState.isOpen = pane.inspectorState.isOpen
            fresh.inspectorState.activeTab = pane.inspectorState.activeTab
            fresh.inspectorState.diffScope = pane.inspectorState.diffScope
            fresh.inspectorState.diffLayout = pane.inspectorState.diffLayout
            fresh.inspectorState.splitFraction = pane.inspectorState.splitFraction
            fresh.inspectorState.isExpanded = pane.inspectorState.isExpanded
            fresh.inspectorState.focusedFilePath = pane.inspectorState.focusedFilePath
            fresh.inspectorState.focusedAgentID = pane.inspectorState.focusedAgentID
            fresh.inspectorState.focusedCommandID = pane.inspectorState.focusedCommandID
            fresh.inspectorState.collapsedFiles = pane.inspectorState.collapsedFiles
            fresh.inspectorState.followAgent = pane.inspectorState.followAgent
        }
        if let shell = pane.shell {
            fresh.setShell(shell)
            fresh.shellVisible = pane.shellVisible
            pane.setShell(nil)
        }
        adopt(fresh)
        peek.clearHover(paneID: pane.id)
        let wasSelected = (selected == pane.id)
        panes[index] = fresh
        if let surface = pane.surface { PCode.retire(surface) }
        pane.composer?.stop()
        schedulePresize()
        if wasSelected {
            select(fresh)
        } else {
            syncOcclusion()
        }
        return .relaunched(attempt: attempt, maxAttempts: maxAttempts)
    }

    /// Ask to close `pane`: pcode's terminal reports back through the close
    /// notification, which confirms first if pcode is still running.
    func requestClose(_ pane: PCodeSessionPane) {
        // A native pane has no terminal to ask: closing it just stops its host.
        guard let surface = pane.surface?.surface else { return close(pane) }
        ghostty.requestClose(surface: surface)
    }

    /// Stop any running background host for `hostID` or `sessionID` (`SIGTERM`),
    /// so closing a workspace pane ends its session rather than detaching.
    @discardableResult
    static func stopHost(
        hostID: String?,
        sessionID: String?,
        in directory: URL = PCodeSessionInfo.hostRoot,
        isAlive: (Int32) -> Bool = { kill($0, 0) == 0 && PCodeProcess.isPCodeOrHost($0) },
        signal: (Int32) -> Void = { _ = kill($0, SIGTERM) }
    ) -> [Int32] {
        let validHost = (hostID?.isEmpty == false) ? hostID : nil
        let validSession = (sessionID?.isEmpty == false) ? sessionID : nil
        guard validHost != nil || validSession != nil else { return [] }
        var stopped: [Int32] = []
        for entry in PCodeSessionInfo.liveHostEntries(in: directory, isAlive: isAlive) {
            let matchesHost = validHost != nil && entry.id == validHost
            let matchesSession = validSession != nil && !entry.sessionID.isEmpty && entry.sessionID == validSession
            guard matchesHost || matchesSession else { continue }
            signal(entry.pid)
            stopped.append(entry.pid)
        }
        return stopped
    }

    /// Remove `pane` and end its terminals (and its session host, unless quitting to restart).
    /// Not from inside one of their callbacks.
    func close(_ pane: PCodeSessionPane) {
        peek.clearHover(paneID: pane.id)
        guard let index = panes.firstIndex(where: { $0 === pane }) else { return }
        panes.remove(at: index)
        let shouldStopHost = !restarting
        let hostID = pane.hostID ?? pane.lastHostID ?? pane.hostStatus?.hostID ?? pane.launchHostID
        let sessionID = pane.sessionID
        if let shell = pane.shell { PCode.retire(shell) }
        if let surface = pane.surface { PCode.retire(surface) }
        pane.composer?.stop()
        if shouldStopHost, Self.stopHost(hostID: hostID, sessionID: sessionID).isEmpty,
           let launchHostID = pane.launchHostID {
            // Closed before its host registered: it may still be importing, so
            // try the id the app gave it again once it has had time to appear.
            for delay in [2.0, 6.0] {
                DispatchQueue.global(qos: .utility).asyncAfter(deadline: .now() + delay) {
                    _ = Self.stopHost(hostID: launchHostID, sessionID: nil)
                }
            }
        }
        saveOpen()
        reloadHistory()
        if selected == pane.id {
            if panes.isEmpty {
                selected = nil
                showingDirectoryPicker = false
                launcherFocusTrigger += 1
            } else {
                select(panes[min(index, panes.count - 1)])
            }
        }
    }

    func pane(for surface: Ghostty.SurfaceView) -> PCodeSessionPane? {
        panes.first { $0.surface === surface }
    }

    func toggleSidebarFileChanges() {
        showSidebarFileChanges.toggle()
    }

    func toggleInspector() {
        guard !showingDirectoryPicker, let pane = selectedPane else { return }
        MainActor.assumeIsolated {
            pane.inspectorState.toggle()
        }
    }

    func selectInspectorTab(_ tab: PCodeInspectorTab) {
        guard !showingDirectoryPicker, let pane = selectedPane else { return }
        MainActor.assumeIsolated {
            pane.inspectorState.activeTab = tab
            pane.inspectorState.isOpen = true
        }
    }

    func toggleInspectorExpanded() {
        guard !showingDirectoryPicker, let pane = selectedPane else { return }
        MainActor.assumeIsolated {
            pane.inspectorState.toggleExpanded()
        }
    }

    func openInspectorFile(_ path: String, in pane: PCodeSessionPane) {
        select(pane)
        MainActor.assumeIsolated {
            pane.inspectorState.openChanges(filePath: path, scope: .session)
        }
    }

    func openInspectorChanges(in pane: PCodeSessionPane) {
        select(pane)
        MainActor.assumeIsolated {
            pane.inspectorState.openChanges(scope: .session)
        }
    }

    func toggleShell() {
        guard let pane = selectedPane else { return }
        if pane.shell == nil {
            var config = Ghostty.SurfaceConfiguration()
            config.workingDirectory = pane.directory
            guard let app = ghostty.app else { return }
            let shell = Ghostty.SurfaceView(app, baseConfig: config)
            PCode.applyColorScheme(to: shell)
            applyCurrentFontSize(to: shell)
            pane.setShell(shell)
        }
        pane.shellVisible.toggle()
        syncOcclusion()
        if pane.shellVisible, let shell = pane.shell {
            DispatchQueue.main.async { Ghostty.moveFocus(to: shell) }
        } else {
            DispatchQueue.main.async { pane.focusInput() }
        }
    }

    private func makePane(
        args: [String],
        directory: String,
        projectPath: String? = nil,
        sessionID: String?
    ) -> PCodeSessionPane? {
        let fm = FileManager.default
        let isDir: (String) -> Bool = { path in
            var flag: ObjCBool = false
            return fm.fileExists(atPath: path, isDirectory: &flag) && flag.boolValue
        }
        let resolvedDir: String
        let resolvedProject: String
        if isDir(directory) {
            resolvedDir = directory
            resolvedProject = projectPath ?? directory
        } else if let projectPath, isDir(projectPath) {
            resolvedDir = projectPath
            resolvedProject = projectPath
        } else {
            let home = Self.normalizePath(NSHomeDirectory())
            resolvedDir = home
            resolvedProject = home
        }
        if PCodeNativeComposer.enabled {
            return makeNativePane(directory: resolvedDir, projectPath: resolvedProject, sessionID: sessionID)
        }
        guard let app = ghostty.app else { return nil }
        var config = Ghostty.SurfaceConfiguration()
        let launch = PCodeWorkspaceMode.launchConfig(args, directory: resolvedDir)
        config.command = launch.command
        for (key, value) in launch.env {
            config.environmentVariables[key] = value
        }
        config.workingDirectory = resolvedDir
        // The pane's single surface runs pcode's editor-only host client (`PCodeDockCard`).
        config.environmentVariables["PCODE_EDITOR_ONLY"] = "1"
        // Tell pcode the native transcript renders the session, so it leaves out
        // the editor rows only a terminal needs (tool rows, live command previews).
        config.environmentVariables["PCODE_NATIVE_TRANSCRIPT"] = "1"
        // Suppress macOS `login(1)` "Last login:" banner in pcode session panes.
        config.environmentVariables["PCODE_HUSH_LOGIN"] = "1"
        // The id of the host pcode starts, known here before the host boots and
        // reports it, so closing the pane early still stops it.
        let launchHostID = Self.newHostID()
        config.environmentVariables["PCODE_HOST_ID"] = launchHostID
        // pcode keeps its own startup errors on screen until Enter (`hold_failed_start`).
        config.waitAfterCommand = false
        let view = Ghostty.SurfaceView(app, baseConfig: config)
        PCodeStartupTrace.paneCreated(launch.command.hasPrefix("/usr/bin/env") ? "direct" : "login-shell fallback")
        view.pcodeSession = sessionID
        // A resumed session has a transcript to show while pcode starts (`PCodeStartupOverlay`).
        view.pcodeDock.bootCovered = sessionID != nil
        PCode.applyColorScheme(to: view)
        applyCurrentFontSize(to: view)
        let pane = PCodeSessionPane(
            surface: view,
            directory: resolvedDir,
            projectPath: resolvedProject,
            sessionID: sessionID)
        pane.launchHostID = launchHostID
        return pane
    }

    /// A native-composer pane: no terminal; the host is spawned headless and the composer
    /// attaches to it over its socket (`PCodeComposerSession`).
    private func makeNativePane(
        directory: String, projectPath: String, sessionID: String?, existingHostID: String? = nil
    ) -> PCodeSessionPane {
        let launchHostID = existingHostID ?? Self.newHostID()
        let session = PCodeComposerSession(
            directory: directory, resumeSessionID: sessionID, launchHostID: launchHostID,
            existingHostID: existingHostID)
        let pane = PCodeSessionPane(composer: session, directory: directory, projectPath: projectPath, sessionID: sessionID)
        pane.launchHostID = launchHostID
        session.onSessionReported = { [weak self, weak pane] sessionID, hostID in
            guard let self, let pane else { return }
            self.sessionReported(pane, sessionID: sessionID, hostID: hostID)
        }
        session.onCloseRequested = { [weak self, weak pane] in
            guard let self, let pane else { return }
            self.close(pane)
        }
        session.start()
        return pane
    }

    /// A fresh host id in pcode's format (`new_host_id`: 8 lowercase hex digits).
    static func newHostID() -> String {
        String(UUID().uuidString.replacingOccurrences(of: "-", with: "").prefix(8)).lowercased()
    }

    // MARK: Workspace-Wide Font Scaling

    /// Every live Ghostty terminal surface owned by the workspace (editors, shells, and overlays).
    private var allLiveSurfaces: [Ghostty.SurfaceView] {
        var result = panes.flatMap(\.surfaces)
        for pane in panes {
            if let overlay = pane.surface?.existingPCodePopup?.slot?.surface,
               !result.contains(where: { $0 === overlay }) {
                result.append(overlay)
            }
        }
        if let overlay = PCode.keyboardOverlay(in: window), !result.contains(where: { $0 === overlay }) {
            result.append(overlay)
        }
        return result
    }

    /// Apply the current `PCodeTypography.shared` delta (relative to `defaultSize`) to a newly created surface.
    func applyCurrentFontSize(to view: Ghostty.SurfaceView) {
        guard let surface = view.surface else { return }
        let delta = Int((PCodeTypography.shared.size - PCodeTypography.defaultSize).rounded())
        if delta > 0 {
            ghostty.changeFontSize(surface: surface, .increase(delta))
        } else if delta < 0 {
            ghostty.changeFontSize(surface: surface, .decrease(-delta))
        }
    }

    private func applyFontSizeDelta(_ delta: Int, reset: Bool = false) {
        for view in allLiveSurfaces {
            guard let surface = view.surface else { continue }
            if reset {
                ghostty.changeFontSize(surface: surface, .reset)
            } else if delta > 0 {
                ghostty.changeFontSize(surface: surface, .increase(delta))
            } else if delta < 0 {
                ghostty.changeFontSize(surface: surface, .decrease(-delta))
            }
        }
    }

    func increaseFontSize() {
        PCodeTypography.shared.increaseSize()
    }

    func decreaseFontSize() {
        PCodeTypography.shared.decreaseSize()
    }

    func resetFontSize() {
        let wasDefault = PCodeTypography.shared.size == PCodeTypography.defaultSize
        PCodeTypography.shared.resetSize()
        if wasDefault {
            applyFontSizeDelta(0, reset: true)
        }
    }

    private func surfaceClosed(_ view: Ghostty.SurfaceView, processAlive: Bool) {
        if let pane = panes.first(where: { $0.shell === view }) {
            pane.shellVisible = false
            pane.setShell(nil)
            view.freeSurface()
            pane.focusInput()
            return
        }
        guard let pane = panes.first(where: { $0.surface === view }) else { return }
        guard processAlive, let window = view.window ?? NSApp.keyWindow else { return close(pane) }
        let alert = NSAlert()
        alert.messageText = "Close this session?"
        alert.informativeText = "pcode is still running in it. The session is saved and can be reopened from the sidebar."
        alert.addButton(withTitle: "Close")
        alert.addButton(withTitle: "Cancel")
        alert.beginSheetModal(for: window) { [weak self, weak pane] response in
            guard response == .alertFirstButtonReturn, let self, let pane else { return }
            self.close(pane)
        }
    }

    private func saveOpen() {
        guard !restarting else { return }
        UserDefaults.standard.set(panes.compactMap(\.sessionID), forKey: Self.openKey)
        let hostPanes = panes.compactMap { pane -> SavedHostPane? in
            guard pane.sessionID == nil, let composer = pane.composer,
                  let hostID = composer.hostID ?? pane.hostID else { return nil }
            return SavedHostPane(hostID: hostID, directory: pane.directory, projectPath: pane.projectPath)
        }
        UserDefaults.standard.set(try? JSONEncoder().encode(hostPanes), forKey: Self.openHostsKey)
    }

    /// Remember the shown session, for `restore` to show again.
    private func saveSelected() {
        guard !restarting, let id = selectedPane?.sessionID else { return }
        UserDefaults.standard.set(id, forKey: Self.selectedKey)
    }

    /// Whether the app is quitting to restart (`PCodeRestart`): the saved
    /// open sessions and selection are kept as they were when it began,
    /// while pcode in each pane exits and the panes close.
    private(set) var restarting = false

    /// Save what is open and shown, then stop saving until the app quits.
    /// The sessions are marked as restarting, for the relaunched app to
    /// wait for their hosts (`PCodeSessionInfo.awaitRestarted`).
    func beginRestart() {
        saveOpen()
        saveSelected()
        let defaults = UserDefaults.standard
        defaults.set(panes.compactMap(\.sessionID), forKey: Self.restartIDsKey)
        defaults.set(Date(), forKey: Self.restartTimeKey)
        restarting = true
    }

    private static let restartIDsKey = "PCodeWorkspaceRestartIDs"
    private static let restartTimeKey = "PCodeWorkspaceRestartTime"

    /// The sessions a recent Restart Pcode moved to hosts. A marker older
    /// than the restart could take (the relaunch failed, and Pcode was
    /// opened by hand much later) is ignored.
    private static func restartMarker() -> [String] {
        let defaults = UserDefaults.standard
        guard let time = defaults.object(forKey: restartTimeKey) as? Date,
              abs(time.timeIntervalSinceNow) < 600 else { return [] }
        return defaults.stringArray(forKey: restartIDsKey) ?? []
    }

    private static func clearRestartMarker() {
        UserDefaults.standard.removeObject(forKey: restartIDsKey)
        UserDefaults.standard.removeObject(forKey: restartTimeKey)
    }
}

/// Pure geometry helpers for the workspace sidebar's width bounds and auto-hide hit zones.
enum PCodeSidebarLayout {
    static let defaultWidth: CGFloat = 250
    static let minWidth: CGFloat = 200
    static let maxWidth: CGFloat = 500
    /// Minimum width reserved for the session pane when resizing the sidebar.
    static let minDetailWidth: CGFloat = 360
    /// Width of the left-edge hot zone that reveals the auto-hidden sidebar.
    static let revealEdgeWidth: CGFloat = 10
    /// Extra margin past the revealed sidebar's right edge so resizing doesn't trigger a hide.
    static let hideGraceMargin: CGFloat = 12
    /// Bounds for user-dragged folder section heights in the sidebar.
    static let minFolderHeight: CGFloat = 44
    static let maxFolderHeight: CGFloat = 800

    static func clampedFolderHeight(_ proposed: CGFloat) -> CGFloat {
        guard proposed.isFinite, proposed > 0 else { return minFolderHeight }
        return min(maxFolderHeight, max(minFolderHeight, proposed))
    }

    static func clampedWidth(_ proposed: CGFloat, totalWidth: CGFloat) -> CGFloat {
        let candidate = (proposed.isFinite && proposed > 0) ? proposed : defaultWidth
        let upper: CGFloat = totalWidth > 0
            ? max(minWidth, min(maxWidth, totalWidth - minDetailWidth))
            : maxWidth
        return min(upper, max(minWidth, candidate))
    }

    static func shouldReveal(at point: CGPoint, in contentBounds: CGRect) -> Bool {
        guard contentBounds.width > 0, contentBounds.height > 0 else { return false }
        return point.x >= contentBounds.minX
            && point.x <= contentBounds.minX + revealEdgeWidth
            && point.y >= contentBounds.minY
            && point.y <= contentBounds.maxY
    }

    static func isInsideRevealedZone(at point: CGPoint, in contentBounds: CGRect, sidebarWidth: CGFloat) -> Bool {
        guard contentBounds.width > 0, contentBounds.height > 0 else { return false }
        return point.x >= contentBounds.minX
            && point.x <= contentBounds.minX + sidebarWidth + hideGraceMargin
            && point.y >= contentBounds.minY
            && point.y <= contentBounds.maxY
    }
}

struct PCodeWorkspaceView: View {
    @EnvironmentObject var ghostty: Ghostty.App
    @ObservedObject var workspace: PCodeWorkspace
    @State private var isDropTargeted = false
    /// `workspace.sidebarWidth` when the current resize drag began.
    @State private var sidebarDragStart: CGFloat?
    @State private var sidebarHandleHovered = false
    /// Keeps the sidebar mounted for the 0.2s slide-out animation, then unmounts it
    /// so hidden controls stay out of the Tab loop and background timers stop.
    @State private var sidebarMounted = false
    @State private var sidebarUnmountWork: DispatchWorkItem?
    private static let sidebarAnimationDuration: TimeInterval = 0.2

    // A plain stack, not NavigationSplitView, and no toolbar: on macOS 26+
    // those lay glass (blur sampled every frame) over the terminal, which is
    // a scroll view, and make the window composite translucent layers.
    var body: some View {
        VStack(spacing: 0) {
            PCodeWindowTitleBar(workspace: workspace)
                .zIndex(10)
            ZStack {
                GeometryReader { geometry in
                    let totalWidth = geometry.size.width
                    let sidebarWidth = workspace.effectiveSidebarWidth(in: totalWidth)
                    let sidebarVisible = !workspace.sidebarAutoHide || workspace.sidebarRevealed
                    let showSidebar = sidebarVisible || sidebarMounted
                    let detailLeadingInset: CGFloat = sidebarVisible ? sidebarWidth + 1 : 0
                    ZStack(alignment: .leading) {
                        // Snap detailPane straight to its target width/position so transcript
                        // reflow and terminal resize happen once instead of jumping through
                        // intermediate widths while the sidebar animates.
                        detailPane
                            .padding(.leading, detailLeadingInset)

                        ZStack(alignment: .trailing) {
                            if showSidebar {
                                HStack(spacing: 0) {
                                    sidebar
                                        .frame(width: sidebarWidth)
                                        .background(sidebarBackground)
                                        .transformEnvironment(\.pcodeWindowOnScreen) { onScreen in
                                            if !sidebarVisible { onScreen = false }
                                        }
                                        .onHover { workspace.sidebarHoverChanged($0) }

                                    Rectangle()
                                        .fill(
                                            sidebarHandleHovered || sidebarDragStart != nil
                                                ? Color.accentColor.opacity(0.6)
                                                : ghostty.config.splitDividerColor
                                        )
                                        .frame(width: 1)
                                }
                                .frame(width: sidebarWidth + 1, height: geometry.size.height)
                                .transition(.identity)
                            }
                        }
                        .frame(
                            width: sidebarVisible ? sidebarWidth + 1 : 0,
                            height: geometry.size.height,
                            alignment: .trailing
                        )
                        .clipped()
                        .allowsHitTesting(sidebarVisible)
                        .accessibilityHidden(!sidebarVisible)
                        .zIndex(2)
                        .animation(.easeOut(duration: Self.sidebarAnimationDuration), value: sidebarVisible)
                        .onAppear {
                            sidebarMounted = sidebarVisible
                        }
                        .onChange(of: sidebarVisible) { visible in
                            sidebarUnmountWork?.cancel()
                            sidebarUnmountWork = nil
                            if visible {
                                sidebarMounted = true
                                workspace.scheduleSidebarHideIfPointerOutside()
                            } else {
                                isDropTargeted = false
                                workspace.isSidebarDropTargeted = false
                                workspace.peek.clearHoverAndUnpinned()
                                let work = DispatchWorkItem {
                                    sidebarUnmountWork = nil
                                    sidebarMounted = false
                                }
                                sidebarUnmountWork = work
                                DispatchQueue.main.asyncAfter(
                                    deadline: .now() + Self.sidebarAnimationDuration + 0.02,
                                    execute: work
                                )
                            }
                        }
                        .onDisappear {
                            sidebarUnmountWork?.cancel()
                            sidebarUnmountWork = nil
                        }

                        // Mounted above both sidebar and detailPane so Ghostty's NSView
                        // never steals cursor tracking from the resize hit zone.
                        if sidebarVisible {
                            PCodeResizeHandle(
                                orientation: .horizontal,
                                onHover: { sidebarHandleHovered = $0 },
                                onDoubleClick: { workspace.resetSidebarWidth() },
                                onDragStart: {
                                    sidebarDragStart = sidebarWidth
                                    workspace.isResizingSidebar = true
                                },
                                onDragChanged: { delta in
                                    let start = sidebarDragStart ?? sidebarWidth
                                    workspace.sidebarWidth = PCodeSidebarLayout.clampedWidth(
                                        start + delta,
                                        totalWidth: totalWidth
                                    )
                                },
                                onDragEnded: {
                                    sidebarDragStart = nil
                                    workspace.isResizingSidebar = false
                                    workspace.scheduleSidebarHideIfPointerOutside()
                                }
                            )
                            .frame(width: 11)
                            .offset(x: sidebarWidth - 5)
                            .zIndex(5)
                        }
                    }
                    .background(ghostty.config.backgroundColor, ignoresSafeAreaEdges: [])
                }

                if workspace.showingDirectoryPicker && workspace.selectedPane != nil {
                    PCodeProjectLauncher(workspace: workspace, mode: .overlay)
                        .transition(.opacity)
                }
            }
        }
        .ignoresSafeArea(.container, edges: .top)
        // Keep workspace keyboard shortcuts mounted even when the sidebar is auto-hidden.
        .background {
            Group {
                Button("") { workspace.toggleShell() }
                    .keyboardShortcut("j", modifiers: .command)
                Button("") { workspace.openDirectoryPicker() }
                    .keyboardShortcut("n", modifiers: [.command, .shift])
                Button("") { workspace.toggleSidebarAutoHide() }
                    .keyboardShortcut("s", modifiers: [.command, .option])
                Button("") { workspace.toggleInspector() }
                    .keyboardShortcut("i", modifiers: .command)
                Button("") { workspace.selectInspectorTab(.changes) }
                    .keyboardShortcut("1", modifiers: [.command, .option])
                Button("") { workspace.selectInspectorTab(.agents) }
                    .keyboardShortcut("2", modifiers: [.command, .option])
                Button("") { workspace.selectInspectorTab(.commands) }
                    .keyboardShortcut("3", modifiers: [.command, .option])
                Button("") { workspace.selectInspectorTab(.tree) }
                    .keyboardShortcut("4", modifiers: [.command, .option])
                Button("") { workspace.toggleInspectorExpanded() }
                    .keyboardShortcut("f", modifiers: [.command, .shift])
            }
            .opacity(0)
            .allowsHitTesting(false)
            .accessibilityHidden(true)
        }
        .onChange(of: isDropTargeted) { workspace.isSidebarDropTargeted = $0 }
        .onAppear {
            workspace.peek.startMonitoring()
            workspace.startSidebarMonitoring()
        }
        .onDisappear {
            workspace.peek.stopMonitoring()
            workspace.stopSidebarMonitoring()
        }
    }

    private var detailPane: some View {
        ZStack {
            if let pane = workspace.selectedPane {
                PCodeSessionPaneView(pane: pane)
                    .id(pane.id)
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
            } else {
                PCodeProjectLauncher(workspace: workspace, mode: .inline)
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
            }

            PCodePeekOverlay(
                peek: workspace.peek,
                resolveLivePane: { paneID in
                    guard paneID != workspace.selected else { return nil }
                    return workspace.panes.first { $0.id == paneID }
                }
            ) { target in
                workspace.openPeekTarget(target)
            }
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .background(ghostty.config.backgroundColor, ignoresSafeAreaEdges: [])
        .background(GeometryReader { proxy in
            Color.clear
                .onAppear { workspace.detailSize = proxy.size }
                .onChange(of: proxy.size) { workspace.detailSize = $0 }
        })
    }

    private var sidebarBackground: Color {
        // Solid, a shade off the terminal's background: no vibrancy behind it.
        let base = NSColor(ghostty.config.backgroundColor)
        let tint: NSColor = base.isLightColor ? .black : .white
        let fraction: CGFloat = base.isLightColor ? 0.035 : 0.06
        return Color(nsColor: base.blended(withFraction: fraction, of: tint) ?? base)
    }

    private var sidebarHeader: some View {
        HStack(spacing: 6) {
            Text("Projects")
                .font(.headline)
                .lineLimit(1)
            Spacer(minLength: 4)
            Button { workspace.toggleSidebarFileChanges() } label: {
                Image(systemName: "list.bullet.indent")
                    .foregroundStyle(workspace.showSidebarFileChanges ? Color.accentColor : Color.primary)
            }
            .help(workspace.showSidebarFileChanges ? "Hide changed files in sidebar" : "Show changed files in sidebar")
            Button { workspace.toggleShell() } label: { Image(systemName: "terminal") }
                .help("Toggle the shell under this session (⌘J)")
            Button { workspace.openDirectoryPicker() } label: { Image(systemName: "folder.badge.plus") }
                .help("Open or add project… (⌘⇧N)")
            Button { workspace.newSession() } label: { Image(systemName: "square.and.pencil") }
                .help("New session in current project (⌘N)")
            Button { workspace.toggleSidebarAutoHide() } label: {
                if workspace.sidebarAutoHide {
                    Image(systemName: "sidebar.left")
                        .foregroundStyle(Color.accentColor)
                } else {
                    Image(systemName: "sidebar.left")
                }
            }
            .help(workspace.sidebarAutoHide ? "Keep sidebar visible (⌥⌘S)" : "Auto-hide sidebar (⌥⌘S)")
            Button { workspace.toggleInspector() } label: { Image(systemName: "sidebar.right") }
                .help("Toggle Session Inspector (⌘I)")
        }
        .buttonStyle(.borderless)
        .padding(.horizontal, 12)
        .padding(.vertical, 8)
    }

    private var sidebar: some View {
        VStack(spacing: 0) {
            sidebarHeader
            sessionList
        }
        .overlay(
            RoundedRectangle(cornerRadius: 4)
                .stroke(Color.accentColor.opacity(isDropTargeted ? 0.8 : 0), lineWidth: 2)
                .padding(2)
                .allowsHitTesting(false)
        )
        .onDrop(of: [.fileURL], isTargeted: $isDropTargeted) { providers in
            handleFolderDrop(providers)
        }
        .onDisappear {
            isDropTargeted = false
            workspace.isSidebarDropTargeted = false
        }
    }

    private var sessionList: some View {
        TimelineView(.periodic(from: .now, by: 30)) { context in
            let now = context.date
            ScrollView(.vertical) {
                VStack(alignment: .leading, spacing: 2) {
                    ForEach(workspace.projectGroups) { group in
                        PCodeFolderSectionView(
                            workspace: workspace,
                            group: group,
                            now: now,
                            dividerColor: ghostty.config.splitDividerColor,
                            folderMenu: { folderMenu(group) },
                            openMenu: { openMenu($0) },
                            hostedMenu: { hostedMenu($0) },
                            recentMenu: { recentMenu($0) }
                        )
                    }
                }
                .padding(.horizontal, 8)
                .padding(.bottom, 12)
            }
        }
    }

    private func handleFolderDrop(_ providers: [NSItemProvider]) -> Bool {
        var accepted = false
        for provider in providers where provider.hasItemConformingToTypeIdentifier(UTType.fileURL.identifier) {
            accepted = true
            provider.loadItem(forTypeIdentifier: UTType.fileURL.identifier, options: nil) { item, _ in
                let url: URL?
                if let data = item as? Data {
                    url = URL(dataRepresentation: data, relativeTo: nil)
                } else if let direct = item as? URL {
                    url = direct
                } else {
                    url = nil
                }
                guard let url else { return }
                let path = url.standardizedFileURL.path
                var isDir: ObjCBool = false
                guard FileManager.default.fileExists(atPath: path, isDirectory: &isDir), isDir.boolValue else {
                    return
                }
                DispatchQueue.main.async {
                    workspace.addFolder(path)
                }
            }
        }
        return accepted
    }

    @ViewBuilder private func folderMenu(_ group: PCodeProjectGroup) -> some View {
        Button("New Session") { workspace.newSession(directory: group.path) }
        if workspace.folderHeights[group.path] != nil {
            Button("Reset Section Height") { workspace.resetFolderHeight(for: group.path) }
        }
        Divider()
        Button("Reveal in Finder") { PCodeWorkspace.reveal(group.path) }
        Button("Copy Path") { PCodeWorkspace.copy(group.path) }
        // Starred sessions keep a folder listed, so removing it would do nothing.
        if group.openPanes.isEmpty && group.hostedSessions.isEmpty && group.starredRecentSessions.isEmpty {
            Divider()
            Button("Remove from Sidebar") { workspace.removeFolder(group.path) }
        }
    }

    @ViewBuilder private func openMenu(_ pane: PCodeSessionPane) -> some View {
        if pane.id != workspace.selected {
            Button {
                workspace.peek.peekFromMenu(workspace.peekTarget(for: pane))
            } label: {
                Label("Peek (⌥ Hover)", systemImage: "eye")
            }
            Divider()
        }
        if let id = pane.sessionID {
            let starred = workspace.isStarred(id)
            Button {
                workspace.toggleStar(id)
            } label: {
                Label(starred ? "Unstar Session" : "Star Session", systemImage: starred ? "star.slash" : "star")
            }
            Divider()
        }
        Button("Close Session") { workspace.requestClose(pane) }
        if pane.sessionID != nil {
            Button("Delete Session…", role: .destructive) { workspace.confirmDelete(pane: pane) }
        }
        Divider()
        Button("Reveal Directory in Finder") { PCodeWorkspace.reveal(pane.directory) }
        if let id = pane.sessionID {
            Button("Copy Session ID") { PCodeWorkspace.copy(id) }
        }
    }

    @ViewBuilder private func hostedMenu(_ hosted: PCodeHostedSession) -> some View {
        Button {
            workspace.peek.peekFromMenu(workspace.peekTarget(for: hosted))
        } label: {
            Label("Peek (⌥ Hover)", systemImage: "eye")
        }
        Button("Attach") { workspace.open(hosted.info) }
        let starred = workspace.isStarred(hosted.id)
        Button {
            workspace.toggleStar(hosted.id)
        } label: {
            Label(starred ? "Unstar Session" : "Star Session", systemImage: starred ? "star.slash" : "star")
        }
        Divider()
        Button("Reveal Directory in Finder") { PCodeWorkspace.reveal(hosted.info.workspace) }
        Button("Copy Session ID") { PCodeWorkspace.copy(hosted.id) }
    }

    @ViewBuilder private func recentMenu(_ info: PCodeSessionInfo) -> some View {
        Button {
            workspace.peek.peekFromMenu(workspace.peekTarget(for: info))
        } label: {
            Label("Peek (⌥ Hover)", systemImage: "eye")
        }
        Button("Open") { workspace.open(info) }
        let starred = workspace.isStarred(info.id)
        Button {
            workspace.toggleStar(info.id)
        } label: {
            Label(starred ? "Unstar Session" : "Star Session", systemImage: starred ? "star.slash" : "star")
        }
        Button("Delete Session…", role: .destructive) { workspace.confirmDelete(info: info) }
        Divider()
        Button("Reveal Directory in Finder") { PCodeWorkspace.reveal(info.workspace) }
        Button("Copy Session ID") { PCodeWorkspace.copy(info.id) }
    }
}

/// One project folder section in the sidebar: auto-fits its sessions until the user drags its bottom divider.
private struct PCodeFolderSectionView<FolderMenu: View, OpenMenu: View, HostedMenu: View, RecentMenu: View>: View {
    @ObservedObject var workspace: PCodeWorkspace
    let group: PCodeProjectGroup
    let now: Date
    let dividerColor: Color
    @ViewBuilder var folderMenu: () -> FolderMenu
    @ViewBuilder var openMenu: (PCodeSessionPane) -> OpenMenu
    @ViewBuilder var hostedMenu: (PCodeHostedSession) -> HostedMenu
    @ViewBuilder var recentMenu: (PCodeSessionInfo) -> RecentMenu

    @State private var measuredHeight: CGFloat = 0
    @State private var dragStartHeight: CGFloat?
    @State private var handleHovered = false

    var body: some View {
        let collapsed = workspace.collapsedFolders.contains(group.path)
        let customHeight = workspace.folderHeights[group.path]

        VStack(alignment: .leading, spacing: 0) {
            PCodeFolderHeader(
                group: group,
                isCollapsed: collapsed,
                hasUnseen: group.hasUnseen(workspace.unseenIDs),
                onToggle: { workspace.toggleCollapsed(group.path) },
                onNewSession: { workspace.newSession(directory: group.path) }
            )
            .padding(.horizontal, 4)
            .contextMenu { folderMenu() }

            if !collapsed {
                if let customHeight {
                    ScrollView(.vertical) {
                        rowsContent
                    }
                    .frame(height: customHeight)
                } else {
                    rowsContent
                        .background(GeometryReader { proxy in
                            Color.clear
                                .onAppear { measuredHeight = proxy.size.height }
                                .onChange(of: proxy.size.height) { measuredHeight = $0 }
                        })
                }

                folderResizeDivider(currentHeight: customHeight ?? measuredHeight)
            }
        }
    }

    @ViewBuilder
    private var rowsContent: some View {
        VStack(alignment: .leading, spacing: 2) {
            // Starred sessions always float to the top of their folder.
            ForEach(group.starredOpenPanes) { pane in
                openPaneRow(pane)
            }
            ForEach(group.starredHostedSessions) { hosted in
                hostedSessionRow(hosted)
            }
            ForEach(group.starredRecentSessions) { info in
                recentSessionRow(info)
            }

            // Unstarred sessions: open (sorted by creation time), detached hosts, then recent saved.
            ForEach(group.unstarredOpenPanes) { pane in
                openPaneRow(pane)
            }
            ForEach(group.unstarredHostedSessions) { hosted in
                hostedSessionRow(hosted)
            }
            // The workspace publishes only the first page or so of each
            // folder's recent sessions; the rest are counted.
            ForEach(group.unstarredRecentSessions) { info in
                recentSessionRow(info)
            }

            if group.remainingRecentCount > 0 {
                let remaining = group.remainingRecentCount
                let next = min(remaining, PCodeWorkspace.recentPageSize)
                Button {
                    workspace.showMoreRecent(group.path)
                } label: {
                    Text(next == remaining ? "Show \(remaining) more…" : "Show \(next) more of \(remaining)…")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                        .padding(.leading, 26)
                        .padding(.vertical, 4)
                }
                .buttonStyle(.plain)
            }

            if group.openPanes.isEmpty && group.hostedSessions.isEmpty && group.recentSessions.isEmpty {
                Button {
                    workspace.newSession(directory: group.path)
                } label: {
                    HStack(spacing: 6) {
                        Image(systemName: "plus")
                            .font(.caption2)
                        Text("New session")
                            .font(.caption)
                    }
                    .foregroundStyle(.secondary)
                    .padding(.leading, 26)
                    .padding(.vertical, 4)
                }
                .buttonStyle(.plain)
            }
        }
        // Stay leading-aligned once a dragged section switches into a ScrollView.
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(.vertical, 2)
    }

    @ViewBuilder
    private func openPaneRow(_ pane: PCodeSessionPane) -> some View {
        let sessionID = pane.sessionID
        let isSelected = pane.id == workspace.selected
        VStack(alignment: .leading, spacing: 0) {
            PCodePaneRow(
                pane: pane,
                info: workspace.info(for: pane),
                now: now,
                isSelected: isSelected,
                isStarred: workspace.isStarred(sessionID),
                onSelect: { workspace.select(pane) },
                onToggleStar: sessionID.map { id in { workspace.toggleStar(id) } },
                onHoverChange: { hovering in
                    guard pane.id != workspace.selected else { return }
                    workspace.peek.rowHoverChanged(workspace.peekTarget(for: pane), hovering: hovering)
                }
            ) { openMenu(pane) }
                .contextMenu { openMenu(pane) }

            if workspace.showSidebarFileChanges {
                PCodeSidebarFileChangesList(
                    changesStore: pane.changesStore,
                    inspectorState: pane.inspectorState,
                    isSelectedPane: isSelected,
                    onSelectFile: { path in
                        workspace.openInspectorFile(path, in: pane)
                    }
                )
            }
        }
    }

    @ViewBuilder
    private func hostedSessionRow(_ hosted: PCodeHostedSession) -> some View {
        PCodeSessionRow(
            title: hosted.title,
            subtitle: PCodeWorkspace.sessionSubtitle(
                worktreeName: hosted.info.worktreeName,
                updatedDate: hosted.updatedDate,
                now: now
            ),
            activity: hosted.activity,
            unseen: hosted.host.unseen || workspace.unseenIDs.contains(hosted.id),
            isSelected: false,
            isStarred: workspace.isStarred(hosted.id),
            onSelect: { workspace.open(hosted.info) },
            onToggleStar: { workspace.toggleStar(hosted.id) },
            onHoverChange: { hovering in
                workspace.peek.rowHoverChanged(workspace.peekTarget(for: hosted), hovering: hovering)
            }
        ) {
            hostedMenu(hosted)
        }
        .contextMenu { hostedMenu(hosted) }
    }

    @ViewBuilder
    private func recentSessionRow(_ info: PCodeSessionInfo) -> some View {
        PCodeSessionRow(
            title: info.label,
            subtitle: PCodeWorkspace.sessionSubtitle(
                worktreeName: info.worktreeName,
                updatedDate: info.updatedDate,
                now: now
            ),
            activity: nil,
            unseen: workspace.unseenIDs.contains(info.id),
            isSelected: false,
            isStarred: workspace.isStarred(info.id),
            onSelect: { workspace.open(info) },
            onToggleStar: { workspace.toggleStar(info.id) },
            onHoverChange: { hovering in
                workspace.peek.rowHoverChanged(workspace.peekTarget(for: info), hovering: hovering)
            }
        ) {
            recentMenu(info)
        }
        .contextMenu { recentMenu(info) }
    }

    private func folderResizeDivider(currentHeight: CGFloat) -> some View {
        ZStack {
            Rectangle()
                .fill(
                    handleHovered || dragStartHeight != nil
                        ? Color.accentColor.opacity(0.6)
                        : dividerColor.opacity(0.6)
                )
                .frame(height: handleHovered || dragStartHeight != nil ? 2 : 1)
                .padding(.horizontal, 4)

            PCodeResizeHandle(
                orientation: .vertical,
                onHover: { handleHovered = $0 },
                onDoubleClick: { workspace.resetFolderHeight(for: group.path) },
                onDragStart: {
                    dragStartHeight = PCodeSidebarLayout.clampedFolderHeight(currentHeight)
                    workspace.isResizingSidebar = true
                },
                onDragChanged: { delta in
                    let start = dragStartHeight ?? PCodeSidebarLayout.clampedFolderHeight(currentHeight)
                    workspace.setFolderHeight(start + delta, for: group.path)
                },
                onDragEnded: {
                    dragStartHeight = nil
                    workspace.isResizingSidebar = false
                }
            )
        }
        .frame(height: 9)
        .help(workspace.folderHeights[group.path] != nil
              ? "Drag to resize section · Double-click to auto-fit"
              : "Drag to adjust section height")
    }
}

/// Whether the workspace window is on screen (not hidden, minimized or fully
/// covered), for views that animate to stop while nobody can see them.
private struct PCodeWindowOnScreenKey: EnvironmentKey {
    static let defaultValue = true
}

extension EnvironmentValues {
    var pcodeWindowOnScreen: Bool {
        get { self[PCodeWindowOnScreenKey.self] }
        set { self[PCodeWindowOnScreenKey.self] = newValue }
    }
}

/// The window's on-screen state, kept apart from `PCodeWorkspace` so a change
/// redraws only what reads `pcodeWindowOnScreen`.
final class PCodeWindowVisibility: ObservableObject {
    @Published var onScreen = true
}

/// Hands `PCodeWindowVisibility` down as `pcodeWindowOnScreen`.
struct PCodeWindowVisibilityReader<Content: View>: View {
    @ObservedObject var visibility: PCodeWindowVisibility
    let content: Content

    var body: some View {
        content.environment(\.pcodeWindowOnScreen, visibility.onScreen)
    }
}

/// A turn running: a soft accent center dot with a thin orbiting arc, distinct
/// in both color and motion from settled unseen (`Color.green`) and failed
/// (`Color.red`) dots. Fills its frame; the center dot is 40% of it (~4.8pt in
/// a 12pt slot) so the orbiting arc reads cleanly.
private struct PCodeRunningDot: View {
    private static let period = 1.5
    private static let lineWidth: CGFloat = 1.2
    @Environment(\.pcodeWindowOnScreen) private var onScreen

    var body: some View {
        GeometryReader { geometry in
            let side = min(geometry.size.width, geometry.size.height)
            ZStack {
                // 30 fps is plenty for a 12pt arc, and keeps the sidebar from
                // redrawing at the terminal's 120.
                // Paused while the window is hidden or covered: nobody sees it.
                TimelineView(.animation(minimumInterval: 1.0 / 30, paused: !onScreen)) { context in
                    let t = context.date.timeIntervalSinceReferenceDate
                    let turns = t.truncatingRemainder(dividingBy: Self.period) / Self.period
                    Circle()
                        .trim(from: 0, to: 0.32)
                        .stroke(
                            AngularGradient(
                                gradient: Gradient(stops: [
                                    .init(color: Color.accentColor.opacity(0), location: 0),
                                    .init(color: Color.accentColor, location: 0.32),
                                    // Fade back out past the head, so the tail's
                                    // round cap (just behind 0) isn't solid.
                                    .init(color: Color.accentColor.opacity(0), location: 1),
                                ]),
                                center: .center
                            ),
                            style: StrokeStyle(lineWidth: Self.lineWidth, lineCap: .round)
                        )
                        .padding(Self.lineWidth / 2)
                        .rotationEffect(.radians(turns * 2 * .pi))
                }
                Circle()
                    .fill(Color.accentColor.opacity(0.85))
                    .frame(width: side * 0.4, height: side * 0.4)
            }
            .frame(width: side, height: side)
            .frame(maxWidth: .infinity, maxHeight: .infinity)
        }
        .help("Running")
    }
}

/// AppKit-backed resize handle with reliable cursor tracking (`resetCursorRects` + `cursorUpdate`)
/// and screen-space dragging so sibling `NSView`s (like Ghostty's terminal) never steal the cursor.
struct PCodeResizeHandle: NSViewRepresentable {
    enum Orientation {
        case horizontal
        case vertical
    }

    let orientation: Orientation
    var onHover: ((Bool) -> Void)?
    var onDoubleClick: (() -> Void)?
    var onDragStart: (() -> Void)?
    var onDragChanged: ((CGFloat) -> Void)?
    var onDragEnded: (() -> Void)?

    func makeNSView(context: Context) -> HandleView {
        let view = HandleView()
        updateNSView(view, context: context)
        return view
    }

    func updateNSView(_ view: HandleView, context: Context) {
        view.orientation = orientation
        view.onHover = onHover
        view.onDoubleClick = onDoubleClick
        view.onDragStart = onDragStart
        view.onDragChanged = onDragChanged
        view.onDragEnded = onDragEnded
    }

    static func dismantleNSView(_ view: HandleView, coordinator: ()) {
        view.reset()
    }

    final class HandleView: NSView {
        var orientation: Orientation = .horizontal {
            didSet {
                if orientation != oldValue { window?.invalidateCursorRects(for: self) }
            }
        }
        var onHover: ((Bool) -> Void)?
        var onDoubleClick: (() -> Void)?
        var onDragStart: (() -> Void)?
        var onDragChanged: ((CGFloat) -> Void)?
        var onDragEnded: (() -> Void)?

        private var trackingArea: NSTrackingArea?
        private var dragOrigin: NSPoint?
        private var isHovered = false
        private var resignObserver: NSObjectProtocol?

        deinit {
            if let resignObserver { NotificationCenter.default.removeObserver(resignObserver) }
        }

        /// Ends any in-flight hover or drag. Called when the view leaves its window, is dismantled,
        /// or the window resigns key mid-drag (so `mouseUp` never arrives), so callers' drag guards
        /// and highlight state never get stuck.
        func reset() {
            if dragOrigin != nil {
                dragOrigin = nil
                onDragEnded?()
            }
            if isHovered {
                isHovered = false
                onHover?(false)
                NSCursor.arrow.set()
            }
        }

        override func viewWillMove(toWindow newWindow: NSWindow?) {
            if newWindow !== window {
                reset()
                if let resignObserver {
                    NotificationCenter.default.removeObserver(resignObserver)
                    self.resignObserver = nil
                }
            }
            super.viewWillMove(toWindow: newWindow)
        }

        override func viewDidMoveToWindow() {
            super.viewDidMoveToWindow()
            guard let window, resignObserver == nil else { return }
            resignObserver = NotificationCenter.default.addObserver(
                forName: NSWindow.didResignKeyNotification,
                object: window,
                queue: .main
            ) { [weak self] _ in
                MainActor.assumeIsolated { self?.reset() }
            }
        }

        private func setHovered(_ hovered: Bool) {
            guard hovered != isHovered else { return }
            isHovered = hovered
            onHover?(hovered)
        }

        private var cursor: NSCursor {
            orientation == .horizontal ? .resizeLeftRight : .resizeUpDown
        }

        override func resetCursorRects() {
            super.resetCursorRects()
            addCursorRect(bounds, cursor: cursor)
        }

        override func updateTrackingAreas() {
            super.updateTrackingAreas()
            if let trackingArea { removeTrackingArea(trackingArea) }
            let area = NSTrackingArea(
                rect: .zero,
                options: [.mouseEnteredAndExited, .cursorUpdate, .activeInKeyWindow, .inVisibleRect],
                owner: self,
                userInfo: nil
            )
            addTrackingArea(area)
            trackingArea = area
        }

        override func cursorUpdate(with event: NSEvent) {
            cursor.set()
        }

        override func mouseEntered(with event: NSEvent) {
            cursor.set()
            setHovered(true)
        }

        override func mouseExited(with event: NSEvent) {
            if dragOrigin == nil {
                setHovered(false)
                NSCursor.arrow.set()
            }
        }

        override func mouseDown(with event: NSEvent) {
            if event.clickCount == 2 {
                onDoubleClick?()
                return
            }
            dragOrigin = NSEvent.mouseLocation
            cursor.set()
            onDragStart?()
        }

        override func mouseDragged(with event: NSEvent) {
            guard let origin = dragOrigin else { return }
            let current = NSEvent.mouseLocation
            let delta: CGFloat = orientation == .horizontal
                ? current.x - origin.x
                : origin.y - current.y
            cursor.set()
            onDragChanged?(delta)
        }

        override func mouseUp(with event: NSEvent) {
            guard dragOrigin != nil else { return }
            dragOrigin = nil
            onDragEnded?()
            let local = convert(event.locationInWindow, from: nil)
            if !bounds.contains(local) {
                setHovered(false)
                NSCursor.arrow.set()
            }
        }
    }
}

/// A project folder header in the sidebar: disclosure chevron, name, live status dots, and hover + button.
private struct PCodeFolderHeader: View {
    let group: PCodeProjectGroup
    let isCollapsed: Bool
    let hasUnseen: Bool
    let onToggle: () -> Void
    let onNewSession: () -> Void
    @State private var hovering = false

    var body: some View {
        HStack(spacing: 5) {
            Button(action: onToggle) {
                HStack(spacing: 5) {
                    Image(systemName: "chevron.right")
                        .font(.system(size: 9, weight: .semibold))
                        .foregroundStyle(.secondary)
                        .rotationEffect(.degrees(isCollapsed ? 0 : 90))
                        .frame(width: 10)
                    Image(systemName: isCollapsed ? "folder" : "folder.fill")
                        .font(.system(size: 11))
                        .foregroundStyle(.secondary)
                    Text(group.name)
                        .font(.subheadline.weight(.semibold))
                        .foregroundStyle(.primary)
                        .lineLimit(1)
                        .truncationMode(.tail)
                    Spacer(minLength: 4)
                }
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .help(group.abbreviatedPath)

            if group.hasRunning || hasUnseen || group.hasFailed {
                HStack(spacing: 4) {
                    if group.hasRunning {
                        PCodeRunningDot().frame(width: 12, height: 12)
                    }
                    if hasUnseen {
                        Circle()
                            .fill(Color.green)
                            .frame(width: 6, height: 6)
                            .help("New response")
                    }
                    if group.hasFailed {
                        Circle()
                            .fill(Color.red)
                            .frame(width: 6, height: 6)
                            .help("Failed turn")
                    }
                }
            }

            Button(action: onNewSession) {
                Image(systemName: "plus")
                    .font(.system(size: 11, weight: .medium))
                    .foregroundStyle(.secondary)
                    .frame(width: 18, height: 18)
                    .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .opacity(hovering ? 1 : 0)
            .help("New session in \(group.name)")
        }
        .textCase(nil)
        .padding(.vertical, 4)
        .contentShape(Rectangle())
        .onHover { hovering = $0 }
    }
}

private struct PCodePaneRow<Actions: View>: View {
    @ObservedObject var pane: PCodeSessionPane
    var info: PCodeSessionInfo?
    var now: Date = Date()
    var isSelected = false
    var isStarred = false
    var onSelect: () -> Void
    var onToggleStar: (() -> Void)? = nil
    var onHoverChange: ((Bool) -> Void)? = nil
    @ViewBuilder var actions: () -> Actions

    var body: some View {
        let title = PCodeWorkspace.displayTitle(paneTitle: pane.title, savedLabel: pane.savedLabel(saved: info))
        let worktree = info?.worktreeName ?? pane.worktreeName
        let responseDate = pane.lastResponseDate(saved: info)
        PCodeSessionRow(
            title: title,
            subtitle: PCodeWorkspace.sessionSubtitle(worktreeName: worktree, updatedDate: responseDate, now: now),
            activity: pane.activity,
            unseen: pane.unseen,
            isSelected: isSelected,
            isStarred: isStarred,
            changesStore: pane.changesStore,
            onSelect: onSelect,
            onToggleStar: onToggleStar,
            onHoverChange: onHoverChange,
            actions: actions)
    }
}

/// A sidebar row; its actions are a click to select, star button, right-click menu, and hover … button.
private struct PCodeSessionRow<Actions: View>: View {
    var title: String
    var subtitle: String?
    /// Nil for a session not open here.
    var activity: PCodeSessionPane.Activity?
    /// A finished response not yet looked at here.
    var unseen = false
    var isSelected = false
    var isStarred = false
    var changesStore: PCodeSessionChangesStore? = nil
    var onSelect: () -> Void
    var onToggleStar: (() -> Void)? = nil
    var onHoverChange: ((Bool) -> Void)? = nil
    @ViewBuilder var actions: () -> Actions
    @State private var hovering = false

    var body: some View {
        HStack(spacing: 8) {
            PCodeActivityIndicator(activity: activity, unseen: unseen)
                .frame(width: 12, height: 12)
            VStack(alignment: .leading, spacing: 1) {
                Text(title).lineLimit(1).truncationMode(.tail)
                if let subtitle, !subtitle.isEmpty {
                    Text(subtitle).font(.caption).foregroundStyle(.secondary).lineLimit(1)
                }
            }
            Spacer(minLength: 4)
            if let changesStore, !hovering {
                PCodeSidebarSessionChangeStats(changesStore: changesStore)
            }
            if let onToggleStar, isStarred || hovering {
                Button(action: onToggleStar) {
                    Image(systemName: isStarred ? "star.fill" : "star")
                        .font(.system(size: 10))
                        .foregroundStyle(isStarred ? Color.yellow : Color.secondary)
                        .frame(width: 16, height: 16)
                        .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
                .help(isStarred ? "Unstar session" : "Star session")
            }
            Menu { actions() } label: { Image(systemName: "ellipsis.circle") }
                .menuStyle(.borderlessButton)
                .menuIndicator(.hidden)
                .fixedSize()
                .opacity(hovering ? 1 : 0)
                .help("Session actions")
        }
        .padding(.horizontal, 8)
        .padding(.vertical, 4)
        .background(
            RoundedRectangle(cornerRadius: 6)
                .fill(
                    isSelected
                        ? Color.accentColor.opacity(0.22)
                        : (hovering ? Color.primary.opacity(0.06) : Color.clear)
                )
        )
        .contentShape(Rectangle())
        .onTapGesture { onSelect() }
        .onHover { inside in
            hovering = inside
            onHoverChange?(inside)
        }
    }
}

/// Owns the local keyDown monitor for `PCodeProjectLauncher` so it cleans up on teardown.
private final class PCodePickerKeyMonitor: ObservableObject {
    private var monitor: Any?
    var handler: ((NSEvent) -> NSEvent?)?

    func start() {
        stop()
        monitor = NSEvent.addLocalMonitorForEvents(matching: .keyDown) { [weak self] event in
            self?.handler?(event) ?? event
        }
    }

    func stop() {
        if let monitor {
            NSEvent.removeMonitor(monitor)
            self.monitor = nil
        }
    }

    deinit { stop() }
}

/// Unified Project Command Center: rendered inline as the empty-state launcher
/// when no session is open, or as a floating ⌘⇧N overlay over an active session.
///
/// - Fuzzy-filters known projects, or lists filesystem subdirectories when typing `/` or `~/`.
/// - `Tab` completes filesystem paths (and drills into a selected project's subdirectories).
/// - `↩` starts a new session in the selected project.
/// - `⌘↩` pins/adds the selected project to the sidebar without starting a session.
/// - `⌘O` opens the macOS folder chooser (`NSOpenPanel`); folders can also be dropped onto the card.
private struct PCodeProjectLauncher: View {
    enum Mode {
        case inline
        case overlay
    }

    @EnvironmentObject var ghostty: Ghostty.App
    @ObservedObject var workspace: PCodeWorkspace
    let mode: Mode

    @StateObject private var keyMonitor = PCodePickerKeyMonitor()
    @State private var query = ""
    @State private var cachedPathSuggestions: [String] = []
    @State private var selectedIndex = 0
    @State private var hoveredID: String?
    @State private var isDropTargeted = false
    @State private var statusBanner: String?
    @State private var statusClearWork: DispatchWorkItem?
    @FocusState private var searchFocused: Bool

    private var filteredCandidates: [PCodeDirectoryCandidate] {
        let base = workspace.pickerCandidates
        let trimmed = query.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return base }

        if PCodePathCompleter.isPathQuery(query) {
            let knownByPath = Dictionary(base.map { ($0.path, $0) }, uniquingKeysWith: { a, _ in a })
            let home = PCodeWorkspace.normalizePath(NSHomeDirectory())
            var result: [PCodeDirectoryCandidate] = []
            var seen: Set<String> = []

            for path in cachedPathSuggestions where seen.insert(path).inserted {
                if let known = knownByPath[path] {
                    result.append(known)
                } else {
                    let last = (path as NSString).lastPathComponent
                    result.append(PCodeDirectoryCandidate(
                        path: path,
                        name: path == home ? "~" : (last.isEmpty ? path : last),
                        abbreviatedPath: PCodeWorkspace.abbreviatePath(path),
                        openCount: 0,
                        recentCount: 0,
                        isCurrent: false,
                        isPinned: false,
                        isRawPath: true))
                }
            }
            return result
        }

        let terms = PCodePickMatch.terms(trimmed)
        return base.filter { candidate in
            let texts = [
                PCodePickMatch.casefold(candidate.name),
                PCodePickMatch.casefold(candidate.abbreviatedPath),
                PCodePickMatch.casefold(candidate.path),
            ]
            return PCodePickMatch.matches(texts, terms: terms, fuzzy: true)
        }
    }

    private func refreshPathSuggestions(for newQuery: String) {
        if PCodePathCompleter.isPathQuery(newQuery) {
            cachedPathSuggestions = PCodePathCompleter.directorySuggestions(for: newQuery)
        } else if !cachedPathSuggestions.isEmpty {
            cachedPathSuggestions = []
        }
    }

    private var cardBackground: Color {
        Color(nsColor: NSColor(ghostty.config.backgroundColor).blended(
            withFraction: 0.08, of: .white) ?? .windowBackgroundColor)
    }

    var body: some View {
        let items = filteredCandidates
        let activeIndex = items.isEmpty ? 0 : min(selectedIndex, items.count - 1)

        Group {
            switch mode {
            case .overlay:
                ZStack(alignment: .top) {
                    Color.black.opacity(0.32)
                        .ignoresSafeArea()
                        .onTapGesture { workspace.dismissDirectoryPicker() }

                    launcherCard(items: items, activeIndex: activeIndex)
                        .frame(width: 500)
                        .shadow(color: .black.opacity(0.35), radius: 20, y: 10)
                        .padding(.top, 72)
                }

            case .inline:
                VStack(spacing: 18) {
                    VStack(spacing: 4) {
                        Text("Open a project")
                            .font(.title2.weight(.semibold))
                            .foregroundStyle(.primary)
                        Text("Press ↩ to start a session, type a path with Tab completion, or drop a folder here.")
                            .font(.callout)
                            .foregroundStyle(.secondary)
                            .multilineTextAlignment(.center)
                    }
                    .padding(.horizontal, 24)

                    launcherCard(items: items, activeIndex: activeIndex)
                        .frame(maxWidth: 520)
                        .shadow(color: .black.opacity(0.22), radius: 14, y: 6)
                }
                .padding(.horizontal, 32)
                .padding(.vertical, 36)
                .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .center)
            }
        }
        .onChange(of: query) { newQuery in
            refreshPathSuggestions(for: newQuery)
            selectedIndex = 0
        }
        .onChange(of: items.count) { count in
            if count == 0 {
                selectedIndex = 0
            } else if selectedIndex >= count {
                selectedIndex = count - 1
            }
        }
        .onChange(of: workspace.launcherFocusTrigger) { _ in
            if mode == .inline {
                DispatchQueue.main.async { searchFocused = true }
            }
        }
        .onAppear {
            refreshPathSuggestions(for: query)
            selectedIndex = 0
            DispatchQueue.main.async { searchFocused = true }
            keyMonitor.handler = handleKeyEvent
            keyMonitor.start()
        }
        .onDisappear {
            statusClearWork?.cancel()
            statusClearWork = nil
            keyMonitor.stop()
            keyMonitor.handler = nil
        }
    }

    private func launcherCard(items: [PCodeDirectoryCandidate], activeIndex: Int) -> some View {
        VStack(spacing: 0) {
            HStack(spacing: 8) {
                Image(systemName: "magnifyingglass")
                    .foregroundStyle(.secondary)
                TextField("Filter projects or type a path…", text: $query)
                    .textFieldStyle(.plain)
                    .font(.body)
                    .focused($searchFocused)
                    .onSubmit {
                        let currentItems = filteredCandidates
                        let idx = currentItems.isEmpty ? 0 : min(selectedIndex, currentItems.count - 1)
                        if currentItems.indices.contains(idx) {
                            selectCandidate(currentItems[idx])
                        }
                    }
                if !query.isEmpty {
                    Button {
                        query = ""
                        searchFocused = true
                    } label: {
                        Image(systemName: "xmark.circle.fill")
                            .foregroundStyle(.secondary)
                    }
                    .buttonStyle(.plain)
                }
            }
            .padding(.horizontal, 14)
            .padding(.vertical, 12)

            Divider()

            if items.isEmpty {
                VStack(spacing: 6) {
                    Text("No matching projects")
                        .font(.callout)
                        .foregroundStyle(.secondary)
                    Text("Type a path like ~/p/app (Tab to complete) or press ⌘O to browse.")
                        .font(.caption)
                        .foregroundStyle(.tertiary)
                }
                .frame(maxWidth: .infinity)
                .padding(.vertical, 28)
            } else {
                ScrollViewReader { proxy in
                    ScrollView {
                        LazyVStack(spacing: 2) {
                            ForEach(Array(items.enumerated()), id: \.element.id) { index, item in
                                candidateRow(item, isSelected: index == activeIndex)
                                    .id(item.id)
                                    .onHover { inside in
                                        hoveredID = inside ? item.id : (hoveredID == item.id ? nil : hoveredID)
                                    }
                                    .onTapGesture {
                                        selectCandidate(item)
                                    }
                            }
                        }
                        .padding(6)
                    }
                    .frame(maxHeight: mode == .inline ? 340 : 280)
                    .onChange(of: activeIndex) { newIndex in
                        if items.indices.contains(newIndex) {
                            proxy.scrollTo(items[newIndex].id, anchor: .center)
                        }
                    }
                }
            }

            Divider()

            HStack(spacing: 10) {
                if let statusBanner {
                    HStack(spacing: 5) {
                        Image(systemName: "pin.fill")
                            .font(.caption2)
                            .foregroundStyle(Color.accentColor)
                        Text(statusBanner)
                            .foregroundStyle(.primary)
                            .lineLimit(1)
                    }
                } else {
                    Text("↩ session")
                    Text("⌘↩ pin")
                    Text("⇥ complete")
                    if mode == .overlay {
                        Text("esc close")
                    }
                }
                Spacer(minLength: 8)
                Button {
                    browseFromPicker()
                } label: {
                    HStack(spacing: 4) {
                        Image(systemName: "folder")
                        Text("Browse… (⌘O)")
                    }
                }
                .buttonStyle(.plain)
                .foregroundStyle(.primary)
                .keyboardShortcut("o", modifiers: .command)
            }
            .font(.caption)
            .foregroundStyle(.secondary)
            .padding(.horizontal, 14)
            .padding(.vertical, 8)
            .background {
                if mode == .overlay {
                    Button("") { workspace.dismissDirectoryPicker() }
                        .keyboardShortcut(.cancelAction)
                        .opacity(0)
                        .frame(width: 0, height: 0)
                }
            }
        }
        .background(cardBackground)
        .clipShape(RoundedRectangle(cornerRadius: 10, style: .continuous))
        .overlay(
            RoundedRectangle(cornerRadius: 10, style: .continuous)
                .stroke(
                    isDropTargeted ? Color.accentColor.opacity(0.85) : Color.primary.opacity(0.14),
                    lineWidth: isDropTargeted ? 2 : 1
                )
        )
        .onDrop(of: [.fileURL], isTargeted: $isDropTargeted) { providers in
            handleDrop(providers)
        }
        .onTapGesture { searchFocused = true }
    }

    private func browseFromPicker() {
        workspace.browseForFolder(startSession: true) {
            searchFocused = true
        }
    }

    private func candidateRow(_ item: PCodeDirectoryCandidate, isSelected: Bool) -> some View {
        let showPinButton = (isSelected || hoveredID == item.id) && !item.isPinned
        return HStack(spacing: 10) {
            Image(systemName: item.isRawPath ? "folder.badge.plus" : "folder.fill")
                .font(.system(size: 13))
                .foregroundStyle(isSelected ? Color.accentColor : .secondary)
                .frame(width: 18)

            VStack(alignment: .leading, spacing: 1) {
                HStack(spacing: 6) {
                    Text(item.name)
                        .font(.body.weight(.medium))
                        .lineLimit(1)
                    if item.isCurrent {
                        Text("current")
                            .font(.caption2)
                            .foregroundStyle(.secondary)
                            .padding(.horizontal, 5)
                            .padding(.vertical, 1)
                            .background(Color.primary.opacity(0.08))
                            .clipShape(Capsule())
                    } else if item.isPinned {
                        Image(systemName: "pin.fill")
                            .font(.system(size: 9))
                            .foregroundStyle(.secondary)
                            .help("Pinned in sidebar")
                    }
                }
                Text(item.abbreviatedPath)
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
                    .truncationMode(.middle)
            }

            Spacer(minLength: 8)

            if showPinButton {
                Button {
                    pinCandidate(item)
                } label: {
                    Image(systemName: "pin")
                        .font(.system(size: 11))
                        .foregroundStyle(.secondary)
                        .frame(width: 22, height: 22)
                        .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
                .help("Add project to sidebar (⌘↩)")
            }

            if item.isRawPath {
                Text("Open path")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            } else {
                let parts = [
                    item.openCount > 0 ? "\(item.openCount) open" : nil,
                    item.recentCount > 0 ? "\(item.recentCount) saved" : nil,
                ].compactMap { $0 }
                if !parts.isEmpty {
                    Text(parts.joined(separator: " · "))
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
            }
        }
        .padding(.horizontal, 10)
        .padding(.vertical, 6)
        .background(
            RoundedRectangle(cornerRadius: 6, style: .continuous)
                .fill(isSelected ? Color.accentColor.opacity(0.18) : Color.clear)
        )
        .contentShape(Rectangle())
    }

    private func selectCandidate(_ item: PCodeDirectoryCandidate) {
        workspace.addFolder(item.path)
        workspace.newSession(directory: item.path)
    }

    private func pinCandidate(_ item: PCodeDirectoryCandidate) {
        if workspace.addFolder(item.path) {
            showStatus("Pinned \(item.name) to sidebar")
        }
        searchFocused = true
    }

    private func showStatus(_ message: String) {
        statusClearWork?.cancel()
        statusBanner = message
        let work = DispatchWorkItem {
            statusBanner = nil
            statusClearWork = nil
        }
        statusClearWork = work
        DispatchQueue.main.asyncAfter(deadline: .now() + 2.0, execute: work)
    }

    private func completePath() {
        let items = filteredCandidates
        let idx = items.isEmpty ? 0 : min(selectedIndex, items.count - 1)
        let selectedPath = items.indices.contains(idx) ? items[idx].path : nil
        guard let result = PCodePathCompleter.complete(query: query, selectedPath: selectedPath) else {
            searchFocused = true
            return
        }
        query = result.completed
        refreshPathSuggestions(for: result.completed)
        searchFocused = true
        let cursorLocation = result.completed.utf16.count
        DispatchQueue.main.async {
            if let editor = workspace.window?.firstResponder as? NSTextView {
                let safeLocation = min(cursorLocation, (editor.string as NSString).length)
                editor.setSelectedRange(NSRange(location: safeLocation, length: 0))
            }
        }
    }

    private func handleDrop(_ providers: [NSItemProvider]) -> Bool {
        var accepted = false
        for provider in providers where provider.hasItemConformingToTypeIdentifier(UTType.fileURL.identifier) {
            accepted = true
            provider.loadItem(forTypeIdentifier: UTType.fileURL.identifier, options: nil) { item, _ in
                let url: URL?
                if let data = item as? Data {
                    url = URL(dataRepresentation: data, relativeTo: nil)
                } else if let direct = item as? URL {
                    url = direct
                } else {
                    url = nil
                }
                guard let url else { return }
                let path = url.standardizedFileURL.path
                var isDir: ObjCBool = false
                guard FileManager.default.fileExists(atPath: path, isDirectory: &isDir), isDir.boolValue else {
                    return
                }
                DispatchQueue.main.async {
                    workspace.addFolder(path)
                    workspace.newSession(directory: path)
                }
            }
            break
        }
        return accepted
    }

    private func handleKeyEvent(_ event: NSEvent) -> NSEvent? {
        // Let modal sheets (like NSOpenPanel), Peek, and other windows handle their own keys.
        guard event.window === workspace.window,
              event.window?.attachedSheet == nil,
              !workspace.peek.isShowing
        else { return event }
        // In inline mode, only intercept while the launcher search field has focus
        // (so keys typed into a session's terminal stay untouched).
        if mode == .inline && (workspace.showingDirectoryPicker || workspace.selectedPane != nil || !searchFocused) {
            return event
        }
        // Don't hijack keys during IME composition.
        if let client = event.window?.firstResponder as? NSTextInputClient, client.hasMarkedText() {
            return event
        }
        let flags = event.modifierFlags.intersection([.command, .option, .control, .shift])
        let chars = event.charactersIgnoringModifiers?.lowercased()

        // Bare Escape
        if flags.isEmpty && event.keyCode == 53 {
            if !query.isEmpty {
                query = ""
                searchFocused = true
                return nil
            }
            if mode == .overlay {
                workspace.dismissDirectoryPicker()
                return nil
            }
            return event
        }
        // ⌘O -> browse via NSOpenPanel
        if flags == .command && chars == "o" {
            browseFromPicker()
            return nil
        }
        // Bare Up arrow or Ctrl+P
        if (flags.isEmpty && event.keyCode == 126) || (flags == .control && chars == "p") {
            let items = filteredCandidates
            if !items.isEmpty {
                let idx = min(selectedIndex, items.count - 1)
                selectedIndex = (idx - 1 + items.count) % items.count
            }
            return nil
        }
        // Bare Down arrow or Ctrl+N
        if (flags.isEmpty && event.keyCode == 125) || (flags == .control && chars == "n") {
            let items = filteredCandidates
            if !items.isEmpty {
                let idx = min(selectedIndex, items.count - 1)
                selectedIndex = (idx + 1) % items.count
            }
            return nil
        }
        // ⌘Return / ⌘Keypad Enter -> pin/add project to sidebar without starting a session
        if flags == .command && (event.keyCode == 36 || event.keyCode == 76) {
            let items = filteredCandidates
            let idx = items.isEmpty ? 0 : min(selectedIndex, items.count - 1)
            if items.indices.contains(idx) {
                pinCandidate(items[idx])
            }
            return nil
        }
        // Bare Return / Keypad Enter -> start new session in selected project
        if flags.isEmpty && (event.keyCode == 36 || event.keyCode == 76) {
            let items = filteredCandidates
            let idx = items.isEmpty ? 0 : min(selectedIndex, items.count - 1)
            if items.indices.contains(idx) {
                selectCandidate(items[idx])
            }
            return nil
        }
        // Bare Tab -> filesystem path completion (or drill into selected project)
        if flags.isEmpty && event.keyCode == 48 {
            completePath()
            return nil
        }
        return event
    }
}

/// Open and idle: a grey dot. A turn running: an accent dot with an orbiting
/// arc. Failed: red. A response not yet looked at: solid green, open or not.
private struct PCodeActivityIndicator: View {
    var activity: PCodeSessionPane.Activity?
    var unseen = false

    var body: some View {
        switch activity {
        case .running?:
            PCodeRunningDot()
        case .failed?:
            Circle().fill(Color.red).frame(width: 6, height: 6)
        case _ where unseen:
            Circle().fill(Color.green).frame(width: 6, height: 6)
                .help("New response")
        case .idle?:
            Circle().fill(Color.secondary.opacity(0.7)).frame(width: 6, height: 6)
        case nil:
            Color.clear
        }
    }
}

/// The pane's boot cover. A resumed session's is sized like the dock card, from
/// the same cell height and rows the transcript reserves room for, so the two
/// always agree and the card's arrival moves nothing.
private struct PCodePaneStartupOverlay: View {
    @ObservedObject var pane: PCodeSessionPane
    @ObservedObject var surface: Ghostty.SurfaceView
    @ObservedObject var dock: PCodeDock

    var body: some View {
        let cell = surface.cellSize.height > 0 ? surface.cellSize.height : 18
        PCodeStartupOverlay(
            isReady: pane.isReady,
            label: (pane.projectPath as NSString).lastPathComponent,
            resuming: pane.resuming,
            compactCardHeight: pane.resuming ? PCodeDock.cardHeight(rows: dock.rows, cell: cell) : nil)
    }
}

/// A session: pcode's terminal with everything pcode lays over it, and the bottom shell.
struct PCodeSessionPaneView: View {
    @ObservedObject var pane: PCodeSessionPane
    var showInspector = true
    /// False for a live peek: a native composer stays with the pane that owns it.
    var interactive = true
    @EnvironmentObject var ghostty: Ghostty.App
    /// The shell's height, set by dragging its top edge; shared by every session.
    @AppStorage("PCodeWorkspaceShellHeight") private var shellHeight: Double = 200
    @State private var dragStart: Double?
    private static let minShell: CGFloat = 80
    /// pcode's own pane keeps at least this much above the shell.
    private static let minSession: CGFloat = 200

    var body: some View {
        if showInspector {
            PCodeInspectorSplit(pane: pane) {
                paneContent
            }
        } else {
            paneContent
        }
    }

    private var paneContent: some View {
        GeometryReader { geometry in
            let maxShell = max(Self.minShell, geometry.size.height - Self.minSession)
            VStack(spacing: 0) {
                ZStack {
                    if let surface = pane.surface {
                        Ghostty.InspectableSurface(surfaceView: surface, isSplit: false)
                        PCodeSurfaceOverlay(surfaceView: surface)
                        PCodePaneStartupOverlay(pane: pane, surface: surface, dock: surface.pcodeDock)
                    } else if let composer = pane.composer {
                        PCodeNativeSessionPaneContent(pane: pane, session: composer, interactive: interactive)
                    }
                }
                if pane.shellVisible, let shell = pane.shell {
                    handle(maxShell: maxShell)
                    Ghostty.SurfaceWrapper(surfaceView: shell, isSplit: false)
                        .id(ObjectIdentifier(shell))
                        .frame(height: min(maxShell, max(Self.minShell, shellHeight)))
                }
            }
        }
    }

    /// The shell's top edge: a hairline to see, a taller strip to drag.
    private func handle(maxShell: CGFloat) -> some View {
        Rectangle()
            .fill(ghostty.config.splitDividerColor)
            .frame(height: 1)
            .overlay(
                Color.clear
                    .frame(height: 9)
                    .contentShape(Rectangle())
                    .backport.pointerStyle(.resizeUpDown)
                    .onHover { inside in
                        if #available(macOS 15, *) { return }
                        if inside { NSCursor.resizeUpDown.push() } else { NSCursor.pop() }
                    }
                    .gesture(
                        DragGesture(coordinateSpace: .global)
                            .onChanged { gesture in
                                let start = dragStart ?? shellHeight
                                dragStart = start
                                shellHeight = min(maxShell, max(Self.minShell, start - gesture.translation.height))
                            }
                            .onEnded { _ in dragStart = nil }
                    )
            )
            .zIndex(1)
    }
}

/// Custom opaque top title bar that sits in the window's traffic-light strip
/// (`.fullSizeContentView` + hidden system title, no NSToolbar / no glass).
/// Shows the active session's activity indicator, title, and project/worktree pill
/// on the left, with model, context usage, and subscription quota pills on the right.
private struct PCodeWindowTitleBar: View {
    @EnvironmentObject var ghostty: Ghostty.App
    @ObservedObject var workspace: PCodeWorkspace
    @Environment(\.controlActiveState) private var controlActiveState

    private var leadingClearance: CGFloat {
        workspace.isFullScreen ? 16 : workspace.trafficLightClearance
    }

    private var titlebarColorScheme: ColorScheme {
        NSColor(ghostty.config.backgroundColor).isLightColor ? .light : .dark
    }

    var body: some View {
        ZStack(alignment: .bottom) {
            ghostty.config.backgroundColor

            Group {
                if let pane = workspace.selectedPane {
                    PCodeActiveSessionTitleContent(
                        pane: pane,
                        workspace: workspace,
                        usageStore: workspace.usageStore,
                        leadingClearance: leadingClearance
                    )
                    .id(pane.id)
                } else {
                    HStack {
                        Spacer(minLength: leadingClearance)
                        Text("pcode")
                            .font(.system(size: 12.5, weight: .medium))
                            .foregroundStyle(.secondary)
                        Spacer(minLength: 16)
                    }
                    .onAppear { workspace.window?.title = "pcode" }
                }
            }
            .opacity(controlActiveState == .inactive ? 0.55 : 1)
            .frame(maxWidth: .infinity, maxHeight: .infinity)

            Rectangle()
                .fill(ghostty.config.splitDividerColor.opacity(0.65))
                .frame(height: 1)
        }
        .environment(\.colorScheme, titlebarColorScheme)
        .frame(height: workspace.titlebarHeight)
        .allowsHitTesting(false)
    }
}

/// Observes the active pane directly so title (`/rename`, auto-title) and activity
/// changes refresh the title bar without re-rendering the whole workspace.
private struct PCodeActiveSessionTitleContent: View {
    @ObservedObject var pane: PCodeSessionPane
    @ObservedObject var workspace: PCodeWorkspace
    @ObservedObject var usageStore: PCodeUsageStore
    @Environment(\.controlActiveState) private var controlActiveState
    let leadingClearance: CGFloat

    @State private var wasRunning = false
    @State private var usageClock = Date()

    private static let usageRefreshTimer = Timer.publish(every: 60, on: .main, in: .common).autoconnect()

    private var savedInfo: PCodeSessionInfo? {
        workspace.info(for: pane)
    }

    private var title: String {
        workspace.displayTitle(for: pane)
    }

    private var projectName: String {
        workspace.projectName(for: pane)
    }

    private var worktreeName: String? {
        savedInfo?.worktreeName ?? pane.worktreeName
    }

    private var modelParts: (provider: String?, name: String) {
        PCodeSessionMetadataDisplay.splitModel(pane.modelName)
    }

    private var effortLabel: String? {
        PCodeSessionMetadataDisplay.normalizedEffort(pane.modelEffort)
    }

    private var contextRatio: Double? {
        PCodeSessionMetadataDisplay.contextFillRatio(pane.contextUsage)
    }

    private var contextAccentColor: Color {
        guard let ratio = contextRatio else { return .secondary }
        if ratio >= 0.90 { return .red.opacity(0.9) }
        if ratio >= 0.75 { return .orange.opacity(0.9) }
        return .secondary
    }

    private var usageWindows: [PCodeUsageWindow] {
        usageStore.windows(forModel: pane.modelName)
    }

    private func usageAccentColor(for usedPercent: Int) -> Color {
        if usedPercent >= 90 { return .red.opacity(0.9) }
        if usedPercent >= 75 { return .orange.opacity(0.9) }
        return .secondary
    }

    var body: some View {
        HStack(spacing: 8) {
            HStack(spacing: 7) {
                PCodeActivityIndicator(activity: pane.activity, unseen: false)
                    .frame(width: 10, height: 10)

                Text(title)
                    .font(.system(size: 12.5, weight: .medium))
                    .foregroundStyle(.primary.opacity(0.92))
                    .lineLimit(1)
                    .truncationMode(.tail)
                    .layoutPriority(1)

                HStack(spacing: 4) {
                    Image(systemName: "folder.fill")
                        .font(.system(size: 9.5))
                        .foregroundStyle(.secondary.opacity(0.85))
                    Text(projectName)
                        .font(.system(size: 11, weight: .medium))
                        .foregroundStyle(.secondary)
                        .lineLimit(1)
                        .truncationMode(.tail)
                    if let worktreeName, !worktreeName.isEmpty {
                        Text("/")
                            .font(.system(size: 10.5))
                            .foregroundStyle(.tertiary)
                        Text(worktreeName)
                            .font(.system(size: 11, weight: .regular))
                            .foregroundStyle(.secondary.opacity(0.9))
                            .lineLimit(1)
                            .truncationMode(.tail)
                    }
                }
                .padding(.horizontal, 7)
                .padding(.vertical, 2)
                .background(
                    Capsule(style: .continuous)
                        .fill(Color.primary.opacity(0.07))
                )
                .overlay(
                    Capsule(style: .continuous)
                        .stroke(Color.primary.opacity(0.08), lineWidth: 0.5)
                )
            }
            .frame(minWidth: 0)

            Spacer(minLength: 12)

            if !modelParts.name.isEmpty || !pane.contextUsage.isEmpty || !usageWindows.isEmpty {
                HStack(spacing: 6) {
                    if !modelParts.name.isEmpty {
                        HStack(spacing: 3) {
                            if let provider = modelParts.provider {
                                Text(provider)
                                    .font(.system(size: 10.5, weight: .regular))
                                    .foregroundStyle(.secondary.opacity(0.75))
                                    .lineLimit(1)
                                    .truncationMode(.tail)
                                Text(":")
                                    .font(.system(size: 10.5, weight: .regular))
                                    .foregroundStyle(.secondary.opacity(0.45))
                            }
                            Text(modelParts.name)
                                .font(.system(size: 11, weight: .medium))
                                .foregroundStyle(.secondary)
                                .lineLimit(1)
                                .truncationMode(.tail)
                                .layoutPriority(1)
                            if let effortLabel {
                                Text("(\(effortLabel))")
                                    .font(.system(size: 10.5, weight: .regular))
                                    .foregroundStyle(.secondary.opacity(0.75))
                                    .lineLimit(1)
                                    .fixedSize(horizontal: true, vertical: false)
                            }
                        }
                        .padding(.horizontal, 7)
                        .padding(.vertical, 2)
                        .background(
                            Capsule(style: .continuous)
                                .fill(Color.primary.opacity(0.06))
                        )
                        .overlay(
                            Capsule(style: .continuous)
                                .stroke(Color.primary.opacity(0.08), lineWidth: 0.5)
                        )
                    }

                    if !pane.contextUsage.isEmpty {
                        HStack(spacing: 5) {
                            if let ratio = contextRatio {
                                let barWidth: CGFloat = 24
                                let fillWidth: CGFloat = ratio > 0 ? max(2, barWidth * ratio) : 0
                                ZStack(alignment: .leading) {
                                    Capsule(style: .continuous)
                                        .fill(Color.primary.opacity(0.10))
                                    if fillWidth > 0 {
                                        Capsule(style: .continuous)
                                            .fill(contextAccentColor.opacity(0.8))
                                            .frame(width: fillWidth)
                                    }
                                }
                                .frame(width: barWidth, height: 3.5)
                            }
                            Text(pane.contextUsage)
                                .font(.system(size: 11, weight: .medium).monospacedDigit())
                                .foregroundStyle(contextAccentColor)
                                .lineLimit(1)
                                .fixedSize(horizontal: true, vertical: false)
                        }
                        .padding(.horizontal, 7)
                        .padding(.vertical, 2)
                        .background(
                            Capsule(style: .continuous)
                                .fill(Color.primary.opacity(0.06))
                        )
                        .overlay(
                            Capsule(style: .continuous)
                                .stroke(Color.primary.opacity(0.08), lineWidth: 0.5)
                        )
                        .layoutPriority(1)
                    }

                    if !usageWindows.isEmpty {
                        HStack(spacing: 5) {
                            ForEach(Array(usageWindows.enumerated()), id: \.offset) { index, window in
                                if index > 0 {
                                    Text("·")
                                        .font(.system(size: 10, weight: .regular))
                                        .foregroundStyle(.secondary.opacity(0.45))
                                }
                                HStack(spacing: 2.5) {
                                    Text(window.displayLabel(now: usageClock))
                                        .font(.system(size: 10.5, weight: .regular).monospacedDigit())
                                        .foregroundStyle(.secondary.opacity(0.75))
                                    Text("\(window.usedPercent)%")
                                        .font(.system(size: 11, weight: .medium).monospacedDigit())
                                        .foregroundStyle(usageAccentColor(for: window.usedPercent))
                                }
                            }
                        }
                        .lineLimit(1)
                        .fixedSize(horizontal: true, vertical: false)
                        .padding(.horizontal, 7)
                        .padding(.vertical, 2)
                        .background(
                            Capsule(style: .continuous)
                                .fill(Color.primary.opacity(0.06))
                        )
                        .overlay(
                            Capsule(style: .continuous)
                                .stroke(Color.primary.opacity(0.08), lineWidth: 0.5)
                        )
                        .layoutPriority(1)
                    }
                }
            }
        }
        .padding(.leading, leadingClearance)
        .padding(.trailing, 12)
        .onAppear {
            syncWindowTitle()
            wasRunning = pane.activity == .running
            usageClock = Date()
            usageStore.refreshIfNeeded(forModel: pane.modelName, directory: pane.directory)
        }
        .onChange(of: title) { _ in syncWindowTitle() }
        .onChange(of: projectName) { _ in syncWindowTitle() }
        .onChange(of: pane.modelName) { model in
            usageClock = Date()
            usageStore.refreshIfNeeded(forModel: model, directory: pane.directory)
        }
        .onChange(of: pane.activity) { activity in
            if wasRunning, activity != .running {
                usageClock = Date()
                usageStore.refreshIfNeeded(
                    forModel: pane.modelName,
                    directory: pane.directory,
                    afterTurn: true)
            }
            wasRunning = activity == .running
        }
        .onChange(of: controlActiveState) { state in
            if state != .inactive {
                usageClock = Date()
                usageStore.refreshIfNeeded(forModel: pane.modelName, directory: pane.directory)
            }
        }
        .onReceive(usageStore.$usageByProvider) { _ in
            usageClock = Date()
        }
        .onReceive(Self.usageRefreshTimer) { _ in
            guard workspace.windowVisible() else { return }
            usageClock = Date()
            usageStore.refreshIfNeeded(forModel: pane.modelName, directory: pane.directory)
        }
    }

    private func syncWindowTitle() {
        let full = "\(projectName) — \(title)"
        if workspace.window?.title != full {
            workspace.window?.title = full
        }
    }
}

/// The one workspace window.
final class PCodeWorkspaceController: NSWindowController, NSWindowDelegate {
    private static var instance: PCodeWorkspaceController?
    let workspace: PCodeWorkspace

    /// The controller, if the window was ever made.
    static var existing: PCodeWorkspaceController? { instance }

    static func shared(_ ghostty: Ghostty.App) -> PCodeWorkspaceController {
        if let instance { return instance }
        let controller = PCodeWorkspaceController(ghostty: ghostty)
        instance = controller
        return controller
    }

    /// Show the window, restoring sessions once on first open.
    static func show(_ ghostty: Ghostty.App) {
        PCodeStartupTrace.mark("show workspace", once: true)
        // First show: start the login shell now, beside building the window.
        if instance == nil { PCodeWorkspaceMode.warmLoginShellCache() }
        let controller = shared(ghostty)
        if !controller.workspace.hasRestored {
            controller.workspace.restore()
        } else if controller.workspace.panes.isEmpty && !controller.workspace.showingDirectoryPicker {
            controller.workspace.reloadHistory()
            controller.workspace.launcherFocusTrigger += 1
        }
        controller.showWindow(nil)
        controller.window?.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
        PCodeStartupTrace.mark("window shown", once: true)
    }

    private init(ghostty: Ghostty.App) {
        workspace = PCodeWorkspace(ghostty: ghostty)
        let window = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 1200, height: 800),
            styleMask: [.titled, .closable, .miniaturizable, .resizable, .fullSizeContentView],
            backing: .buffered, defer: false)
        window.title = "pcode"
        window.titleVisibility = .hidden
        window.titlebarSeparatorStyle = .none
        // Opaque, in the terminal's own color, as Ghostty's windows are: the
        // window server then never blends anything under the terminal.
        window.titlebarAppearsTransparent = true
        window.isOpaque = true
        window.backgroundColor = NSColor(ghostty.config.backgroundColor).withAlphaComponent(1)
        window.tabbingMode = .disallowed
        window.isReleasedWhenClosed = false
        // Keep mouseMoved events flowing even when no terminal SurfaceView is open so auto-hide reveal still works.
        window.acceptsMouseMovedEvents = true
        let hosting = NSHostingController(
            rootView: PCodeWindowVisibilityReader(
                visibility: workspace.windowVisibility,
                content: PCodeWorkspaceView(workspace: workspace)
                    .frame(minWidth: 700, minHeight: 400)
                    .environmentObject(ghostty)))
        // The window keeps its own size; by default the controller shrinks it
        // to SwiftUI's ideal size, which for a split view is tiny.
        hosting.sizingOptions = [.minSize]
        window.contentViewController = hosting
        window.setContentSize(NSSize(width: 1200, height: 800))
        window.setFrameAutosaveName("PCodeWorkspace")
        if !window.setFrameUsingName("PCodeWorkspace") || window.frame.width < 700 {
            window.setContentSize(NSSize(width: 1200, height: 800))
            window.center()
        }
        super.init(window: window)
        window.delegate = self
        workspace.window = window
        syncTitlebarMetrics(for: window)
        workspace.followAppearance()
        workspace.windowVisible = { [weak window] in
            window?.occlusionState.contains(.visible) ?? false
        }
        // A light/dark switch (`theme = light:…,dark:…`) reloads the config;
        // the titlebar and any gap must take the new background too.
        configSubscription = ghostty.$config
            .receive(on: DispatchQueue.main)
            .sink { [weak window] config in
                window?.backgroundColor = NSColor(config.backgroundColor).withAlphaComponent(1)
            }
        // Intercept ⌘+/⌘=/⌘-/⌘0 at window level before any focused Ghostty SurfaceView
        // can handle it as a single-surface terminal binding, so transcript, diff view,
        // editor, and shell drawer always scale together regardless of focus.
        fontSizeKeyMonitor = NSEvent.addLocalMonitorForEvents(matching: .keyDown) { [weak self] event in
            guard let self, event.window === self.window,
                  let action = PCodeFontSizeShortcut.match(event)
            else { return event }
            switch action {
            case .increase: self.increaseFontSize(self)
            case .decrease: self.decreaseFontSize(self)
            case .reset: self.resetFontSize(self)
            }
            return nil
        }
    }

    private var configSubscription: AnyCancellable?
    private var fontSizeKeyMonitor: Any?

    deinit {
        if let fontSizeKeyMonitor {
            NSEvent.removeMonitor(fontSizeKeyMonitor)
        }
    }

    func windowDidChangeOcclusionState(_ notification: Notification) {
        workspace.syncOcclusion()
        workspace.selectedMaybeSeen()
    }

    func windowDidBecomeKey(_ notification: Notification) {
        workspace.selectedMaybeSeen()
    }

    func windowDidResignKey(_ notification: Notification) {
        workspace.peek.windowDidResignKey()
        workspace.sidebarWindowDidResignKey()
    }

    func windowWillClose(_ notification: Notification) {
        if workspace.showingDirectoryPicker {
            workspace.showingDirectoryPicker = false
        }
        workspace.peek.dismiss(restoreFocus: false)
        workspace.sidebarWindowWillClose()
    }

    private func syncTitlebarMetrics(for window: NSWindow) {
        let barHeight = window.frame.height - window.contentLayoutRect.height
        if barHeight > 0 {
            workspace.titlebarHeight = barHeight
        }
        if let zoom = window.standardWindowButton(.zoomButton) {
            let maxX = zoom.convert(zoom.bounds, to: nil).maxX
            if maxX > 0 {
                workspace.trafficLightClearance = max(78, ceil(maxX) + 14)
            }
        }
    }

    func windowWillEnterFullScreen(_ notification: Notification) {
        workspace.isFullScreen = true
    }

    func windowDidFailToEnterFullScreen(_ window: NSWindow) {
        workspace.isFullScreen = false
        syncTitlebarMetrics(for: window)
    }

    func windowWillExitFullScreen(_ notification: Notification) {
        workspace.isFullScreen = false
    }

    func windowDidExitFullScreen(_ notification: Notification) {
        workspace.isFullScreen = false
        if let window {
            syncTitlebarMetrics(for: window)
        }
    }

    func windowDidFailToExitFullScreen(_ window: NSWindow) {
        workspace.isFullScreen = true
    }

    required init?(coder: NSCoder) { fatalError("not restorable from a coder") }

    // MARK: First Responder

    /// ⌘W: a picker or popup with the keyboard closes itself; otherwise the
    /// focused shell, or the session (the docked editor stands for its session).
    @IBAction func close(_ sender: Any) {
        if workspace.showingDirectoryPicker {
            workspace.dismissDirectoryPicker()
            return
        }
        if workspace.peek.isShowing {
            workspace.peek.dismiss()
            return
        }
        if let picker = PCode.keyboardPicker(in: window) {
            picker.cancel()
            return
        }
        if let overlay = PCode.keyboardOverlay(in: window)?.surface {
            workspace.ghostty.requestClose(surface: overlay)
            return
        }
        guard let pane = workspace.selectedPane else { return }
        if pane.shellVisible, let shell = pane.shell, let surface = shell.surface,
           let responder = window?.firstResponder as? NSView,
           responder === shell || responder.isDescendant(of: shell) {
            workspace.ghostty.requestClose(surface: surface)
            return
        }
        workspace.requestClose(pane)
    }

    @IBAction func closeWindow(_ sender: Any) {
        window?.performClose(sender)
    }

    @IBAction func increaseFontSize(_ sender: Any) {
        workspace.increaseFontSize()
    }

    @IBAction func decreaseFontSize(_ sender: Any) {
        workspace.decreaseFontSize()
    }

    @IBAction func resetFontSize(_ sender: Any) {
        workspace.resetFontSize()
    }
}
