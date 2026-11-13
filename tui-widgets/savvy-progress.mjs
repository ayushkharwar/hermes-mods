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
 *   burn            ~$0.55/h
 *   saved           ~$0.2401
 *   today      ~$2.1871  25 chats
 *   tok/day           18.4M
 *   codex     ~$0.0000 21.6M?
 *   pi        ~$1.6343 111.3M
 *   dsh       ~$3.0715  93.8M
 *   others    ~$4.7058 226.8M
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
 * missing file keeps the last good snapshot on screen (the title then shows its age).
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
/** A snapshot this old is shown with its age in the title, so stale numbers never look live. */
const STALE_S = 120

const EMPTY = {
  burn_source: 'none',
  burn_usd_per_hour: 0,
  cache_hit_pct: 0,
  cache_saved_source: 'none',
  cache_saved_usd: 0,
  context_tokens: 0,
  context_window: DEFAULT_CONTEXT_WINDOW,
  cost_source: 'none',
  cost_usd: 0,
  day_cost_usd: 0,
  day_sessions: 0,
  day_source: 'none',
  day_tokens: 0,
  others: [],
  others_cost_usd: 0,
  others_source: 'none',
  others_tokens: 0,
  others_unpriced: false,
  hidden_rows: [],
  delegates: { cost_source: 'none', cost_usd: 0, count: 0 },
  input_tokens: 0,
  model: '',
  output_tokens: 0,
  primary_session: '',
  total_cost_usd: 0,
  turn: 0,
  turns: [],
  ts: 0
}

const COST_SOURCES = new Set(['provider', 'table', 'agent', 'none'])

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

  return (source === 'table' || source === 'agent' ? `~${body}` : ` ${body}`).padStart(COST_W)
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

/** Burn row: dollars per hour, 2 decimals, always 22 cells. `~` = estimated from tokens,
 *   `$n/a/h` when the session has no priced cost yet:
 *   `            ~$0.55/h`   `              $n/a/h` */
export function formatBurn(value, source) {
  if (source !== 'provider' && source !== 'table') {
    return '$n/a/h'.padStart(VALUE_W)
  }

  const body = `$${Math.max(0, num(value)).toFixed(2)}/h`

  return (source === 'table' || source === 'agent' ? `~${body}` : ` ${body}`).padStart(VALUE_W)
}

/** Saved row: dollars the cache avoided, 4 decimals like the cost row. */
export function formatSaved(value, source) {
  if (source !== 'provider' && source !== 'table') {
    return '$n/a'.padStart(VALUE_W)
  }

  const body = `$${Math.max(0, num(value)).toFixed(4)}`

  return (source === 'table' ? `~${body}` : ` ${body}`).padStart(VALUE_W)
}

/** Today row: every session in the window, as cost plus chat count. Fixed 22 cells:
 *   `      ~$2.1871  25 chats`   `                      none` */
export function formatToday(value, source, sessions) {
  const count = Math.max(0, Math.floor(num(sessions)))

  if (!count) {
    return 'none'.padStart(VALUE_W)
  }

  if (source !== 'provider' && source !== 'table') {
    return `${String(count)} chats`.padStart(VALUE_W)
  }
  const cost = formatCost(value, source).trimStart()
  const tail = `${count} chat${count === 1 ? '' : 's'}`

  return `${cost}  ${tail}`.padStart(VALUE_W)
}

/** Day token row: every token the day pushed through a model (input + cache read +
 *  cache write + output), compacted into 6 cells and padded to the value field:
 *   `                18.4M`   `                    0` */
export function formatDayTokens(value) {
  return fmt(Math.max(0, num(value))).padStart(VALUE_W)
}

/** One other-agent row: cost then that agent's token total, or `none` when the agent has
 *  no transcript on this machine. `?` marks a cost that leaves an unpriced model out.
 *  Fixed 22 cells:  ``~$1.6343 111.3M``   ``      $n/a 21.6M?``   ``                  none`` */
export function formatAgent(entry) {
  if (!entry || typeof entry !== 'object') {
    return 'none'.padStart(VALUE_W)
  }

  const tokens = Math.max(0, num(entry.tokens))

  if (!tokens && !num(entry.cost_usd)) {
    return 'none'.padStart(VALUE_W)
  }

  const source = sourceField(entry.cost_source, 'none')
  const cost = formatCost(entry.cost_usd, source).trimStart()
  const mark = entry.unpriced && source !== 'none' ? '?' : ''

  return `${cost}${mark} ${fmt(tokens)}`.padStart(VALUE_W)
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
  const others = Array.isArray(raw.others)
    ? raw.others
        .filter(entry => entry && typeof entry === 'object')
        .map(entry => ({
          name: typeof entry.name === 'string' ? entry.name : '',
          tokens: Math.max(0, num(entry.tokens)),
          cost_usd: Math.max(0, num(entry.cost_usd)),
          cost_source: sourceField(entry.cost_source, 'none'),
          unpriced: entry.unpriced === true
        }))
    : []

  return {
    burn_source: sourceField(raw.burn_source, costSourceOf(raw)),
    burn_usd_per_hour: Math.max(0, num(raw.burn_usd_per_hour)),
    cache_hit_pct: Math.min(100, Math.max(0, num(raw.cache_hit_pct))),
    cache_saved_source: sourceField(raw.cache_saved_source, 'none'),
    cache_saved_usd: Math.max(0, num(raw.cache_saved_usd)),
    context_tokens: Math.max(0, num(raw.context_tokens)),
    context_window: window > 0 ? window : DEFAULT_CONTEXT_WINDOW,
    cost_source: costSourceOf(raw),
    cost_usd: Math.max(0, num(raw.cost_usd)),
    day_cost_usd: Math.max(0, num(raw.day_cost_usd)),
    day_sessions: Math.max(0, Math.floor(num(raw.day_sessions))),
    day_source: sourceField(raw.day_source, 'none'),
    day_tokens: Math.max(0, num(raw.day_tokens)),
    delegates: {
      cost_source: sourceField(rawDelegates.cost_source, 'none'),
      cost_usd: Math.max(0, num(rawDelegates.cost_usd)),
      count
    },
    input_tokens: Math.max(0, num(raw.input_tokens)),
    model: typeof raw.model === 'string' ? raw.model : '',
    others,
    others_cost_usd: Math.max(0, num(raw.others_cost_usd)),
    others_source: sourceField(raw.others_source, 'none'),
    others_tokens: Math.max(0, num(raw.others_tokens)),
    others_unpriced: raw.others_unpriced === true,
    hidden_rows: Array.isArray(raw.hidden_rows)
      ? raw.hidden_rows.filter(name => typeof name === 'string')
      : [],
    output_tokens: Math.max(0, num(raw.output_tokens)),
    primary_session: typeof raw.primary_session === 'string' ? raw.primary_session : '',
    total_cost_usd: Math.max(0, num(raw.total_cost_usd, num(raw.cost_usd))),
    turn: Math.max(0, num(raw.turn)),
    turns,
    ts: Math.max(0, num(raw.ts))
  }
}

/** Compact token counts with a fixed ceiling of 6 cells (`999.9k`, `1.2M`). */
function fmt(n) {
  n = num(n)

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

/** Read the snapshot file. Returns ``null`` when the file is missing, unreadable, not JSON,
 *  or not a JSON object.
 *
 *  ``null`` is the honest answer for "no fresh data": the caller then keeps the last good
 *  snapshot on screen, so a write in flight, a rolled log or a corrupt byte cannot blank
 *  the card for a tick. Exported as a test seam: the smoke test calls it without a TUI. */
export async function readSnapshot() {
  try {
    const [fsp, os] = await Promise.all([import('node:fs/promises'), import('node:os')])
    const home = (process.env.HERMES_HOME || '').trim() || `${os.homedir()}/.hermes`
    const raw = await fsp.readFile(`${home}/state/savvy-progress.json`, 'utf8')
    const parsed = JSON.parse(raw)

    if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
      return null
    }

    return normalize(parsed)
  } catch {
    return null
  }
}

/** Keep the last good snapshot when a poll comes back empty. Pure, so the test can drive
 *  it: ``mergeSnapshot(prev, null)`` returns ``prev`` unchanged. */
export function mergeSnapshot(prev, next) {
  if (!next || typeof next !== 'object' || Array.isArray(next)) {
    return prev
  }

  return normalize(next)
}

/** Age of a snapshot in words: `40s`, `12m`, `3h`, `2d`. */
export function formatAge(seconds) {
  const s = Math.max(0, num(seconds))

  if (s < 90) {
    return `${Math.round(s)}s`
  }

  if (s < 5400) {
    return `${Math.round(s / 60)}m`
  }

  if (s < 172800) {
    return `${Math.round(s / 3600)}h`
  }

  return `${Math.round(s / 86400)}d`
}

/** Suffix for the card title when the snapshot is no longer fresh: `· 4m`, else ''. */
export function staleLabel(ts, nowSeconds) {
  const t = num(ts)

  if (t <= 0) {
    return ''
  }

  const age = num(nowSeconds) - t

  if (age < STALE_S) {
    return ''
  }

  return ` · ${formatAge(age)}`
}

/** Build the card rows from a normalized snapshot. Exported as a test seam: the smoke
 *  test walks the returned tree and checks every row is exactly ROW_W cells wide.
 *  `parts` carries the renderer primitives (`Box`, `Text`, `gauge`, `h`). */
export function buildRows(parts, snap, t) {
  const { Box, Text, gauge, h } = parts

  /** One card row: fixed label column plus a fixed 22-cell value field.
   *  `align` is 'end' for numbers and 'start' for text, so nothing shifts between ticks. */
  const row = (key, label, value, tone, align = 'end') => {
    const body = String(value ?? '')
    const cell = (body.length > VALUE_W ? `${body.slice(0, VALUE_W - 1)}…` : body)
    const pad = align === 'start' ? cell.padEnd(VALUE_W) : cell.padStart(VALUE_W)

    return h(
      Box,
      { flexDirection: 'row', key, rowKey: key, width: ROW_W },
      h(Text, { color: t.color.muted }, label.padEnd(LABEL_W)),
      h(Text, { color: tone }, pad)
    )
  }

  const hit = Math.min(100, Math.max(0, num(snap.cache_hit_pct)))
  const hitTone = hit >= 70 ? t.color.ok : hit >= 30 ? t.color.warn : t.color.error
  const window = Math.max(0, num(snap.context_window))
  const ctxRatio = window > 0
    ? Math.min(1, Math.max(0, num(snap.context_tokens) / window))
    : 0
  const ctxTone = ctxRatio >= 0.9 ? t.color.error : ctxRatio >= 0.7 ? t.color.warn : t.color.text

  /* Fixed-cell fields: 12-cell gauge plus a 5-cell percent on both gauge rows. */
  const gaugeText = String(gauge(hit / 100, GAUGE_W)).slice(0, GAUGE_W).padEnd(GAUGE_W)
  const pctText = `${hit.toFixed(1)}%`.padStart(PCT_W)
  const ctxGauge = String(gauge(ctxRatio, GAUGE_W)).slice(0, GAUGE_W).padEnd(GAUGE_W)
  const ctxPct = `${(ctxRatio * 100).toFixed(1)}%`.padStart(PCT_W)
  const hasAgents = snap.delegates.count > 0
  const agentOf = name => (snap.others || []).find(entry => entry.name === name)
  const agentTone = entry => (entry && (num(entry.tokens) || num(entry.cost_usd)))
    ? t.color.text
    : t.color.muted
  const othersTotal = {
    tokens: snap.others_tokens,
    cost_usd: snap.others_cost_usd,
    cost_source: snap.others_source,
    unpriced: snap.others_unpriced
  }

  const rows = [
    row('model', 'model', shortModel(snap.model), t.color.text, 'start'),
    row('tokens', 'tokens', `${fmt(snap.input_tokens)} / ${fmt(snap.output_tokens)}`, t.color.text),
    row('cache', 'cache', `${gaugeText} ${pctText}`, hitTone),
    row('ctx', 'ctx', `${ctxGauge} ${ctxPct}`, ctxTone),
    row('cost', 'cost', formatCost(snap.total_cost_usd, snap.cost_source), t.color.accent),
    row('burn', 'burn', formatBurn(snap.burn_usd_per_hour, snap.burn_source), t.color.text),
    row('saved', 'saved', formatSaved(snap.cache_saved_usd, snap.cache_saved_source), t.color.ok),
    row('today', 'today', formatToday(snap.day_cost_usd, snap.day_source, snap.day_sessions), t.color.accent),
    row('daytok', 'tok/day', formatDayTokens(snap.day_tokens), t.color.text),
    row('codex', 'codex', formatAgent(agentOf('codex')), agentTone(agentOf('codex'))),
    row('pi', 'pi', formatAgent(agentOf('pi')), agentTone(agentOf('pi'))),
    row('dsh', 'dsh', formatAgent(agentOf('dsh')), agentTone(agentOf('dsh'))),
    row('others', 'others', formatAgent(othersTotal), t.color.accent),
    row('agents', 'agents', formatAgents(snap.delegates), hasAgents ? t.color.text : t.color.muted)
  ]
  const hide = new Set(snap.hidden_rows || [])

  return hide.size ? rows.filter(entry => !hide.has(entry.props.rowKey)) : rows
}

export default function register(sdk) {
  const { Box, Dialog, React, Text, defineWidgetApp, gauge, h } = sdk

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

          if (alive && next) {
            setSnap(current => mergeSnapshot(current, next))
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

    return h(
      Dialog,
      { title: `PROGRESS${staleLabel(snap.ts, Date.now() / 1000)}`, width: WIDTH },
      h(
        Box,
        { flexDirection: 'column', width: ROW_W },
        ...buildRows({ Box, Text, gauge, h }, snap, t)
      )
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
      // The card owns the Dialog so the title can show snapshot age.
      return h(Card, { t })
    }
  })

  // Always-on: dock the card as soon as the widget file loads, so the panel is
  // permanent and needs no /progress. Typing /progress still toggles it off and on.
  if (typeof sdk.openWidget === 'function') {
    sdk.openWidget(app, app.init(''))
  }
}
