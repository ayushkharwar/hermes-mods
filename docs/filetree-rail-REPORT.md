# REPORT — filetree rail for Hermes Agent

## What was built

Two pieces, ports of the idea (not the code) of `claude-code-filetree`:

| File | Role |
| --- | --- |
| `plugin.yaml` | Plugin manifest. Declares `post_tool_call` in `provides_hooks`. |
| `__init__.py` | Activity recorder. `register(ctx)` adds the hook and `/filetree-clear`. |
| `filetree.mjs` | Ambient TUI widget. 40 columns, 6 rows, zone `top-right`. |
| `test_plugin.py` | 17 checks against the hook contract. |
| `test_widget.mjs` | 13 checks against the render tree. |
| `e2e_probe.py` | Runs the plugin through the real Hermes plugin loader. |
| `SMOKE.md` | Commands and real output. |

### Plugin

- Tool map: `read_file` → read, `write_file` → write, `patch` → edit,
  `search_files` → search, `terminal` → ignored.
- Accepts `path` and `file_path`. Ignores `.`, empty strings, and non-strings.
  Expands `~`. Joins a relative path to `cwd` when the hook supplies one, else
  keeps it as given.
- State file: `$HERMES_HOME/state/filetree.json`. The home comes from
  `hermes_constants.get_hermes_home()`, so profiles resolve correctly.
- Writes: `state/filetree.json.tmp` then `os.replace`. At most 2 writes per
  second. The in-memory state is authoritative.
- Caps: 40 files (newest first, one entry per path), 200 events (newest last).
- The hook body is wrapped in `try/except` and logs at debug. It never raises.
- `counts` holds read, write, and edit only. A `search` touch appears in
  `files` and `events`, not in `counts`. A write or an edit adds to that file
  entry's `writes`.

### Widget

- `defineWidgetApp({id:'filetree', mode:'ambient', zone:'top-right', width:40})`.
  It calls `openWidget` at the end of `register()`, so the rail is live without
  a slash command. `/filetree` toggles it.
- A `Poller` component polls the state file every 1000 ms inside
  `React.useEffect` with `setInterval`, and clears the interval on unmount.
  Each update goes through `updateWidget`. An unchanged snapshot causes no
  repaint.
- Fixed geometry, exactly 40 cells per row:
  2 (glyph + space) + 20 (basename) + 14 (dim parent) + 4 (right-aligned touch
  count).
- Rows: 1 header (`FILES` + `r/w/e` counts) + 5 file rows. Missing entries are
  padded with blank rows. Cold start (no file) shows the header and 5 blank
  rows. No throw.
- Long names and directories shorten from the left with `…`. Directories cut on
  a `/` so no half word appears.
- Colours come only from theme tones: `accent`, `text`, `muted`, `ok`, `warn`,
  `label`. No hex literal.

## What is proved

1. `python3 test_plugin.py` — 17 checks pass. Contract keys, newest-first
   order, counts, `terminal` ignored, absolute paths, `~` and `cwd` handling,
   junk payloads, throttle, atomic write, 40/200 caps after 250 events.
2. `node test_widget.mjs` — 13 checks pass. App contract, cold start, 12-file
   state, 40-cell rows, no string over 40, real default path,
   `readState()` against the real file, poller effect plus one live poll.
3. `python3 e2e_probe.py` — the real loader (`discover_plugins`) parses the
   manifest, loads the plugin, `has_hook('post_tool_call')` is True, and
   `invoke_hook` writes the state file.
4. The rail has no hard row cap in the TUI source (`host.tsx:265-285`,
   `appLayout.tsx:663-685`). Budget used: 6 rows, from
   `tui-widgets.md:127` ("≤ ~6 rows").

## What is not proved

- **Real Ink rendering.** `ui-tui/packages/hermes-ink/dist` is not built, so
  the card was never rendered by Ink. Row widths are proved as strings, not as
  laid-out terminal cells.
- **Live TUI run.** The widget was never hot-loaded in a running
  `hermes --tui`, and the rail never appeared on screen.
- **Install path.** `hermes plugins enable filetree-rail` was not run, and
  nothing was copied to `~/.hermes/plugins/` or `~/.hermes/tui-widgets/`
  (forbidden by the work order).
- **Gateway/multi-profile behaviour.** Hooks were fired from a plain Python
  process, not from a running gateway with several profiles.
- **Glyph width.** `○ ● ◆ ▹` are East-Asian ambiguous. A CJK-width terminal
  may draw them 2 cells wide and push a row past 40.

## Known limits

- The log is global, not per session. The file holds the last `session_id`
  only.
- Only the 5 newest files are visible. 35 more are stored.
- `search_files` rows show a touch count of 0, because the count is
  `reads + writes`.
- Disk writes lag up to 0.5 s behind the memory state.
- A file touched in a subagent appears like any other touch.
- Auto-dock runs again on every `/widgets-reload`.
- User plugins are opt-in: `plugins.enabled` must list `filetree-rail`.
- The verification wrote a real state file at
  `~/.hermes/state/filetree.json`. That is the runtime path, and the widget
  reads it.

## Side effects on this machine

- `~/.hermes/state/filetree.json` exists with test data. `/filetree-clear`
  resets it during real use.
- Temporary homes for the tests were created inside the stage dir and removed.
- No change to `~/.hermes/config.yaml`, no gateway restart, no sudo, no
  download.
