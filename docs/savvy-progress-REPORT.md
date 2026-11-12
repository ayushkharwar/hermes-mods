# REPORT: savvy-progress for Hermes Agent

Built in `plugins/savvy-progress/`. Nothing was installed, copied to
`~/.hermes/plugins/` or `~/.hermes/tui-widgets/`, and `config.yaml` was not touched.
No downloads, no sudo, no gateway restart.

## What was built

Two independent pieces, joined by one JSON file.

```
Hermes turn
  └─ post_tool_call hook
       └─ __init__.py  ── read ~/.hermes/state.db (read only)
            └─ ~/.hermes/state/savvy-progress.json   (atomic .tmp + os.replace)
                 └─ savvy-progress.mjs ── polls every 1000 ms
                      └─ ambient widget card, top-right, width 36
```

### Producer: `plugin.yaml` + `__init__.py`

- `plugin.yaml` declares `name`, `version`, `description`, and `provides_hooks: [post_tool_call]`.
- `register(ctx)` calls `ctx.register_hook("post_tool_call", _on_post_tool_call)` and
  `ctx.register_command("svprogress", ...)`.
- The callback reads `session_id` from the hook payload and ignores the other fields.
  Signature is `**kwargs`, so it accepts the full payload.
- The DB opens read only: `sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=1.0)`.
- Throttle: one write per second (`MIN_WRITE_INTERVAL_S`). A change inside the window is
  dropped, not queued. No write when the payload equals the last one on disk.
- Last snapshot on disk doubles as the baseline for deltas, so a restart does not lose the
  turn history and does not invent a giant delta.
- `turns` keeps 24 entries, oldest first, each `{i, in, out, cache, cost}`.
- Cost resolution per row: `actual_cost_usd` > 0, then `estimated_cost_usd` > 0
  (`cost_source="provider"`), then `PRICE_TABLE` (`"table"`), else `0.0` with `"none"`.
  `PRICE_TABLE` is USD per 1,000,000 tokens as `(input, output, cache_read)`, keyed by
  case-insensitive model substring, longest key first. Add a line to price a new model.
- The snapshot key is `cost_source`, not the DB column of the same name.
- The hook body is wrapped in `try/except` and logs at debug level. `register()` also
  guards the command registration.

### Consumer: `savvy-progress.mjs`

- Plain ESM, `export default function register(sdk)`, no JSX. `h` is `React.createElement`.
- `defineWidgetApp({id:'progress', help, mode:'ambient', zone:'top-right', width:36, init, reduce, render})`.
- `Dialog` gets `width: 36`. Each row is padded to 30 cells with `padEnd`/`padStart`, so the
  card never changes width while ticking.
- Colors come only from `t.color.muted|text|accent|ok|warn|error`. No hex literal.
- `React.useEffect` starts the poll on mount and clears the interval on unmount. A busy flag
  stops overlapping reads.
- File read uses `await import('node:fs/promises')` plus `node:os` inside an async function.
  This path was proved in node before the widget was written.
- Cold start (file missing, bad JSON, wrong shape) renders the same card with zeroed values.
  The reader returns `EMPTY`; it never throws.

## Contract checks done against the Hermes source

| Claim | Where it was read |
|---|---|
| Hook name and kwargs (`tool_name`, `args`, `result`, `status`, `session_id`, ...) | `model_tools.py:697-719`, `_emit_post_tool_call_hook` |
| `register_hook` / `register_command` signatures | `hermes_cli/plugins_context.py:741`, `:456` |
| `sdk` key list (21 keys) | `ui-tui/src/sdk/userWidgets.ts:36-57` |
| Widget load path and cache-busted `import()` | `ui-tui/src/sdk/userWidgets.ts:61,117,125` |
| `defineWidgetApp` shape, `init/reduce/render`, `mode`, `zone`, `width` | `ui-tui/src/sdk/registry.ts:7`, `ui-tui/src/sdk/types.ts:42-73` |
| `Dialog` accepts `title` and `width` | `ui-tui/src/components/overlay.tsx:69-96` |
| `gauge` and `sparkline` signatures, fixed width behavior | `ui-tui/src/lib/charts.ts:19,61` |
| Theme tone names | `ui-tui/src/theme.ts:6-51` |

## What was proved by running code

| Check | Result |
|---|---|
| Widget module imports, `defineWidgetApp` called once | PASS |
| `init('')` returns an object, app id/mode/zone/width correct | PASS |
| `render({state, t: fakeTheme, cols:120})` returns an element tree | PASS |
| Card has 6 text rows at most, every row 30 cells (cold and populated) | PASS |
| `readSnapshot` follows `HERMES_HOME`, parses and normalizes | PASS |
| `register(ctx)` registers one `post_tool_call` callback | PASS |
| One hook call writes a snapshot with every contract key | PASS |
| A second call inside 1 s does not touch the file (mtime equal) | PASS |
| Same data after the window expired still does not rewrite | PASS |
| Changed data after the window expired rewrites, delta appended | PASS |
| `cache_hit_pct`, `cost_usd`, `api_calls` match an independent DB recomputation | PASS |
| `_delta` and `_cache_hit_pct` guard cases | PASS |
| `hermes plugins validate <stage>` | PASS, 16 rows, isolation note only |

Full commands and output: `SMOKE.md`.

## Schema differences found (work order vs real DB)

1. `session_model_usage` has `billing_provider`, not `provider`. The JSON field `provider` is
   filled from `billing_provider` with the tag after `:` removed (`custom:Api.river.ai` to
   `custom`).
2. `estimated_cost_usd` and `actual_cost_usd` are `NOT NULL DEFAULT 0`, and this provider lane
   (`billing_provider` `custom` / `api.river.ai`) writes `0.0` for both with
   `cost_status='unknown'`. 321 sessions, 0 nonzero cost rows. The old
   `CASE WHEN actual_cost_usd > 0 ...` sum therefore returned `0.0` and the card read
   `$0.0000`. Fixed: cost resolves per row as actual, then estimated, then `PRICE_TABLE`
   (USD per 1M tokens, keyed by case-insensitive model substring), else `0.0` with
   `cost_source="none"`. The JSON carries `cost_source` so the card can mark an estimate.
3. Real session ids look like `local-cc0e16af...` (a hash), not `20261111_101500_ab12cd`.
   The producer copies whatever `session_id` the hook gives it, so the format does not matter.
4. Rows exist for auxiliary tasks (`task='approval'`). They are summed with the main task,
   because the cost is real. The `model` and `provider` fields come from the row with the
   highest `api_call_count`.

## Unproven

- **The real TUI render.** No widget was loaded into a running TUI. The stage dir must not
  write to `~/.hermes/tui-widgets/`, so the loader path (`loadUserWidgets`, hot reload,
  ambient rail placement) was not exercised. Only the mock-SDK contract test ran.
- **The hook firing inside Hermes.** The plugin was never enabled in a live `hermes` session.
  `hermes plugins validate` ran `register()` in the capability probe, and the callback was
  called directly with the documented kwargs. Whether `post_tool_call` actually reaches this
  callback during a real turn is unproven here.
- **Live cost numbers.** This session's provider reports `cost_status='unknown'` and both
  cost columns are `0.0`, so `cost_usd` comes from `PRICE_TABLE` and reads `~$0.0164`
  (`cost_source="table"`). The seeded rates are a reading of the provider's list price, not a
  bill: they are unverified against an invoice.

## Known limits

- Cost is an estimate on any provider that does not price its calls. The card marks it with
  `~`, so an estimate is never mistaken for a bill.
- `cache_write_tokens` is not priced: the table carries no cache-write rate. A session whose
  traffic is mostly cache writes will read low.
- A model that is missing from `PRICE_TABLE` yields `cost_source="none"` and the card shows
  `$n/a` instead of a fake `$0.0000`. Add a line to the table to price it.
- `cache_hit_pct` trusts the DB's split between `input_tokens` and `cache_read_tokens`.
  A provider that counts cache reads inside `input_tokens` will show a ratio that is too low.
- Throttling drops a change instead of deferring it. A burst of tool calls shows the last
  state after the next second, not every state.
- Turn deltas reset when the snapshot file is deleted or when the session id changes. The
  history is per session by design.
- The card is 5 content rows plus the title, and the sparkline is 22 cells. The cost field is
  a fixed 8 cells, and the row is `padStart(22)`, so a cost wider than `$999.9999` shifts
  characters instead of widening the card.
- The plugin writes `$HERMES_HOME/state/savvy-progress.json` on every run of
  `test_plugin.py` (that path is the data contract, not the forbidden install path). Delete
  that one file to remove all traces.

## Files in the stage dir

```
plugin.yaml          manifest, one hook declared
__init__.py          producer: DB read, throttle, delta, atomic write, /svprogress
savvy-progress.mjs    consumer: ambient card, 1 s poll, fixed width
test_widget.mjs      mock-SDK contract test, also prints both cards
test_plugin.py       hook, file, throttle and maths test against the real DB
readback.py          reads the produced JSON and prints it
SMOKE.md             exact commands and real output
REPORT.md            this file
```

## Primary / delegate split and the context bar

The card is the user's own session. `sessions.parent_session_id` is the authority:

```sql
SELECT s.id, s.parent_session_id, s.model, s.input_tokens, s.output_tokens,
       s.cache_read_tokens, s.cache_write_tokens, s.estimated_cost_usd,
       s.actual_cost_usd, COALESCE(s.last_activity_at, u.seen) AS seen
FROM sessions s
LEFT JOIN (SELECT session_id, MAX(last_seen) AS seen
           FROM session_model_usage GROUP BY session_id) u ON u.session_id = s.id
```

- primary: `parent_session_id IS NULL` and id `local-*`, newest `seen`, inside 6 hours.
  Otherwise the newest session of any kind.
- delegates: children of the primary plus their descendants, depth at most 3, cycle-safe,
  active within 6 hours. Read from the per-session token and cost columns; cost resolves
  actual, then estimated, then `PRICE_TABLE`. The primary is never its own delegate.
- no `sessions` table (old database): the id-prefix rule decides, as a last resort.
- `context_tokens` is one model row's `(input + cache_read + cache_write)` divided by that
  row's `api_call_count`, max over rows. The plain sum is lifetime throughput, not the
  prompt: on this box it reads 12.0M against a 1M window, so the card would sit at 100%
  forever. `context_window` is `model.context_length` from `config.yaml`, fallback 1e6.
- the snapshot has 19 keys: the original 14, plus `primary_session`, `delegates`,
  `total_cost_usd`, `context_tokens`, `context_window`.
