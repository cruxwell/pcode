import CryptoKit
import Foundation

/// One `/` command, in the shape `SessionController.session_state()` sends
/// (`src/pcode/controller.py`), plus where the app runs it.
struct PCodeCommand: Codable, Equatable, Sendable {
    /// Who runs a command typed in the native composer.
    enum Handling: String, Codable, Sendable {
        /// The session host: send it with the client's `command` intent.
        case session
        /// The terminal editor in pcode, with no native equivalent yet.
        case terminal
        /// The app itself (a native view, picker or composer action).
        case native
    }

    var name: String
    var description: String
    var arguments: [String] = []
    var aliases: [String] = []
    var freeArguments = false
    var group = "Other"
    var argumentDescriptions: [String: String] = [:]
    /// The controller completer pcode runs locally for it (`aside_completions`
    /// for `/btw`), if any.
    var completer: String?
    /// Completes `$MODEL` words from the model list (`/btw`).
    var models = false
    var handling: Handling = .session

    enum CodingKeys: String, CodingKey {
        case name, description, arguments, aliases, group, completer, models, handling
        case freeArguments = "free_arguments"
        case argumentDescriptions = "argument_descriptions"
    }

    init(
        _ name: String, _ description: String, arguments: [String] = [], aliases: [String] = [],
        freeArguments: Bool = false, group: String = "Other", argumentDescriptions: [String: String] = [:],
        completer: String? = nil, models: Bool = false, handling: Handling = .session
    ) {
        self.name = name
        self.description = description
        self.arguments = arguments
        self.aliases = aliases
        self.freeArguments = freeArguments
        self.group = group
        self.argumentDescriptions = argumentDescriptions
        self.completer = completer
        self.models = models
        self.handling = handling
    }

    /// Lenient: the host may add keys, and an older one may lack some.
    init(from decoder: Decoder) throws {
        let values = try decoder.container(keyedBy: CodingKeys.self)
        name = try values.decode(String.self, forKey: .name)
        description = try values.decodeIfPresent(String.self, forKey: .description) ?? ""
        arguments = try values.decodeIfPresent([String].self, forKey: .arguments) ?? []
        aliases = try values.decodeIfPresent([String].self, forKey: .aliases) ?? []
        freeArguments = try values.decodeIfPresent(Bool.self, forKey: .freeArguments) ?? false
        group = try values.decodeIfPresent(String.self, forKey: .group) ?? "Other"
        argumentDescriptions = try values.decodeIfPresent([String: String].self, forKey: .argumentDescriptions) ?? [:]
        completer = try values.decodeIfPresent(String.self, forKey: .completer)
        models = try values.decodeIfPresent(Bool.self, forKey: .models) ?? false
        handling = try values.decodeIfPresent(Handling.self, forKey: .handling) ?? .session
    }
}

/// The native composer's `/` menu: the terminal's own commands (static, below)
/// plus the session's, from the host's session state or a saved snapshot of it.
///
/// Completion follows `SlashCompleter.get_completions` in
/// `src/pcode/commands.py`. See dev/native-app-plan.md, "3b. Native composer".
struct PCodeCommandCatalog: Equatable, Sendable {
    /// The catalog parts of a host's `session_state`, which is also what is saved.
    struct Snapshot: Codable, Equatable, Sendable {
        var commands: [PCodeCommand] = []
        /// Dynamic argument lists by command name (`/mcp`, `/jobs`, ...).
        var arguments: [String: [String]] = [:]
        var skills: [String] = []
        /// For `$MODEL` completion.
        var models: [String] = []

        init(commands: [PCodeCommand] = [], arguments: [String: [String]] = [:], skills: [String] = [], models: [String] = []) {
            self.commands = commands
            self.arguments = arguments
            self.skills = skills
            self.models = models
        }

        init(from decoder: Decoder) throws {
            let values = try decoder.container(keyedBy: CodingKeys.self)
            commands = try values.decodeIfPresent([PCodeCommand].self, forKey: .commands) ?? []
            arguments = try values.decodeIfPresent([String: [String]].self, forKey: .arguments) ?? [:]
            skills = try values.decodeIfPresent([String].self, forKey: .skills) ?? []
            models = try values.decodeIfPresent([String].self, forKey: .models) ?? []
        }

        /// From a `session_state` payload already parsed with `JSONSerialization`;
        /// nil when it holds no catalog.
        init?(sessionState: [String: Any]) {
            let parts = sessionState.filter { ["commands", "arguments", "skills", "models"].contains($0.key) }
            guard parts["commands"] != nil, JSONSerialization.isValidJSONObject(parts),
                  let data = try? JSONSerialization.data(withJSONObject: parts),
                  let decoded = try? JSONDecoder().decode(Snapshot.self, from: data) else { return nil }
            self = decoded
        }

        /// From a raw `session_state` JSON object (extra keys ignored).
        init(sessionStateJSON data: Data) throws {
            self = try JSONDecoder().decode(Snapshot.self, from: data)
        }
    }

    /// One completion: replace the last `replaceLength` UTF-16 units before the
    /// caret with `text`.
    struct Completion: Equatable, Sendable {
        let text: String
        let replaceLength: Int
        /// What the menu shows; `text` unless it differs.
        let display: String
        /// The description column.
        let meta: String
    }

    var snapshot: Snapshot

    init(snapshot: Snapshot = Snapshot()) {
        self.snapshot = snapshot
    }

    /// Every command the composer offers: the terminal's, then the session's.
    /// A session command never shadows a terminal one (pcode refuses those names).
    var commands: [PCodeCommand] {
        let builtIn = Set(Self.terminalCommands.flatMap { [$0.name] + $0.aliases })
        return Self.terminalCommands + snapshot.commands.filter { !builtIn.contains($0.name) }
    }

    /// The command for a name or alias.
    func find(_ name: String) -> PCodeCommand? {
        commands.first { $0.name == name || $0.aliases.contains(name) }
    }

    /// Completions for the composer's text around the caret, as pcode's `/` menu offers them.
    ///
    /// A `$model` word being typed anywhere in a prompt or `/btw` completes from
    /// the model list. Otherwise only a single-line `/command` with nothing after
    /// the caret completes: its name (or an alias) until the first space, then
    /// its argument from the dynamic list or the fixed one.
    func completions(before: String, after: String = "") -> [Completion] {
        if let btw = find("/btw"), btw.models || btw.completer != nil, namesTargets(btw, before) {
            if after.first.map({ $0.isWhitespace }) ?? true {
                return modelCompletions(before)
            }
        }
        if before.contains("\n") || after.contains("\n") || !after.isEmpty || !before.hasPrefix("/") {
            return []
        }
        let typed = (before as NSString).length
        if !before.contains(where: \.isWhitespace) {
            return commands
                .filter { ([$0.name] + $0.aliases).contains { $0.hasPrefix(before) } }
                .map { Completion(text: $0.name, replaceLength: typed, display: $0.name, meta: $0.description) }
        }
        let words = before.split(whereSeparator: \.isWhitespace)
        let name = String(words[0])
        // `text.split(maxsplit=1)`: the rest, leading whitespace dropped, trailing kept.
        let prefix = words.count > 1
            ? String(before[before.index(after: before.firstIndex(where: \.isWhitespace)!)...].drop(while: \.isWhitespace))
            : ""
        guard let command = find(name) else { return [] }
        // Commands whose arguments pcode completes with its own code are skipped
        // here, but `/btw`'s model words were handled above.
        if command.completer != nil { return [] }
        let arguments = snapshot.arguments[command.name] ?? command.arguments
        let length = (prefix as NSString).length
        return arguments.filter { $0.hasPrefix(prefix) }.map {
            Completion(text: $0, replaceLength: length, display: $0, meta: command.argumentDescriptions[$0] ?? "")
        }
    }

    /// `_names_targets`: the text ends in a `$`/`+` word in a prompt or `/btw`,
    /// not in a `!command` (whose `$` is the shell's) or another slash command.
    private func namesTargets(_ btw: PCodeCommand, _ text: String) -> Bool {
        guard Self.typedWord(text) != nil, !text.drop(while: \.isWhitespace).hasPrefix("!") else { return false }
        guard text.hasPrefix("/") else { return true }
        let first = text.split(whereSeparator: \.isWhitespace).first.map(String.init) ?? ""
        return ([btw.name] + btw.aliases).contains(first)
    }

    /// `aside_completions` for `$model` words; `+effort` words complete nothing here.
    private func modelCompletions(_ text: String) -> [Completion] {
        guard let word = Self.typedWord(text), word.hasPrefix("$"), !word.contains("+") else { return [] }
        let fragment = String(word.dropFirst())
        let needle = PCodePickMatch.casefold(fragment)
        let length = (word as NSString).length
        return snapshot.models
            .filter { needle.isEmpty || PCodePickMatch.casefold($0).contains(needle) }
            .map { Completion(text: "$" + $0, replaceLength: length, display: $0, meta: "") }
    }

    /// `aside.typed_word`: the `$` or `+` word ending the text, if any.
    static func typedWord(_ text: String) -> String? {
        guard let last = text.last, !last.isWhitespace,
              let word = text.split(whereSeparator: \.isWhitespace).last else { return nil }
        return word.hasPrefix("$") || word.hasPrefix("+") ? String(word) : nil
    }

    // MARK: Terminal commands

    /// What pcode's terminal handles itself and so never appears in session
    /// state: `TERMINAL_COMMANDS` in `src/pcode/controller.py`, with names,
    /// descriptions, arguments and groups from their `Command(...)`
    /// registrations in `PcodeApp.__init__` (`src/pcode/app.py`). Keep in sync.
    ///
    /// `.native` marks the ones dev/native-app-plan.md (3b) gives a native view,
    /// pick list or composer action; the rest still need pcode's terminal.
    /// `/syntax` takes Pygments style names, which pcode lists at run time, and
    /// `/config` lists every setting key; only its subcommands are here.
    static let terminalCommands: [PCodeCommand] = {
        let onOff = ["on", "off"]
        let modes = ["off", "activity-panel", "scrollback"]  // THINKING_MODES, COMMAND_MODES
        return [
            PCodeCommand("/help", "List commands and keyboard shortcuts", aliases: ["/commands"], group: "App", handling: .terminal),
            PCodeCommand(
                "/config", "Browse saved settings, or use list / diff / get KEY / set KEY VALUE / unset KEY",
                arguments: ["list", "diff", "get", "set", "unset"], freeArguments: true, group: "App", handling: .native),
            PCodeCommand("/bind", "List, set, or reset prompt keybindings; bind KEY /command [args]", freeArguments: true, group: "App", completer: "complete_bind", handling: .terminal),
            PCodeCommand("/unbind", "Disable a prompt binding; /bind reset KEY restores its default", freeArguments: true, group: "App", handling: .terminal),
            PCodeCommand("/quit", "Exit pcode", aliases: ["/exit"], group: "App", handling: .terminal),
            PCodeCommand("/status", "Show model, workspace, session, and context usage", group: "Inspect", handling: .terminal),
            PCodeCommand("/tools", "Browse tool calls and results; 'failed' or 'edits' narrows them", arguments: ["failed", "edits"], group: "Inspect", handling: .native),
            PCodeCommand("/diffs", "Git diff of this worktree's branch, or of files edited this session", group: "Inspect", handling: .native),
            PCodeCommand("/links", "Pick a URL from this conversation and open it in the browser", group: "Inspect", handling: .native),
            PCodeCommand("/copy", "Choose a response to copy, or a quote or code block from it", group: "Inspect", handling: .native),
            PCodeCommand("/paste-image", "Save the clipboard's image and add its path to the prompt", group: "Inspect", handling: .native),
            PCodeCommand("/tree", "Browse the conversation tree and fork from any point", group: "Inspect", handling: .native),
            PCodeCommand("/agents", "Follow delegated agents' own output, live and read-only", group: "Inspect", handling: .native),
            PCodeCommand("/resume", "Browse and search saved sessions to resume", group: "Session", handling: .native),
            PCodeCommand(
                "/attach", "Show a running session here, leaving this one running if it is in a host: HOST | - (the previous one); bare lists them",
                aliases: ["/switch"], freeArguments: true, group: "Session", handling: .native),
            PCodeCommand(
                "/fork", "Copy this conversation into a background session in its own worktree; with PROMPT it works on that and this terminal stays here",
                freeArguments: true, group: "Session", handling: .terminal),
            PCodeCommand(
                "/restart", "Restart this background session's host on the pcode installed now, continuing a running turn; all: every host on older code",
                arguments: ["all"], group: "Session", handling: .terminal),
            PCodeCommand("/stop", "End this background session's host and quit, as any exit does", group: "Session", handling: .terminal),
            PCodeCommand("/detach", "Quit, leaving this session's background host running; a session running here moves into a host instead", group: "Session", handling: .terminal),
            PCodeCommand("/show-activity", "Activity panel (thinking, tool calls, tasks, commands): on / off; bare toggles", arguments: onOff, group: "Display", handling: .terminal),
            PCodeCommand("/show-tasks", "Task list in the activity panel: on / off; bare toggles", arguments: onOff, group: "Display", handling: .terminal),
            PCodeCommand("/autohide-tasks", "Hide the task list when a turn ends: on / off; bare toggles", arguments: onOff, group: "Display", handling: .terminal),
            PCodeCommand("/show-thinking", "Where thinking shows: off / activity-panel / scrollback; bare cycles", arguments: modes, freeArguments: true, group: "Display", handling: .terminal),
            PCodeCommand("/show-edits", "Edit diffs and previews in scrollback: on / off; bare toggles", arguments: onOff, group: "Display", handling: .terminal),
            PCodeCommand("/show-commands", "Where shell command output shows: off / activity-panel / scrollback; bare cycles", arguments: modes, freeArguments: true, group: "Display", handling: .terminal),
            PCodeCommand("/group-tools", "Fold runs of tool calls into one scrollback line: on / off; bare toggles", arguments: onOff, group: "Display", handling: .terminal),
            PCodeCommand("/theme", "Set the palette: dark / light / auto; bare toggles dark/light", arguments: ["dark", "light", "auto"], group: "Display", handling: .terminal),
            PCodeCommand("/syntax", "Set the code, menu and prompt style for the active palette", group: "Display", handling: .terminal),
            PCodeCommand("/theme-preview", "Sample output plus every syntax style and how to select one", group: "Display", handling: .terminal),
            PCodeCommand("/redraw", "Rebuild scrollback at the current width and display settings", group: "Display", handling: .terminal),
        ]
    }()
}

/// Saves and loads catalog snapshots so the `/` menu works on the first frame,
/// before the host answers: one file per workspace, plus the last one saved
/// anywhere for a workspace opened for the first time.
///
/// Files live in `~/Library/Application Support/<bundle id>/composer-catalog/`
/// as `<sha256 of the workspace path>.json` and `last.json`. Only commands,
/// arguments and models are kept: skills are commands already.
struct PCodeCatalogStore: Sendable {
    let directory: URL

    init(directory: URL = PCodeCatalogStore.defaultDirectory) {
        self.directory = directory
    }

    static var defaultDirectory: URL {
        let support = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask).first
            ?? FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Library/Application Support")
        return support
            .appendingPathComponent(Bundle.main.bundleIdentifier ?? "com.pcode.terminal")
            .appendingPathComponent("composer-catalog")
    }

    static let lastName = "last.json"

    /// The workspace's file name: the SHA-256 of its standardized path, in hex.
    static func fileName(for workspace: String) -> String {
        let path = URL(fileURLWithPath: workspace).standardizedFileURL.path
        let digest = SHA256.hash(data: Data(path.utf8))
        return digest.map { String(format: "%02x", $0) }.joined() + ".json"
    }

    /// Write `snapshot` for `workspace` and as the global last one. Best effort.
    @discardableResult
    func save(_ snapshot: PCodeCommandCatalog.Snapshot, workspace: String) -> Bool {
        let kept = PCodeCommandCatalog.Snapshot(commands: snapshot.commands, arguments: snapshot.arguments, models: snapshot.models)
        let encoder = JSONEncoder()
        encoder.outputFormatting = [.sortedKeys]
        guard let data = try? encoder.encode(kept) else { return false }
        do {
            try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
            try data.write(to: directory.appendingPathComponent(Self.fileName(for: workspace)), options: .atomic)
            try data.write(to: directory.appendingPathComponent(Self.lastName), options: .atomic)
            return true
        } catch {
            return false
        }
    }

    /// The workspace's snapshot, else the last one saved, else nil.
    func load(workspace: String) -> PCodeCommandCatalog.Snapshot? {
        for name in [Self.fileName(for: workspace), Self.lastName] {
            if let data = try? Data(contentsOf: directory.appendingPathComponent(name)),
               let snapshot = try? JSONDecoder().decode(PCodeCommandCatalog.Snapshot.self, from: data) {
                return snapshot
            }
        }
        return nil
    }

    /// The catalog to show before the host connects: saved, or built-ins only.
    func catalog(workspace: String) -> PCodeCommandCatalog {
        PCodeCommandCatalog(snapshot: load(workspace: workspace) ?? .init())
    }
}
