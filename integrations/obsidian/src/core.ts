// Pure logic for the pcode bridge: no Obsidian or Node APIs, so it can be unit tested.

/** One running pcode session host, as `pcode --hosts --json` describes it. */
export interface Host {
	id: string;
	session_id: string;
	state: string;
	workspace: string;
	title: string;
	last_prompt: string;
	attached: number;
}

export function parseHosts(output: string): Host[] {
	const data: unknown = JSON.parse(output);
	if (!Array.isArray(data)) throw new Error("pcode --hosts --json did not print a list");
	return data.filter(
		(entry): entry is Host =>
			typeof entry === "object" && entry !== null && typeof entry.id === "string",
	);
}

/** The host running `session` (a session or host ID, or a prefix of one), as pcode's own lookup matches. */
export function findHost(hosts: Host[], session: string): Host | undefined {
	const wanted = session.trim();
	if (!wanted) return undefined;
	const matches = hosts.filter(
		(host) => host.id.startsWith(wanted) || (host.session_id ?? "").startsWith(wanted),
	);
	return matches.length === 1 ? matches[0] : undefined;
}

/** The session a note's `session:` property names, if it is a usable string. */
export function sessionOf(frontmatter: Record<string, unknown> | undefined): string | undefined {
	const value = frontmatter?.session;
	if (typeof value === "string" && value.trim()) return value.trim();
	if (typeof value === "number") return String(value);
	return undefined;
}

export function quote(text: string): string {
	return text
		.replace(/\s+$/, "")
		.split("\n")
		.map((line) => (line ? `> ${line}` : ">"))
		.join("\n");
}

export function askPrompt(notePath: string, question: string, selection = ""): string {
	const parts = [`About the note \`${notePath}\` (I'm asking from Obsidian):`];
	if (selection.trim()) parts.push(quote(selection));
	parts.push(question.trim());
	return parts.join("\n\n");
}

export function reviewPrompt(notePath: string): string {
	return (
		`I've edited \`${notePath}\` in Obsidian. Re-read it and act on my answers and ` +
		"`> [!comment]` callouts: fold each one in or reply to it, remove the comments you've " +
		"handled, and log what changed, as the note's format describes. Then tell me briefly " +
		"what you changed and what's still open."
	);
}

export function goAheadPrompt(notePath: string): string {
	return `Go ahead: the plan in \`${notePath}\` is approved. Implement it.`;
}

export const COMMENT = "> [!comment] ";

/**
 * Where a callout goes for a cursor on `line`: after the paragraph (or
 * callout, or list) holding it, or in place of the blank line the cursor is
 * on, with blank lines around it so neighbouring text never runs into it.
 * Returns the line whose whole content `text` replaces, and where the cursor
 * belongs afterwards: the end of the callout.
 */
export function calloutInsertion(
	lines: string[],
	line: number,
	callout = COMMENT,
): { line: number; text: string; cursor: { line: number; ch: number } } {
	const blank = (index: number) => index < 0 || index >= lines.length || lines[index].trim() === "";
	const body = callout.replace(/\n+$/, "");
	const rows = body.split("\n");
	const last = rows[rows.length - 1].length;
	if (blank(line)) {
		const before = blank(line - 1) ? "" : "\n";
		const after = blank(line + 1) ? "" : "\n";
		const start = line + (before ? 1 : 0);
		return { line, text: before + body + after, cursor: { line: start + rows.length - 1, ch: last } };
	}
	let end = line;
	while (!blank(end + 1)) end += 1;
	return {
		line: end,
		text: `${lines[end]}\n\n${body}`,
		cursor: { line: end + 1 + rows.length, ch: last },
	};
}

export const SEND_HINT = "Enter to send, Shift+Enter for a new line";

/**
 * Whether a keydown in a question box sends it: Enter, as in chat apps, with
 * Shift+Enter for a new line. Cmd/Ctrl+Enter still sends, as it used to. An
 * Enter that confirms an input method's composition (Japanese, Chinese, ...)
 * never sends.
 */
export function isSendKey(event: {
	key: string;
	shiftKey: boolean;
	isComposing?: boolean;
	keyCode?: number;
}): boolean {
	if (event.key !== "Enter" || event.shiftKey) return false;
	return !(event.isComposing || event.keyCode === 229);
}

/** A reply kept in the note: a callout, so it reads as pcode's words and not yours. */
export function replyCallout(reply: string): string {
	return `> [!note] pcode\n${quote(reply)}\n`;
}

// Escape sequences pcode's stderr may carry (colour, cursor movement, OSC titles).
const ANSI = /\u001b\[[0-?]*[ -/]*[@-~]|\u001b\][^\u0007\u001b]*(?:\u0007|\u001b\\)|\r/g;

export function stripAnsi(text: string): string {
	return text.replace(ANSI, "");
}

/** Arguments for one message to a running host: the text itself goes on stdin. */
export function attachArgs(hostId: string): string[] {
	return ["--attach", hostId, "-p"];
}

export function exitMessage(code: number | null, stderr: string): string {
	if (code === 0) return "";
	if (code === 130) return "Stopped following; the turn keeps running in the session.";
	const last = stripAnsi(stderr).trim().split("\n").filter(Boolean).pop();
	if (code === 1) {
		const reason = "pcode did not finish this message (it was dropped or the turn failed)";
		return last ? `${reason}: ${last}` : `${reason}.`;
	}
	return last ?? (code === null ? "pcode was stopped." : `pcode exited with code ${code}.`);
}

/** Splits a stream into whole lines, keeping a partial last line for the next chunk. */
export class LineBuffer {
	private partial = "";

	push(chunk: string): string[] {
		const lines = (this.partial + chunk).split("\n");
		this.partial = lines.pop() ?? "";
		return lines;
	}

	flush(): string[] {
		const rest = this.partial;
		this.partial = "";
		return rest ? [rest] : [];
	}
}

/** Candidate paths for `pcode`, since apps started from the Dock don't get the shell's PATH. */
export function pcodeCandidates(configured: string, home: string): string[] {
	if (configured.trim()) return [configured.trim()];
	return [`${home}/.local/bin/pcode`, "/opt/homebrew/bin/pcode", "/usr/local/bin/pcode", "pcode"];
}

/** What to tell the user when the note's session has no running host. */
export function notRunningMessage(session: string): string {
	return (
		`Session ${session} isn't running in the background, so Obsidian can't reach it. ` +
		"In the terminal showing it, type /detach (the terminal stays on it). If it's closed, " +
		`run \`pcode --attach ${session}\` from its repository, then /detach. ` +
		"Keep a terminal attached: an unwatched host stops once it goes idle."
	);
}
