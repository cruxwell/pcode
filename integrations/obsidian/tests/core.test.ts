import assert from "node:assert/strict";
import { test } from "node:test";
import {
	LineBuffer,
	askPrompt,
	attachArgs,
	calloutInsertion,
	isSendKey,
	exitMessage,
	findHost,
	notRunningMessage,
	parseHosts,
	pcodeCandidates,
	quote,
	replyCallout,
	reviewPrompt,
	sessionOf,
	stripAnsi,
} from "../src/core.ts";

const host = (id: string, session_id: string) => ({
	id,
	session_id,
	state: "idle",
	workspace: "/repo",
	title: "",
	last_prompt: "",
	attached: 1,
});

test("hosts are parsed from pcode --hosts --json and matched like pcode does", () => {
	const hosts = parseHosts(JSON.stringify([host("aaaa1111", "abc123"), host("bbbb2222", "abd999"), { nope: 1 }]));
	assert.equal(hosts.length, 2);
	assert.equal(findHost(hosts, "abc123")?.id, "aaaa1111");
	assert.equal(findHost(hosts, "bbbb")?.id, "bbbb2222");
	assert.equal(findHost(hosts, "ab"), undefined, "an ambiguous prefix matches nothing");
	assert.equal(findHost(hosts, "  "), undefined);
	assert.throws(() => parseHosts("{}"));
});

test("the session comes from front matter", () => {
	assert.equal(sessionOf({ session: " abc123 " }), "abc123");
	assert.equal(sessionOf({ session: 42 }), "42");
	assert.equal(sessionOf({ session: "" }), undefined);
	assert.equal(sessionOf(undefined), undefined);
});

test("prompts name the note and quote the selection", () => {
	const prompt = askPrompt("/v/plans/Plan.md", " Why this? ", "line one\n\nline two\n");
	assert.equal(prompt, "About the note `/v/plans/Plan.md` (I'm asking from Obsidian):\n\n> line one\n>\n> line two\n\nWhy this?");
	assert.ok(!askPrompt("/n.md", "Q").includes(">"));
	assert.match(reviewPrompt("/n.md"), /\[!comment\]/);
	assert.equal(quote("a\nb"), "> a\n> b");
	assert.equal(replyCallout("hi"), "> [!note] pcode\n> hi\n");
});

// Apply an insertion the way the editor does: replace the line's whole content.
function apply(lines: string[], line: number, callout?: string) {
	const insertion = calloutInsertion(lines, line, callout);
	const result = [...lines];
	result.splice(insertion.line, 1, ...insertion.text.split("\n"));
	return { result, cursor: insertion.cursor, at: result[insertion.cursor.line].slice(0, insertion.cursor.ch) };
}

test("comments go after the paragraph holding the cursor, never touching other text", () => {
	const lines = ["# Title", "", "First line", "second line", "", "Next"];
	const inParagraph = apply(lines, 2);
	assert.deepEqual(inParagraph.result, ["# Title", "", "First line", "second line", "", "> [!comment] ", "", "Next"]);
	assert.equal(inParagraph.at, "> [!comment] ");
	// On the blank line between two blocks: blank lines on both sides, so the
	// next paragraph doesn't become a lazy continuation of the callout.
	const between = apply(lines, 1);
	assert.deepEqual(between.result, ["# Title", "", "> [!comment] ", "", "First line", "second line", "", "Next"]);
	assert.equal(between.at, "> [!comment] ");
	const last = apply(lines, 5);
	assert.deepEqual(last.result.slice(-3), ["Next", "", "> [!comment] "]);
	assert.equal(last.at, "> [!comment] ");
	// Whitespace-only lines count as blank (four spaces would make a code block).
	assert.deepEqual(apply(["a", "    ", "", "b"], 1).result, ["a", "", "> [!comment] ", "", "b"]);
	assert.deepEqual(apply([""], 0).result, ["> [!comment] "]);
});

test("a multi-line reply callout lands the cursor at its end", () => {
	const { result, cursor } = apply(["Para"], 0, replyCallout("one\ntwo"));
	assert.deepEqual(result, ["Para", "", "> [!note] pcode", "> one", "> two"]);
	assert.deepEqual(cursor, { line: 4, ch: 5 });
});

test("Enter sends, Shift+Enter is a new line, and IME composition never sends", () => {
	const key = (extra = {}) => ({ key: "Enter", shiftKey: false, ...extra });
	assert.equal(isSendKey(key()), true);
	assert.equal(isSendKey(key({ metaKey: true })), true);
	assert.equal(isSendKey(key({ shiftKey: true })), false);
	assert.equal(isSendKey(key({ isComposing: true })), false);
	assert.equal(isSendKey(key({ keyCode: 229 })), false);
	assert.equal(isSendKey({ key: "a", shiftKey: false }), false);
});

test("stderr is split into whole lines across chunks", () => {
	const buffer = new LineBuffer();
	assert.deepEqual(buffer.push("Running mak"), []);
	assert.deepEqual(buffer.push("e test\nDone\npar"), ["Running make test", "Done"]);
	assert.deepEqual(buffer.flush(), ["par"]);
	assert.deepEqual(buffer.flush(), []);
});

test("the message goes on stdin, never into the arguments", () => {
	assert.deepEqual(attachArgs("aaaa1111"), ["--attach", "aaaa1111", "-p"]);
});

test("exit codes become messages", () => {
	assert.equal(exitMessage(0, ""), "");
	assert.match(exitMessage(1, ""), /did not finish.*\.$/);
	assert.match(exitMessage(1, "Agent startup failed\n"), /did not finish.*: Agent startup failed$/);
	assert.equal(exitMessage(null, ""), "pcode was stopped.");
	assert.match(exitMessage(130, ""), /keeps running/);
	assert.equal(exitMessage(2, "noise\n\u001b[31mNo running session host matches 'x'.\u001b[0m\n"), "No running session host matches 'x'.");
	assert.equal(stripAnsi("\u001b]0;title\u0007a\r\n"), "a\n");
});

test("pcode is looked for where GUI apps can't see the shell's PATH", () => {
	assert.deepEqual(pcodeCandidates(" /x/pcode ", "/h"), ["/x/pcode"]);
	assert.equal(pcodeCandidates("", "/h")[0], "/h/.local/bin/pcode");
	assert.match(notRunningMessage("abc123"), /\/detach/);
});
