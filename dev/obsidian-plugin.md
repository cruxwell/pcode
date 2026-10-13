# Testing the Obsidian bridge plugin

`npm test` in `integrations/obsidian/` covers only the pure logic in
`src/core.ts`, and `tsc` passing proves little about the UI. Run the plugin in
a real Obsidian before calling a UI change done. The first release shipped
an Ask dialog with no text field: `Modal` already has a `selection` property,
so a constructor field with that name was overwritten, and `onOpen` threw
before drawing anything. Nothing short of opening the dialog showed it.

A second Obsidian with its own profile leaves the user's running one alone
(Electron's single-instance lock is per `--user-data-dir`), and its remote
debugging port lets a script drive it:

```sh
T=/tmp/obs-test; mkdir -p $T/data $T/vault/.obsidian/plugins
cp -R integrations/obsidian/{manifest.json,main.js,styles.css} $T/vault/.obsidian/plugins/pcode-bridge/  # after npm run build
printf '["pcode-bridge"]' > $T/vault/.obsidian/community-plugins.json
printf '{"vaults":{"t":{"path":"%s","ts":1,"open":true}}}' $T/vault > $T/data/obsidian.json
~/Applications/Obsidian.app/Contents/MacOS/Obsidian --user-data-dir=$T/data --remote-debugging-port=9333 &
```

Then `playwright`'s `chromium.connect_over_cdp("http://127.0.0.1:9333")`
(it is in pcode's venv) can `page.evaluate` against `window.app`:
`app.plugins.plugins['pcode-bridge']` is the plugin instance, so you can call
its methods, read the DOM, and take screenshots.

- Commands with an `editorCallback` don't run from `executeCommandById` unless
  the editor has focus. Call the plugin's method with
  `app.workspace.activeEditor.editor` instead.
- Close dialogs with Escape. Removing `.modal-container` elements by hand
  leaves Obsidian believing a modal is open, and later ones never appear.
- For a send end to end, start a host from this checkout's venv
  (`pcode.remote.spawn_host`) and set the plugin's `pcodePath` to
  `.venv/bin/pcode`, so it runs this branch's CLI rather than the global one.
  Stop it afterwards with `pcode --attach ID -p /stop`.
