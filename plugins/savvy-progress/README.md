# savvy-progress

Live token, cache and cost card for the Hermes TUI, as an ambient widget plus a `/svprogress`
command.

The Python plugin reads the session database read-only and writes one small JSON snapshot.
The widget (`$HERMES_HOME/tui-widgets/savvy-progress.mjs`) polls that file twice a second and
draws a fixed-size card in the top-right corner, so nothing on screen ever reflows.

```
PROGRESS
model   deepseek-v4.1-flash
tokens          198.3k / 50.4k
cache       ████████████ 97.1%
ctx         █░░░░░░░░░░░  8.7%
cost                  ~$0.1606
burn                  ~$0.24/h
saved                 ~$1.9864
today       ~$2.2493  22 chats
tok/day              48.7M
codex        $n/a   21.6M?
pi        ~$1.6343 111.3M
dsh       ~$3.0715  93.8M
others    ~$4.7058 226.8M
agents                    none
```

## Day boundary

`today` and `tok/day` are a local calendar day: 00:00 to the next 00:00, not a rolling 24 h
window. A rolling window counted yesterday evening's chats as today's, which reads as a
miscount. DST is handled by asking `mktime` for the next midnight.

## Rows

| Row | Meaning |
| --- | --- |
| `model` | Busiest model row of the tracked session |
| `tokens` | Billed input / output tokens for that session |
| `cache` | `cache_read / (input + cache_read)`, green at 70% and above |
| `ctx` | Biggest prompt sent vs the configured context window |
| `cost` | Session cost plus delegate children. `~` = estimate from the price table, `$n/a` = unpriced |
| `burn` | Session cost per hour over the session's own wall clock, floored at 5 minutes |
| `saved` | Dollars the cache avoided: `cache_read` priced at list input instead of the cache rate |
| `today` | Every session started today, from local 00:00 to 24:00, with its session count, so cron runs and parallel terminals cannot hide |
| `tok/day` | Tokens today across every session: `input_tokens + cache_read_tokens + cache_write_tokens + output_tokens`. Cached prompts are counted, because they still went through the model |
| `agents` | Delegate children of the tracked session and their added cost |
| `codex` | Spend and tokens in the local codex transcripts, cost then token total |
| `pi` | Same for pi sessions (`~/.pi/agent/sessions`) |
| `dsh` | Same for DeepSeek Harness sessions (`~/.dsh/sessions`, read through `zstd`) |
| `others` | The three rows above combined: money spent outside Hermes, and total tokens |

## Other agents

The card can see the spend of three sibling agents, because each one writes its own
transcript on this machine:

| Agent | Source | Token rule | Cost rule |
| --- | --- | --- | --- |
| codex | `~/.codex/sessions/**/*.jsonl` | last `token_count.info.total_token_usage` per rollout | price table, else `n/a` |
| pi | `~/.pi/agent/sessions/**/*.jsonl` | every `message.usage` row | the agent's own reported cost, else the price table |
| dsh | `~/.dsh/sessions/**/session.jsonl.zstd` | every `assistant/chunk` usage chunk | price table, else `n/a` |

A `?` after a cost means at least one model in that row has no price in the table, and that
part is left out of the money instead of being called free. A model the agent bills itself
(pi reports cost per response) always wins over the table.

The scan costs about 0.4 s over the full history, so the hook never waits for it: a stale
cache returns the previous numbers and one background thread refreshes them. `/svprogress`
waits, because you asked for a printout. Results are cached in
`$HERMES_HOME/state/savvy-progress.agents.json` for 60 s. Set `SAVVY_AGENTS_OFF=1` to skip
the scan entirely.

### `/svprogress` rows

The slash card prints one line per agent plus its top three models and the combined total:

```
  other agents
  pi      3 models          1.0M ~$0.5500?
          · deepseek-v4.1…  1.0M ~$0.3000
          · paid-model        15 ~$0.2500
          · Spark-X2.6-4B…   713      $n/a
  total 3 all models      2.0M ~$0.8500?
```

## Hiding rows

Every row can be switched off. The list lives in
`$HERMES_HOME/state/savvy-progress.rows.json` (`{"hidden": ["burn", "codex"]}`), so it
survives a restart and one edit does the same thing as the command:

| Command | Effect |
| --- | --- |
| `/svprogress hide burn codex` | Stop drawing those rows |
| `/svprogress show burn` | Draw them again |
| `/svprogress reset` | Draw every row |
| `/svprogress rows` | Print which rows are shown and hidden |

Row names are the widget keys: `model`, `api`, `tokens`, `cache`, `ctx`, `cost`, `burn`,
`saved`, `today`, `daytok`, `codex`, `pi`, `dsh`, `others`, `agents`. `tokens` hides input,
output and cache-read together. The spoken labels work too (`tok/day`, `delegates`,
`context`, `calls`). A hidden row draws no line, no blank gap and no zero.

`cost_source` travels with each number: `provider` is a real billed figure, `table` is an
estimate from list prices, `none` means there is no honest number and the row prints `n/a`.

## Configuration

### Prices

The baked table lives in `PRICE_TABLE` (`__init__.py`), USD per 1M tokens, keyed by a
case-insensitive substring of the model name, longest key first. Add a rate without editing
code by writing `$HERMES_HOME/state/savvy-progress.prices.json`:

```json
{
  "deepseek-v4.1-flash": [0.30, 1.20, 0.006],
  "some-other-model": { "input": 1.0, "output": 4.0, "cache_read": 0.05 }
}
```

An override outranks a baked row, because it is the more specific statement of intent. A
malformed row is ignored, not fatal: the card falls back to the baked table.

### Context window

Read from `model.context_length` in `$HERMES_HOME/config.yaml`, cached until the file
changes, and defaulting to 1,000,000.

## Fallbacks

| Failure | Behaviour |
| --- | --- |
| `state.db` missing, corrupt, locked, or an old schema | Every query returns no rows. The card shows zeros and `n/a`, the turn is never affected |
| Snapshot unreadable or half-written | The widget keeps the last good card on screen |
| Snapshot older than 120 s | The title becomes `PROGRESS · 4m` so stale numbers never look live |
| State directory read-only, or a parent path is a file | The write is skipped, the previous snapshot stays valid, the hook returns normally |
| `Infinity` / `NaN` in a cost field | Sanitised before serialisation; the file always parses as strict JSON |
| Two Hermes processes firing at once | An advisory `flock` plus an atomic `os.replace`; a busy lock skips one write instead of interleaving |
| A codex, pi or dsh tree missing or unreadable | That agent reports `available: false`, contributes zero tokens, and is skipped on the card |
| Cross-agent scan slower than the turn | The hook returns the previous numbers and one background thread refreshes them; the scan is single-flight |
| `zstd` not installed | dsh reports no data; no transcript is guessed |
| Unpriced model | Rows print `n/a` instead of inventing a dollar figure |
| Missing `started_at` | Burn reports 0 instead of dividing by the epoch |
| Hostile payload (list where a count belongs) | Coerced to 0, the card still renders |

Files written by the plugin are atomic, mode `0600`, and never left as a `.tmp`.

## Commands

| Command | Effect |
| --- | --- |
| `/progress` | Toggle the docked card. TUI only |
| `/widgets-reload` | Reload `tui-widgets/*.mjs` after editing the widget. TUI only |
| `/svprogress` | Print the card as text, in the TUI or the classic CLI |

The docked card and `/progress` exist only in `hermes --tui`: the classic CLI has no widget
surface, and the plugin does not pretend otherwise.

## Tests

The suites live outside the plugin, in `~/toDelete/savvy-test/`:

| File | Covers |
| --- | --- |
| `test_plugin.py` | Row maths, render edge cases, strict-JSON safety, drift detection |
| `test_integration.py` | Day window edges on a synthetic DB, provider/table mixing, concurrent threads, hot-path timing |
| `test_hardening.py` | Missing/corrupt/locked DB, wrong schema, read-only dirs, price overrides, 2000-payload fuzz, 20k-row scale |
| `test_concurrency.py` | Four processes writing at once: valid JSON, no lost entries, no `.tmp` left |
| `test_widget.mjs` | Row widths in every snapshot shape, `readSnapshot`/`mergeSnapshot`, age labels, register contract |

Run them all:

```bash
cd ~/toDelete/savvy-test
for t in test_plugin.py test_integration.py test_hardening.py test_concurrency.py; do python3 "$t"; done
node test_widget.mjs
HERMES_HOME=$HOME/.hermes node live_card.mjs   # renders the card from the real snapshot
```
