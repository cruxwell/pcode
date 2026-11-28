import AppKit
import Darwin

/// Restart Pcode: move every session in the workspace into a background
/// pcode host, quit, and start the newest build, whose workspace reopens the
/// sessions (`pcode -c <id>` attaches to their hosts).
///
/// pcode moves its session into a host and exits 0 on SIGUSR2 (mid-turn too).
/// A pcode without that support dies of the signal, which is fine as well:
/// its session is saved, and reopens the ordinary way.
@MainActor
final class PCodeRestart {
    private static var current: PCodeRestart?
    /// Set once quitting to relaunch, so the quit is not confirmed again.
    static private(set) var terminating = false

    /// How long to wait for the sessions to move before relaunching anyway;
    /// any pcode still running then is ended by the quit, its session saved.
    private static let timeout: TimeInterval = 30

    private let workspace: PCodeWorkspace?
    private var waiting: [pid_t] = []
    private var total = 0
    private var started = Date()
    private var timer: Timer?
    private var panel: NSPanel?
    private var detail: NSTextField?

    private init(workspace: PCodeWorkspace?) {
        self.workspace = workspace
    }

    /// Ask, then restart. A second request while one runs only shows it.
    static func begin(workspace: PCodeWorkspace?) {
        if let current {
            current.panel?.makeKeyAndOrderFront(nil)
            return
        }
        let restart = PCodeRestart(workspace: workspace)
        current = restart
        restart.confirm()
    }

    private var sheetWindow: NSWindow? {
        guard let window = workspace?.window, window.isVisible else { return nil }
        return window
    }

    private func confirm() {
        let sessions = workspace?.panes.filter { $0.sessionID != nil }.count ?? 0
        let target = PCodeRelaunch.target()
        let alert = NSAlert()
        alert.messageText = "Restart Pcode?"
        var lines: [String] = []
        if sessions > 0 {
            let noun = sessions == 1 ? "session keeps" : "sessions keep"
            lines.append("\(sessions) pcode \(noun) running in the background, turns included, "
                + "and \(sessions == 1 ? "reopens" : "reopen") when Pcode starts again.")
        }
        lines.append("Shells under sessions, sessions without a turn yet, and any other "
            + "terminal windows close, ending what runs in them.")
        if target.resolvingSymlinksInPath() != Bundle.main.bundleURL.resolvingSymlinksInPath() {
            lines.append("Pcode starts again from the newest build, \(target.lastPathComponent).")
        }
        alert.informativeText = lines.joined(separator: "\n\n")
        alert.addButton(withTitle: "Restart")
        alert.addButton(withTitle: "Cancel")
        let handle: (NSApplication.ModalResponse) -> Void = { [self] response in
            if response == .alertFirstButtonReturn { proceed() } else { Self.current = nil }
        }
        if let window = sheetWindow {
            alert.beginSheetModal(for: window, completionHandler: handle)
        } else {
            handle(alert.runModal())
        }
    }

    private func proceed() {
        // From here on, panes whose pcode exits leave the saved list alone.
        workspace?.beginRestart()
        // A native-composer pane has no pcode client to signal: its host keeps running
        // (`beginRestart` stops panes closing hosts) and the relaunched pane reattaches.
        for pane in workspace?.panes ?? [] {
            guard let surface = pane.surface else { continue }
            for pid in PCodeProcess.pids(in: surface) where kill(pid, SIGUSR2) == 0 {
                waiting.append(pid)
            }
        }
        total = waiting.count
        guard total > 0 else { return relaunch() }
        started = Date()
        showProgress()
        // In the common modes, so it keeps firing while a menu tracks or a
        // sheet runs modally.
        let timer = Timer(timeInterval: 0.1, repeats: true) { [weak self] _ in
            MainActor.assumeIsolated { self?.poll() }
        }
        RunLoop.main.add(timer, forMode: .common)
        self.timer = timer
    }

    private func poll() {
        waiting.removeAll { !Self.alive($0) }
        if waiting.isEmpty || Date().timeIntervalSince(started) > Self.timeout { return relaunch() }
        detail?.stringValue = progressText
    }

    private static func alive(_ pid: pid_t) -> Bool {
        kill(pid, 0) == 0 || errno == EPERM
    }

    private var progressText: String {
        "\(total - waiting.count) of \(total) done. Turns keep running in the background."
    }

    private func showProgress() {
        let panel = NSPanel(
            contentRect: NSRect(x: 0, y: 0, width: 400, height: 120),
            styleMask: [.titled], backing: .buffered, defer: false)
        panel.title = "Restart Pcode"
        panel.isReleasedWhenClosed = false

        let spinner = NSProgressIndicator()
        spinner.style = .spinning
        spinner.controlSize = .small
        spinner.startAnimation(nil)
        let noun = total == 1 ? "session" : "sessions"
        let title = NSTextField(labelWithString: "Moving \(total) \(noun) to the background…")
        title.font = .boldSystemFont(ofSize: NSFont.systemFontSize)
        let detail = NSTextField(labelWithString: progressText)
        detail.textColor = .secondaryLabelColor
        detail.font = .systemFont(ofSize: NSFont.smallSystemFontSize)
        self.detail = detail
        let now = NSButton(title: "Restart Now", target: self, action: #selector(restartNow))
        now.keyEquivalent = "\r"
        now.toolTip = "Don't wait: pcode still running is ended by the quit; its session is saved."

        let heading = NSStackView(views: [spinner, title])
        heading.spacing = 8
        let text = NSStackView(views: [heading, detail])
        text.orientation = .vertical
        text.alignment = .leading
        text.spacing = 6
        let buttons = NSStackView(views: [NSView(), now])
        buttons.distribution = .fill
        let stack = NSStackView(views: [text, buttons])
        stack.orientation = .vertical
        stack.alignment = .leading
        stack.spacing = 16
        stack.edgeInsets = NSEdgeInsets(top: 20, left: 20, bottom: 16, right: 20)
        stack.translatesAutoresizingMaskIntoConstraints = false
        let content = NSView()
        content.addSubview(stack)
        NSLayoutConstraint.activate([
            stack.leadingAnchor.constraint(equalTo: content.leadingAnchor),
            stack.trailingAnchor.constraint(equalTo: content.trailingAnchor),
            stack.topAnchor.constraint(equalTo: content.topAnchor),
            stack.bottomAnchor.constraint(equalTo: content.bottomAnchor),
            buttons.widthAnchor.constraint(equalTo: stack.widthAnchor, constant: -40),
            content.widthAnchor.constraint(greaterThanOrEqualToConstant: 400),
        ])
        panel.contentView = content
        self.panel = panel
        if let window = sheetWindow {
            window.beginSheet(panel)
        } else {
            panel.center()
            panel.makeKeyAndOrderFront(nil)
        }
    }

    @objc private func restartNow() { relaunch() }

    private func relaunch() {
        // Once: "Restart Now" can race the last pcode exiting.
        guard !Self.terminating else { return }
        Self.terminating = true
        timer?.invalidate()
        timer = nil
        // AppKit refuses to terminate while a sheet is up.
        if let panel {
            if let parent = panel.sheetParent { parent.endSheet(panel) }
            panel.orderOut(nil)
        }
        let app = PCodeRelaunch.target()
        if !PCodeRelaunch.openWhenGone(app) {
            let alert = NSAlert()
            alert.messageText = "Couldn't start Pcode again"
            alert.informativeText = "Pcode quits now; open it yourself and the sessions reopen."
            alert.runModal()
        }
        // On the next turn, after the sheet (or the confirm alert, when
        // nothing needed waiting for) has fully closed.
        DispatchQueue.main.async { NSApp.terminate(nil) }
    }
}

/// pcode's process in a workspace pane's terminal.
enum PCodeProcess {
    /// The pcode in `view`'s terminal, if one is running.
    ///
    /// Not the terminal's child, nor its foreground process group: on macOS
    /// Ghostty starts commands through `login(1)`, which stays on as pcode's
    /// parent and holds the foreground group, and a `$PCODE_COMMAND` such as
    /// `uv run … pcode` adds a layer of its own. So: of the processes on the
    /// pane's tty, the outermost that is pcode: any with a pcode anywhere
    /// above it (a pcode started from a shell pcode runs) is left out. All
    /// of them when several unrelated ones remain.
    @MainActor
    static func pids(in view: Ghostty.SurfaceView) -> [pid_t] {
        guard let tty = view.surfaceModel?.ttyName else { return [] }
        return pids(onTTY: tty)
    }

    static func pids(onTTY name: String) -> [pid_t] {
        let path = name.hasPrefix("/") ? name : "/dev/" + name
        var info = stat()
        guard stat(path, &info) == 0 else { return [] }
        let processes = Self.processes(tty: info.st_rdev)
        let parents = Dictionary(processes.map { ($0.pid, $0.ppid) }, uniquingKeysWith: { first, _ in first })
        let pcode = Set(processes.map(\.pid).filter(isPCode))
        // Up the whole chain, past processes off the tty too.
        func underPCode(_ pid: pid_t) -> Bool {
            var seen: Set<pid_t> = [pid]
            var parent = parents[pid] ?? parentPID(pid)
            while let current = parent, current > 1, seen.insert(current).inserted {
                if pcode.contains(current) || (parents[current] == nil && isPCode(current)) { return true }
                parent = parents[current] ?? parentPID(current)
            }
            return false
        }
        return pcode.filter { !underPCode($0) }.sorted()
    }

    private static func parentPID(_ pid: pid_t) -> pid_t? {
        var mib: [Int32] = [CTL_KERN, KERN_PROC, KERN_PROC_PID, pid]
        var info = kinfo_proc()
        var size = MemoryLayout<kinfo_proc>.stride
        guard sysctl(&mib, 4, &info, &size, nil, 0) == 0, size > 0 else { return nil }
        return info.kp_eproc.e_ppid
    }

    private static func processes(tty: dev_t) -> [(pid: pid_t, ppid: pid_t)] {
        var mib: [Int32] = [CTL_KERN, KERN_PROC, KERN_PROC_TTY, tty]
        var size = 0
        guard sysctl(&mib, 4, nil, &size, nil, 0) == 0, size > 0 else { return [] }
        let stride = MemoryLayout<kinfo_proc>.stride
        // Room for processes started between the two calls.
        size += 8 * stride
        var buffer = [kinfo_proc](repeating: kinfo_proc(), count: size / stride)
        guard sysctl(&mib, 4, &buffer, &size, nil, 0) == 0 else { return [] }
        return buffer.prefix(size / stride).map { ($0.kp_proc.p_pid, $0.kp_eproc.e_ppid) }
    }

    /// Whether `pid`'s command line is pcode: `…/pcode ARGS` (renamed by
    /// `pcode.proctitle`, or a binary), or `…/python3 …/pcode ARGS`.
    static func isPCode(_ pid: pid_t) -> Bool { isPCode(arguments: arguments(pid)) }

    static func isPCode(arguments argv: [String]) -> Bool {
        func named(_ arg: String) -> Bool { (arg as NSString).lastPathComponent == "pcode" }
        guard let first = argv.first else { return false }
        if named(first) { return true }
        guard isPython(first), argv.count > 1 else { return false }
        return named(argv[1]) || (argv[1] == "-m" && argv.count > 2 && argv[2] == "pcode")
    }

    /// Whether `pid` is pcode or one of its background hosts, which run as
    /// `…/python3 -m pcode.host ARGS`.
    static func isPCodeOrHost(_ pid: pid_t) -> Bool { isPCodeOrHost(arguments: arguments(pid)) }

    static func isPCodeOrHost(arguments argv: [String]) -> Bool {
        if isPCode(arguments: argv) { return true }
        guard argv.count > 2, isPython(argv[0]) else { return false }
        return argv[1] == "-m" && argv[2] == "pcode.host"
    }

    /// `python3`, `python3.15`, or a framework build's `…/Python.app/Contents/MacOS/Python`
    /// (Homebrew's, which a uv tool install runs hosts on).
    private static func isPython(_ path: String) -> Bool {
        (path as NSString).lastPathComponent.lowercased().hasPrefix("python")
    }

    /// A process's argv, read as `ps` does (KERN_PROCARGS2: argc, the exec
    /// path, NUL padding, then the arguments).
    static func arguments(_ pid: pid_t) -> [String] {
        var argmax: Int32 = 0
        var length = MemoryLayout<Int32>.size
        var argmaxMib: [Int32] = [CTL_KERN, KERN_ARGMAX]
        guard sysctl(&argmaxMib, 2, &argmax, &length, nil, 0) == 0, argmax > 0 else { return [] }
        var size = Int(argmax)
        var buffer = [UInt8](repeating: 0, count: size)
        var mib: [Int32] = [CTL_KERN, KERN_PROCARGS2, pid]
        guard sysctl(&mib, 3, &buffer, &size, nil, 0) == 0, size > MemoryLayout<Int32>.size else { return [] }
        let argc = buffer.withUnsafeBytes { $0.loadUnaligned(as: Int32.self) }
        var index = MemoryLayout<Int32>.size
        // The exec path, then the padding after it.
        while index < size, buffer[index] != 0 { index += 1 }
        while index < size, buffer[index] == 0 { index += 1 }
        var argv: [String] = []
        while argv.count < argc, index < size {
            let start = index
            while index < size, buffer[index] != 0 { index += 1 }
            argv.append(String(decoding: buffer[start..<index], as: UTF8.self))
            index += 1
        }
        return argv
    }
}

/// Starting the newest build once this process has exited.
enum PCodeRelaunch {
    /// The build to start: `builds/current` beside the running bundle, as
    /// pcode's `make app` leaves it, when that is this same app (by
    /// bundle identifier: every install repoints `current`, whatever its
    /// `APP_NAME`, so it may be some test build); else the newest bundle of
    /// this app beside it; else the running bundle (an install elsewhere,
    /// such as Homebrew's in /Applications).
    static func target(running: URL = Bundle.main.bundleURL) -> URL {
        let directory = running.deletingLastPathComponent()
        let current = directory.appendingPathComponent("current")
        guard FileManager.default.fileExists(atPath: current.path),
              let identifier = bundleIdentifier(running) else { return running }
        let resolved = current.resolvingSymlinksInPath()
        if bundleIdentifier(resolved) == identifier { return resolved }
        let siblings = (try? FileManager.default.contentsOfDirectory(
            at: directory, includingPropertiesForKeys: [.creationDateKey], options: [.skipsHiddenFiles])) ?? []
        func created(_ url: URL) -> Date {
            (try? url.resourceValues(forKeys: [.creationDateKey]).creationDate) ?? .distantPast
        }
        let builds = siblings.filter { $0.pathExtension == "app" && bundleIdentifier($0) == identifier }
        return builds.max { created($0) < created($1) } ?? running
    }

    /// Read from the bundle's Info.plist, not `Bundle(url:)`, which caches
    /// by path and so can describe an earlier install at the same path.
    private static func bundleIdentifier(_ app: URL) -> String? {
        let plist = app.appendingPathComponent("Contents/Info.plist")
        guard FileManager.default.isExecutableFile(atPath: app.appendingPathComponent("Contents/MacOS/ghostty").path),
              let info = NSDictionary(contentsOf: plist) else { return nil }
        return info["CFBundleIdentifier"] as? String
    }

    /// What the relaunched app needs from this one's environment: `open`
    /// starts it with launchd's, not ours. Unset ones stay unset.
    private static let forwarded = [
        "GHOSTTY_DEBUG_SOCKET", "PCODE_COMMAND", "PCODE_WORKSPACE", "PCODE_SESSION_DIR",
        "PCODE_HOST_DIR", "XDG_STATE_HOME", "XDG_CONFIG_HOME",
    ]

    /// Start a detached helper that waits for this process to exit, then
    /// opens `app`. In its own session, with no inherited descriptors (a pty
    /// held open would keep a terminal's process from seeing it close).
    /// Should opening `app` by path fail (an install that replaced it as
    /// this one quit), the app is opened by bundle identifier instead.
    static func openWhenGone(_ app: URL) -> Bool {
        let script = #"""
            pid=$1 app=$2 id=$3; shift 3
            while kill -0 "$pid" 2>/dev/null; do sleep 0.1; done
            /usr/bin/open -n "$@" "$app" && exit 0
            [ -n "$id" ] && exec /usr/bin/open -n "$@" -b "$id"
            exit 1
            """#
        let identifier = bundleIdentifier(app) ?? Bundle.main.bundleIdentifier ?? ""
        var args = ["/bin/sh", "-c", script, "sh", String(getpid()), app.path, identifier]
        let environment = ProcessInfo.processInfo.environment
        for key in forwarded {
            if let value = environment[key], !value.isEmpty { args += ["--env", "\(key)=\(value)"] }
        }

        var attributes: posix_spawnattr_t?
        posix_spawnattr_init(&attributes)
        defer { posix_spawnattr_destroy(&attributes) }
        posix_spawnattr_setflags(&attributes, Int16(
            POSIX_SPAWN_SETSID | POSIX_SPAWN_CLOEXEC_DEFAULT | POSIX_SPAWN_SETSIGMASK | POSIX_SPAWN_SETSIGDEF))
        var mask = sigset_t()
        sigemptyset(&mask)
        posix_spawnattr_setsigmask(&attributes, &mask)
        var defaults = sigset_t()
        sigemptyset(&defaults)
        for signal in [SIGHUP, SIGINT, SIGQUIT, SIGTERM, SIGPIPE, SIGCHLD, SIGUSR1, SIGUSR2] {
            sigaddset(&defaults, signal)
        }
        posix_spawnattr_setsigdefault(&attributes, &defaults)

        var actions: posix_spawn_file_actions_t?
        posix_spawn_file_actions_init(&actions)
        defer { posix_spawn_file_actions_destroy(&actions) }
        for fd: Int32 in 0...2 {
            posix_spawn_file_actions_addopen(&actions, fd, "/dev/null", fd == 0 ? O_RDONLY : O_WRONLY, 0)
        }

        let argv = args.map { strdup($0) } + [nil]
        defer { argv.forEach { free($0) } }
        var pid = pid_t()
        let status = posix_spawn(&pid, "/bin/sh", &actions, &attributes, argv, environ)
        if status != 0 {
            Ghostty.logger.warning("restart: could not start the relaunch helper: \(status)")
        }
        return status == 0
    }
}
