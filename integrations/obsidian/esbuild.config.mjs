// Bundles src/main.ts into main.js, the file Obsidian loads. `obsidian` and
// Node built-ins are provided by the app at runtime.
import { builtinModules } from "node:module";
import esbuild from "esbuild";

await esbuild.build({
	entryPoints: ["src/main.ts"],
	bundle: true,
	outfile: "main.js",
	format: "cjs",
	platform: "node",
	target: "es2022",
	external: ["obsidian", "electron", ...builtinModules, ...builtinModules.map((name) => `node:${name}`)],
	sourcemap: process.argv.includes("--dev") ? "inline" : false,
	logLevel: "info",
});
