/**
 * Stress test: savvy-progress TUI widget (framed themed card).
 *
 * Imports the real widget module, builds the row tree with a fake renderer, and asserts
 * every row is exactly FRAME_W cells wide in every snapshot shape: full, legacy, all-none,
 * overflow, garbage, extras on, and file-level cases (missing, malformed, one chat).
 *
 * The card is a spine frame now, not a flat 30-cell row: each row is
 *   spine(3) + gap(1) + label(8) + value(22) + gap(1) + spine(3) = 38 cells.
 * The old `formatAgents` helper and the `agents` row are gone: the delegate share of the
 * day rides on `tok/day` (`· 1.2M ag`) and the delegate dollars are inside `cost`, so the
 * `formatAgents` assertions became `formatDayTokens` ones.
 *
 * Run: node test_widget.mjs
 */
import { buildRows, formatCost, formatBurn, formatSaved, formatToday, formatDayTokens, formatAgent, cellsOf, fitCells, readSnapshot, mergeSnapshot, formatAge, staleLabel } from '../../tui-widgets/savvy-progress.mjs'
import register from '../../tui-widgets/savvy-progress.mjs'
import { mkdtempSync, mkdirSync, writeFileSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'

const LABEL_W = 8
const VALUE_W = 22
const SPINE_W = 3
const GAP_W = 1
const ROW_W = LABEL_W + VALUE_W
const FRAME_W = SPINE_W + GAP_W + ROW_W + GAP_W + SPINE_W
/** Where the 22-cell value field starts inside a frame line. */
const BODY_AT = SPINE_W + GAP_W + LABEL_W
/** Rows in the base card (extras off): 2 canopy art, header, 3 separators, 12 values,
 *  the root art row and the closing rule. */
const BASE_ROWS = 20
const BASE_ROW_KEYS = ['logo', 'model', 'sep', 'last', 'cache', 'ctx', 'cost', 'burn',
  'saved', 'today', 'daytok', 'codex', 'pi', 'dsh', 'others', 'rule']

let checks = 0
const fails = []

function check(name, cond, detail = '') {
  checks += 1
  if (!cond) {
    fails.push(`${name}${detail ? ' :: ' + detail : ''}`)
    console.log(`FAIL  ${name}  ${detail}`)
  } else {
    console.log(`ok    ${name}`)
  }
}

const t = {
  color: { ok: 'ok', warn: 'warn', error: 'error', muted: 'muted', text: 'text', accent: 'accent' }
}
// React moves `key` off props onto the element. The stub does the same, so a filter that
// reads props.key fails here exactly as it failed in the live TUI.
const h = (type, props, ...kids) => {
  const { key, ...rest } = props || {}

  return { type, props: rest, kids, key }
}
const gauge = (ratio, width) => {
  const filled = Math.round(Math.min(1, Math.max(0, ratio)) * width)
  return '█'.repeat(filled) + '░'.repeat(width - filled)
}
const parts = { Box: 'Box', Text: 'Text', gauge, h }

const textOf = node => node == null ? '' : typeof node === 'string' ? node : (node.kids || []).map(textOf).join('')

function rowsOf(snapshot) {
  return buildRows(parts, snapshot, t)
}

function assertRows(name, snapshot, expectations = {}, count = BASE_ROWS) {
  const rows = rowsOf(snapshot)
  check(`${name}: row count`, rows.length === count, String(rows.length))
  for (const row of rows) {
    const key = row.props?.rowKey
    const text = textOf(row)
    const width = cellsOf(text)
    check(`${name}: ${key} width ${FRAME_W}`, width === FRAME_W, `got ${width} (${JSON.stringify(text)})`)
    check(`${name}: ${key} props width`, row.props?.width === FRAME_W, String(row.props?.width))
    check(`${name}: ${key} spine is ${SPINE_W} cells`,
      cellsOf(text.slice(0, SPINE_W)) === SPINE_W && cellsOf(text.slice(-SPINE_W)) === SPINE_W,
      JSON.stringify(text))
  }
  const byKey = Object.fromEntries(
    rows.map(r => [r.props.rowKey, textOf(r).slice(BODY_AT, BODY_AT + VALUE_W)])
  )
  for (const [key, expected] of Object.entries(expectations)) {
    check(`${name}: ${key} shows ${JSON.stringify(expected)}`, byKey[key] === expected, JSON.stringify(byKey[key]))
  }
  return byKey
}

const normal = {
  model: 'deepseek-ai/deepseek-v4.1-flash-1017',
  input_tokens: 35969,
  output_tokens: 4072,
  cache_hit_pct: 91.7,
  day_cache_hit_pct: 91.7,
  context_tokens: 28802,
  context_window: 1000000,
  cost_source: 'table',
  total_cost_usd: 0.018062,
  burn_usd_per_hour: 0.55,
  burn_source: 'table',
  cache_saved_usd: 0.147,
  cache_saved_source: 'table',
  day_cost_usd: 2.1871,
  day_source: 'table',
  day_sessions: 25,
  /* Pin the base shape: this suite walks the 20-row card, and the extras have their own
   * section below. A snapshot with no key at all is 'off' in `normalize`, and `buildRows`
   * now applies the same rule, so an old payload draws the base card too. */
  extras: 'off',
  delegates: { count: 0, cost_usd: 0, cost_source: 'none' }
}

// 1. normal
const r1 = assertRows('normal', normal, {
  burn: formatBurn(0.55, 'table'),
  saved: formatSaved(0.147, 'table'),
  today: formatToday(2.1871, 'table', 25)
})
check('normal: burn reads ~$0.55/h', r1.burn.includes('~$0.55/h'), JSON.stringify(r1.burn))
check('normal: today reads 25 chats', r1.today.includes('25 chats'), JSON.stringify(r1.today))
check('normal: context reads the prompt share', r1.ctx.includes('2.9%'), JSON.stringify(r1.ctx))

// 2. legacy snapshot with no new keys at all
const legacy = { model: 'deepseek-v4.1-flash', input_tokens: 1, output_tokens: 1, cache_hit_pct: 50, context_tokens: 1, context_window: 1000000, cost_source: 'provider', total_cost_usd: 0.01, delegates: { count: 0 } }
const r2 = assertRows('legacy', legacy)
check('legacy: burn unnormalized reads n/a', r2.burn.includes('$n/a/h'), JSON.stringify(r2.burn))
check('legacy: saved reads n/a', r2.saved.includes('$n/a'), JSON.stringify(r2.saved))
check('legacy: today reads none', r2.today.trim() === 'none', JSON.stringify(r2.today))

// 3. every source none
const r3 = assertRows('all-none', { ...normal, cost_source: 'none', total_cost_usd: 0, burn_source: 'none', burn_usd_per_hour: 0, cache_saved_source: 'none', cache_saved_usd: 0, day_source: 'none', day_cost_usd: 0, day_sessions: 0 })
check('all-none: burn n/a/h', r3.burn.includes('$n/a/h'), JSON.stringify(r3.burn))
check('all-none: saved n/a', r3.saved.includes('$n/a'), JSON.stringify(r3.saved))
check('all-none: today none', r3.today.trim() === 'none', JSON.stringify(r3.today))

// 4. overflow values must truncate, never push the row wide
const r4 = assertRows('overflow', { ...normal, total_cost_usd: 12345678.9, burn_usd_per_hour: 999999.99, cache_saved_usd: 1234567.891, day_cost_usd: 98765.4321, day_sessions: 54321 })
check('overflow: truncation marker present', r4.burn.includes('…') || r4.saved.includes('…') || r4.today.includes('…'), JSON.stringify(r4))

// 5. garbage / hostile values
const r5 = assertRows('garbage', { ...normal, burn_usd_per_hour: 'lol', burn_source: 'weird', cache_saved_usd: null, cache_saved_source: 123, day_cost_usd: -5, day_source: 'TABLE', day_sessions: 'x', context_tokens: 'x', context_window: 'x', delegates: { count: 'x', cost_usd: null, cost_source: 7 } })
check('garbage: burn n/a/h after bad source', r5.burn.includes('$n/a/h'), JSON.stringify(r5.burn))
check('garbage: today none after bad count', r5.today.trim() === 'none', JSON.stringify(r5.today))
check('garbage: a hostile context window falls back to 1M', r5.ctx.includes('0.0%'), JSON.stringify(r5.ctx))

// 6. singular chat label and provider cost
const r6 = assertRows('one-chat', { ...normal, day_cost_usd: 0.5, day_source: 'provider', day_sessions: 1 })
check('one-chat: singular label', r6.today.includes('1 chat') && !r6.today.includes('1 chats'), JSON.stringify(r6.today))
check('one-chat: provider plain dollar', !r6.today.includes('~'), JSON.stringify(r6.today))

// 7. delegate tokens ride on the day token row (the `agents` row is gone)
const r7 = assertRows('delegates', { ...normal, delegates: { count: 3, cost_usd: 0.0412, cost_source: 'table', input_tokens: 1_200_000, output_tokens: 0, cache_read: 0 } })
check('delegates: tok/day carries the agent share', r7.daytok.includes('1.2M ag'), JSON.stringify(r7.daytok))

// 8. huge token counts and full context
const r8 = assertRows('huge-tokens', { ...normal, input_tokens: 123456789, output_tokens: 987654321, context_tokens: 1000000, context_window: 1000000 })
check('huge-tokens: context gauge pinned at 100.0%', r8.ctx.includes('100.0%'), JSON.stringify(r8.ctx))

// 9. formatter contracts
check('formatBurn none', formatBurn(1, 'none') === '$n/a/h'.padStart(VALUE_W))
check('formatBurn table', formatBurn(0.5543, 'table') === '~$0.55/h'.padStart(VALUE_W))
check('formatBurn provider', formatBurn(0.5543, 'provider') === ' $0.55/h'.padStart(VALUE_W))
check('formatSaved none', formatSaved(1, 'none') === '$n/a'.padStart(VALUE_W))
check('formatSaved table', formatSaved(0.147, 'table') === '~$0.1470'.padStart(VALUE_W))
check('formatToday none', formatToday(1, 'table', 0) === 'none'.padStart(VALUE_W))
check('formatToday one', formatToday(0.5, 'provider', 1) === ' $0.5000  1 chat'.padStart(VALUE_W), JSON.stringify(formatToday(0.5, 'provider', 1)))
check('formatCost still 8 cells', formatCost(0.0123, 'table').length === 8)
check('formatDayTokens none width', formatDayTokens(18_400_000).length === VALUE_W)
check('formatDayTokens carries the agent share', formatDayTokens(18_400_000, 1_200_000).includes('1.2M ag'), JSON.stringify(formatDayTokens(18_400_000, 1_200_000)))

// 9b. other-agent rows: cost + tokens, `none` when absent, `?` on an unpriced part
check('cells: CJK is two cells', cellsOf('界界') === 4, String(cellsOf('界界')))
check('fitCells: wide text fits the cell budget', cellsOf(fitCells('界'.repeat(22), VALUE_W)) === VALUE_W,
  String(cellsOf(fitCells('界'.repeat(22), VALUE_W))))
check('fitCells: no unpaired surrogate', !/[\uD800-\uDBFF](?![\uDC00-\uDFFF])/.test(fitCells('a\n界'.repeat(9), VALUE_W)))
check('fitCells: control characters removed', !fitCells('a\u0000b\u001b[31m', VALUE_W).includes('\u001b'))
const wideRows = buildRows(parts, { ...normal, model: '界'.repeat(24) }, t)
const wideModel = wideRows.find(row => row.props.rowKey === 'model')
check('wide model row stays FRAME_W cells', cellsOf(textOf(wideModel)) === FRAME_W,
  String(cellsOf(textOf(wideModel))))
check('formatAgent none width', formatAgent(null).length === VALUE_W)
check('formatAgent none text', formatAgent(null).trim() === 'none', JSON.stringify(formatAgent(null)))
check('formatAgent tokens only', formatAgent({ tokens: 21631256, cost_usd: 0, cost_source: 'none', unpriced: true }).trim() === '$n/a 21.6M', JSON.stringify(formatAgent({ tokens: 21631256, cost_usd: 0, cost_source: 'none', unpriced: true })))
check('formatAgent priced width', formatAgent({ tokens: 111347005, cost_usd: 1.6343, cost_source: 'table', unpriced: true }).length === VALUE_W)
check('formatAgent priced marks estimate', formatAgent({ tokens: 111347005, cost_usd: 1.6343, cost_source: 'table', unpriced: true }).trim().startsWith('~$1.6343?'), JSON.stringify(formatAgent({ tokens: 111347005, cost_usd: 1.6343, cost_source: 'table', unpriced: true })))
check('formatAgent zero is none', formatAgent({ tokens: 0, cost_usd: 0, cost_source: 'none' }).trim() === 'none')
const r9 = assertRows('others', { ...normal, others: [
  { name: 'codex', tokens: 21631256, cost_usd: 0, cost_source: 'none', unpriced: true },
  { name: 'pi', tokens: 111347005, cost_usd: 1.6343, cost_source: 'table', unpriced: true },
  { name: 'dsh', tokens: 93826836, cost_usd: 3.0715, cost_source: 'table', unpriced: true }
], others_tokens: 226805097, others_cost_usd: 4.7058, others_source: 'table', others_unpriced: true })
check('others: codex row', r9.codex.includes('21.6M'), JSON.stringify(r9.codex))
check('others: pi row', r9.pi.includes('111.3M'), JSON.stringify(r9.pi))
check('others: total row', r9.others.includes('226.8M'), JSON.stringify(r9.others))
const hiddenSet = buildRows(parts, { ...normal, hidden_rows: ['burn', 'saved', 'codex', 'pi', 'dsh'] }, t)
check('hidden rows: five rows dropped', hiddenSet.length === BASE_ROWS - 5, String(hiddenSet.length))
check('hidden rows: no hidden key survives',
  !hiddenSet.some(entry => ['burn', 'saved', 'codex', 'pi', 'dsh'].includes(entry.props.rowKey)),
  hiddenSet.map(entry => entry.props.rowKey).join(','))
check('hidden rows: the rest keep their width',
  hiddenSet.every(entry => cellsOf(textOf(entry)) === FRAME_W),
  hiddenSet.map(entry => cellsOf(textOf(entry))).join(','))
check('hidden rows: every row hides',
  buildRows(parts, { ...normal, hidden_rows: BASE_ROW_KEYS }, t).length === 0,
  buildRows(parts, { ...normal, hidden_rows: BASE_ROW_KEYS }, t).map(entry => entry.props.rowKey).join(','))
check('hidden rows: unknown names are ignored',
  buildRows(parts, { ...normal, hidden_rows: ['warp-drive'] }, t).length === BASE_ROWS)

const r9b = assertRows('others-empty', { ...normal, others: [] })
check('others-empty: codex none', r9b.codex.trim() === 'none', JSON.stringify(r9b.codex))
check('others-empty: total none', r9b.others.trim() === 'none', JSON.stringify(r9b.others))

// 9c. the extras switch: five extra rows, plus `warn` only when there is a reason
const extrasOn = { ...normal, extras: 'on', miss_usd: 0.12, miss_source: 'table',
  week: [{ cost_usd: 0.1 }, { cost_usd: 0.2 }, { cost_usd: 0.3 }, { cost_usd: 0.4 },
    { cost_usd: 0.5 }, { cost_usd: 0.6 }, { cost_usd: 0.7 }],
  week_source: 'table', week_total_usd: 2.8, errors: { count: 0, signal: false } }
const extraRows = buildRows(parts, extrasOn, t)
check('extras on: five rows added', extraRows.length === BASE_ROWS + 4, String(extraRows.length))
check('extras on: every row keeps the frame width',
  extraRows.every(entry => cellsOf(textOf(entry)) === FRAME_W),
  extraRows.map(entry => cellsOf(textOf(entry))).join(','))
check('extras on: the extras row keys are drawn',
  ['miss', 'week', 'errors', 'tip'].every(key => extraRows.some(entry => entry.props.rowKey === key)),
  extraRows.map(entry => entry.props.rowKey).join(','))
check('extras on: no warn row without a reason',
  !extraRows.some(entry => entry.props.rowKey === 'warn'))
const warned = buildRows(parts, { ...extrasOn, budget_usd: 1.0 }, t)
check('extras on: a budget breach draws the warn row', warned.length === BASE_ROWS + 5 &&
  warned.some(entry => entry.props.rowKey === 'warn'), String(warned.length))
check('extras on: the closing rule is still last', warned[warned.length - 1].props.rowKey === 'rule',
  warned[warned.length - 1].props.rowKey)

// 10. file-level: missing file, malformed JSON, populated file
const home = mkdtempSync(join(tmpdir(), 'savvy-widget-'))
process.env.HERMES_HOME = home

let snap = await readSnapshot()
check('missing file: null, not a fake card', snap === null, JSON.stringify(snap).slice(0, 80))

mkdirSync(join(home, 'state'), { recursive: true })
writeFileSync(join(home, 'state/savvy-progress.json'), '{not json')
snap = await readSnapshot()
check('malformed json: null', snap === null, JSON.stringify(snap).slice(0, 80))

writeFileSync(join(home, 'state/savvy-progress.json'), JSON.stringify({ ...normal, day_sessions: 3 }))
snap = await readSnapshot()
check('populated file: parsed', snap.day_sessions === 3 && Math.abs(snap.burn_usd_per_hour - 0.55) < 1e-9, JSON.stringify({ d: snap.day_sessions, b: snap.burn_usd_per_hour }))
assertRows('file-snapshot', snap)

writeFileSync(join(home, 'state/savvy-progress.json'), JSON.stringify([1, 2, 3]))
snap = await readSnapshot()
check('json array: null', snap === null, JSON.stringify(snap).slice(0, 80))

// 11. legacy file through readSnapshot: burn inherits the cost source, saved stays n/a
writeFileSync(join(home, 'state/savvy-progress.json'), JSON.stringify(legacy))
snap = await readSnapshot()
check('legacy file: burn_source inherits provider', snap.burn_source === 'provider', snap.burn_source)
check('legacy file: burn renders plain dollar', formatBurn(snap.burn_usd_per_hour, snap.burn_source) === ' $0.00/h'.padStart(VALUE_W), JSON.stringify(formatBurn(snap.burn_usd_per_hour, snap.burn_source)))
check('legacy file: saved stays n/a', snap.cache_saved_source === 'none', snap.cache_saved_source)
assertRows('legacy-file', snap)

rmSync(home, { recursive: true, force: true })

// 12. NaN / Infinity inside a parsed snapshot must never print "NaN"
const r12 = assertRows('nan-values', { ...normal, burn_usd_per_hour: Number.NaN, cache_saved_usd: Number.POSITIVE_INFINITY, day_cost_usd: Number.NEGATIVE_INFINITY, day_sessions: Number.NaN, total_cost_usd: Number.NaN, cache_hit_pct: Number.NaN, day_cache_hit_pct: Number.NaN, context_tokens: Number.NaN, input_tokens: Number.NaN })
const joined = Object.values(r12).join(' ')
check('nan-values: no NaN or Infinity text', !/NaN|Infinity/.test(joined), JSON.stringify(joined))

// 13. a JSON file with a bare Infinity literal (invalid JSON) degrades to the cold card
const home2 = mkdtempSync(join(tmpdir(), 'savvy-widget2-'))
mkdirSync(join(home2, 'state'), { recursive: true })
process.env.HERMES_HOME = home2
writeFileSync(join(home2, 'state/savvy-progress.json'), '{"burn_usd_per_hour": Infinity, "day_sessions": 9}')
snap = await readSnapshot()
check('infinity literal: null, no throw', snap === null, JSON.stringify(snap).slice(0, 80))
check('infinity literal: last good snapshot survives', mergeSnapshot({ day_sessions: 4 }, snap).day_sessions === 4)
rmSync(home2, { recursive: true, force: true })

// 14. register() against a stub SDK: app contract, and no crash on an older SDK.
//     The card is a plain Box block (no Dialog), so the checks read the Box and its rows.
let captured = null
const stubSdk = {
  Box: 'Box', Dialog: 'Dialog', Text: 'Text',
  React: { useState: init => [typeof init === 'function' ? init() : init, () => {}], useEffect: () => {} },
  defineWidgetApp: app => app,
  gauge,
  h,
  openWidget: app => { captured = app }
}
let registerError = null
try {
  register(stubSdk)
} catch (err) {
  registerError = err
}
check('register: no throw', registerError === null, String(registerError))
check('register: docks the widget', captured !== null)
check('register: app id', captured?.id === 'progress', String(captured?.id))
check('register: ambient mode', captured?.mode === 'ambient', String(captured?.mode))
check('register: top-right zone', captured?.zone === 'top-right', String(captured?.zone))
check('register: width 44', captured?.width === 44, String(captured?.width))
const el = captured?.render({ t })
check('register: render returns the card component', Boolean(el) && typeof el.type === 'function', String(el?.type))
const tree = el ? el.type(el.props) : null
check('register: card renders a Box, not a Dialog', Boolean(tree) && tree.type === 'Box', String(tree?.type))
check('register: card container is the 42-cell gutter box', tree?.props?.width === 42, String(tree?.props?.width))
check('register: 20 rows inside', (tree?.kids || []).length === BASE_ROWS, String((tree?.kids || []).length))

let oldSdkError = null
try {
  register({ ...stubSdk, openWidget: undefined })
} catch (err) {
  oldSdkError = err
}
check('register: old SDK without openWidget does not throw', oldSdkError === null, String(oldSdkError))

// 15. fallbacks: merge keeps the last good card, age labels stay short
const good = { day_sessions: 7, burn_usd_per_hour: 0.5, ts: 1000 }
check('merge: null keeps previous', mergeSnapshot(good, null) === good)
check('merge: array keeps previous', mergeSnapshot(good, [1, 2]) === good)
check('merge: string keeps previous', mergeSnapshot(good, 'x') === good)
const merged = mergeSnapshot(good, { ...good, day_sessions: 9 })
check('merge: fresh object replaces previous', merged.day_sessions === 9 && merged.ts === 1000, JSON.stringify(merged).slice(0, 60))
check('age: seconds', formatAge(40) === '40s', formatAge(40))
check('age: minutes', formatAge(600) === '10m', formatAge(600))
check('age: hours', formatAge(7200) === '2h', formatAge(7200))
check('age: days', formatAge(200000) === '2d', formatAge(200000))
check('age: negative clamps', formatAge(-5) === '0s', formatAge(-5))
check('stale: fresh stays silent', staleLabel(Date.now() / 1000, Date.now() / 1000) === '', staleLabel(Date.now() / 1000, Date.now() / 1000))
check('stale: old shows age', staleLabel(Date.now() / 1000 - 300, Date.now() / 1000).startsWith(' · 5m'), staleLabel(Date.now() / 1000 - 300, Date.now() / 1000))
check('stale: no ts stays silent', staleLabel(0, Date.now() / 1000) === '', staleLabel(0, Date.now() / 1000))

// 16. a seeded stale snapshot must put its age in the header row (no Dialog title now)
const headerOf = card => {
  const rows = card?.kids || []
  const header = rows.find(row => row.props?.rowKey === 'model')
  return header ? textOf(header) : ''
}
const staleTree = captured.render({ t }).type({ t, snapshot: { ...normal, ts: Date.now() / 1000 - 900 } })
check('card: stale seed labels the header', /· 15m/.test(headerOf(staleTree)), JSON.stringify(headerOf(staleTree)))
const freshTree = captured.render({ t }).type({ t, snapshot: { ...normal, ts: Date.now() / 1000 } })
check('card: fresh seed has a plain header', !/·/.test(headerOf(freshTree)), JSON.stringify(headerOf(freshTree)))
const extrasTree = captured.render({ t }).type({ t, snapshot: { ...normal, extras: 'on' } })
check('card: extras snapshot renders the extra rows', (extrasTree?.kids || []).length === BASE_ROWS + 4,
  String((extrasTree?.kids || []).length))

console.log(`\n${checks - fails.length}/${checks} checks passed`)
if (fails.length) {
  console.log('FAILURES:')
  for (const f of fails) console.log('  -', f)
  process.exit(1)
}
console.log('RESULT: PASS')
