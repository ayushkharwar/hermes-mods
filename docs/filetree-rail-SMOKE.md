# SMOKE — filetree rail: exact commands and real output

All commands were run from `plugins/filetree-rail/` on
2026-11-11, Ubuntu 26.04, node v26.5.0, python3 3.14.9.

Nothing here writes to `~/.hermes/plugins/`, `~/.hermes/tui-widgets/` or
`~/.hermes/config.yaml`.

## 0. Node and the dynamic-import pattern

```
$ node --version
v26.5.0

$ node /tmp/probe.mjs
dynamic import ok: MISSING
```

`probe.mjs` body: `async function readIt(p){ const fsp = await import('node:fs/promises'); try { return await fsp.readFile(p,'utf8') } catch { return 'MISSING' } }`

Result: `await import('node:fs/promises')` inside an async function works from
an `.mjs` loaded through `import(pathToFileURL(file))`. No fallback is needed.

## 1. Plugin test

```
$ python3 plugins/filetree-rail/test_plugin.py
ok 1 - plugin module imported from plugins/filetree-rail/__init__.py
ok 2 - register(ctx) registered one post_tool_call hook
ok 3 - register(ctx) registered the /filetree-clear command
ok 4 - caps: 40 files, 200 events
ok 5 - JSON written to plugins/filetree-rail/home-a-wuo7wpf9/state/filetree.json
ok 6 - contract keys present: ts, session_id, counts, files, events
ok 7 - counts match the fired calls: read 1, write 1, edit 1
ok 8 - files newest-first, one entry per path, edit merged into the read entry
ok 9 - session_id carried from the hook kwargs
ok 10 - terminal and empty args added nothing (3 events only)
ok 11 - stored paths are absolute
ok 12 - ~ expanded to $HOME, relative path joined to the hook cwd
ok 13 - garbage hook payloads do not raise
ok 14 - throttled writes: 0 of 20 on disk mid-burst, 20 after flush
ok 15 - atomic write: no .tmp file left after replace
ok 16 - after 250 events: files capped at 40 (newest first), events at 200 (newest last)
ok 17 - real state written to $HERMES_HOME/state/filetree.json

17 checks passed, 0 failed
```

(exit code 0)

## 2. Widget test

```
$ node plugins/filetree-rail/test_widget.mjs
ok 1 - import with cache-busting query, default export is a function
ok 2 - defineWidgetApp: id/mode/zone/width as contracted (ambient, top-right, 40)
ok 3 - openWidget called once at register time (live rail, no slash command)
ok 4 - init('') returns a state object
ok 5 - render() returns an element tree (Box root)
ok 6 - cold start: 6 rows of 40 cells, no string over 40
ok 7 - cold start: FILES header + r0 w0 e0 + 5 blank padded rows
ok 8 - 12-file state: 6 rows of 40 cells, no string over 40
ok 9 - 12 files: 5 shown newest-first, left-shortened dir, right-aligned touch count
ok 10 - real default path resolves to $HERMES_HOME/state/filetree.json
ok 11 - readState() on the real path: parsed object
ok 12 - poller registers one useEffect and returns a cleanup function
ok 13 - live poll delivered 1 update(s) from the real state file

card preview (12-file state, 40 columns, 6 rows):
|FILES                          r12 w3 e5|
|○ module_0.ts         …/number-0       0|
|● module_1.ts         …/number-1       2|
|◆ module_2.ts         …/number-2       4|
|▹ module_3.ts         …/number-3       3|
|○ module_4.ts         …/number-4       5|

13 checks passed, 0 failed
```

(exit code 0)

## 3. Rail row budget in the TUI source

The rail has **no hard-coded row cap**. Read sources:

- `ui-tui/src/sdk/host.tsx:265` — `AmbientRail`: a `Box` with
  `flexDirection="column"`, `flexShrink={0}`, `paddingX={1}`, and
  `width={ambientRailWidth(side, ...)}` (`host.tsx:274-285`). No `height`, no
  `maxHeight`, no `overflow` — it stretches to the height of the flex row.
- `ui-tui/src/components/appLayout.tsx:663-685` — the rail sits inside
  `<Box flexDirection="row" flexGrow={1}>`, next to the transcript. The prompt
  and composer panes below (`appLayout.tsx:689-712`) take their rows first, so
  the rail height is `terminal rows - prompt - composer - status chrome`.
- `ui-tui/src/sdk/host.tsx:241` — `DEFAULT_RAIL_WIDTH = 44` (columns only).

The only written budget is the widget contract note:
`skills/autonomous-ai-agents/hermes-agent/references/tui-widgets.md:127` —
"Ambient widgets must stay small (≤ ~6 rows)".

Adopted budget: **6 rows total, 40 columns** (1 header + 5 file rows, no
border, so the card is exactly 6 rendered rows). That respects the documented
ambient limit and leaves room in the rail on a 24-row terminal.

## 4. End-to-end through the real Hermes plugin loader

```
$ python3 plugins/filetree-rail/e2e_probe.py | grep -v '^plugin keys loaded'
manifest: name=filetree-rail version=0.1.0 provides_hooks=['post_tool_call']
load error: None
has_hook('post_tool_call') = True
state written: plugins/filetree-rail/home-e2e-mqwgi4du/state/filetree.json
counts: {'read': 1, 'write': 0, 'edit': 1}  files: 1  events: 2
files[0]: {'path': '/home/user/e2e/read.py', 'op': 'edit', 'reads': 1, 'writes': 1, 'ts': 1794413911.2450683}
events: [('read_file', 'read'), ('patch', 'edit')]
E2E ok: real loader registered the hook, real dispatch wrote the state file
```

The unfiltered run printed 65 plugin keys, `filetree-rail` among them. This run
creates a throwaway `HERMES_HOME` inside the stage dir with
`plugins/filetree -> <stage>` and `plugins.enabled: [filetree-rail]`, then calls
`hermes_cli.plugins.discover_plugins(force=True)` and `invoke_hook`.

Without `plugins.enabled` the loader reported:
`load error: 'not enabled in config (run hermes plugins enable filetree-rail to activate)'`.
User plugins are opt-in. That is a required install step.

## 5. Read-back of the produced JSON

```
$ python3 plugins/filetree-rail/test_plugin.py
```

tail of that run (real output, file read from `~/.hermes/state/filetree.json`):

```
trimmed read-back of ~/.hermes/state/filetree.json:
{
  "ts": 1794413745.942,
  "session_id": "20261111_101500_ab12cd",
  "counts": {
    "read": 2,
    "write": 1,
    "edit": 1
  },
  "files": [
    {"path": "filetree", "op": "search", "reads": 0, "writes": 0},
    {"path": "test_plugin.py", "op": "write", "reads": 0, "writes": 1},
    {"path": "filetree.mjs", "op": "edit", "reads": 0, "writes": 1},
    {"path": "__init__.py", "op": "read", "reads": 1, "writes": 0},
    {"path": "WORKORDER.md", "op": "read", "reads": 1, "writes": 0}
  ],
  "events": [
    {"op": "read", "path": "WORKORDER.md", "tool": "read_file"},
    {"op": "read", "path": "__init__.py", "tool": "read_file"},
    {"op": "edit", "path": "filetree.mjs", "tool": "patch"},
    {"op": "write", "path": "test_plugin.py", "tool": "write_file"},
    {"op": "search", "path": "filetree", "tool": "search_files"}
  ]
}
```

The `path` values above are file names only; the JSON on disk stores the full
absolute paths.

## 6. Install steps (not executed here — the work order forbids it)

```
mkdir -p ~/.hermes/plugins/filetree-rail
cp plugin.yaml __init__.py ~/.hermes/plugins/filetree-rail/
mkdir -p ~/.hermes/tui-widgets
cp filetree.mjs ~/.hermes/tui-widgets/
hermes plugins enable filetree-rail
```

Then start the TUI (`hermes --tui`). The widget hot-loads and docks itself in
the top-right rail. `/filetree` toggles it, `/filetree-clear` resets the log.
