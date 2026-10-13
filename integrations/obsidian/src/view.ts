// The pcode side panel: which session the note belongs to, and the replies to
// what you sent from Obsidian. Follow-ups are just more messages: everything
// goes to the session's one conversation.
import { Component, ItemView, MarkdownRenderer, Notice, TFile, WorkspaceLeaf, setIcon } from "obsidian";
import { Host, LineBuffer, SEND_HINT, exitMessage, isSendKey, findHost, notRunningMessage, stripAnsi } from "./core";
import type PcodeBridge from "./main";
import { Run, listHosts, send } from "./pcode";

export const VIEW_TYPE = "pcode-bridge";
const RENDER_DELAY_MS = 150;
const ACTIVITY_LINES = 40;

export class PcodeView extends ItemView {
	private header!: HTMLElement;
	private log!: HTMLElement;
	private input!: HTMLTextAreaElement;
	// Each refresh lists hosts (a pcode start-up); a slower, older one must not
	// overwrite a newer one's header.
	private generation = 0;
	private shownSession: string | undefined;
	private runs = new Set<Run>();

	constructor(
		leaf: WorkspaceLeaf,
		private plugin: PcodeBridge,
	) {
		super(leaf);
	}

	getViewType() {
		return VIEW_TYPE;
	}

	getDisplayText() {
		return "pcode";
	}

	getIcon() {
		return "terminal-square";
	}

	async onOpen() {
		const root = this.contentEl;
		root.empty();
		root.addClass("pcode-bridge");
		this.header = root.createDiv("pcode-header");
		const actions = root.createDiv("pcode-actions");
		this.button(actions, "Review comments", "message-square-reply", () => this.plugin.review());
		this.button(actions, "Go ahead", "play", () => this.plugin.goAhead());
		this.button(actions, "Refresh", "refresh-cw", () => this.refresh());
		this.log = root.createDiv("pcode-log");
		const composer = root.createDiv("pcode-composer");
		this.input = composer.createEl("textarea", {
			attr: { placeholder: `Ask about this note… (${SEND_HINT})`, rows: "3" },
		});
		this.input.addEventListener("keydown", (event) => {
			if (isSendKey(event)) {
				event.preventDefault();
				this.submit();
			}
		});
		this.button(composer, "Ask", "send", () => this.submit());
		this.registerEvent(this.app.workspace.on("file-open", () => void this.refresh()));
		// Re-indexing happens as you type: only a changed session: is worth a lookup.
		this.registerEvent(
			this.app.metadataCache.on("changed", (file) => {
				if (file === this.plugin.note() && this.plugin.sessionFor(file) !== this.shownSession) {
					void this.refresh();
				}
			}),
		);
		await this.refresh();
	}

	async onClose() {
		// pcode runs in its own process group, so it would outlive the panel.
		for (const run of this.runs) run.stop();
	}

	private button(parent: HTMLElement, label: string, icon: string, action: () => unknown) {
		const button = parent.createEl("button", { attr: { "aria-label": label } });
		setIcon(button.createSpan(), icon);
		button.createSpan({ text: label });
		button.addEventListener("click", () => void action());
		return button;
	}

	private submit() {
		const question = this.input.value.trim();
		if (!question) return;
		this.input.value = "";
		void this.plugin.ask(question, "");
	}

	/** Show `note`'s session (the active note by default) and whether Obsidian can reach it. */
	async refresh(note: TFile | null = this.plugin.note()): Promise<Host | undefined> {
		const generation = ++this.generation;
		const session = note ? this.plugin.sessionFor(note) : undefined;
		this.shownSession = session;
		let host: Host | undefined;
		let problem = "";
		if (note && session) {
			try {
				host = findHost(await listHosts(this.plugin.bin()), session);
			} catch (error) {
				problem = String(error);
			}
		}
		if (generation !== this.generation) return host; // A newer refresh owns the header.
		this.header.empty();
		if (!note) {
			this.header.setText("Open a note to talk to its pcode session.");
			return undefined;
		}
		this.header.createDiv({ cls: "pcode-note", text: note.basename });
		const status = this.header.createDiv("pcode-status");
		if (!session) {
			status.addClass("pcode-warn");
			status.setText(
				"No session: property. Plans from /plan --obsidian get one; or run “pcode bridge: Set this note's session”.",
			);
		} else if (problem) {
			status.addClass("pcode-warn");
			status.setText(problem);
		} else if (host) {
			status.setText(`Session ${session.slice(0, 8)} · ${host.state} · ${host.workspace}`);
		} else {
			status.addClass("pcode-warn");
			status.setText(notRunningMessage(session));
		}
		return host;
	}

	/** Send `text` to the note's session and stream the reply into the panel. */
	async send(note: TFile, label: string, text: string): Promise<void> {
		const session = this.plugin.sessionFor(note);
		if (!session) {
			new Notice("This note has no session: property. Run “pcode bridge: Set this note's session” first.");
			return;
		}
		const host = await this.refresh(note);
		if (!host) {
			new Notice(notRunningMessage(session), 10000);
			return;
		}
		const entry = this.log.createDiv("pcode-entry");
		entry.createDiv({ cls: "pcode-sent", text: label });
		const reply = entry.createDiv("pcode-reply markdown-rendered");
		const activity = entry.createEl("details", { cls: "pcode-activity" });
		activity.createEl("summary", { text: "Activity" });
		const activityText = activity.createEl("pre");
		const footer = entry.createDiv("pcode-entry-footer");
		const state = footer.createSpan({ cls: "pcode-state", text: "Sending…" });

		let output = "";
		let lines: string[] = [];
		const stderrLines = new LineBuffer();
		const showActivity = (more: string[]) => {
			lines = [...lines, ...more.map(stripAnsi).filter((line) => line.trim())].slice(-ACTIVITY_LINES);
			activityText.setText(lines.join("\n"));
		};
		// One render at a time, each into its own component so the last one's
		// children are unloaded rather than piling up on the view.
		let rendered: Component | undefined;
		let rendering: Promise<void> | undefined;
		let dirty = false;
		let timer: number | undefined;
		const render = async (): Promise<void> => {
			if (rendering) {
				dirty = true;
				return rendering;
			}
			rendering = (async () => {
				do {
					dirty = false;
					if (rendered) this.removeChild(rendered);
					rendered = this.addChild(new Component());
					const target = createDiv();
					await MarkdownRenderer.render(this.app, output, target, note.path, rendered);
					reply.replaceChildren(...Array.from(target.childNodes));
				} while (dirty);
			})();
			try {
				await rendering;
			} finally {
				rendering = undefined;
			}
		};
		const schedule = () => {
			timer ??= window.setTimeout(() => {
				timer = undefined;
				void render();
			}, RENDER_DELAY_MS);
		};

		const run: Run = send(this.plugin.bin(), host, text, {
			stdout: (chunk) => {
				output += chunk;
				state.setText("Replying…");
				schedule();
			},
			stderr: (chunk) => {
				showActivity(stderrLines.push(chunk));
				if (!output) state.setText("Working…");
			},
		});
		this.runs.add(run);
		const stop = this.button(footer, "Stop following", "square", () => run.stop());
		entry.scrollIntoView({ block: "end" });
		const { code, stderr } = await run.done;
		this.runs.delete(run);
		showActivity(stderrLines.flush());
		if (timer !== undefined) window.clearTimeout(timer);
		timer = undefined;
		await rendering;
		await render();
		stop.remove();
		const problem = exitMessage(code, stderr);
		state.setText(problem || "Done");
		state.toggleClass("pcode-warn", Boolean(problem));
		if (output.trim()) {
			this.button(footer, "Copy", "copy", () => navigator.clipboard.writeText(output));
			this.button(footer, "Insert into note", "text-cursor-input", () => this.plugin.insertReply(note, output));
		}
		entry.scrollIntoView({ block: "end" });
	}
}
