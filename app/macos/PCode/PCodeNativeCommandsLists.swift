import Foundation

/// The lists and rows the native command routes show, built from plain data so
/// they can be tested without a window.
extension PCodeNativeCommands {
    // MARK: Picker payloads

    /// A `PCodePicker.showLocal` list in `pcode.pick_ui.payload`'s shape.
    static func pickList(
        title: String, header: String = "", empty: String = "No matches.", selected: String? = nil,
        custom: String? = nil, items: [[String: Any]], enterLabel: String
    ) -> [String: Any] {
        [
            "title": title,
            "header": header,
            "empty": empty,
            "selected": selected ?? NSNull(),
            "query": "",
            "match": "fuzzy",
            "custom": custom ?? NSNull(),
            "items": items,
            "actions": [[String: Any]](),
            "hints": [
                ["key": "↩", "label": enterLabel, "keep": true],
                ["key": "esc", "label": "Cancel", "keep": true],
            ],
        ]
    }

    /// One picker row. `search` is casefolded here, as `PickItem.texts` sends it:
    /// the matcher folds only the query.
    static func pickItem(id: String, label: String, detail: String = "", marker: String = "", depth: Int = 0, search: [String]) -> [String: Any] {
        ["id": id, "label": label, "detail": detail, "marker": marker, "depth": depth,
         "search": search.map(PCodePickMatch.casefold), "confirm": ""]
    }

    /// `pcode.models.PROVIDERS`: provider prefixes and the names the model picker shows.
    static let providerNames: [String: String] = [
        "alibaba": "Alibaba", "anthropic": "Anthropic", "antigravity": "Antigravity", "azure": "Azure OpenAI",
        "bedrock": "Bedrock", "bedrock-mantle": "Bedrock Mantle", "cerebras": "Cerebras", "claude": "Claude Code",
        "crusoe": "Crusoe", "deepseek": "DeepSeek", "fireworks": "Fireworks", "github-copilot": "GitHub Copilot",
        "google": "Google", "google-cloud": "Google Cloud", "groq": "Groq", "heroku": "Heroku",
        "moonshotai": "Moonshot", "nebius": "Nebius", "ollama": "Ollama", "openai": "OpenAI",
        "openai-chat": "OpenAI (chat)", "openai-codex": "OpenAI Codex", "openai-responses": "OpenAI (responses)",
        "openrouter": "OpenRouter", "ovhcloud": "OVHcloud", "sambanova": "SambaNova", "snowflake": "Snowflake",
        "together": "Together", "vercel": "Vercel", "vllm": "vLLM", "xai": "xAI", "zai": "Z.ai",
    ]

    /// The model picker, as `pcode.model_ui` builds it: one row per model labelled
    /// `Provider · model`, matched by id or by provider and model name, the current
    /// one marked, and a typed `provider:model-id` offered as a row of its own.
    static func modelList(_ models: [String], providers: [String], current: String?) -> [String: Any] {
        let items = models.map { model -> [String: Any] in
            let parts = model.split(separator: ":", maxSplits: 1).map(String.init)
            let provider = parts.first ?? model
            let name = parts.count > 1 ? parts[1] : ""
            let shown = providerNames[provider] ?? provider
            return pickItem(
                id: model, label: "\(shown) · \(name)", marker: model == current ? "current" : "",
                search: [model, "\(shown) \(name)"])
        }
        return pickList(
            title: "Choose model",
            header: "Type provider:model-id for a custom model.",
            empty: "No matches. Try another filter or provider:model-id.",
            selected: current.flatMap { models.contains($0) ? $0 : nil },
            custom: customModelPattern(providers), items: items, enterLabel: "Choose")
    }

    /// `model_ui.custom_pattern`: any known provider (and any the host named), a colon, an id.
    static func customModelPattern(_ providers: [String]) -> String {
        let names = Set(providerNames.keys).union(providers).sorted()
        let alternatives = names.map(NSRegularExpression.escapedPattern(for:)).joined(separator: "|")
        return "(?:\(alternatives)):[A-Za-z0-9_.:/-]+"
    }

    // MARK: Links

    struct Link: Equatable {
        let url: String
        let label: String
        let source: String
    }

    private static let markdownLink = try! NSRegularExpression(
        pattern: #"\[([^\]\n]+)\]\(((?:https?|file)://[^\s)\\]+)\)"#, options: [.caseInsensitive])
    private static let bareURL = try! NSRegularExpression(
        pattern: #"(?:https?|file)://[^\s<>"'`\]\\]+"#, options: [.caseInsensitive])

    /// `pcode.links._strip`: trailing prose punctuation and unmatched close parens go.
    static func stripURL(_ url: String) -> String {
        let trailing = Set(".,;:!?'\"")
        var result = url
        while let last = result.last, trailing.contains(last) { result.removeLast() }
        while result.hasSuffix(")"),
              result.filter({ $0 == "(" }).count < result.filter({ $0 == ")" }).count {
            result.removeLast()
            while let last = result.last, trailing.contains(last) { result.removeLast() }
        }
        return result
    }

    /// `pcode.links.extract_links`: Markdown links (keeping their label) and bare
    /// URLs, in the order they appear.
    static func extractLinks(_ text: String, source: String) -> [Link] {
        let ns = text as NSString
        var found: [(Int, Link)] = []
        for match in markdownLink.matches(in: text, range: NSRange(location: 0, length: ns.length)) {
            let label = ns.substring(with: match.range(at: 1)).trimmingCharacters(in: .whitespacesAndNewlines)
            found.append((match.range.location, Link(url: stripURL(ns.substring(with: match.range(at: 2))), label: label, source: source)))
        }
        // Hide Markdown links from the bare pass, keeping offsets.
        let maskedNS = NSMutableString(string: text)
        for match in markdownLink.matches(in: text, range: NSRange(location: 0, length: ns.length)).reversed() {
            maskedNS.replaceCharacters(in: match.range, with: String(repeating: " ", count: match.range.length))
        }
        for match in bareURL.matches(in: maskedNS as String, range: NSRange(location: 0, length: maskedNS.length)) {
            found.append((match.range.location, Link(url: stripURL(maskedNS.substring(with: match.range)), label: "", source: source)))
        }
        return found.sorted { $0.0 < $1.0 }.map(\.1).filter { !$0.url.isEmpty }
    }

    /// `pcode.links.conversation_links` over messages: distinct URLs at their
    /// latest position, keeping a label seen earlier. Oldest first.
    static func conversationLinks(_ messages: [Message]) -> [Link] {
        var order: [String] = []
        var byURL: [String: Link] = [:]
        for message in messages {
            for link in extractLinks(message.text, source: message.role.rawValue) {
                let previous = byURL[link.url]
                order.removeAll { $0 == link.url }
                order.append(link.url)
                byURL[link.url] = Link(url: link.url, label: link.label.isEmpty ? (previous?.label ?? "") : link.label, source: link.source)
            }
        }
        return order.compactMap { byURL[$0] }
    }

    /// The `/links` picker, newest first, as `links_ui.link_rows` labels them.
    static func linkList(_ links: [Link]) -> [String: Any] {
        let items = links.reversed().map { link -> [String: Any] in
            let text = link.label.isEmpty ? link.url : "\(link.label) — \(link.url)"
            let label = text.replacingOccurrences(of: "\n", with: " ↵ ").replacingOccurrences(of: "\r", with: " ")
            return pickItem(id: link.url, label: label, detail: link.source, search: [link.url, link.label])
        }
        return pickList(title: "Links", empty: "No matching links.", items: items, enterLabel: "Open")
    }

    // MARK: Copy

    /// Fenced code blocks in Markdown, in order.
    static func codeBlocks(_ markdown: String) -> [String] {
        var blocks: [String] = []
        var current: [String]?
        var fence = ""
        for line in markdown.components(separatedBy: "\n") {
            let trimmed = line.trimmingCharacters(in: .whitespaces)
            if var lines = current {
                if trimmed.hasPrefix(fence), trimmed.allSatisfy({ $0 == fence.first }) {
                    blocks.append(lines.joined(separator: "\n"))
                    current = nil
                } else {
                    lines.append(line)
                    current = lines
                }
            } else if trimmed.hasPrefix("```") || trimmed.hasPrefix("~~~") {
                let mark = trimmed.first!
                fence = String(trimmed.prefix { $0 == mark })
                current = []
            }
        }
        return blocks
    }

    /// The `/copy` picker: responses newest first, each with its code blocks
    /// nested under it. Returns the list and the text each row id copies.
    static func copyList(_ responses: [String]) -> (list: [String: Any], texts: [String: String]) {
        var items: [[String: Any]] = []
        var texts: [String: String] = [:]
        for (index, response) in responses.enumerated().reversed() {
            let id = "r\(index)"
            texts[id] = response
            let firstLine = response.split(whereSeparator: \.isNewline).first.map(String.init) ?? ""
            items.append(pickItem(
                id: id, label: String(firstLine.prefix(100)), detail: "response \(index + 1)", search: [response]))
            for (number, code) in codeBlocks(response).enumerated() {
                let codeID = "\(id)c\(number)"
                texts[codeID] = code
                let line = code.split(whereSeparator: \.isNewline).first.map(String.init) ?? ""
                items.append(pickItem(id: codeID, label: String(line.prefix(100)), detail: "code", depth: 1, search: [code]))
            }
        }
        return (pickList(title: "Copy", empty: "No matching responses.", items: items, enterLabel: "Copy"), texts)
    }

    // MARK: Help

    struct HelpGroup: Equatable, Identifiable {
        let name: String
        let commands: [PCodeCommand]
        var id: String { name }
    }

    /// The catalog's commands by group, as `CommandRegistry.grouped` lists them for
    /// `/help`: groups and members both in catalog order.
    static func helpGroups(_ catalog: PCodeCommandCatalog) -> [HelpGroup] {
        var names: [String] = []
        var members: [String: [PCodeCommand]] = [:]
        for command in catalog.commands {
            if members[command.group] == nil { names.append(command.group) }
            members[command.group, default: []].append(command)
        }
        return names.map { HelpGroup(name: $0, commands: members[$0]!) }
    }

    // MARK: Session info

    struct InfoRow: Equatable, Identifiable {
        let label: String
        let value: String
        var id: String { label }
    }

    /// Rows from what the client already mirrors, shown until `session_overview` answers.
    static func sessionRows(state: PCodeHostClientState, hostID: String?) -> [InfoRow] {
        let rows: [(String, String)] = [
            ("Model", state.model),
            ("Effort", state.effort),
            ("Context", state.context),
            ("Session", state.sessionID),
            ("Title", state.sessionTitle),
            ("Workspace", state.workspace),
            ("Thinking", state.thinkingMode),
        ]
        return hostRows(hostID: hostID) + rows.filter { !$0.1.isEmpty }.map { InfoRow(label: $0.0, value: $0.1) }
    }

    static func hostRows(hostID: String?) -> [InfoRow] {
        guard let hostID, !hostID.isEmpty else { return [] }
        return [InfoRow(label: "Host", value: hostID)]
    }

    /// `session_overview`'s `[(label, value)]`, sent as lists.
    static func overviewRows(_ raw: Any?) -> [InfoRow] {
        guard let pairs = raw as? [Any] else { return [] }
        return pairs.compactMap { pair in
            guard let items = pair as? [Any], items.count == 2,
                  let label = items[0] as? String else { return nil }
            return InfoRow(label: label, value: (items[1] as? String) ?? "\(items[1])")
        }
    }

    // MARK: Jobs

    struct Job: Equatable, Identifiable {
        let id: String
        let command: String
        let purpose: String
    }

    /// `peek()`'s `jobs`: the running background jobs.
    static func jobs(fromPeek raw: Any?) -> [Job] {
        guard let peek = raw as? [String: Any], let jobs = peek["jobs"] as? [[String: Any]] else { return [] }
        return jobs.compactMap { job in
            guard let id = job["id"] as? String else { return nil }
            return Job(id: id, command: (job["command"] as? String) ?? "", purpose: (job["purpose"] as? String) ?? "")
        }
    }
}
