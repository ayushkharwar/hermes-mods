# SMOKE.md: exact commands and real output

Stage dir: `plugins/savvy-progress/`
Host: Ubuntu 26.04, node v26.5.0, Python 3.14.9, Hermes v0.23.1.
All commands below ran from the stage dir. Output is copied, not edited.

> Sections 1 to 4 are the original build and show the pre-fix state: `cost_usd` was `0.0`
> because the provider reports no cost. Section 5 is the cost fix, with current output.

---

## 1. Widget syntax and contract smoke test

```
$ node test_widget.mjs
PASS  default export is a function
PASS  defineWidgetApp called once  captured=1
PASS  app id is progress  id=progress
PASS  app is ambient  mode=ambient
PASS  app zone is top-right  zone=top-right
PASS  app width is 36  width=36
PASS  init("") returns an object  {"source":"savvy-progress.json"}
PASS  render returns an element tree
PASS  dialog receives an explicit width  width=36
PASS  dialog title is PROGRESS  title=PROGRESS
PASS  at most 6 text rows  rows=6
PASS  every row is 30 cells  30,30,30,30,30

--- cold-start card (file not read; state is zeroed) ---
| PROGRESS                       |
| model                  unknown |
| tokens                   0 / 0 |
| cache       ░░░░░░░░░░░░  0.0% |
| cost                   $0.0000 |
| t00                            |
PASS  populated card keeps every row at 30 cells  30,30,30,30,30
PASS  populated card shows the short model  model      deepseek-v4.1-flash
PASS  populated card shows cost and turn

--- populated card ---
| PROGRESS                       |
| model      deepseek-v4.1-flash |
| tokens           123.5k / 4.3k |
| cache       ██████████░░ 80.0% |
| cost                   $0.0123 |
| t07                 ▂▁▅▁▃▂█▂▇▁ |
PASS  readSnapshot returns an object
PASS  readSnapshot has numeric cost_usd  cost_usd=0
readSnapshot: {"cache_hit_pct":95.8,"cost_usd":0,"input_tokens":84685,"model":"deepseek-v4.1-flash","output_tokens":25569,"turn":0,"turns":[]}
PASS  readSnapshot follows HERMES_HOME  {"cache_hit_pct":42.5,"cost_usd":0.5,"input_tokens":0,"model":"tmp/model-x","output_tokens":0,"turn":3,"turns":[{"cost":0.1}]}

ALL CHECKS PASSED
```

Exit code 0.

---

## 2. Plugin smoke test

```
$ python3 test_plugin.py
PASS  register() registered post_tool_call  hooks=['post_tool_call']
PASS  register() registered one callback
PASS  register() ignored command conflicts safely  commands=['svprogress']

session_id = local-cc0e16af68c938f414474bf18128d33bbe8d557d6b224b048e13baa5d91c4f84
snapshot   = $HERMES_HOME/state/savvy-progress.json
db         = $HERMES_HOME/state.db
PASS  snapshot file exists after one hook call
PASS  snapshot has every data-contract key  missing=[]
PASS  session_id matches  local-cc0e16af68c938f414474bf18128d33bbe8d557d6b224b048e13baa5d91c4f84
PASS  turns is a list  len=0
PASS  turn is an int  turn=0
PASS  throttled call did not rewrite the file  mtime 1794413707086267440 -> 1794413707086267440
PASS  unchanged data did not rewrite the file
PASS  changed data rewrote the file
PASS  rewritten tokens are the new totals  in=91074
PASS  one delta appended  turns=[{'in': 1000, 'out': 200, 'cache': 0, 'cost': 0.5, 'i': 1}]
PASS  cache_hit_pct matches recomputation  96.7 vs 96.7
PASS  cost_usd matches recomputation  0.0 vs 0.0
PASS  api_calls matches recomputation  44 vs 44
PASS  turn equals the last delta index  turn=0 turns=0
PASS  _delta computes per-turn differences  {"in": 300, "out": 30, "cache": 25, "cost": 0.5}
PASS  _cache_hit_pct guards division by zero

--- snapshot ---
{
  "ts": 1794413707.162,
  "session_id": "local-cc0e16af68c938f414474bf18128d33bbe8d557d6b224b048e13baa5d91c4f84",
  "model": "deepseek-v4.1-flash",
  "provider": "custom",
  "turn": 0,
  "api_calls": 44,
  "input_tokens": 90074,
  "output_tokens": 29177,
  "cache_read": 2624896,
  "cache_write": 0,
  "cache_hit_pct": 96.7,
  "cost_usd": 0.0,
  "turns": []
}

ALL CHECKS PASSED
```

Exit code 0.

Note: `turns` is empty in the final file because the state DB did not change between the
last two hook calls. The delta path is proved separately by the "one delta appended" check
and the `_delta` unit check.

---

## 3. `hermes plugins validate`

The subcommand accepts an arbitrary directory. It ran in isolation and passed.

```
$ hermes plugins validate savvy-progress
✓ manifest — plugin.yaml parses
✓ manifest fields — name, version, description present
✓ requires_hermes — not declared
✓ config schema — not declared
✓ requires_env — all entries UPPER_SNAKE
✓ oauth — not declared
✓ loadable — entry: __init__.py
✓ python dependencies — none declared
✓ capability probe — register() ran in isolation
✓ declared tools — matches registrations
✓ declared hooks — matches registrations
✓ declared middleware — matches registrations
✓ built-in tool collisions — no tools to check
✓ security scan — safe
✓ no core override — no runtime rebinds of Hermes core
◆ Isolation — runs in the plugin host (plugins.isolation: host)

Validation passed.
```

Exit code 0.

---

## 4. Read back the produced JSON

```
$ python3 readback.py
file   : $HERMES_HOME/state/savvy-progress.json
size   : 361 bytes
mtime  : 2026-11-11 21:44:12
keys   : ts, session_id, model, provider, turn, api_calls, input_tokens, output_tokens, cache_read, cache_write, cache_hit_pct, cost_usd, turns
age_s  : 15.392

--- JSON ---
{
  "ts": 1794413652.83,
  "session_id": "local-cc0e16af68c938f414474bf18128d33bbe8d557d6b224b048e13baa5d91c4f84",
  "model": "deepseek-v4.1-flash",
  "provider": "custom",
  "turn": 0,
  "api_calls": 36,
  "input_tokens": 84685,
  "output_tokens": 25569,
  "cache_read": 1936640,
  "cache_write": 0,
  "cache_hit_pct": 95.8,
  "cost_usd": 0.0,
  "turns": []
}
```

Exit code 0. The file is rewritten by every later run of `test_plugin.py`; numbers move with
the session.

---

## Environment probe: dynamic import of `node:fs/promises`

Proved before the widget was written, from an ESM file loaded with `import(file://...)`:

```
$ node --input-type=module -e "
const mod = await import('file://$HERMES_HOME/cache/scratch/esm_probe.mjs?t=' + Date.now())
const out = await mod.default({a:1})
console.log('read result:', JSON.stringify(out))
"
read result: {"error":"ENOENT: no such file or directory, open '$HERMES_HOME/state/savvy-progress.json'"}
```

The dynamic import resolves and the read fails only on the missing file. Chosen path:
`await import('node:fs/promises')` inside an async function. No `fetch('file://')` fallback
was needed.

---

## 5. Cost fix: price table, `cost_source`, widget markers

Ran after the fix: 33 widget checks, 36 plugin checks, 0 failures.

### 5.1 Widget

```
$ node test_widget.mjs
PASS  formatCost is exported for the cost-source test
PASS  formatCost returns an 8-cell field for every source  [" $0.0123","~$0.0123","    $n/a"]
PASS  provider cost is a plain $ value  " $0.0123"
PASS  table cost is marked as an estimate  "~$0.0123"
PASS  none cost is n/a, not $0.0000  "    $n/a"
...
--- cost row, all three sources ---
provider  | cost                   $0.0123 |
table     | cost                  ~$0.0123 |
none      | cost                      $n/a |
PASS  every source keeps rows at 30 cells  30,30,30,30,30 | 30,30,30,30,30 | 30,30,30,30,30
PASS  the cost cell is 8 cells wide in all three cases
PASS  a snapshot without cost_source keeps the plain $ form  cost_source=provider
PASS  readSnapshot passes cost_source=table through  cost_source=table
PASS  readSnapshot passes cost_source=none through  cost_source=none
PASS  an invalid cost_source falls back to provider  cost_source=provider

ALL CHECKS PASSED
```

The cold-start card now reads `cost                      $n/a`, not `$0.0000`. Row width stays 30.

### 5.2 Plugin

```
$ python3 test_plugin.py
PASS  cost_source is a contract value  table
PASS  cost_usd stays numeric  0.012118
PASS  price lookup is case-insensitive  (0.12, 0.48, 0.0024)
PASS  price lookup prefers the longest key
PASS  price lookup returns None for an unknown model
PASS  actual cost wins when > 0
PASS  estimated cost is used when actual is 0
PASS  table prices input+output+cache_read  0.6023999999999999 via table
PASS  no price match marks the row  0.0 via none
PASS  _read_totals prices an unpriced row from the table  0.6024 via table
PASS  _read_totals prefers actual_cost_usd  0.125
PASS  _read_totals falls back to estimated_cost_usd  0.0625
PASS  _read_totals marks an unpriced session as none  0.0
PASS  _read_totals still returns None for an unknown session
PASS  real session cost_usd is nonzero  cost_usd=0.012118 cost_source=table
PASS  real cost matches the independent recomputation  0.012118 vs 0.012118

ALL CHECKS PASSED
```

The three source paths and the no-match case run through `_read_totals` against a throwaway
`.testdb/*.db` created and deleted inside the stage dir. `state.db` stays read-only.

### 5.3 One real snapshot from the real DB

```
$ python3 -c 'plugin.snapshot(latest session)'
session_id  : 20261111_215329_c4f44b
model       : deepseek-v4.1-flash | provider: custom
cost_usd    : 0.020652
cost_source : table
input_tokens: 68249 output_tokens: 18950 cache_read: 1402496
api_calls   : 33 | cache_hit_pct: 95.4

$ python3 readback.py | grep -E "keys|cost"
keys   : ts, session_id, model, provider, turn, api_calls, input_tokens, output_tokens, cache_read, cache_write, cache_hit_pct, cost_usd, cost_source, turns
  "cost_usd": 0.020652,
  "cost_source": "table",
      "cost": 0.000519,
```

Hand check: 68249 x 0.12/1e6 = 0.0081899, 18950 x 0.48/1e6 = 0.0090960,
1402496 x 0.0024/1e6 = 0.0033660. Sum 0.0206519, rounded to `0.020652`. Matches.

The session is live, so the totals move. A later run read `cost_usd = 0.026824`,
`cost_source = "table"`, still 14 keys on disk.

