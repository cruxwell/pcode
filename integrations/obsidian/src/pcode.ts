// Running the `pcode` CLI. The plugin's only channel to pcode: it never speaks
// the session host's socket protocol, which is internal to pcode.
import { spawn } from "child_process";
import { existsSync } from "fs";
import { homedir } from "os";
import { delimiter, dirname } from "path";
import { Host, attachArgs, parseHosts, pcodeCandidates } from "./core";

export function resolvePcode(configured: string): string {
	const candidates = pcodeCandidates(configured, homedir());
	return candidates.find((path) => path.includes("/") && existsSync(path)) ?? candidates[candidates.length - 1];
}

export interface Run {
	done: Promise<{ code: number | null; stderr: string }>;
	/** Stop following: pcode detaches, and the turn keeps running in the session. */
	stop(): void;
}

export function runPcode(
	bin: string,
	args: string[],
	input: string,
	on: { stdout?: (text: string) => void; stderr?: (text: string) => void } = {},
): Run {
	const path = [bin.includes("/") ? dirname(bin) : "", process.env.PATH ?? "", "/opt/homebrew/bin", "/usr/local/bin"]
		.filter(Boolean)
		.join(delimiter);
	const child = spawn(bin, args, {
		// Its own session: no controlling terminal for pcode to query, and a
		// process group to signal as a whole.
		detached: true,
		env: { ...process.env, PATH: path, NO_COLOR: "1", TERM: "dumb" },
		stdio: ["pipe", "pipe", "pipe"],
	});
	let stderr = "";
	child.stdout.setEncoding("utf8").on("data", (text: string) => on.stdout?.(text));
	child.stderr.setEncoding("utf8").on("data", (text: string) => {
		stderr += text;
		on.stderr?.(text);
	});
	const done = new Promise<{ code: number | null; stderr: string }>((resolve) => {
		child.on("error", (error) => resolve({ code: 127, stderr: `${stderr}\nCould not run ${bin}: ${error.message}` }));
		child.on("close", (code) => resolve({ code, stderr }));
	});
	// The message goes on stdin, never into a command line.
	child.stdin.on("error", () => undefined);
	child.stdin.end(input);
	return {
		done,
		stop() {
			if (child.pid !== undefined && child.exitCode === null) {
				try {
					process.kill(-child.pid, "SIGINT");
				} catch {
					child.kill("SIGINT");
				}
			}
		},
	};
}

export async function listHosts(bin: string): Promise<Host[]> {
	let output = "";
	const { code, stderr } = await runPcode(bin, ["--hosts", "--json"], "", {
		stdout: (text) => (output += text),
	}).done;
	if (code !== 0) {
		const reason = stderr.trim().split("\n").pop() || `exit code ${code}`;
		throw new Error(`pcode --hosts --json failed: ${reason}. Is pcode up to date?`);
	}
	return parseHosts(output);
}

export function send(
	bin: string,
	host: Host,
	text: string,
	on: { stdout?: (text: string) => void; stderr?: (text: string) => void },
): Run {
	return runPcode(bin, attachArgs(host.id), text, on);
}
