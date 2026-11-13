/**
 * Stress test: savvy-progress TUI widget (burn / saved / today rows).
 *
 * Imports the real widget module, builds the row tree with a fake renderer, and asserts
 * every row is exactly ROW_W cells wide in every snapshot shape: full, legacy, all-none,
 * overflow, garbage, and file-level cases (missing, malformed, one chat).
 * Run: node test_widget.mjs
 */
import { buildRows, formatCost, formatBurn, formatSaved, formatToday, formatAgents, formatAgent, readSnapshot, mergeSnapshot, formatAge, staleLabel } from '../../tui-widgets/savvy-progress.mjs'
import register from '../../tui-widgets/savvy-progress.mjs'
import { mkdtempSync, mkdirSync, writeFileSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'

const LABEL_W = 8
const VALUE_W = 22
const ROW_W = LABEL_W + VALUE_W

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

function assertRows(name, snapshot, expectations = {}) {
  const rows = rowsOf(snapshot)
  check(`${name}: row count`, rows.length === 14, String(rows.length))
  for (const row of rows) {
    const key = row.props?.rowKey
    const text = textOf(row)
    const width = [...text].length
    check(`${name}: ${key} width ${ROW_W}`, width === ROW_W, `got ${width} (${JSON.stringify(text)})`)
    check(`${name}: ${key} props width`, row.props?.width === ROW_W, String(row.props?.width))
  }
  const byKey = Object.fromEntries(rows.map(r => [r.props.rowKey, textOf(r).slice(LABEL_W)]))
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
const r5 = assertRows('garbage', { ...normal, burn_usd_per_hour: 'lol', burn_source: 'weird', cache_saved_usd: null, cache_saved_source: 123, day_cost_usd: -5, day_source: 'TABLE', day_sessions: 'x', delegates: { count: 'x', cost_usd: null, cost_source: 7 } })
check('garbage: burn n/a/h after bad source', r5.burn.includes('$n/a/h'), JSON.stringify(r5.burn))
check('garbage: today none after bad count', r5.today.trim() === 'none', JSON.stringify(r5.today))

// 6. singular chat label and provider cost
const r6 = assertRows('one-chat', { ...normal, day_cost_usd: 0.5, day_source: 'provider', day_sessions: 1 })
check('one-chat: singular label', r6.today.includes('1 chat') && !r6.today.includes('1 chats'), JSON.stringify(r6.today))
check('one-chat: provider plain dollar', !r6.today.includes('~'), JSON.stringify(r6.today))

// 7. delegates plus burn together
const r7 = assertRows('delegates', { ...normal, delegates: { count: 3, cost_usd: 0.0412, cost_source: 'table' } })
check('delegates: agents row shows count', r7.agents.includes('3'), JSON.stringify(r7.agents))

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
check('formatAgents none width', formatAgents({ count: 0 }).length === VALUE_W)

// 9b. other-agent rows: cost + tokens, `none` when absent, `?` on an unpriced part
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
check('hidden rows: five rows dropped', hiddenSet.length === 9, String(hiddenSet.length))
check('hidden rows: no hidden key survives',
  !hiddenSet.some(entry => ['burn', 'saved', 'codex', 'pi', 'dsh'].includes(entry.props.rowKey)),
  hiddenSet.map(entry => entry.props.rowKey).join(','))
check('hidden rows: the rest keep their width',
  hiddenSet.every(entry => [...textOf(entry)].length === ROW_W),
  hiddenSet.map(entry => [...textOf(entry)].length).join(','))
check('hidden rows: every row hides',
  buildRows(parts, { ...normal, hidden_rows: ['model', 'tokens', 'cache', 'ctx', 'cost', 'burn', 'saved', 'today', 'daytok', 'codex', 'pi', 'dsh', 'others', 'agents'] }, t).length === 0)
check('hidden rows: unknown names are ignored',
  buildRows(parts, { ...normal, hidden_rows: ['warp-drive'] }, t).length === 14)

const r9b = assertRows('others-empty', { ...normal, others: [] })
check('others-empty: codex none', r9b.codex.trim() === 'none', JSON.stringify(r9b.codex))
check('others-empty: total none', r9b.others.trim() === 'none', JSON.stringify(r9b.others))

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
const r12 = assertRows('nan-values', { ...normal, burn_usd_per_hour: Number.NaN, cache_saved_usd: Number.POSITIVE_INFINITY, day_cost_usd: Number.NEGATIVE_INFINITY, day_sessions: Number.NaN, total_cost_usd: Number.NaN, cache_hit_pct: Number.NaN, input_tokens: Number.NaN })
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

// 14. register() against a stub SDK: app contract, and no crash on an older SDK
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
check('register: width 36', captured?.width === 36, String(captured?.width))
const el = captured?.render({ t })
check('register: render returns the card component', Boolean(el) && typeof el.type === 'function', String(el?.type))
const tree = el ? el.type(el.props) : null
check('register: card renders a Dialog', Boolean(tree) && tree.type === 'Dialog', String(tree?.type))
check('register: dialog title PROGRESS', String(tree?.props?.title).startsWith('PROGRESS'), String(tree?.props?.title))
check('register: dialog width 36', tree?.props?.width === 36, String(tree?.props?.width))
const inner = tree?.kids?.[0]
check('register: 14 rows inside', (inner?.kids || []).length === 14, String((inner?.kids || []).length))

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

// 16. a seeded stale snapshot must put its age in the title
const staleTree = captured.render({ t }).type({ t, snapshot: { ...normal, ts: Date.now() / 1000 - 900 } })
check('card: stale seed labels the title', /· 15m/.test(String(staleTree?.props?.title)), String(staleTree?.props?.title))
const freshTree = captured.render({ t }).type({ t, snapshot: { ...normal, ts: Date.now() / 1000 } })
check('card: fresh seed has a plain title', freshTree?.props?.title === 'PROGRESS', String(freshTree?.props?.title))

console.log(`\n${checks - fails.length}/${checks} checks passed`)
if (fails.length) {
  console.log('FAILURES:')
  for (const f of fails) console.log('  -', f)
  process.exit(1)
}
console.log('RESULT: PASS')
