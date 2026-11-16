# savvy-progress

> **"prompt once, read forever."**

Live token, cache and cost card for the Hermes TUI, as an ambient widget plus a `/svprogress`
command.

The Python plugin reads the session database read-only and writes one small JSON snapshot.
The widget (`$HERMES_HOME/tui-widgets/savvy-progress.mjs`) polls that file twice a second and
draws a fixed-size card in the top-right corner, so nothing on screen ever reflows.

```
PROGRESS  deepseek-v4.1-flash
────────────── ◇ ─────────────
last          1.8k/2.2k ~$0.0036
cache       ████████░░░░ 96.5% avg
cost                  ~$0.1606
saved                 ~$1.9864
────────────── ◇ ─────────────
today       ~$2.2493  22 chats
tok/day        18.4M · 1.2M ag
────────────── ◇ ─────────────
codex   5 models      41.6M~$17.9381?
pi     15 models     111.3M ~$1.6343?
dsh     3 models      93.8M ~$3.0715?
others              246.7M~$22.6438?
 ·--┬----┬-----┬----┬----┬--·
──────────────────────────────
```

The printed card draws the same two sky art rows above `PROGRESS` (the block above shows
the body only) and the same ground row, so `/svprogress` and the widget agree in every
palette.

## Day boundary

`today` is a local calendar day: 00:00 to the next 00:00, not a rolling 24 h window. A
rolling window counted yesterday evening's chats as today's, which reads as a miscount. DST
is handled by asking `mktime` for the next midnight.

## Rows (widget)

| Row | Meaning |
| --- | --- |
| `logo` | Banner art: two sky rows above the values and the ground row below, three rows in every palette. `/svprogress hide logo` drops all three |
| `model` | The header line: `PROGRESS` in the 8-cell label column, the model tail right-aligned in the value column (plus the snapshot's age when it is stale). `/svprogress hide model` drops the line |
| `last` | Previous turn only: its input / output split and what that turn cost |
| `cache` | **Today's** blended hit rate: `cache_read / (input + cache_read)` over every session of the local day, `avg` tag on the right, green at 70% and above |
| `cost` | Session cost plus delegate children. `~` = estimate from the price table, `$n/a` = unpriced |
| `saved` | Dollars the cache avoided: `cache_read` priced at list input instead of the cache rate |
| `today` | Every session started today, from local 00:00 to 24:00, with its session count, so cron runs and parallel terminals cannot hide |
| `tok/day` | Every token the day pushed through a model, plus the delegate share as `· 1.2M ag` |
| `sep` | The three group separators: one after the header, one between SESSION and TODAY, one between TODAY and AGENT. `/svprogress hide sep` drops them |

The widget draws 18 rows in every palette and every separator style: two sky art rows, the
`model` header line, three separators, the ten value rows above, the ground art row and the
closing rule. A palette or separator switch recolours the block; it never changes the row
count or any row's width.

`burn` and `agents` were dropped: a per-hour rate and a delegate count read as noise beside
the dollars, and the delegate share of the day's traffic fits on the `tok/day` row.

The session token totals moved to the text card, which still prints `input`, `output` and
`cache read` rows. The widget answers "what did the last turn cost me" instead, because the
totals never changed fast enough to act on.

`cost_source` travels with each number: `provider` is a real billed figure, `table` is an
estimate from list prices, `none` means there is no honest number and the row prints `n/a`.

## Palette (widget)

Two palettes live in `THEMES` in the widget (eight foreground hexes each) plus the
matching banner art in `ART`; the plugin owns which one is active:

| Theme | Colours |
| --- | --- |
| `savvy` (default) | Neon night: label `#40E0D0`, value `#F5F5F5`, accent `#57E3FF`, ok `#00FF7F`, highlight `#FFD700`, warn `#FF9BD2`, error `#FF4D6D`, violet `#8E7BF0` |
| `grove` | Green garden: label `#4E7A5A`, value `#EAF7EE`, accent `#7CE38B`, ok `#9BE564`, highlight `#C8E86A`, warn `#FFC46B`, error `#FF6B6B`, violet `#8E7BF0` |

`ROLE_OF` maps every card role onto one of the eight colours: label column and ground art
use `label`, `model` / `last` / `tok/day` use `value`, logo and both sky art rows use
`accent`, `codex` / `pi` / `dsh` use `violet`, `cost` / `today` / `others` use `highlight`,
`saved` and the cache gauge use `ok`. The cache row keeps its threshold: `>= 70%` draws
`ok`, `>= 30%` `warn`, below that `error`.

The banner is three rows in both themes — two sky rows above the values, one ground row
under the last value row — so a theme switch recolours the card and never resizes it. All
three carry the `logo` row key, so `/svprogress hide logo` drops the whole banner.

The choice is stored in `$HERMES_HOME/state/savvy-progress.theme.json` as
`{"theme": "savvy", "sep": "gem"}`, the same shape the row toggle uses, and the payload
carries both keys. A snapshot without them, or with unknown names, draws `savvy` / `gem` —
a stale payload never blanks the card. The printed card (`/svprogress`) draws the same three
art lines and the same separator as the widget, so both surfaces agree.

## Separator (widget)

The card is a plain `Box` block, not a `Dialog`: the host hardcodes a rounded border and a
border colour on a `Dialog`, and this block is framed by its own rules instead. Rows sit in
a `Box` two cells wider than the 30-cell content on each side and centred, so the left and
right gutters stay equal as rows change width; the app width carries those cells so a
narrow rail cannot clip.

Inside the rail, the card centres itself vertically: the outer `Box` takes the rail's full
height (`height="100%"`, `flexGrow={1}`) with `justifyContent="center"`, so the leftover
rail height splits evenly above and below. No filler rows — the rail height is not known to
the widget, so a hardcoded pad would break on resize. Proof (real Ink, 40 and 60 rows): 11
blank lines above and 11 below at 40 rows, 21 and 21 at 60.

Each separator is one 30-cell row with the theme's glyph centred between two `─` runs, built
from `ROW_W` and the glyph's cell count — never hand-counted, so it cannot drift:

| `sep` | Row |
| --- | --- |
| `gem` (default) | `────────────── ◇ ─────────────` (savvy) / `───────────── ,^, ────────────` (grove) |
| `rule` | `──────────────────────────────` |
| `dots` | `· · · · · · · · · · · · · · ·` |
| `shade` | `────────── ░▒▓██▓▒░ ──────────` |

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

### Quote bank

The card no longer prints a quote; the lines live here instead, one per day, picked from the
local date so the same day always reads the same.

| Day seed | Quote |
| --- | --- |
| 0 | cache me if you can |
| 1 | warm cache, cold brew |
| 2 | hit rate is a love language |
| 3 | cheap tokens, rich taste |
| 4 | measure twice, prompt once |
| 5 | every miss is a lesson |
| 6 | no cache, no glory |
| 7 | tokens are a tax on thought |
| 8 | prompt once, read forever |
| 9 | the cache remembers |

## Fallbacks

| Failure | Behaviour |
| --- | --- |
| `state.db` missing, corrupt, locked, or an old schema | Every query returns no rows. The card shows zeros and `n/a`, the turn is never affected |
| Snapshot unreadable or half-written | The widget keeps the last good card on screen |
| Snapshot older than 120 s | The title becomes `PROGRESS · 4m` so stale numbers never look live |
| State directory read-only, or a parent path is a file | The write is skipped, the previous snapshot stays valid, the hook returns normally |
| `Infinity` / `NaN` in a cost field | Sanitised before serialisation; the file always parses as strict JSON |
| Two Hermes processes firing at once | An advisory `flock` plus an atomic `os.replace`; a busy lock skips one write instead of interleaving |
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
| `/svprogress hide logo\|cache\|last ...` | Drop one row from both surfaces |
| `/svprogress rows` | List which rows are shown |
| `/svprogress theme` | Print the active palette and the choices |
| `/svprogress theme savvy\|grove` | Switch the widget palette and the printed card's art |
| `/svprogress theme next` | Cycle to the other palette (`savvy` -> `grove` -> `savvy`) |
| `/svprogress sep` | Print the active separator style and the choices |
| `/svprogress sep gem\|rule\|dots\|shade` | Switch the separator on both surfaces |
| `/svprogress sep next` | Cycle to the next separator (`gem` -> `rule` -> `dots` -> `shade`) |

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
