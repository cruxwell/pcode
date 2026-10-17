# Keybindings

Use `/bind` to customize the main prompt's action keys without replacing the
whole editor keymap. For example, give the copy action another key:

> /bind e @copy

With the default prefix, **Ctrl+E** now copies your draft, or the last response
when the draft is empty, instead of moving to the end of the line. Ctrl+Y still
works: adding a binding does not remove another key for the same action. To move copy rather than duplicate it, also run
`/unbind y`.

Bindings apply only to the main prompt. They do not change
[popup keys](commands.md#popup-keys), search fields, or ordinary editor motions.

## Sending and newlines

In the main prompt:

| Key | Action |
| --- | --- |
| Enter | Send using the selected send mode, or accept a selected completion |
| Shift+Enter | Insert a newline |
| Ctrl+Enter | Interrupt the current turn and send the draft; send normally when idle |
| Ctrl+J | Insert a newline (portable fallback) |

Ctrl+Enter overrides the selected send mode for that send only. An empty draft
has no effect. Slash commands and `!shell` drafts retain their normal behavior.

Shift+Enter and Ctrl+Enter require a terminal that reports modified Enter keys
separately. Pcode requests extended keyboard reporting while the editor owns the
terminal and restores the previous mode when it hands the terminal back. Terminal
multiplexers must support forwarding those keys too. If Shift+Enter still submits,
use Ctrl+J and check your terminal or multiplexer configuration; pcode cannot
distinguish keys that arrive as identical bytes. In tmux, pcode requests its
extended-key mode while editing and restores the previous mode afterward; tmux's
`extended-keys` option must not be `off`. Outside tmux, pcode requests Kitty's
keyboard protocol. Other terminals need to send distinct modified-key reports
without that request, or use a terminal mapping for Shift+Enter to Ctrl+J.

## Prefixes and action menus

The default global `key_prefix` is **`ctrl`**: hold Ctrl while pressing the
action key, such as **Ctrl+L** for models or **Ctrl+Y** to copy. There is no global
action-menu leader by default. **Ctrl+/** opens contextual help, and the shortcuts
it lists still work while it is open.

To use a global leader, set it explicitly:

> pcode config set key_prefix ctrl+b

After restarting, press and release Ctrl+B, then press an action key. The leader
opens a menu showing available actions, including custom bindings; **Esc**
dismisses it without changing your draft.

Choose another global leader, a sequence of leaders, or restore direct Ctrl chords:

> pcode config set key_prefix ctrl+p

> pcode config set key_prefix "ctrl+x ctrl+p"

> pcode config set key_prefix ctrl

Restore the default with `pcode config unset key_prefix`. Restart the terminal
session after changing prompt prefix settings. A leader takes over its old
editor function; for example, a configured Ctrl+B replaces backward-character
movement, so use the left arrow instead. If tmux also uses Ctrl+B, send its prefix
through or choose a different pcode leader. See
[Shortcut prefix](commands.md#shortcut-prefix) for accepted global leader keys
and terminal restrictions.

In direct `ctrl` mode, an action key such as `y` means **Ctrl+Y**, not a leader
followed by `y`. Only terminal-safe Ctrl chords are available this way. Other
mappings require a real global leader or an enabled vi normal-mode leader.
Bindings never take over reserved chords such as Ctrl+C, Ctrl+J, or Enter.
For example, `/bind c @copy` works behind Ctrl+B or the vi leader; it does not
replace Ctrl+C in direct `ctrl` mode.

## Managing bindings

| Command | Effect |
| --- | --- |
| `/bind` or `/bind list` | List effective prompt bindings, distinguishing defaults, custom bindings, and disabled keys |
| `/bind KEY` | Show the binding for one key |
| `/bind KEY /command args` | Save a slash command and its arguments for that key |
| `/bind KEY @action` | Save one named built-in action for that key |
| `/unbind KEY` | Disable a key, including a default binding |
| `/bind reset KEY` | Restore that key's default, or remove it if it was an added key |
| `/bind reset` | Remove all customizations and restore every default |
| `/bind actions` | List the available named built-in actions |

`KEY` is one printable, non-whitespace character, not a chord name such as
`ctrl+c`. With a leader, keys can include uppercase letters, digits, punctuation,
and letters whose Ctrl chords are reserved. Uppercase and lowercase keys are
distinct behind a leader. These mappings still need a usable leader when no
terminal-safe direct Ctrl chord exists.

For example, configure a global leader and bind a command with arguments:

> pcode config set key_prefix ctrl+b

Restart pcode, then run:

> /bind T /show-thinking off

Press Ctrl+B, then uppercase `T`, to turn thinking display off. The arguments are
saved verbatim; pressing the key runs the command rather than typing it into the
editor. Use `/bind T` to inspect it, `/unbind T` to disable it, or `/bind reset T`
to remove the added key.

A target is either a slash command with its arguments or one named `@action`.
Bindings are not shell commands or keystroke macros. Unknown commands and invalid
declared argument choices are rejected when you set a binding. Commands that
accept free-form arguments, such as `/config`, validate their own syntax when
run. A saved extension command can become unavailable if that extension is
unloaded; pressing its key then reports an error without touching your draft or
sending a prompt to the model.

### Drafts and command behavior

A bound key runs its target directly. It does not insert command text into the
editor, replace the draft, move its cursor, or submit it to the model. Custom
bindings appear alongside the built-in actions in the prompt's action menu.

That protects the draft from the shortcut itself, not from the command's intended
behavior. A binding to a session-switching command still switches sessions; a
binding to `/quit` still exits. Choose the target you actually want to run.

## Default actions

These are the built-in prompt mappings. Hold Ctrl while pressing each key by
default (for example, `l` means Ctrl+L and `^` means Ctrl+^), or use your configured
global or vi leader. Ctrl+/ browses contextual help. `/bind actions` lists the named
targets you can reuse on another key.

| Key | Named target | Action |
| --- | --- | --- |
| `s` | `@send-mode` | Cycle the send mode for the next send |
| `l` | `@model` | Open the model picker |
| `n` | `@effort-up` | Increase thinking effort |
| `p` | `@effort-down` | Decrease thinking effort |
| `o` | `@tasks` | Show or hide the task panel |
| `t` | `@thinking` | Choose thinking visibility |
| `g` | `@commands` | Show or hide command output in scrollback |
| `^` | `@previous-session` | Return to the session previously shown in this terminal |
| `y` | `@copy` | Copy the draft, or the last response when the draft is empty |
| `q` | `@side-threads` | Open the [`/btw` panel](side-questions.md#the-panel), reply in it, or show its next thread |

Two more defaults run a slash command, much as if you had bound them yourself;
`/unbind KEY` disables them and `/bind reset KEY` restores them:

| Key | Command | Action |
| --- | --- | --- |
| `v` | `/show-edits` | Show or hide edit diffs in scrollback |
| `]` | `/group-tools` | Fold runs of tool calls into one line, or expand them |

They take Ctrl+V and Ctrl+], which the Emacs-style editor leaves unbound or
uses only for character search. With vi editing, Ctrl+V is also quoted insert
and visual-block selection; run `/unbind v` to get those back. Terminals that
paste on Ctrl+V never send it to pcode, so there use `/bind` to put
`/show-edits` on another key.

`@copy` is different from binding
`/copy`, which operates on responses rather than choosing between the draft and
the last response. Likewise, `@thinking` opens the visibility selector, while
`/show-thinking off` selects a specific mode.

Adding or overriding one key leaves all other defaults intact. To restore `y`
after disabling or overriding it, run `/bind reset y`.

## Optional vi editing

The prompt uses Emacs-style editing by default. Enable vi editing for the next
launch:

> pcode config set editing_mode vi

The editor starts in insert mode. Escape enters normal mode; `i` or `a` returns
to insert mode. Normal-mode motions and editing commands work, while Enter still
submits and Ctrl+J inserts a newline. Restore Emacs editing with
`pcode config unset editing_mode` and restart.

### Leave insert mode with jj

To use `jj` instead of reaching for Escape:

> pcode config set vi_escape_sequence jj

Restart after setting it. This requires `editing_mode vi` and applies only in the
main prompt's insert mode, not in popup search fields. Escape still works. Other printable sequences without
spaces, such as `jk`, are allowed. Type the sequence without pausing for a second
between keys: a partial sequence waits up to one second before insertion, and a
nonmatching next key inserts the pending text immediately. Bracketed paste treats
the sequence as text, not a mode change. Restore Escape-only behavior with
`pcode config unset vi_escape_sequence` and restart.

### Use Space as the vi leader

Add a leader that works only in the main prompt's vi normal mode:

> pcode config set vi_key_prefix '<space>'

Inside pcode, use `/config set vi_key_prefix <space>` instead. The shell command
needs quotes so the shell does not interpret `<space>` as redirection. Restart
to apply it. The default is `off`; a single printable character such as `,` or
`\` also works.

After Escape or `jj`, press Space to see every key the prompt understands, the
same list Ctrl+/ shows, then press an action key. Space `l` opens the model
picker; after `/bind c @copy`, Space `c` copies your draft or last response.
The global prefix and vi leader share the same mapping, so customization applies
to both. The global prefix continues to work too.

Escape, Ctrl+C, or pressing the vi leader again dismisses its menu. If your vi
leader is also an action key, that key dismisses its own menu instead of running
the action; use the global shortcut or bind the action to another key.

The vi leader is inactive in insert, replace, and visual modes, while an operator
awaits a motion, and in popup search fields. Space still inserts a space in
insert mode, and bracketed paste never invokes shortcuts. Disable the vi leader
with `pcode config unset vi_key_prefix` and restart. See
[Optional vi editing](commands.md#optional-vi-editing) for further editing and
newline details.

## Storage and when changes apply

Bindings are user-wide, not project settings. `/bind` and `/unbind` save to
`bindings.json` in the user configuration directory:

1. `$PCODE_CONFIG_DIR`, when set;
2. otherwise `$XDG_CONFIG_HOME/pcode`;
3. otherwise `~/.config/pcode`.

They are separate from `preferences.json`; a project's `.pcode/preferences.json`
cannot define them. Writes are locked and atomic so simultaneous edits do not
partially overwrite the file.

Binding changes apply immediately in the terminal where you make them. Other
running terminals do not automatically synchronize: they read the file on their
next start or when you make a `/bind` management edit there (including `/unbind`).
Changing `key_prefix`, `editing_mode`, `vi_escape_sequence`, or `vi_key_prefix`
requires restarting the prompt. See
[Configuration](configuration.md#keybindings) for the storage overview.
