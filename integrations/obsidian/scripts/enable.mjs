// Enable the plugin in a vault: add its id to .obsidian/community-plugins.json,
// the list Obsidian loads on start. Usage: node scripts/enable.mjs VAULT
import { readFileSync, writeFileSync } from "node:fs";
import { join } from "node:path";

const ID = JSON.parse(readFileSync(new URL("../manifest.json", import.meta.url), "utf8")).id;
const file = join(process.argv[2], ".obsidian", "community-plugins.json");

let enabled = [];
try {
	enabled = JSON.parse(readFileSync(file, "utf8"));
} catch (error) {
	if (error.code !== "ENOENT") {
		// Never overwrite a list we can't read: that would disable every other plugin.
		console.error(`Not enabling ${ID}: can't read ${file} (${error.message}). Enable it in Obsidian.`);
		process.exit(1);
	}
}
if (!Array.isArray(enabled)) {
	console.error(`Not enabling ${ID}: ${file} isn't a list. Enable it in Obsidian.`);
	process.exit(1);
}
if (enabled.includes(ID)) {
	console.log(`${ID} was already enabled.`);
} else {
	writeFileSync(file, `${JSON.stringify([...enabled, ID], null, 2)}\n`);
	console.log(`Enabled ${ID} in ${file}.`);
}
