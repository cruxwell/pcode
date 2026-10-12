// pcode bridge: talk to a note's pcode session from Obsidian.
//
// A plan written by `/plan --obsidian` names its session in front matter
// (`session:`). Ask, Review comments and Go ahead send a message to that
// session through `pcode --attach <host> -p`, so it is answered in the
// session's own repository, environment and conversation; the reply streams
// into the side panel and any attached terminal shows the turn as usual.
// Comment inserts a `> [!comment]` callout, which reaches pcode as part of the
// diff of your edits with the next message.
import {
	App,
	Editor,
	FileSystemAdapter,
	MarkdownView,
	Modal,
	Notice,
	Plugin,
	PluginSettingTab,
	Setting,
	SuggestModal,
	TFile,
} from "obsidian";
import {
	Host,
	askPrompt,
	COMMENT,
	calloutInsertion,
	goAheadPrompt,
	replyCallout,
	reviewPrompt,
	sessionOf,
} from "./core";
import { listHosts, resolvePcode } from "./pcode";
import { PcodeView, VIEW_TYPE } from "./view";

interface Settings {
	pcodePath: string;
	confirmGoAhead: boolean;
}

const DEFAULTS: Settings = { pcodePath: "", confirmGoAhead: true };

export default class PcodeBridge extends Plugin {
	settings: Settings = DEFAULTS;

	async onload() {
		this.settings = { ...DEFAULTS, ...(await this.loadData()) };
		this.registerView(VIEW_TYPE, (leaf) => new PcodeView(leaf, this));
		this.addRibbonIcon("terminal-square", "Open the pcode panel", () => void this.panel());
		this.addSettingTab(new BridgeSettings(this.app, this));

		this.addCommand({ id: "open-panel", name: "Open panel", callback: () => void this.panel() });
		this.addCommand({
			id: "ask",
			name: "Ask about the selection…",
			editorCallback: (editor) => this.askFrom(editor),
		});
		this.addCommand({
			id: "comment",
			name: "Add a comment",
			editorCallback: (editor) => this.comment(editor),
		});
		this.addCommand({ id: "review", name: "Review my comments", callback: () => void this.review() });
		this.addCommand({ id: "go-ahead", name: "Go ahead (approve the plan)", callback: () => void this.goAhead() });
		this.addCommand({
			id: "set-session",
			name: "Set this note's session",
			callback: () => void this.chooseSession(),
		});

		this.registerEvent(
			this.app.workspace.on("editor-menu", (menu, editor) => {
				menu.addItem((item) =>
					item.setTitle("Ask pcode…").setIcon("message-circle-question").onClick(() => this.askFrom(editor)),
				);
				menu.addItem((item) =>
					item.setTitle("Add comment").setIcon("message-square-plus").onClick(() => this.comment(editor)),
				);
			}),
		);
	}

	bin(): string {
		return resolvePcode(this.settings.pcodePath);
	}

	/** The note being worked on: the active one, or the last one active before the panel took focus. */
	note(): TFile | null {
		return this.app.workspace.getActiveFile();
	}

	sessionFor(file: TFile): string | undefined {
		return sessionOf(this.app.metadataCache.getFileCache(file)?.frontmatter);
	}

	/** An absolute path, so the session can read the note whatever its working directory. */
	path(file: TFile): string {
		const adapter = this.app.vault.adapter;
		return adapter instanceof FileSystemAdapter ? adapter.getFullPath(file.path) : file.path;
	}

	async panel(): Promise<PcodeView> {
		const { workspace } = this.app;
		let leaf = workspace.getLeavesOfType(VIEW_TYPE)[0];
		if (!leaf) {
			leaf = workspace.getRightLeaf(false) ?? workspace.getLeaf("split");
			await leaf.setViewState({ type: VIEW_TYPE, active: false });
		}
		await workspace.revealLeaf(leaf);
		return leaf.view as PcodeView;
	}

	private async sendNote(label: string, prompt: (path: string) => string): Promise<void> {
		const note = this.note();
		if (!note) {
			new Notice("Open the note first.");
			return;
		}
		const view = await this.panel();
		await view.send(note, label, prompt(this.path(note)));
	}

	ask(question: string, selection: string): Promise<void> {
		const label = selection.trim() ? `“${selection.trim().slice(0, 80)}” ${question}` : question;
		return this.sendNote(label, (path) => askPrompt(path, question, selection));
	}

	review(): Promise<void> {
		return this.sendNote("Review my comments", reviewPrompt);
	}

	async goAhead(): Promise<void> {
		if (this.settings.confirmGoAhead && !(await confirm(this.app, "Approve the plan and start implementing?"))) {
			return;
		}
		await this.sendNote("Go ahead", goAheadPrompt);
	}

	private askFrom(editor: Editor) {
		const selection = editor.getSelection();
		new QuestionModal(this.app, selection, (question) => void this.ask(question, selection)).open();
	}

	comment(editor: Editor) {
		insertCallout(editor, COMMENT);
		editor.focus();
	}

	insertReply(note: TFile, reply: string) {
		const view = this.app.workspace
			.getLeavesOfType("markdown")
			.map((leaf) => leaf.view)
			.find((view): view is MarkdownView => view instanceof MarkdownView && view.file === note);
		if (!view) {
			new Notice(`Open ${note.basename} to insert the reply.`);
			return;
		}
		// After the paragraph the cursor is in, as a comment would go.
		insertCallout(view.editor, replyCallout(reply));
	}

	private async chooseSession() {
		const note = this.note();
		if (!note) {
			new Notice("Open the note first.");
			return;
		}
		let hosts: Host[];
		try {
			hosts = await listHosts(this.bin());
		} catch (error) {
			new Notice(String(error));
			return;
		}
		if (!hosts.length) {
			new Notice("No pcode session is running in the background. /detach one in its terminal first.");
			return;
		}
		new HostModal(this.app, hosts, (host) => {
			void this.app.fileManager.processFrontMatter(note, (frontmatter) => {
				// A host's ID is a fallback for a session not yet saved (no first prompt).
				frontmatter.session = String(host.session_id || host.id);
			});
		}).open();
	}

	async saveSettings() {
		await this.saveData(this.settings);
	}
}

function insertCallout(editor: Editor, callout: string) {
	const lines = editor.getValue().split("\n");
	const { line, text, cursor } = calloutInsertion(lines, editor.getCursor("to").line, callout);
	editor.replaceRange(text, { line, ch: 0 }, { line, ch: lines[line]?.length ?? 0 });
	editor.setCursor(cursor);
}

class QuestionModal extends Modal {
	constructor(
		app: App,
		private selection: string,
		private onSubmit: (question: string) => void,
	) {
		super(app);
	}

	onOpen() {
		this.titleEl.setText("Ask pcode");
		if (this.selection.trim()) {
			this.contentEl.createEl("blockquote", { cls: "pcode-quote", text: this.selection.trim().slice(0, 600) });
		}
		const input = this.contentEl.createEl("textarea", {
			cls: "pcode-question",
			attr: { rows: "4", placeholder: "Your question (Ctrl/Cmd+Enter to send)" },
		});
		const submit = () => {
			const question = input.value.trim();
			if (!question) return;
			this.close();
			this.onSubmit(question);
		};
		input.addEventListener("keydown", (event) => {
			if (event.key === "Enter" && (event.metaKey || event.ctrlKey)) {
				event.preventDefault();
				submit();
			}
		});
		new Setting(this.contentEl).addButton((button) => button.setButtonText("Ask").setCta().onClick(submit));
		input.focus();
	}

	onClose() {
		this.contentEl.empty();
	}
}

class HostModal extends SuggestModal<Host> {
	constructor(
		app: App,
		private hosts: Host[],
		private onChoose: (host: Host) => void,
	) {
		super(app);
		this.setPlaceholder("Choose the pcode session for this note");
	}

	getSuggestions(query: string): Host[] {
		const wanted = query.toLowerCase();
		return this.hosts.filter((host) =>
			[host.id, host.session_id, host.title, host.workspace].some((field) => (field ?? "").toLowerCase().includes(wanted)),
		);
	}

	renderSuggestion(host: Host, el: HTMLElement) {
		el.createDiv({ text: host.title || host.last_prompt || host.session_id || host.id });
		el.createEl("small", { text: `${host.workspace} · ${host.state}` });
	}

	onChooseSuggestion(host: Host) {
		this.onChoose(host);
	}
}

function confirm(app: App, question: string): Promise<boolean> {
	return new Promise((resolve) => {
		const modal = new Modal(app);
		let answered = false;
		modal.titleEl.setText(question);
		new Setting(modal.contentEl)
			.addButton((button) =>
				button.setButtonText("Cancel").onClick(() => {
					modal.close();
				}),
			)
			.addButton((button) =>
				button
					.setButtonText("Go ahead")
					.setCta()
					.onClick(() => {
						answered = true;
						modal.close();
					}),
			);
		modal.onClose = () => resolve(answered);
		modal.open();
	});
}

class BridgeSettings extends PluginSettingTab {
	constructor(
		app: App,
		private plugin: PcodeBridge,
	) {
		super(app, plugin);
	}

	display() {
		const { containerEl } = this;
		containerEl.empty();
		new Setting(containerEl)
			.setName("pcode path")
			.setDesc(
				`Leave empty to look in ~/.local/bin, /opt/homebrew/bin and /usr/local/bin (apps opened from the Dock don't get your shell's PATH). Using: ${this.plugin.bin()}`,
			)
			.addText((text) =>
				text
					.setPlaceholder("/Users/you/.local/bin/pcode")
					.setValue(this.plugin.settings.pcodePath)
					.onChange(async (value) => {
						this.plugin.settings.pcodePath = value;
						await this.plugin.saveSettings();
					}),
			);
		new Setting(containerEl)
			.setName("Confirm Go ahead")
			.setDesc("Ask before approving a plan, since that starts implementation.")
			.addToggle((toggle) =>
				toggle.setValue(this.plugin.settings.confirmGoAhead).onChange(async (value) => {
					this.plugin.settings.confirmGoAhead = value;
					await this.plugin.saveSettings();
				}),
			);
	}
}
