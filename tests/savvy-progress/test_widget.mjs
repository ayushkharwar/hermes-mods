/**
 * savvy-progress widget smoke test. No TUI, no React install: a mock SDK captures the app
 * definition and renders the card with a fake theme, then the rows are counted and printed.
 *
 * Run: node tests/savvy-progress/test_widget.mjs
 */

const FILE = new URL('../../tui-widgets/savvy-progress.mjs', import.meta.url).href

const fakeTheme = {
  color: {
    accent: '#fff',
    border: '#fff',
    error: '#fff',
    label: '#fff',
    muted: '#fff',
    ok: '#fff',
    primary: '#fff',
    text: '#fff',
    warn: '#fff'
  }
}

const captured = []
const opened = []
const h = (type, props, ...children) => ({ type, props: props || {}, children: children.flat() })

const mockReact = {
  useState: init => [typeof init === 'function' ? init() : init, () => {}],
  useEffect: () => {},
  useRef: value => ({ current: value })
}

const mockSdk = {
  Accordion: 'Accordion',
  Box: 'Box',
  Dialog: 'Dialog',
  GridAreas: 'GridAreas',
  Overlay: 'Overlay',
  React: mockReact,
  Shimmer: 'Shimmer',
  ShimmerRows: 'ShimmerRows',
  Text: 'Text',
  WidgetGrid: 'WidgetGrid',
  defineWidgetApp: app => {
    captured.push(app)

    return app
  },
  gauge: (ratio, width) => {
    const filled = Math.round(Math.min(1, Math.max(0, ratio)) * width)

    return '█'.repeat(filled) + '░'.repeat(Math.max(0, width - filled))
  },
  h,
  hbars: (values, width) => values.map(() => '█'.repeat(width)),
  isCtrl: () => false,
  openWidget: (app, state) => {
    opened.push({ id: app && app.id, state })
  },
  sparkRows: (series, width, rows) => Array.from({ length: rows }, () => '▄'.repeat(width)),
  sparkline: (series, width = series.length) => {
    const blocks = '▁▂▃▄▅▆▇█'
    if (!series.length) {
      return ' '.repeat(Math.max(0, width))
    }
    const view = series.slice(-width)
    const min = Math.min(...view)
    const range = Math.max(...view) - min || 1

    return view
      .map(v => blocks[Math.min(7, Math.floor(((v - min) / range) * 8))])
      .join('')
      .padStart(width)
  },
  updateWidget: () => {},
  useShimmerPhase: () => 0
}

const failures = []
const check = (name, ok, detail = '') => {
  console.log(`${ok ? 'PASS' : 'FAIL'}  ${name}${detail ? `  ${detail}` : ''}`)
  if (!ok) {
    failures.push(name)
  }
}

const mod = await import(`${FILE}?t=${Date.now()}`)
check('default export is a function', typeof mod.default === 'function')
check('formatCost is exported for the cost-source test', typeof mod.formatCost === 'function')
check('formatAgents is exported for the delegate row test', typeof mod.formatAgents === 'function')

/* Agents row: same 22 cells with and without delegates. */
const agentsSome = mod.formatAgents({ count: 3, cost_usd: 0.0412, cost_source: 'table' })
const agentsNone = mod.formatAgents({ count: 0, cost_usd: 0, cost_source: 'none' })
check('formatAgents keeps a 22-cell field in both cases',
  agentsSome.length === 22 && agentsNone.length === 22, JSON.stringify([agentsSome, agentsNone]))
check('agents row shows the count and the added cost',
  agentsSome.trim() === '3   +~$0.0412', JSON.stringify(agentsSome))
check('agents row reads none without delegates', agentsNone.trim() === 'none', JSON.stringify(agentsNone))

/* Cost field: same width for all three sources, so the card never resizes. */
const costViews = ['provider', 'table', 'none'].map(source => mod.formatCost(0.0123, source))
check('formatCost returns an 8-cell field for every source',
  costViews.every(v => v.length === 8), JSON.stringify(costViews))
check('provider cost is a plain $ value', costViews[0].trim() === '$0.0123', JSON.stringify(costViews[0]))
check('table cost is marked as an estimate', costViews[1].trim() === '~$0.0123', JSON.stringify(costViews[1]))
check('none cost is n/a, not $0.0000', costViews[2].trim() === '$n/a', JSON.stringify(costViews[2]))

mod.default(mockSdk)
check('defineWidgetApp called once', captured.length === 1, `captured=${captured.length}`)

const app = captured[0]
check('app id is progress', app.id === 'progress', `id=${app.id}`)
check('app is ambient', app.mode === 'ambient', `mode=${app.mode}`)
check('app zone is top-right', app.zone === 'top-right', `zone=${app.zone}`)
check('app width is 36', app.width === 36, `width=${app.width}`)

check('auto-docked on load', opened.length === 1, `opened=${opened.length}`)
check('auto-dock used the progress app', (opened[0] || {}).id === 'progress', JSON.stringify(opened[0]))
check('auto-dock passed an init state', Boolean(opened[0] && opened[0].state), JSON.stringify((opened[0] || {}).state))

const state = app.init('')
check('init("") returns an object', state !== null && typeof state === 'object', JSON.stringify(state))

/* Render, then expand one level of function components so the card becomes inspectable. */
const tree = app.render({ cols: 120, rows: 40, state, t: fakeTheme })
check('render returns an element tree', !!tree && typeof tree === 'object' && 'type' in tree)

const expand = node => {
  if (Array.isArray(node)) {
    return node.map(expand)
  }

  if (!node || typeof node !== 'object') {
    return node
  }

  const { type, props, children } = node
  const kids = (children || []).map(expand)

  if (typeof type === 'function') {
    const rendered = type({ ...props, children: kids })

    return expand(rendered)
  }

  return { ...node, children: kids }
}

const dialog = expand(tree)
check('dialog receives an explicit width', typeof dialog.props.width === 'number', `width=${dialog.props.width}`)
check('dialog title is PROGRESS', dialog.props.title === 'PROGRESS', `title=${dialog.props.title}`)

const card = dialog.children[0]
const rows = card.children
const rowText = node =>
  (node.children || [])
    .map(child => (typeof child === 'string' ? child : rowText(child)))
    .join('')

/* count text rows: title + card rows */
const textRows = rows.length + 1
check('at most 6 card rows', rows.length <= 6, `rows=${rows.length}`)
check('every row is 30 cells', rows.every(r => rowText(r).length === 30), rows.map(r => rowText(r).length).join(','))

console.log('\n--- cold-start card (file not read; state is zeroed) ---')
console.log(`| ${dialog.props.title.padEnd(30)} |`)
for (const row of rows) {
  console.log(`| ${rowText(row)} |`)
}

/* Populated card: seed the Card with a synthetic snapshot to exercise a live layout. */
const cardType = tree.children[0].type
const seedBase = {
  cache_hit_pct: 80,
  context_tokens: 34200,
  context_window: 1000000,
  cost_source: 'table',
  cost_usd: 0.0123,
  input_tokens: 123456,
  model: 'custom/deepseek-v4.1-flash',
  output_tokens: 4321,
  primary_session: 'local-50ccc51cc86cf3e727fc505bdff80e08949c5ef08c19fac196a355f196412e7d',
  total_cost_usd: 0.0535,
  turn: 7,
  turns: [0.001, 0.0008, 0.0021, 0.0005, 0.0014, 0.0009, 0.0033, 0.0011, 0.0026, 0.0007].map(cost => ({ cost }))
}
const withDelegates = { ...seedBase, delegates: { count: 3, cost_usd: 0.0412, cost_source: 'table' } }
const withoutDelegates = {
  ...seedBase,
  total_cost_usd: 0.0123,
  delegates: { count: 0, cost_usd: 0, cost_source: 'none' }
}

const renderRows = snapshot => expand(cardType({ t: fakeTheme, snapshot })).children.map(rowText)
const seededTree = expand(cardType({ t: fakeTheme, snapshot: withDelegates }))
const seeded = seededTree.children.map(rowText)
const seededNone = renderRows(withoutDelegates)

check('populated card keeps every row at 30 cells',
  seeded.every(r => r.length === 30) && seededNone.every(r => r.length === 30),
  `${seeded.map(r => r.length).join(',')} | ${seededNone.map(r => r.length).join(',')}`)
check('populated card has exactly 6 rows', seeded.length === 6 && seededNone.length === 6,
  `rows=${seeded.length}/${seededNone.length}`)
check('populated card shows the short model', seeded[0].includes('deepseek-v4.1-flash'), seeded[0].trim())
check('cost row shows the total, not the primary share',
  seeded[4].slice(-8) === '~$0.0535', JSON.stringify(seeded[4].slice(-8)))
check('agents row shows the delegate count and the added cost',
  seeded[5].slice(8) === mod.formatAgents(withDelegates.delegates), JSON.stringify(seeded[5].slice(8)))
check('agents row reads none when no delegate ran',
  seededNone[5].slice(8).trim() === 'none' && seededNone[4].slice(-8) === '~$0.0123',
  JSON.stringify(seededNone[5].slice(8)))

/* Cosmetics: fixed label column, numbers right-aligned, both gauge rows identical in shape. */
const labels = seededTree.children.map(node => rowText(node.children[0]).trim())
check('labels are model/tokens/cache/ctx/cost/agents',
  labels.join(',') === 'model,tokens,cache,ctx,cost,agents', labels.join(','))
check('tokens row is right-aligned in its 22-cell field',
  seeded[1].endsWith(`${'123.5k / 4.3k'.padStart(22)}`), JSON.stringify(seeded[1]))
check('model row is left-aligned in its 22-cell field',
  seeded[0].slice(8) === 'deepseek-v4.1-flash'.padEnd(22), JSON.stringify(seeded[0].slice(8)))
const cacheValue = seeded[2].slice(8)
check('cache row is a 12-cell gauge plus a 5-cell percent',
  cacheValue.length === 22 && cacheValue.trim().length === 18 && cacheValue.endsWith('80.0%'),
  JSON.stringify(cacheValue))
const ctxValue = seeded[3].slice(8)
check('ctx row is a 12-cell gauge plus a 5-cell percent',
  ctxValue.length === 22 && ctxValue.trim().length === 18 && ctxValue.endsWith('3.4%'),
  JSON.stringify(ctxValue))
check('ctx gauge fills in proportion to context_tokens / context_window',
  ctxValue.trimStart().slice(0, 12) === '░░░░░░░░░░░░' && ctxValue.trimStart().slice(12) === '  3.4%',
  JSON.stringify(ctxValue))

console.log('\n--- mock card: delegates present ---')
console.log(`| ${dialog.props.title.padEnd(30)} |`)
for (const row of seeded) {
  console.log(`| ${row} |`)
}

console.log('\n--- mock card: no delegates ---')
console.log(`| ${dialog.props.title.padEnd(30)} |`)
for (const row of seededNone) {
  console.log(`| ${row} |`)
}

/* A brand-new chat: its own zeros, never the previous chat's numbers. */
const zeroPrimary = {
  cache_hit_pct: 0,
  context_tokens: 0,
  context_window: 1000000,
  cost_source: 'none',
  cost_usd: 0,
  delegates: { count: 0, cost_source: 'none', cost_usd: 0 },
  input_tokens: 0,
  model: 'deepseek-v4.1-flash',
  output_tokens: 0,
  primary_session: 'local-bbbb2222-new-chat',
  total_cost_usd: 0,
  turn: 0,
  turns: []
}
const zeroRows = renderRows(zeroPrimary)
check('zero-usage primary keeps six 30-cell rows',
  zeroRows.length === 6 && zeroRows.every(r => r.length === 30),
  zeroRows.map(r => r.length).join(','))
check('zero-usage primary shows 0 / 0 tokens',
  zeroRows[1].slice(8).trim() === '0 / 0', JSON.stringify(zeroRows[1].slice(8).trim()))
check('zero-usage primary shows an empty cache gauge at 0.0%',
  zeroRows[2].endsWith('  0.0%') && zeroRows[2].slice(8).trimStart().startsWith('░'),
  JSON.stringify(zeroRows[2].slice(8)))
check('zero-usage primary shows $n/a, not a stale dollar amount',
  zeroRows[4].slice(-8) === '    $n/a', JSON.stringify(zeroRows[4].slice(-8)))
check('zero-usage primary has no agents row value', zeroRows[5].slice(8).trim() === 'none')

console.log('\n--- mock card: zero-usage primary (new chat) ---')
console.log(`| ${dialog.props.title.padEnd(30)} |`)
for (const row of zeroRows) {
  console.log(`| ${row} |`)
}

/* All three cost sources, same seed otherwise: the card must not resize. */
const seededFor = source => renderRows({
  ...seedBase,
  cost_source: source,
  delegates: { count: 2, cost_usd: 0.0412, cost_source: source }
})

const tableRows = seededFor('table')
const providerRows = seededFor('provider')
const noneRows = seededFor('none')

check('cost_source=table prefixes the number with ~', tableRows[4].slice(-8) === '~$0.0535',
  JSON.stringify(tableRows[4].slice(-8)))
check('cost_source=provider shows a plain $ value', providerRows[4].slice(-8) === ' $0.0535',
  JSON.stringify(providerRows[4].slice(-8)))
check('cost_source=none shows $n/a', noneRows[4].slice(-8) === '    $n/a',
  JSON.stringify(noneRows[4].slice(-8)))
check('every source keeps all six rows at 30 cells',
  [tableRows, providerRows, noneRows].every(rows => rows.length === 6 && rows.every(r => r.length === 30)),
  [tableRows, providerRows, noneRows].map(rows => rows.map(r => r.length).join(',')).join(' | '))
check('the cost cell is 8 cells wide in all three cases',
  new Set([tableRows, providerRows, noneRows].map(rows => rows[4].slice(-8).length)).size === 1 &&
    [tableRows, providerRows, noneRows].every(rows => rows[4].length === 30))

console.log('\n--- cost row, all three sources ---')
for (const [source, rows] of [['provider', providerRows], ['table', tableRows], ['none', noneRows]]) {
  console.log(`${source.padEnd(9)} | ${rows[4]} |`)
}

/* Live read path: proves the fs/promises dynamic import works from inside the widget module. */
const live = await mod.readSnapshot()
check('readSnapshot returns an object', !!live && typeof live === 'object')
check('readSnapshot has numeric cost_usd', typeof live.cost_usd === 'number', `cost_usd=${live.cost_usd}`)
console.log('readSnapshot:', JSON.stringify(live))

/* HERMES_HOME-driven read: a temp home with a synthetic file proves the env path and parsing. */
const fsp = await import('node:fs/promises')
const os = await import('node:os')
const tmpHome = await fsp.mkdtemp(`${os.tmpdir()}/savvy-progress-`)
const previousHome = process.env.HERMES_HOME
await fsp.mkdir(`${tmpHome}/state`, { recursive: true })
await fsp.writeFile(
  `${tmpHome}/state/savvy-progress.json`,
  JSON.stringify({ model: 'tmp/model-x', cost_usd: 0.5, turn: 3, cache_hit_pct: 42.5, turns: [{ cost: 0.1 }] })
)
process.env.HERMES_HOME = tmpHome
const tilted = await mod.readSnapshot()
check('readSnapshot follows HERMES_HOME', tilted.model === 'tmp/model-x' && tilted.turn === 3, JSON.stringify(tilted))
check('a snapshot without cost_source keeps the plain $ form', tilted.cost_source === 'provider',
  `cost_source=${tilted.cost_source}`)

await fsp.writeFile(
  `${tmpHome}/state/savvy-progress.json`,
  JSON.stringify({ model: 'tmp/model-x', cost_usd: 0.25, cost_source: 'table', turn: 3 })
)
const estimated = await mod.readSnapshot()
check('readSnapshot passes cost_source=table through', estimated.cost_source === 'table',
  `cost_source=${estimated.cost_source}`)

await fsp.writeFile(
  `${tmpHome}/state/savvy-progress.json`,
  JSON.stringify({ model: 'tmp/model-x', cost_usd: 0.0, cost_source: 'none', turn: 3 })
)
const unpriced = await mod.readSnapshot()
check('readSnapshot passes cost_source=none through', unpriced.cost_source === 'none',
  `cost_source=${unpriced.cost_source}`)

await fsp.writeFile(
  `${tmpHome}/state/savvy-progress.json`,
  JSON.stringify({ model: 'tmp/model-x', cost_usd: 0.25, cost_source: 'bogus', turn: 3 })
)
const bogus = await mod.readSnapshot()
check('an invalid cost_source falls back to provider', bogus.cost_source === 'provider',
  `cost_source=${bogus.cost_source}`)

process.env.HERMES_HOME = previousHome
await fsp.rm(tmpHome, { recursive: true, force: true })

console.log(`\n${failures.length === 0 ? 'ALL CHECKS PASSED' : `FAILED: ${failures.join(', ')}`}`)
process.exit(failures.length === 0 ? 0 : 1)
