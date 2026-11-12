# WORK ORDER 1: "savvy-progress" for Hermes Agent

You are a build agent. Build the mod described below, verify it with real execution, report.

Reply and write files in Simplified Technical English. Short sentences. No em dashes. No marketing words.

## Mission

Port the IDEA of the Claude Code plugin `savvy-progress` to Hermes Agent: a live token/cost
progress band for the current session. Do NOT port their code. Their file is a React .tsx module
that draws inside Claude Code's own renderer. Hermes cannot load it.

Build instead: one Python Hermes plugin (data producer) + one TUI widget (display).

## Hard rules

- STAGE DIR: `plugins/savvy-progress/`. Write ALL deliverable files there.
- NEVER write to `$HERMES_HOME/plugins/` or `$HERMES_HOME/tui-widgets/`.
- NEVER edit `$HERMES_HOME/config.yaml`. NEVER restart the gateway. NEVER use sudo.
- NEVER download anything.
- Do not invent APIs. Read the Hermes source to confirm each call:
  `$HERMES_HOME/hermes-agent/` (v0.23.1).
- Verify by RUNNING code. Paste real command output in your report. No claimed output.

## Environment facts

- HERMES_HOME = `$HERMES_HOME`
- Node v26.5.0 (`node`), python3 available.
- Hermes state DB: `$HERMES_HOME/state.db` (SQLite, table `session_model_usage`).
- Columns: `session_id, model, input_tokens, output_tokens, cache_read_tokens, cache_write_tokens,
  reasoning_tokens, estimated_cost_usd, actual_cost_usd, api_call_count, first_seen, last_seen`.
- TUI widget source of truth: `$HERMES_HOME/hermes-agent/ui-tui/src/sdk/userWidgets.ts`,
  `ui-tui/src/sdk/registry.ts`, `ui-tui/src/lib/charts.ts`, `ui-tui/src/theme.ts`.
  Read them before coding.

## Hermes plugin contract (confirmed)

Directory `<name>/plugin.yaml` + `<name>/__init__.py`.

`plugin.yaml`:
```yaml
name: savvy-progress
version: "0.1.0"
description: Session token and cost snapshot producer for the Hermes TUI progress widget
provides_hooks:
  - post_tool_call
```

`__init__.py` exposes `register(ctx)`. Inside register:
- `ctx.register_hook("post_tool_call", cb)`. The callback receives kwargs including
  `tool_name`, `args`, `result`, `status`, `session_id`. Confirm the exact names in
  `$HERMES_HOME/hermes-agent/hermes_cli/plugins.py` before relying on them.
- Optionally `ctx.register_command(name, handler, description)`.

Hooks must never raise. Wrap everything in try/except and log at debug level.

## Data contract (fixed, the widget depends on it)

File: `$HERMES_HOME/state/savvy-progress.json`, atomic write (write `.tmp`, then `os.replace`).

```json
{
  "ts": 1762830000.0,
  "session_id": "20261111_101500_ab12cd",
  "model": "deepseek-v4.1-flash",
  "provider": "custom",
  "turn": 7,
  "api_calls": 9,
  "input_tokens": 123456,
  "output_tokens": 4321,
  "cache_read": 98765,
  "cache_write": 0,
  "cache_hit_pct": 80.0,
  "cost_usd": 0.0123,
  "turns": [{"i": 1, "in": 1200, "out": 300, "cache": 800, "cost": 0.001}]
}
```

Rules:
- `turns` holds at most 24 recent per-turn deltas, oldest first. Build deltas by comparing the
  current DB totals against the previous snapshot for the same `session_id`.
- `cost_usd` = sum of `COALESCE(actual_cost_usd, estimated_cost_usd)`. If both are NULL, write 0.
- `cache_hit_pct` = cache_read / (input_tokens + cache_read) * 100, rounded to 1 decimal.
  Guard division by zero.
- Write at most once per second (throttle). No write if nothing changed.
- Open the DB read-only: `sqlite3.connect("file:$HERMES_HOME/state.db?mode=ro", uri=True)`,
  short `timeout=1.0`.

## Widget contract (confirmed)

File: `<stage>/savvy-progress.mjs`, plain ESM, no JSX, no build step.

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
- `defineWidgetApp({id: 'progress', help, mode: 'ambient', zone: 'top-right', width: 36, init, reduce, render})`.
  `Dialog` must get an explicit `width`. Pad dynamic numbers with `padStart` so the card never
  changes width while ticking.
- Widget files are loaded with `import(pathToFileURL(file))`, so inside an async function
  `await import('node:fs/promises')` is allowed. TEST this first in node. If it fails, fallback:
  read the JSON through `fetch('file:///...')` or a second approach you verify. State in the
  report which path you proved.
- Poll the JSON file every 1000 ms from a component `React.useEffect` with `setInterval`.
  Clear the interval on unmount. Keep the render row count constant per phase.
- Cold start (file missing): render the same card with zeroed values, not a different layout.
  A missing file must never throw.
- Content, 6 rows max: title `PROGRESS`, model (short), tokens `in/out`, cache hit `%`,
  cost `$`, and `sparkRows` or `sparkline` of per-turn cost. Use `gauge` for cache hit ratio
  if it fits without breaking the row count.

## Verification (all must run, paste output)

1. Widget syntax + contract smoke test, no TUI needed. Write `<stage>/test_widget.mjs`:
   - `const mod = await import('file://plugins/savvy-progress/savvy-progress.mjs?t=' + Date.now())`
   - mock sdk: `defineWidgetApp: d => captured.push(d)`, `h: (type, props, ...children) => ({type, props, children})`,
     plus stubs for every other key the file touches.
   - assert `captured.length === 1`, `init('')` returns an object (not null), `render({state, t: fakeTheme, cols: 120})`
     returns an element tree, and the tree has at most 6 text rows.
   - fakeTheme: `{color: {primary:'#fff', accent:'#fff', border:'#fff', text:'#fff', muted:'#fff', label:'#fff', ok:'#fff', error:'#fff', warn:'#fff'}}`.
   Run: `node plugins/tests/savvy-progress/test_widget.mjs`
2. Plugin smoke test. Write `<stage>/test_plugin.py`:
   - fake `ctx` object capturing hooks and commands
   - import the plugin package, call `register(ctx)`
   - invoke the `post_tool_call` callback with `tool_name='terminal'`, `args={}`, `result='ok'`,
     `status='ok'`, `session_id=<a real session id from the DB, or 'test-session'>`
   - assert the JSON file exists, parses, and has every key in the data contract
   - assert a second call inside the throttle window does NOT rewrite the file (mtime stable)
   Run: `python3 plugins/tests/savvy-progress/test_plugin.py`
3. `hermes plugins validate <stage>` if the subcommand accepts an arbitrary directory. If it
   rejects for catalog-only reasons, say so in one line, do not fight it.
4. Read back the produced JSON with `python3 script.py` (file, not `-c`) and paste it.

Avoid command patterns that trigger approval prompts: no `sudo`, no `rm -rf`, prefer
`python3 path/to/script.py` over `python3 -c`.

## Deliverables in the stage dir

```
savvy-progress/
  plugin.yaml
  __init__.py
  savvy-progress.mjs
  test_widget.mjs
  test_plugin.py
  SMOKE.md      # exact commands + real output
  REPORT.md     # what you built, what you proved, what is still unproven, known limits
```

## Final answer format

Max 25 lines:
1. Files written (paths).
2. Test commands + pass/fail + one line of real output each.
3. Unproven items (the real TUI render, gateway hook firing) stated plainly.
4. Blockers, if any.
