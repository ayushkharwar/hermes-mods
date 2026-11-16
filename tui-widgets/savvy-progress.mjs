/**
 * savvy-progress — ambient TUI widget for the current session's token and cost progress.
 *
 * Reads `$HERMES_HOME/state/savvy-progress.json` (written by the `savvy-progress` Python
 * plugin) once per second and draws one fixed-size card in the right rail:
 *
 *   \|/  PROGRESS  deepseek-v4.1-flash  /|
 *    |,  ─────────────── ◇ ──────────  `|
 *    |   last           1.8k/2.2k ~$0.0036
 *   ,|   cache   ███████░░░░░  91.7% avg
 *    |   cost          ~$0.0123
 *   '|    saved           ~$0.2401
 *    |   ─────────────── ◇ ──────────
 *    |   today      ~$2.1871  25 chats
 *   ,|   tok/day     18.4M · 1.2M ag
 *    |   ─────────────── ◇ ──────────
 *    |   codex     ~$0.0000 21.6M?
 *    |   pi        ~$1.6343 111.3M
 *    |   dsh       ~$3.0715  93.8M
 *    |   others    ~$4.7058 226.8M
 *   \|    ,^, ,^,  ,^,  ,^,  ,^, ,^,  \|
 *   \|/  ──────────────────────────  \|/
 *
 * The card is a FRAME, not a banner: a three-cell spine column runs down the left and the
 * right of all 18 rows, so the motif climbs the whole card. `grove` grows a vine (stem,
 * leaves at irregular heights, one branch that leaves the spine, roots at the base and a
 * canopy over the top); `savvy` climbs a circuit trace (nodes, branches and 90-degree
 * bends). The right spine is the left one mirrored and shifted a few rows, so the two
 * sides ornament each other instead of copy each other.
 *
 * 18 rows at every palette and every separator style: 2 canopy art rows, the header,
 * three separators, 10 value rows, the root art row and the closing rule. All three art
 * rows carry the `logo` row key (`/svprogress hide logo` drops the canopy and the roots
 * but leaves the spine), and all three separators carry `sep` (`/svprogress hide sep`
 * drops them).
 *
 * The card is a plain Box, not a Dialog: the host hardcodes a rounded border on a Dialog
 * and this block is framed by its own spines instead. The container is GUTTER*2 cells
 * wider than the 38-cell frame and centres its rows (with the same padding on both
 * sides), so the gutters stay equal when a row's width changes.
 *
 * Placement: the app docks in the host's `center-right` ambient zone, and only when the
 * host advertises that zone (`sdk.ambientZones`); an older host gets `top-right`, which
 * every build renders. A zone the rail does not match draws NOTHING, so the fallback is
 * not cosmetic. The RAIL centres the card (a grown spacer above and below it), so the
 * widget never fakes vertical centring with `height: '100%'` — that resolves against an
 * auto-height parent and centres nothing. One mechanism, owned by the host.
 *
 * Palette: `THEMES` holds eight foreground hexes each, and the plugin publishes the
 * chosen name as `theme` in the snapshot (`/svprogress theme savvy|grove|next`). `ROLE_OF`
 * maps every card role onto one of those eight, so switching palette recolours the card
 * and never re-lays it out.
 *   savvy  neon night (default): label #40E0D0 · value #F5F5F5 · accent #57E3FF ·
 *          ok #00FF7F · highlight #FFD700 · warn #FF9BD2 · error #FF4D6D · violet #8E7BF0
 *   grove  green garden: label #4E7A5A · value #EAF7EE · accent #7CE38B · ok #9BE564 ·
 *          highlight #C8E86A · warn #FFC46B · error #FF6B6B · violet #8E7BF0
 * The cache row keeps its threshold: hit >= 70 draws `ok`, >= 30 `warn`, else `error`.
 * Agent rows (codex/pi/dsh) draw `violet`; label, model, last and the canopy art rows
 * follow `ROLE_OF`, and the root art row draws the `label` tone.
 *
 * Column rules: the model is cut to its last path segment, every number is padded into
 * a fixed 22-cell value field, and the cache gauge keeps a 12-cell bar plus a 6-cell
 * percent and a 4-cell `avg` tag, so no row can resize while ticking.
 *
 * The cache row draws TODAY's blended hit rate (cached input over every billed input
 * token, all sessions of the local day), not this session's own rate: a cold start in one
 * chat should not make a warm day look cold. The context row and the session token totals
 * are gone; the `last` row replaces the totals with the previous turn's split and cost.
 *
 * Delegate children never take the model or cache rows: their cost is summed into the
 * `cost` row and their tokens are appended to `tok/day` with an `ag` tag, so one row
 * answers "how much of today's traffic came from agents". The `burn` and `agents` rows are
 * gone: a per-hour rate and a delegate count read as noise next to the dollars.
 *
 * Cost row follows `cost_source`: `provider` prints a plain `$`, `table` marks an estimate
 * with `~`, `none` prints `$n/a`. The cost field is always 8 cells, so the card cannot resize.
 *
 * Contract: plain ESM, no JSX (`h` is React.createElement), colours come only from the
 * active palette (hex for `savvy`, `t.color` tones for `classic`), every row padded to a
 * fixed width so the card never resizes while ticking, and a missing file keeps the last
 * good snapshot on screen (the title then shows its age).
 */

const WIDTH = 44
const LABEL_W = 8
const VALUE_W = 22
const GAUGE_W = 12
const ROW_W = LABEL_W + VALUE_W
/** The spine is 3 cells on each side, one clear cell between the spine and the text,
 *  so one content line is SPINE_W + GAP + ROW_W + GAP + SPINE_W = 38 cells. */
const SPINE_W = 3
const GAP_W = 1
const FRAME_W = SPINE_W + GAP_W + ROW_W + GAP_W + SPINE_W
/** Symmetric gutter inside the card's own container, 2 cells each side. The container is
 *  wider than the 38-cell frame by GUTTER*2 and centres its rows, so the trailing gutter
 *  comes from the layout rather than from counted spaces. WIDTH also carries the rail's
 *  own `paddingX={1}` on both sides, so the frame fills the rail's content box exactly
 *  and the gutters stay equal at 40 and 60 rows. */
const GUTTER = 2
const INNER_W = FRAME_W + GUTTER * 2
const COST_W = 8
const PCT_W = 6
const POLL_MS = 500
/** Seconds one footer tip stays on screen. Overridable from the payload as `tip_s`,
 *  and picked by the same wall-clock rule the plugin uses, so both surfaces agree. */
const TIP_S = 12
/** Cells in the week sparkline: seven local calendar days, oldest left. */
const WEEK_W = 7
/** Fixed spine slots for the extras rows. The 18 base rows still take their index from
 *  call order (which is what keeps the card byte-identical when extras are off); these
 *  five are pinned because `warn` is conditional and must not shift the rows under it. */
const EXTRAS_SPINE = { miss: 8, week: 12, warn: 18, errors: 19, tip: 20 }
/** Index of the root art row: 16 in the 18-row card, 21 once the five extras rows are
 *  in. The closing rule follows it. */
const GROUND_AT = { off: 16, on: 21 }
/** A snapshot this old is shown with its age in the title, so stale numbers never look live. */
const STALE_S = 120

/** Default palette. The plugin owns the choice and publishes it as `snap.theme`; a
 *  snapshot without that key (or with a name not in the table) draws this one. */
export const DEFAULT_THEME = 'wine'

/** Two palettes, eight foreground colours each, all `#rrggbb` because a true-colour
 *  terminal prints them directly. `savvy` is the neon night measured off the mock: cyan
 *  labels, near-white values, cyan logo and sky art, spring-green cache gauge and `saved`,
 *  gold `cost`/`today`/`others`, pink warn, red error, violet codex/pi/dsh rows. `grove`
 *  is the garden: moss labels, pale leaves, grass accents, lime cache, lime-gold
 *  highlights, amber warn, coral error. */
export const THEMES = {
  savvy: {
    label: '#40E0D0',
    value: '#F5F5F5',
    accent: '#57E3FF',
    ok: '#00FF7F',
    highlight: '#FFD700',
    warn: '#FF9BD2',
    error: '#FF4D6D',
    violet: '#8E7BF0'
  },
  grove: {
    label: '#4E7A5A',
    value: '#EAF7EE',
    accent: '#7CE38B',
    ok: '#9BE564',
    highlight: '#C8E86A',
    warn: '#FFC46B',
    error: '#FF6B6B',
    violet: '#8E7BF0'
  }
}

/** Which palette colour each card role draws. Shared by both themes, so switching
 *  recolours the card but never re-lays it out. */
export const ROLE_OF = {
  logo: 'accent',
  node: 'ok',
  leaf: 'accent',
  stem: 'label',
  model: 'value',
  last: 'value',
  cache: 'ok',
  cacheWarn: 'warn',
  cacheError: 'error',
  cost: 'highlight',
  saved: 'ok',
  today: 'highlight',
  daytok: 'value',
  agent: 'violet',
  agentEmpty: 'violet',
  others: 'highlight',
  label: 'label',
  ground: 'label',
  /* Extras. `miss` is money a warmer cache would have saved, so it borrows the warn
   * tone; `errors` is the error tone; `week` rides the accent; the tip is quiet. */
  miss: 'warn',
  errors: 'error',
  week: 'accent',
  tip: 'label',
  warnWarn: 'warn',
  warnError: 'error'
}

/** Canopy and root art per palette, centred inside the 30-cell middle of the frame. Two
 *  rows over the header, one under the last value group, the same three-row shape in both
 *  themes so switching never resizes the card. Only the glyphs differ. */
export const ART = {
  savvy: {
    top: [
      '·   ●   ·   ●   ·   ●   ·',
      '─┬──┬───┬───┬───┬───┬───┬─'
    ],
    ground: '·──┬───┬───┬───┬───┬───┬──·'
  },
  grove: {
    top: [
      ',^, ,^, ,^, ,^, ,^, ,^, ,^,',
      ' \\|/   \\|/   \\|/   \\|/   \\|/'
    ],
    ground: ',|, \\|/ ,|, \\|/ ,|, \\|/ ,|,'
  }
}

/** The LEFT spine column per palette: 18 rows of 3 cells, top to bottom. The right column
 *  is the same list mirrored and shifted (see `spineColumns`), so nothing is hand-copied.
 *
 *  grove  a vine climbing from roots to canopy: a stem with leaves at irregular heights,
 *         one branch that leaves the stem (rows 9-10) and rejoins it, roots that flare at
 *         the base and a crown that spills over the top row.
 *  savvy  a circuit trace: nodes and 90-degree bends, with the trace turning off the spine
 *         and back on at rows 7 and 15.
 */
export const SPINES = {
  savvy: [
    ' · ',
    ' │ ',
    ' ├─',
    ' │ ',
    ' ● ',
    ' │ ',
    ' ╭─',
    ' │ ',
    ' · ',
    ' │ ',
    '─┤ ',
    ' │ ',
    ' ● ',
    ' │ ',
    ' ╰─',
    ' │ ',
    ' ● ',
    ' · '
  ],
  grove: [
    '/|\\',
    ' |^',
    ',| ',
    ' | ',
    '\'| ',
    ' |\\',
    '  |',
    '  |',
    ' |/',
    ',| ',
    ' | ',
    '\'| ',
    ' | ',
    ' |,',
    ' |^',
    '\\| ',
    ' | ',
    '\\|/'
  ]
}

/** Rows the right column shifts down against the left, so both sides are ornamented
 *  instead of mirror-lockstepped. The first and last rows are pinned: the crown closes
 *  over the top and the roots close under the base. */
export const SPINE_OFFSET = { savvy: 3, grove: 4 }

const SPINE_PIN = 2

/** Character mirror for a column that grows toward the text from both sides. */
const MIRROR_CHAR = {
  '\\': '/',
  '/': '\\',
  ',': '.',
  '.': ',',
  "'": '`',
  '`': "'",
  '├': '┤',
  '┤': '├',
  '╭': '╮',
  '╮': '╭',
  '╰': '╯',
  '╯': '╰',
  '┌': '┐',
  '┐': '┌',
  '└': '┘',
  '┘': '└'
}

/** One 3-cell spine turned into its facing twin: cells reversed, glyphs flipped. */
export function mirrorCells(cells) {
  return [...cells].reverse().map(char => MIRROR_CHAR[char] || char).join('')
}

/** Both spine columns for one palette, 18 rows each. The right column is the left one
 *  mirrored, shifted by SPINE_OFFSET rows inside the sleeve between the pinned ends. */
export function spineColumns(themeName) {
  const theme = themeNameOf(themeName)
  const left = SPINES[theme]
  const shift = SPINE_OFFSET[theme]
  const from = SPINE_PIN
  const to = left.length - SPINE_PIN
  const span = to - from

  const right = left.map((cells, index) => {
    if (index < from || index >= to) {
      return mirrorCells(cells)
    }

    return mirrorCells(left[from + ((index - from + shift) % span)])
  })

  return { left, right }
}

const SAVVY_NODES = new Set(['●'])
const SAVVY_TERMINALS = new Set(['·'])
const GROVE_STEMS = new Set(['|', '─', '+', '-'])

/** Spine art for the five rows the extras add past the closing rule (warn, errors, tip,
 *  the roots and the rule), per palette. Kept OUT of SPINES on purpose: SPINES stays 18
 *  rows, so the base card keeps its exact left AND right column when extras are off
 *  instead of the mirror shift recomputing over a longer list. */
export const EXTRAS_SPINES = {
  savvy: [' │ ', ' ● ', ' │ ', ' ╰─', ' · '],
  grove: [' | ', ' |,', '\\| ', '/| ', '\\|/']
}
/** The extras column is the left one mirrored and shifted, like the base column. */
const EXTRAS_MIRROR_OFFSET = 2

/** The 3-cell spine for one row slot on one side. Slots 0..17 read the palette spine
 *  columns; the five extras slots read EXTRAS_SPINES. Never undefined: an index past both
 *  lists is three spaces, so a row added later can never break the frame. */
export function spineCellsAt(themeName, index, side) {
  const theme = themeNameOf(themeName)
  const { left, right } = spineColumns(theme)

  if (index < left.length) {
    return side === 'l' ? left[index] : right[index]
  }

  const extras = EXTRAS_SPINES[theme] || EXTRAS_SPINES[DEFAULT_THEME]
  const at = index - left.length

  if (at >= extras.length) {
    return '   '
  }

  return side === 'l'
    ? extras[at]
    : mirrorCells(extras[(at + EXTRAS_MIRROR_OFFSET) % extras.length])
}

/** Palette role for one spine character: nodes glow, leaves shade, stems stay quiet. */
export function spineRole(themeName, char) {
  const theme = themeNameOf(themeName)

  if (theme === 'savvy') {
    if (SAVVY_NODES.has(char)) {
      return 'node'
    }

    return SAVVY_TERMINALS.has(char) ? 'stem' : 'leaf'
  }

  return GROVE_STEMS.has(char) ? 'stem' : 'leaf'
}

/** Separator styles, all one 30-cell row. `gem` is the shipped default: a rule with a
 *  centred theme glyph. `rule` is a plain line, `dots` a `· ` chain, `shade` a shade ramp.
 *  `/svprogress sep gem|rule|dots|shade` switches them; the plugin persists the choice. */
export const SEP_STYLES = ['gem', 'rule', 'dots', 'shade']

/* wine jungle: the grove vine drawn in deep wine ink. The art objects are SHARED, not
 * copied, so one edit to the jungle glyphs reaches both palettes and they can never drift.
 * Ink: dusty wine labels, near-white values, wine-rose accents, jungle-green cache, amber
 * highlights, pink warn, red error, muted violet agent rows. */
THEMES.wine = {
  label: '#9A5F71',
  value: '#F7EFF2',
  accent: '#C2185B',
  ok: '#7BE38B',
  highlight: '#FFB86B',
  warn: '#FF9BD2',
  error: '#FF4D6D',
  violet: '#A07BE0'
}
ART.wine = ART.grove
SPINES.wine = SPINES.grove
SPINE_OFFSET.wine = SPINE_OFFSET.grove
EXTRAS_SPINES.wine = EXTRAS_SPINES.grove

/** Separator glyph per palette; `wine` keeps the jungle bush so its rule matches its art. */
const GEM_GLYPH = { savvy: ' ◇ ', grove: ' ,^, ', wine: ' ,^, ' }
const SHADE_GLYPH = ' ░▒▓██▓▒░ '

/** Rotating footer tips. One array, plain text, no emoji, so they are easy to edit and
 *  cannot carry a wide glyph into a fixed-width row. Mirrors TIPS in the plugin. */
export const TIPS = [
  'cache: keep the stable prefix first',
  'cache: a warm day beats one warm chat',
  'cache: cache reads bill far below input',
  'cache: a long system prompt pays itself back',
  'cache: one big prompt beats many small ones',
  'cost: output tokens are the expensive ones',
  'cost: judge the day, not this one chat',
  'cost: a retry bills the whole prompt again',
  'cost: an unpriced model reads as zero, not free',
  'bloat: drop the file you already quoted',
  'bloat: one tool result beats three',
  'bloat: trim old turns before the next call'
]

/** Block ramp for the week sparkline. One cell per glyph, no emoji, so the row keeps
 *  its seven cells in every terminal. Mirrors SPARK_RAMP in the plugin. */
export const SPARK_RAMP = ['▁', '▂', '▃', '▄', '▅', '▆', '▇', '█']

/** `tip_s` from a payload, or the default. A period below one second is nonsense. */
export function tipPeriodOf(value) {
  const step = typeof value === 'number' && Number.isFinite(value) ? value : TIP_S

  return step >= 1 ? step : TIP_S
}

/** The tip on screen at `nowSeconds`. Pure, so a test can drive the rotation: the same
 *  second always picks the same tip, and only the text changes, never the width. */
export function tipAt(nowSeconds, period) {
  const step = tipPeriodOf(period)
  const clock = typeof nowSeconds === 'number' && Number.isFinite(nowSeconds) ? nowSeconds : 0

  return TIPS[Math.floor(Math.max(0, clock) / step) % TIPS.length]
}

/** Seven daily costs to one `WEEK_W`-cell sparkline. Oldest left. An all-zero week is
 *  seven floor cells, never a short string, so the row keeps its width. */
export function sparkOf(values) {
  const series = (Array.isArray(values) ? values : []).slice(0, WEEK_W).map(value =>
    typeof value === 'number' && Number.isFinite(value) ? Math.max(0, value) : 0)

  while (series.length < WEEK_W) {
    series.push(0)
  }

  const top = Math.max(...series)

  if (top <= 0) {
    return SPARK_RAMP[0].repeat(WEEK_W)
  }

  return series
    .map(value => SPARK_RAMP[Math.min(SPARK_RAMP.length - 1, Math.round((value / top) * (SPARK_RAMP.length - 1)))])
    .join('')
}

function hexToRgb(hex) {
  const body = typeof hex === 'string' && hex.startsWith('#') && hex.length === 7 ? hex.slice(1) : '000000'

  return [
    Number.parseInt(body.slice(0, 2), 16) || 0,
    Number.parseInt(body.slice(2, 4), 16) || 0,
    Number.parseInt(body.slice(4, 6), 16) || 0
  ]
}

/** One `#rrggbb` between two palette colours. A non-hex tone (the `classic` skin) is
 *  passed through untouched, so an unknown palette can never print a broken colour. */
export function mixHex(from, to, ratio) {
  if (typeof from !== 'string' || !from.startsWith('#') || typeof to !== 'string' || !to.startsWith('#')) {
    return from
  }

  const a = hexToRgb(from)
  const b = hexToRgb(to)
  const t = Math.min(1, Math.max(0, ratio))

  return `#${a.map((part, index) => {
    const value = Math.round(part + (b[index] - part) * t)

    return Math.min(255, Math.max(0, value)).toString(16).padStart(2, '0')
  }).join('')}`
}

/** The cache gauge as one Text per cell: a fixed ramp from the palette's `ok` on the
 *  left to `error` at the tip of the gauge, so a nearly full bar reaches the warm end
 *  and a short bar stays in the cold end. Always exactly `width` cells of one cell
 *  each, so the row's width cannot move. */
export function gradientGauge(ratio, width, palette) {
  const filled = Math.round(Math.min(1, Math.max(0, ratio)) * width)
  const cells = []

  for (let index = 0; index < width; index += 1) {
    const ramp = width > 1 ? index / (width - 1) : 0

    cells.push(index < filled
      ? { char: '█', color: mixHex(palette.ok, palette.error, ramp) }
      : { char: '░', color: palette.label })
  }

  return cells
}

/** The separator name a raw or normalized snapshot asks for, or `gem` when unknown. */
export function sepNameOf(value) {
  const key = typeof value === 'string' ? value.trim().toLowerCase() : ''

  return SEP_STYLES.includes(key) ? key : 'gem'
}

/** One separator row, built to exactly ROW_W cells for any theme and style: the glyph is
 *  centred between two `─` runs, and the left run takes the extra cell when the remainder
 *  is odd. Nothing here is hand-counted. */
export function separatorLine(themeName, sep) {
  const style = sepNameOf(sep)
  const theme = themeNameOf(themeName)

  if (style === 'rule') {
    return '─'.repeat(ROW_W)
  }

  if (style === 'dots') {
    return '· '.repeat(ROW_W / 2)
  }

  const glyph = style === 'shade' ? SHADE_GLYPH : GEM_GLYPH[theme]

  if (cellsOf(glyph) >= ROW_W) {
    return '─'.repeat(ROW_W)
  }

  const rest = ROW_W - cellsOf(glyph)
  const left = Math.ceil(rest / 2)

  return `${'─'.repeat(left)}${glyph}${'─'.repeat(rest - left)}`
}

/** Palette for one name, or the default for anything unknown. Never throws. */
export function themePalette(name) {
  const key = typeof name === 'string' ? name.trim().toLowerCase() : ''

  return THEMES[key] || THEMES[DEFAULT_THEME]
}

/** The theme name a raw or normalized snapshot asks for: an unknown or missing key
 *  falls back to the default instead of blanking the card. */
export function themeNameOf(value) {
  const key = typeof value === 'string' ? value.trim().toLowerCase() : ''

  return THEMES[key] ? key : DEFAULT_THEME
}

/** Resolve one palette colour to what the terminal draws. A hex stays itself; a tone name
 *  is looked up in the host skin, and an unknown name falls back to the palette's value
 *  colour, so a half-written palette can never print `undefined`. */
export function resolveTone(palette, role, t) {
  const value = palette[role]

  if (typeof value === 'string' && value.startsWith('#')) {
    return value
  }

  const skin = (t && t.color) || {}
  const tone = typeof value === 'string' ? skin[value] : undefined

  return tone || palette.value || skin.text || 'text'
}

const EMPTY = {
  theme: DEFAULT_THEME,
  sep: 'gem',
  cache_hit_pct: 0,
  day_cache_hit_pct: 0,
  cache_saved_source: 'none',
  cache_saved_usd: 0,
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
  /* Extras. A snapshot with no `extras` key predates the switch, so its rows stay OFF:
   * an old payload carries none of their data and would only draw n/a. */
  extras: 'off',
  miss_usd: 0,
  miss_source: 'none',
  day_unpriced: false,
  day_input_tokens: 0,
  week: [],
  week_source: 'none',
  week_total_usd: 0,
  errors: { count: 0, signal: false },
  budget_usd: null,
  tip_s: TIP_S,
  delegates: { cost_source: 'none', cost_usd: 0, count: 0, tokens: 0 },
  last_turn: null,
  model: '',
  output_tokens: 0,
  primary_session: '',
  total_cost_usd: 0,
  turn: 0,
  turns: [],
  ts: 0
}

const COST_SOURCES = new Set(['provider', 'table', 'agent', 'estimate', 'none'])

/** Terminal cells, not codepoints. A CJK glyph is two cells wide, so a row padded by
 *  codepoint count overflows its declared width and the card reflows. */
const WIDE_RANGES = [
  [0x1100, 0x115f], [0x2e80, 0xa4cf], [0xac00, 0xd7a3], [0xf900, 0xfaff],
  [0xfe30, 0xfe6f], [0xff00, 0xff60], [0xffe0, 0xffe6], [0x1f300, 0x1f64f],
  [0x1f900, 0x1f9ff]
]

export function cellWidth(char) {
  const code = char.codePointAt(0) || 0

  return WIDE_RANGES.some(([low, high]) => code >= low && code <= high) ? 2 : 1
}

/** Drop control characters. A model name comes from another agent's transcript. */
export function stripControl(value) {
  return [...String(value ?? '')].filter(char => char >= ' ' && char !== '\x7f').join('')
}

export function cellsOf(value) {
  let total = 0

  for (const char of stripControl(value)) {
    total += cellWidth(char)
  }

  return total
}

/** Fit a string into a cell budget: cut with an ellipsis, then pad on the asked side. */
export function fitCells(value, width, align = 'end') {
  const body = stripControl(value)
  let out = ''
  let used = 0

  for (const char of body) {
    const size = cellWidth(char)

    if (used + size > width) {
      break
    }

    out += char
    used += size
  }

  if (cellsOf(body) > width && out) {
    out = out.slice(0, -1) + '…'
  }

  const pad = ' '.repeat(Math.max(0, width - cellsOf(out)))

  return align === 'start' ? out + pad : pad + out
}

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

  return (source === 'table' || source === 'agent' || source === 'estimate'
    ? `~${body}`
    : ` ${body}`).padStart(COST_W)
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
 *  cache write + output), plus the agent share when delegates ran. Fixed 22 cells:
 *   `                18.4M`   `       18.4M · 1.2M ag` */
export function formatDayTokens(value, agentTokens = 0) {
  const agents = Math.max(0, num(agentTokens))
  const body = agents > 0
    ? `${fmt(value)} · ${fmt(agents)} ag`
    : fmt(value)

  return body.padStart(VALUE_W)
}

/** Last-turn row: the previous turn's input/output split and what it cost, or `none`
 *  before the first recorded turn. Fixed 22 cells:
 *   `      1.8k/2.2k ~$0.0036`   `                     none` */
export function formatLast(turn, source) {
  if (!turn || typeof turn !== 'object') {
    return 'none'.padStart(VALUE_W)
  }

  const tin = Math.max(0, num(turn.in))
  const tout = Math.max(0, num(turn.out))
  const cost = Math.max(0, num(turn.cost))

  if (!tin && !tout && !cost) {
    return 'none'.padStart(VALUE_W)
  }

  return `${fmt(tin)}/${fmt(tout)} ${formatCost(cost, source).trimStart()}`.padStart(VALUE_W)
}

/** Miss row: what a perfect cache would have saved today, 4 decimals like the cost and
 *  saved rows and the same `~` estimate mark. Fixed 22 cells. */
export function formatMiss(value, source) {
  return formatSaved(value, source)
}

/** Week row: seven sparkline cells then the seven-day total, right-aligned into the
 *  remaining 15 cells. Fixed 22 cells whatever the buckets hold. */
export function formatWeek(days, source, total) {
  const costs = (Array.isArray(days) ? days : []).map(day => (day && typeof day === 'object' ? day.cost_usd : 0))
  const sum = costs.reduce((acc, value) => acc + Math.max(0, num(value)), 0)
  const money = formatCost(Array.isArray(days) && days.length ? Math.max(0, num(total, sum)) : 0, source)

  return `${sparkOf(costs)}${money.padStart(VALUE_W - WEEK_W)}`
}

/** Errors row: failed calls and the spend they wasted. The money is `n/a` because no
 *  failed attempt's tokens are recorded anywhere, and the whole field is `n/a` when the
 *  runtime never published a failure signal. Fixed 22 cells. */
export function formatErrors(errors) {
  const state = errors && typeof errors === 'object' ? errors : {}

  if (!state.signal) {
    return 'n/a'.padStart(VALUE_W)
  }

  const count = Math.max(0, Math.floor(num(state.count)))

  return `${count} failed  $n/a`.padStart(VALUE_W)
}

/** The one reason the card is unhappy, or null when it is all well. Priority order,
 *  most severe first, and only ever one reason: a cold cache, a day over the soft
 *  budget when one is set, an unpriced model in the day rows, then a cold snapshot.
 *  A quiet day is not a cold cache, so the cache check needs billed input tokens. */
export function warnReason(snap, nowSeconds) {
  const now = typeof nowSeconds === 'number' && Number.isFinite(nowSeconds) ? nowSeconds : 0
  const billed = num(snap.day_input_tokens, num(snap.day_tokens))
  const hit = Math.min(100, Math.max(0, num(snap.day_cache_hit_pct)))

  if (billed > 0 && hit < WARN_CACHE_HIT_MIN) {
    return { text: `cache ${hit.toFixed(1)}% under ${WARN_CACHE_HIT_MIN}%`, tone: 'warnWarn' }
  }

  const budget = typeof snap.budget_usd === 'number' ? snap.budget_usd : null
  const spend = Math.max(0, num(snap.day_cost_usd))

  if (budget !== null && spend > budget) {
    return {
      text: `day $${spend.toFixed(2)} over $${budget.toFixed(2)}`,
      tone: 'warnError'
    }
  }

  if (snap.day_unpriced) {
    return { text: 'unpriced model today', tone: 'warnWarn' }
  }

  const ts = num(snap.ts)
  const age = now - ts

  if (ts > 0 && age > WARN_STALE_S) {
    return { text: `snapshot ${formatAge(age)} old`, tone: 'warnWarn' }
  }

  return null
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
        .map(turn => ({
          in: Math.max(0, num(turn.in)),
          out: Math.max(0, num(turn.out)),
          cache: Math.max(0, num(turn.cache)),
          cost: Math.max(0, num(turn.cost))
        }))
    : []

  const rawDelegates = raw.delegates && typeof raw.delegates === 'object' ? raw.delegates : {}
  const count = Math.max(0, Math.floor(num(rawDelegates.count)))
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

  const delegateTokens = Math.max(
    0,
    num(rawDelegates.input_tokens) + num(rawDelegates.output_tokens) + num(rawDelegates.cache_read)
  )

  return {
    cache_hit_pct: Math.min(100, Math.max(0, num(raw.cache_hit_pct))),
    /* The day rate is what the card draws. A snapshot from before the key existed still
     * carries the session rate, so the row never blanks on an old file. */
    day_cache_hit_pct: Math.min(
      100,
      Math.max(0, num(raw.day_cache_hit_pct, num(raw.cache_hit_pct)))
    ),
    cache_saved_source: sourceField(raw.cache_saved_source, 'none'),
    cache_saved_usd: Math.max(0, num(raw.cache_saved_usd)),
    cost_source: costSourceOf(raw),
    cost_usd: Math.max(0, num(raw.cost_usd)),
    day_cost_usd: Math.max(0, num(raw.day_cost_usd)),
    day_sessions: Math.max(0, Math.floor(num(raw.day_sessions))),
    day_source: sourceField(raw.day_source, 'none'),
    day_tokens: Math.max(0, num(raw.day_tokens)),
    delegates: {
      cost_source: sourceField(rawDelegates.cost_source, 'none'),
      cost_usd: Math.max(0, num(rawDelegates.cost_usd)),
      count,
      tokens: delegateTokens
    },
    /* The `last` row reads the newest turn only: the session totals moved to the text card. */
    last_turn: turns.length ? turns[turns.length - 1] : null,
    model: typeof raw.model === 'string' ? raw.model : '',
    /* The palette name travels with the snapshot. An unknown or absent value draws the
     * default instead of throwing, so a stale payload cannot blank the card. */
    theme: themeNameOf(raw.theme),
    /* The separator style travels beside it, same fallback rule. */
    sep: sepNameOf(raw.sep),
    others,
    others_cost_usd: Math.max(0, num(raw.others_cost_usd)),
    others_source: sourceField(raw.others_source, 'none'),
    others_tokens: Math.max(0, num(raw.others_tokens)),
    others_unpriced: raw.others_unpriced === true,
    hidden_rows: Array.isArray(raw.hidden_rows)
      ? raw.hidden_rows.filter(name => typeof name === 'string')
      : [],
    /* One master switch for the six extras. Only an explicit 'on' draws them, so a
     * payload from a plugin that predates the switch cannot leave n/a rows on screen. */
    extras: typeof raw.extras === 'string' && raw.extras.trim().toLowerCase() === 'on'
      ? 'on'
      : 'off',
    miss_usd: Math.max(0, num(raw.miss_usd)),
    miss_source: sourceField(raw.miss_source, 'none'),
    day_unpriced: raw.day_unpriced === true,
    day_input_tokens: Math.max(0, num(raw.day_input_tokens)),
    week: Array.isArray(raw.week)
      ? raw.week.filter(day => day && typeof day === 'object')
          .map(day => ({
            label: typeof day.label === 'string' ? day.label : '',
            date: typeof day.date === 'string' ? day.date : '',
            cost_usd: Math.max(0, num(day.cost_usd)),
            sessions: Math.max(0, Math.floor(num(day.sessions)))
          }))
      : [],
    week_source: sourceField(raw.week_source, 'none'),
    week_total_usd: Math.max(0, num(raw.week_total_usd)),
    /* Failed calls. `signal` false means the runtime never published a failure field,
     * so the row prints n/a instead of a confident zero. */
    errors: {
      count: Math.max(0, Math.floor(num(raw.errors && raw.errors.count))),
      signal: Boolean(raw.errors && raw.errors.signal)
    },
    budget_usd: typeof raw.budget_usd === 'number' && Number.isFinite(raw.budget_usd)
      ? Math.max(0, raw.budget_usd)
      : null,
    tip_s: tipPeriodOf(raw.tip_s),
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

function stripModelTail(model) {
  return stripControl((model || '').split('/').pop() || 'unknown')
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

/** Below this blended day cache hit rate the `warn` row draws. The gauge keeps its own
 *  70 / 30 thresholds: the gauge grades, this row complains. */
export const WARN_CACHE_HIT_MIN = 90
/** A snapshot older than this is not live data. Same window as the stale title. */
export const WARN_STALE_S = 300

/** One art line, centred and padded to ROW_W. Pure and exported for the smoke test. */
export function artLine(text) {
  const pad = Math.max(0, Math.floor((ROW_W - cellsOf(text)) / 2))

  return fitCells(`${' '.repeat(pad)}${text}`, ROW_W, 'start')
}

/** The three 30-cell art lines for one palette, in order: two canopy rows over the
 *  header, one root row under the last value group. `buildRows` frames each of them with
 *  the spine; they carry rowKey 'logo' there, so `/svprogress hide logo` drops the
 *  canopy and the roots in one command while the spine keeps climbing. */
export function artRows(parts, themeName, t) {
  const { Text, h } = parts
  const palette = themePalette(themeName)
  const art = ART[themeNameOf(themeName)] || ART[DEFAULT_THEME]

  const line = (key, text, role) => h(
    Text,
    { key: `${key}-art`, color: resolveTone(palette, ROLE_OF[role], t) },
    artLine(text)
  )

  return [
    line('logo-top1', art.top[0], 'logo'),
    line('logo-top2', art.top[1], 'logo'),
    line('logo-ground', art.ground, 'ground')
  ]
}

/** Build the card rows from a normalized snapshot. Exported as a test seam: the smoke
 *  test walks the returned tree and checks every row is exactly FRAME_W cells wide.
 *  `parts` carries the renderer primitives (`Box`, `Text`, `gauge`, `h`). `themeName`
 *  overrides the palette for a test; production takes it from `snap.theme`.
 *
 *  Every row is one frame line: three spine cells, a clear cell, the 30-cell body, a
 *  clear cell, three spine cells (38 cells, top to bottom 18 rows). The body keeps the
 *  old fixed columns, so nothing reflows while ticking; only the spine glyphs change with
 *  the palette. Each spine character is its own Text, so a leaf shades while the stem
 *  stays quiet without breaking the fixed width. */
export function buildRows(parts, snap, t, themeName) {
  const { Box, Text, gauge, h } = parts
  const theme = typeof themeName === 'string' && THEMES[themeName.trim().toLowerCase()]
    ? themeName.trim().toLowerCase()
    : themeNameOf(snap && snap.theme)
  const palette = themePalette(theme)
  /** One card role to the colour the terminal draws. */
  const tone = role => resolveTone(palette, ROLE_OF[role], t)
  const { left, right } = spineColumns(theme)
  const rowsSoFar = []

  /** Hidden rows must not leave a bare spine behind: the filter runs on the frame rows
   *  and the spine index is taken from the FULL list, so a hidden row shifts no glyph. */
  const frame = (key, rowKey, body, spineAt) => {
    const at = typeof spineAt === 'number' ? spineAt : rowsSoFar.length
    /** One 3-cell spine, one Text per cell: fixed width, two palette roles. The cell comes
     *  from the row's slot, so a hidden row shifts no glyph. */
    const spine = (side) => [...spineCellsAt(theme, at, side)].map((char, index) => h(
      Text,
      { key: `${key}-${side}${index}`, color: tone(spineRole(theme, char)) },
      char
    ))

    const row = h(
      Box,
      { flexDirection: 'row', key, rowKey, width: FRAME_W },
      ...spine('l'),
      h(Text, { key: `${key}-gap-l` }, ' '.repeat(GAP_W)),
      ...body,
      h(Text, { key: `${key}-gap-r` }, ' '.repeat(GAP_W)),
      ...spine('r')
    )

    rowsSoFar.push(row)

    return row
  }

  /** One card row: fixed label column plus a fixed 22-cell value field.
   *  `align` is 'end' for numbers and 'start' for text, so nothing shifts between ticks. */
  const row = (key, label, value, role, align = 'end', spineAt) => frame(key, key, [
    h(Text, { key: `${key}-label`, color: tone('label') }, label.padEnd(LABEL_W)),
    h(Text, { key: `${key}-value`, color: tone(role) }, fitCells(value, VALUE_W, align))
  ], spineAt)

  /** A full-width band: the closing rule, and the separators that split the value groups.
   *  Both are ROW_W cells, so neither can resize the card. */
  const band = (key, text, rowKey, spineAt) => frame(key, rowKey, [
    h(Text, { key: `${key}-text`, color: tone('label') }, text)
  ], spineAt)
  /* Separators are style-driven: gem / rule / dots / shade, all exactly ROW_W cells. */
  const sepRow = key => band(key, separatorLine(theme, snap.sep), 'sep')

  /* Today's blended cache hit rate: every session of the local day, not this chat. */
  const hit = Math.min(100, Math.max(0, num(snap.day_cache_hit_pct)))
  const hitRole = hit >= 70 ? 'cache' : hit >= 30 ? 'cacheWarn' : 'cacheError'

  /* Fixed-cell field: 12 gradient gauge cells, 6-cell percent, 4-cell `avg` tag = 22
   * cells. One Text per gauge cell, so the bar is a ramp instead of one flat colour. */
  const gaugeCells = gradientGauge(hit / 100, GAUGE_W, palette)
  const pctText = `${hit.toFixed(1)}%`.padStart(PCT_W)
  const agentOf = name => (snap.others || []).find(entry => entry.name === name)
  const agentRole = entry => (entry && (num(entry.tokens) || num(entry.cost_usd)))
    ? 'agent'
    : 'agentEmpty'
  const othersTotal = {
    tokens: snap.others_tokens,
    cost_usd: snap.others_cost_usd,
    cost_source: snap.others_source,
    unpriced: snap.others_unpriced
  }

  /* Header line: PROGRESS in the 8-cell label column, the model tail right-aligned in the
   * 22-cell value column, with the snapshot's age appended when it is stale. The value
   * keeps one leading cell, so `PROGRESS` never collides with a model that fills the field.
   * The title lives here because the card has no Dialog chrome to hang a title on. */
  const stale = staleLabel(snap.ts, Date.now() / 1000)
  const tail = stripModelTail(snap.model)
  /* The age keeps its cells: it is reserved before the model is fitted, so a long model
   * is cut instead of the age disappearing. */
  const modelField = ` ${fitCells(tail, Math.max(1, VALUE_W - 1 - cellsOf(stale)), 'end')}${stale}`
  const header = () => frame('header', 'model', [
    h(Text, { key: 'header-label', color: tone('logo') }, fitCells('PROGRESS', LABEL_W, 'start')),
    h(Text, { key: 'header-value', color: tone('model') }, modelField)
  ])

  /* Canopy over the header, roots under the last value group, and a separator at each
   * group boundary. All three art rows carry rowKey 'logo'. Every frame() call below is
   * in final screen order: the spine index is the call order. */
  const art = artRows(parts, theme, t)

  frame('logo-top1', 'logo', [art[0]])
  frame('logo-top2', 'logo', [art[1]])
  header()

  sepRow('sep-1')
  row('last', 'last', formatLast(snap.last_turn, snap.cost_source), 'last')
  frame('cache', 'cache', [
    h(Text, { key: 'cache-label', color: tone('label') }, 'cache'.padEnd(LABEL_W)),
    ...gaugeCells.map((cell, index) => h(
      Text,
      { key: `cache-g${index}`, color: cell.color },
      cell.char
    )),
    h(Text, { key: 'cache-value', color: tone(hitRole) }, `${pctText} avg`)
  ])
  row('cost', 'cost', formatCost(snap.total_cost_usd, snap.cost_source), 'cost')
  row('saved', 'saved', formatSaved(snap.cache_saved_usd, snap.cache_saved_source), 'saved')
  /* --- extras -------------------------------------------------------------
   * Five rows plus the conditional warn, all behind one switch. Extras off builds none
   * of them, so the card is the same 18 rows it was: the spine index of every base row
   * still comes from call order and none of these slots is taken.
   */
  const extrasOn = snap.extras !== 'off'

  if (extrasOn) {
    row('miss', 'miss', formatMiss(snap.miss_usd, snap.miss_source), 'miss', 'end', EXTRAS_SPINE.miss)
  }
  sepRow('sep-2')
  row('today', 'today', formatToday(snap.day_cost_usd, snap.day_source, snap.day_sessions), 'today')
  row('daytok', 'tok/day', formatDayTokens(snap.day_tokens, snap.delegates.tokens), 'daytok')
  if (extrasOn) {
    row('week', 'week', formatWeek(snap.week, snap.week_source, snap.week_total_usd),
      'week', 'end', EXTRAS_SPINE.week)
  }
  sepRow('sep-3')
  row('codex', 'codex', formatAgent(agentOf('codex')), agentRole(agentOf('codex')))
  row('pi', 'pi', formatAgent(agentOf('pi')), agentRole(agentOf('pi')))
  row('dsh', 'dsh', formatAgent(agentOf('dsh')), agentRole(agentOf('dsh')))
  row('others', 'others', formatAgent(othersTotal), 'others')

  if (extrasOn) {
    /* The warn row is the only conditional one: no reason means no row at all, never a
     * blank placeholder. A `!` marker sits in the label column, coloured by severity. */
    const reason = warnReason(snap, Date.now() / 1000)

    if (reason) {
      frame('warn', 'warn', [
        h(Text, { key: 'warn-label', color: tone(reason.tone) }, fitCells('!', LABEL_W, 'start')),
        h(Text, { key: 'warn-value', color: tone(reason.tone) }, fitCells(reason.text, VALUE_W, 'start'))
      ], EXTRAS_SPINE.warn)
    }

    row('errors', 'errors', formatErrors(snap.errors), 'errors', 'end', EXTRAS_SPINE.errors)
    row('tip', 'tip', tipAt(Date.now() / 1000, snap.tip_s), 'tip', 'start', EXTRAS_SPINE.tip)
  }

  const groundAt = GROUND_AT[extrasOn ? 'on' : 'off']

  frame('logo-ground', 'logo', [art[2]], groundAt)
  band('rule-bottom', '─'.repeat(ROW_W), 'rule', groundAt + 1)

  const rows = rowsSoFar
  const hide = new Set(snap.hidden_rows || [])

  return hide.size ? rows.filter(entry => !hide.has(entry.props.rowKey)) : rows
}

const DESIRED_ZONE = 'center-right'
const FALLBACK_ZONE = 'top-right'
/** What the host rendered before the centre zone existed. Used only when an older
 *  host does not advertise its zone list. */
const LEGACY_ZONES = ['top-left', 'top-right', 'bottom-left', 'bottom-right', 'dock-top', 'dock-bottom']

/** The placement this card asks for, but ONLY when the host that is loading it renders
 *  that zone.
 *
 *  A zone string the host's rail does not match is not a degrade: `AmbientRail` filters
 *  by zone and renders nothing, so the card disappears from the screen with no error.
 *  `sdk.ambientZones` is the host's own list of placement strings (single source of
 *  truth in ui-tui), so the card picks `center-right` where it is supported and falls
 *  back to `top-right`, which every build draws, where it is not. Exported as a test
 *  seam: the acceptance probe calls it with a stub SDK. */
export function zoneFor(sdk) {
  const advertised = Array.isArray(sdk && sdk.ambientZones)
    ? sdk.ambientZones.filter(zone => typeof zone === 'string')
    : LEGACY_ZONES

  return advertised.includes(DESIRED_ZONE) ? DESIRED_ZONE : FALLBACK_ZONE
}

export default function register(sdk) {
  const { Box, React, Text, defineWidgetApp, gauge, h } = sdk

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

    /* No Dialog: the host hardcodes a rounded border and a border colour on it, and this
     * card is intended as a spine-framed block rather than a box.
     *
     * No vertical-centring trick here, by design. The host's `center-right` rail zone owns
     * it: the rail grows a spacer above and below the middle stack and centres the card
     * against the real terminal height. An inner `height: '100%'` or `flexGrow` resolves
     * against this card's own content-sized height and centres nothing, so adding one back
     * would only hide which mechanism is doing the work. The column keeps the 38-cell
     * frame with equal gutters. */
    return h(
      Box,
      {
        flexDirection: 'column',
        width: INNER_W,
        alignItems: 'center',
        paddingLeft: GUTTER,
        paddingRight: GUTTER
      },
      ...buildRows({ Box, Text, gauge, h }, snap, t)
    )
  }

  const app = defineWidgetApp({
    id: 'progress',
    help: 'session tokens, today cache hit rate and cost',
    mode: 'ambient',
    /* `center-right` only when the host advertises it; a host without the rail's centre
     * stack gets `top-right`, which every build renders. An unknown zone is never drawn,
     * so this is a visibility guard, not a cosmetic one. */
    zone: zoneFor(sdk),
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
