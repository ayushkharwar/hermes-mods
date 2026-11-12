# WORK ORDER 5: "filetree rail" for Hermes Agent

You are a build agent. Build the mod described below, verify it with real execution, report.

Reply and write files in Simplified Technical English. Short sentences. No em dashes. No marketing words.

## Mission

Port the IDEA of the Claude Code plugin `claude-code-filetree` to Hermes Agent: a live rail that
shows which files the agent reads and edits, with write/edit counts. Do NOT port their code. Their
files are React `.tsx` modules plus `hooks.json` modules, loaded by Claude Code's own renderer.
Hermes cannot load them.

Build instead: one Python Hermes plugin (activity recorder) + one TUI widget (rail display).

## Hard rules

- STAGE DIR: `plugins/filetree-rail/`. Write ALL deliverable files there.
- NEVER write to `$HERMES_HOME/plugins/` or `$HERMES_HOME/tui-widgets/`.
- NEVER edit `$HERMES_HOME/config.yaml`. NEVER restart the gateway. NEVER use sudo.
- NEVER download anything.
- Do not invent APIs. Read the Hermes source to confirm each call:
  `$HERMES_HOME/hermes-agent/` (v0.23.1).
- Verify by RUNNING code. Paste real command output in your report. No claimed output.

## Environment facts

- HERMES_HOME = `$HERMES_HOME`
- Node v26.5.0 (`node`), python3 available.
- Widget source of truth: `$HERMES_HOME/hermes-agent/ui-tui/src/sdk/userWidgets.ts`,
  `ui-tui/src/sdk/registry.ts`, `ui-tui/src/components/overlay.tsx` (zones),
  `ui-tui/src/lib/charts.ts`, `ui-tui/src/theme.ts`. Read them before coding.
  IMPORTANT: check the real rail (`top-right`) height limit in the TUI source. Report the row
  budget you found. Keep the card inside it.

## Hermes plugin contract (confirmed)

Directory `<name>/plugin.yaml` + `<name>/__init__.py`.

`plugin.yaml`:
```yaml
name: filetree-rail
version: "0.1.0"
description: Records file read/write activity for the Hermes TUI filetree rail
provides_hooks:
  - post_tool_call
```

`__init__.py` exposes `register(ctx)` with:
- `ctx.register_hook("post_tool_call", cb)`. Callback kwargs include `tool_name`, `args`,
  `result`, `status`, `session_id`. Confirm exact names in
  `$HERMES_HOME/hermes-agent/hermes_cli/plugins.py`.
- Optional `ctx.register_command("filetree-clear", handler, description)` to reset the log.

Hooks must never raise. Wrap in try/except, log at debug level.

## Tool to path mapping (confirm arg names in the tool schemas)

| tool_name | path arg | op |
|---|---|---|
| `read_file` | `path` or `file_path` | `read` |
| `write_file` | `path` or `file_path` | `write` |
| `patch` | `path` or `file_path` | `edit` |
| `search_files` | `path` (may be a dir) | `search` |
| `terminal` | none, skip | skip |

Accept both `path` and `file_path` keys. Ignore `.` and empty strings. Store absolute paths.
Normalise `~` to `$HOME`. If a path is relative, resolve it against the hook's cwd when present,
else leave it as given.

## Data contract (fixed, the widget depends on it)

File: `$HERMES_HOME/state/filetree.json`, atomic write (write `.tmp`, then `os.replace`).

```json
{
  "ts": 1762830000.0,
  "session_id": "20261111_101500_ab12cd",
  "counts": {"read": 12, "write": 3, "edit": 5},
  "files": [
    {"path": "/home/user/x.py", "op": "edit", "reads": 2, "writes": 1, "ts": 1762830000.0}
  ],
  "events": [{"ts": 1762830000.0, "op": "read", "path": "/home/user/x.py", "tool": "read_file"}]
}
```

Rules:
- `files`: most recent first, at most 40 entries, one entry per distinct path. Update counts and
  `ts` on repeat touches, move the path to the front.
- `events`: append-only ring buffer, at most 200 entries, newest last.
- Throttle disk writes to at most 2 per second. Keep the in-memory state authoritative.
- Truncate long paths for storage? No. Store full paths; the widget shortens them for display.

## Widget contract (confirmed)

File: `<stage>/filetree.mjs`, plain ESM, no JSX, no build step.

```js
export default function register(sdk) { /* defineWidgetApp({...}) */ }
```

Available `sdk` keys (exact list, from `ui-tui/src/sdk/userWidgets.ts`):
`Accordion, Box, Dialog, GridAreas, Overlay, React, Shimmer, ShimmerRows, Text, WidgetGrid,
defineWidgetApp, gauge, h, hbars, isCtrl, openWidget, sparkRows, sparkline, updateWidget,
useShimmerPhase`

Rules:
- `h(type, props, ...children)` is `React.createElement`. Never write JSX.
- Colors ONLY from theme tones: `t.color.primary|accent|border|text|muted|label|ok|error|warn`.
  Never a hex literal.
- `defineWidgetApp({id: 'filetree', help, mode: 'ambient', zone: 'top-right', width: 40, init, reduce, render})`.
- Widget files load with `import(pathToFileURL(file))`, so `await import('node:fs/promises')`
  inside an async function is allowed. TEST this first in node. If it fails, prove a fallback and
  name it in the report.
- Poll `$HERMES_HOME/state/filetree.json` every 1000 ms in a component `React.useEffect` with
  `setInterval`, cleared on unmount.
- Display: header `FILES` with counts `r/w/e`, then one row per recent file: op glyph, basename,
  dim parent dir, right-aligned touch count. Keep row count CONSTANT (pad with blank rows to the
  row budget you found). Width 40 columns exactly, no overflow. Shorten long paths from the left
  with a leading `…`.
- Cold start: file missing means header plus blank padded rows. Never throw.

## Verification (all must run, paste output)

1. `<stage>/test_widget.mjs`:
   - import the widget with a cache-busting query string
   - mock sdk: capture `defineWidgetApp` definitions, `h: (type, props, ...children) => ({type, props, children})`,
     stubs for every other key touched
   - seed a fake `filetree.json` path? No. Test the real default path, and also test one state
     object you build by hand with 12 files
   - assert `init('')` returns an object, `render({state, t: fakeTheme, cols: 120})` returns an
     element tree, and no row string exceeds 40 characters
   - fakeTheme: `{color: {primary:'#fff', accent:'#fff', border:'#fff', text:'#fff', muted:'#fff', label:'#fff', ok:'#fff', error:'#fff', warn:'#fff'}}`
   Run: `node plugins/filetree-rail/test_widget.mjs`
2. `<stage>/test_plugin.py`:
   - fake `ctx` capturing hooks
   - import the plugin, `register(ctx)`
   - fire the `post_tool_call` callback for each mapped tool with real arg shapes, including a
     `read_file` with `path`, a `write_file` with `file_path`, a `patch`, and a `terminal` call
     that must be ignored
   - assert the JSON file exists, parses, has all contract keys, `files` is newest-first, counts
     match, and the `terminal` call added nothing
   - assert the 40-file and 200-event caps hold after 250 synthetic events
   Run: `python3 plugins/filetree-rail/test_plugin.py`
3. Report the rail row budget you found in the TUI source, with the `file:line` you read it from.
4. Read back the produced JSON with `python3 script.py` (file, not `-c`) and paste a trimmed copy.

Avoid command patterns that trigger approval prompts: no `sudo`, no `rm -rf`, prefer
`python3 path/to/script.py` over `python3 -c`.

## Deliverables in the stage dir

```
filetree/
  plugin.yaml
  __init__.py
  filetree.mjs
  test_widget.mjs
  test_plugin.py
  SMOKE.md      # exact commands + real output
  REPORT.md     # what you built, what you proved, what is still unproven, known limits
```

## Final answer format

Max 25 lines:
1. Files written (paths).
2. Test commands + pass/fail + one line of real output each.
3. Rail row budget + source citation.
4. Unproven items stated plainly.
5. Blockers, if any.
