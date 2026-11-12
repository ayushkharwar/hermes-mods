/**
 * savvy-progress — ambient TUI widget for the current session's token and cost progress.
 *
 * Reads `$HERMES_HOME/state/savvy-progress.json` (written by the `savvy-progress` Python
 * plugin) once per second and draws one fixed-size card in the top-right corner:
 *
 *   PROGRESS
 *   model      deepseek-v4.1-flash
 *   tokens        51.2k / 1.7k
 *   cache   ███████░░░░░  91.7%
 *   ctx     █░░░░░░░░░░░   3.4%
 *   cost          ~$0.0123
 *   agents   3   +$0.0412
 *
 * Column rules: the model is cut to its last path segment, every number is padded into
 * a fixed 22-cell value field, and both gauge rows keep a 12-cell gauge plus a 5-cell
 * percent, so no row can resize while ticking.
 *
 * The card follows the PRIMARY session (the user's own `local-` session). Delegate
 * children never take the model, tokens, cache or context rows: their cost is summed and
 * printed on the `agents` row, and the cost row shows the total. With no delegates the
 * agents row reads `none` at the same width.
 *
 * Cost row follows `cost_source`: `provider` prints a plain `$`, `table` marks an estimate
 * with `~`, `none` prints `$n/a`. The cost field is always 8 cells, so the card cannot resize.
 *
 * Contract: plain ESM, no JSX (`h` is React.createElement), no hex colors (theme tones
 * only), every row padded to a fixed width so the card never resizes while ticking, and a
 * missing file renders the same card with zeroed values.
 */

const WIDTH = 36
const LABEL_W = 8
const VALUE_W = 22
const GAUGE_W = 12
const ROW_W = LABEL_W + VALUE_W
const COST_W = 8
const PCT_W = 5
const DEFAULT_CONTEXT_WINDOW = 1000000
const POLL_MS = 500

const EMPTY = {
  cache_hit_pct: 0,
  context_tokens: 0,
  context_window: DEFAULT_CONTEXT_WINDOW,
  cost_source: 'none',
  cost_usd: 0,
  delegates: { cost_source: 'none', cost_usd: 0, count: 0 },
  input_tokens: 0,
  model: '',
  output_tokens: 0,
  primary_session: '',
  total_cost_usd: 0,
  turn: 0,
  turns: []
}

const COST_SOURCES = new Set(['provider', 'table', 'none'])

function num(value, fallback = 0) {
  return typeof value === 'number' && Number.isFinite(value) ? value : fallback
}

function sourceField(value, fallback) {
  return typeof value === 'string' && COST_SOURCES.has(value) ? value : fallback
}

/** `provider` = a real billed number, `table` = estimated from tokens, `none` = unknown.
 *  A snapshot written before `cost_source` existed carried provider numbers, so it keeps
 *  the plain `$` form instead of flipping to n/a. */
function costSourceOf(raw) {
  if (raw && typeof raw === 'object') {
    return sourceField(raw.cost_source, 'provider')
  }

  return 'none'
}

/** Fixed 8-cell cost field, so the card cannot resize when the source changes:
 *   table    `~$0.0123`   provider ` $0.0123`   none `    $n/a`   */
export function formatCost(value, source) {
  if (source === 'none') {
    return '$n/a'.padStart(COST_W)
  }

  const body = `$${Math.max(0, num(value)).toFixed(4)}`

  return (source === 'table' ? `~${body}` : ` ${body}`).padStart(COST_W)
}

/** Agents row: the delegate count and their added cost, or `none`. Always 22 cells,
 *  so the row is the same width with and without delegates:
 *   `      3  +$0.0412`   `                  none` */
export function formatAgents(delegates) {
  const d = delegates && typeof delegates === 'object' ? delegates : {}
  const count = Math.max(0, Math.floor(num(d.count)))

  if (!count) {
    return 'none'.padStart(VALUE_W)
  }

  const plus = `+${formatCost(d.cost_usd, sourceField(d.cost_source, 'none')).trimStart()}`
  const body = `${String(count).padStart(3)}${plus.padStart(12)}`

  return body.padStart(VALUE_W)
}

/** Coerce one parsed JSON object into the shape the rows expect. Never throws. */
function normalize(raw) {
  if (!raw || typeof raw !== 'object') {
    return { ...EMPTY }
  }

  const turns = Array.isArray(raw.turns)
    ? raw.turns
        .filter(turn => turn && typeof turn === 'object')
        .map(turn => ({ cost: Math.max(0, num(turn.cost)) }))
    : []

  const rawDelegates = raw.delegates && typeof raw.delegates === 'object' ? raw.delegates : {}
  const count = Math.max(0, Math.floor(num(rawDelegates.count)))
  const window = Math.floor(num(raw.context_window, DEFAULT_CONTEXT_WINDOW))

  return {
    cache_hit_pct: Math.min(100, Math.max(0, num(raw.cache_hit_pct))),
    context_tokens: Math.max(0, num(raw.context_tokens)),
    context_window: window > 0 ? window : DEFAULT_CONTEXT_WINDOW,
    cost_source: costSourceOf(raw),
    cost_usd: Math.max(0, num(raw.cost_usd)),
    delegates: {
      cost_source: sourceField(rawDelegates.cost_source, 'none'),
      cost_usd: Math.max(0, num(rawDelegates.cost_usd)),
      count
    },
    input_tokens: Math.max(0, num(raw.input_tokens)),
    model: typeof raw.model === 'string' ? raw.model : '',
    output_tokens: Math.max(0, num(raw.output_tokens)),
    primary_session: typeof raw.primary_session === 'string' ? raw.primary_session : '',
    total_cost_usd: Math.max(0, num(raw.total_cost_usd, num(raw.cost_usd))),
    turn: Math.max(0, num(raw.turn)),
    turns
  }
}

/** Compact token counts with a fixed ceiling of 6 cells (`999.9k`, `1.2M`). */
function fmt(n) {
  if (n >= 1e6) {
    return `${(n / 1e6).toFixed(1)}M`
  }

  if (n >= 1e3) {
    return `${(n / 1e3).toFixed(1)}k`
  }

  return String(Math.round(n))
}

function shortModel(model) {
  const tail = (model || '').split('/').pop() || 'unknown'
  const body = tail.length > VALUE_W ? `${tail.slice(0, VALUE_W - 1)}…` : tail

  return body.padEnd(VALUE_W)
}

/** Read the snapshot file. Missing or malformed file = cold start, never a throw.
 *  Exported as a test seam: the smoke test calls it without a TUI. */
export async function readSnapshot() {
  try {
    const [fsp, os] = await Promise.all([import('node:fs/promises'), import('node:os')])
    const home = (process.env.HERMES_HOME || '').trim() || `${os.homedir()}/.hermes`
    const raw = await fsp.readFile(`${home}/state/savvy-progress.json`, 'utf8')

    return normalize(JSON.parse(raw))
  } catch {
    return { ...EMPTY }
  }
}

export default function register(sdk) {
  const { Box, Dialog, React, Text, defineWidgetApp, gauge, h } = sdk

  /** One card row: fixed label column plus a fixed 22-cell value field.
   *  `align` is 'end' for numbers and 'start' for text, so nothing shifts between ticks. */
  function row(key, label, value, tone, t, align = 'end') {
    const body = String(value ?? '')
    const cell = (body.length > VALUE_W ? `${body.slice(0, VALUE_W - 1)}…` : body)
    const pad = align === 'start' ? cell.padEnd(VALUE_W) : cell.padStart(VALUE_W)

    return h(
      Box,
      { flexDirection: 'row', key, width: ROW_W },
      h(Text, { color: t.color.muted }, label.padEnd(LABEL_W)),
      h(Text, { color: tone }, pad)
    )
  }

  /** `snapshot` is an optional seed: production passes only `t` and the card polls;
   *  the smoke test seeds one to exercise the populated layout without a TUI. */
  function Card({ t, snapshot }) {
    const [snap, setSnap] = React.useState(() => (snapshot ? normalize(snapshot) : { ...EMPTY }))

    React.useEffect(() => {
      let alive = true
      let busy = false

      const tick = async () => {
        if (busy) {
          return
        }

        busy = true

        try {
          const next = await readSnapshot()

          if (alive) {
            setSnap(next)
          }
        } catch {
          // readSnapshot never throws; a broken repaint must not kill the interval.
        } finally {
          busy = false
        }
      }

      void tick()

      const id = setInterval(() => {
        void tick()
      }, POLL_MS)

      return () => {
        alive = false
        clearInterval(id)
      }
    }, [])

    const hit = snap.cache_hit_pct
    const hitTone = hit >= 70 ? t.color.ok : hit >= 30 ? t.color.warn : t.color.error
    const ctxRatio = snap.context_window > 0
      ? Math.min(1, Math.max(0, snap.context_tokens / snap.context_window))
      : 0
    const ctxTone = ctxRatio >= 0.9 ? t.color.error : ctxRatio >= 0.7 ? t.color.warn : t.color.text

    /* Fixed-cell fields: 12-cell gauge plus a 5-cell percent on both gauge rows. */
    const gaugeText = String(gauge(hit / 100, GAUGE_W)).slice(0, GAUGE_W).padEnd(GAUGE_W)
    const pctText = `${hit.toFixed(1)}%`.padStart(PCT_W)
    const ctxGauge = String(gauge(ctxRatio, GAUGE_W)).slice(0, GAUGE_W).padEnd(GAUGE_W)
    const ctxPct = `${(ctxRatio * 100).toFixed(1)}%`.padStart(PCT_W)
    const hasAgents = snap.delegates.count > 0

    return h(
      Box,
      { flexDirection: 'column', width: ROW_W },
      row('model', 'model', shortModel(snap.model), t.color.text, t, 'start'),
      row('tokens', 'tokens', `${fmt(snap.input_tokens)} / ${fmt(snap.output_tokens)}`, t.color.text, t),
      row('cache', 'cache', `${gaugeText} ${pctText}`, hitTone, t),
      row('ctx', 'ctx', `${ctxGauge} ${ctxPct}`, ctxTone, t),
      row('cost', 'cost', formatCost(snap.total_cost_usd, snap.cost_source), t.color.accent, t),
      row('agents', 'agents', formatAgents(snap.delegates), hasAgents ? t.color.text : t.color.muted, t)
    )
  }

  const app = defineWidgetApp({
    id: 'progress',
    help: 'session tokens, cache hit and cost for this session',
    mode: 'ambient',
    zone: 'top-right',
    width: WIDTH,

    init() {
      // Ambient app: no launch argument, no input. The card owns its polling loop.
      return { source: 'savvy-progress.json' }
    },

    reduce(state) {
      return state
    },

    render({ t }) {
      return h(Dialog, { title: 'PROGRESS', width: WIDTH }, h(Card, { t }))
    }
  })

  // Always-on: dock the card as soon as the widget file loads, so the panel is
  // permanent and needs no /progress. Typing /progress still toggles it off and on.
  if (typeof sdk.openWidget === 'function') {
    sdk.openWidget(app, app.init(''))
  }
}
