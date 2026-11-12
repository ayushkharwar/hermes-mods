/**
 * test_widget.mjs — verification for filetree.mjs.
 *
 * No React, no Ink. The mock sdk captures the app definition and builds plain
 * element objects, so the render tree can be inspected as data.
 *
 * Run: node tests/filetree-rail/test_widget.mjs
 */

import assert from 'node:assert/strict'
import fs from 'node:fs'
import path from 'node:path'

const HOME = process.env.HOME || '/home/user'
import { pathToFileURL } from 'node:url'

const WIDTH = 40
const ROWS = 6
const here = import.meta.dirname
const file = path.join(here, '..', '..', 'tui-widgets', 'filetree.mjs')

let checks = 0
const ok = label => {
  checks += 1
  console.log(`ok ${checks} - ${label}`)
}

// ── module load (cache-busted, exactly as the TUI host does it) ─────────────

const mod = await import(`${pathToFileURL(file).href}?t=${Date.now()}`)

assert.equal(typeof mod.default, 'function', 'default export must be register(sdk)')
ok('import with cache-busting query, default export is a function')

// ── mock sdk ────────────────────────────────────────────────────────────────

const fakeTheme = {
  color: {
    accent: '#ff5f5f',
    border: '#5f5fff',
    error: '#ff0000',
    label: '#5fafff',
    muted: '#808080',
    ok: '#5fd75f',
    primary: '#ffffff',
    text: '#d7d7d7',
    warn: '#ffd75f'
  }
}

const apps = []
const opened = []
const updates = []
const effects = []

const h = (type, props, ...children) => ({ type, props: props || {}, children: children.flat() })

const sdk = {
  Box: function Box() {},
  Dialog: function Dialog() {},
  Overlay: function Overlay() {},
  React: {
    useEffect(fn) {
      effects.push(fn)
    },
    useState(value) {
      return [value, () => {}]
    }
  },
  Text: function Text() {},
  defineWidgetApp(app) {
    apps.push(app)
    return app
  },
  h,
  isCtrl: () => false,
  openWidget(app, state) {
    opened.push({ id: app.id, state })
  },
  updateWidget(app, fn) {
    updates.push(fn)
  }
}

mod.default(sdk)

// ── helpers ─────────────────────────────────────────────────────────────────

const strings = (node, out = []) => {
  if (node === null || node === undefined) {
    return out
  }

  if (typeof node === 'string' || typeof node === 'number') {
    out.push(String(node))

    return out
  }

  if (Array.isArray(node)) {
    for (const child of node) {
      strings(child, out)
    }

    return out
  }

  for (const child of node.children || []) {
    strings(child, out)
  }

  return out
}

const rowsOf = tree =>
  (tree.children || []).filter(node => node && node.props && node.props.flexDirection === 'row' && node.props.width === WIDTH)

const rowText = row => strings(row).join('')

const maxStringLength = (tree, max = 0) => strings(tree).reduce((acc, s) => Math.max(acc, s.length), max)

const checkGeometry = (tree, label) => {
  const rows = rowsOf(tree)

  assert.equal(rows.length, ROWS, `${label}: row count must be constant ${ROWS}`)

  for (const row of rows) {
    assert.equal(rowText(row).length, WIDTH, `${label}: every row must be exactly ${WIDTH} cells`)
  }

  assert.ok(maxStringLength(tree) <= WIDTH, `${label}: no Text string may exceed ${WIDTH} chars`)
  ok(`${label}: ${ROWS} rows of ${WIDTH} cells, no string over ${WIDTH}`)

  return rows
}

// ── 1. app definition ───────────────────────────────────────────────────────

assert.equal(apps.length, 2, 'exactly two apps registered (ambient rail + modal explorer)')
assert.deepEqual(apps.map(a => a.id).sort(), ['explorer', 'filetree'], 'app ids')
const app = apps.find(a => a.id === 'filetree')
const explorer = apps.find(a => a.id === 'explorer')

assert.equal(app.mode, 'ambient')
assert.equal(app.zone, 'top-right')
assert.equal(app.width, WIDTH)
assert.equal(typeof app.help, 'string')
assert.equal(typeof app.reduce, 'function')
ok('defineWidgetApp: id/mode/zone/width as contracted (ambient, top-right, 40)')

assert.equal(opened.length, 1, 'register() docks only the ambient rail')
assert.equal(opened[0].id, 'filetree')
assert.equal(typeof opened[0].state, 'object')
ok('openWidget called once at register time (explorer is not auto-opened)')

// ── 2. init + cold start render ─────────────────────────────────────────────

const initial = app.init('')
assert.equal(typeof initial, 'object')
assert.ok(initial !== null)
ok("init('') returns a state object")

const cold = app.render({ cols: 120, rows: 40, state: initial, t: fakeTheme })
assert.equal(typeof cold, 'object')
assert.equal(cold.type, sdk.Box)
ok('render() returns an element tree (Box root)')

const coldRows = checkGeometry(cold, 'cold start')
assert.ok(rowText(coldRows[0]).startsWith('FILES'), 'header starts with FILES')
assert.equal(rowText(coldRows[0]).slice(-8), 'r0 w0 e0', 'counts render as r/w/e')
for (const row of coldRows.slice(1)) {
  assert.equal(rowText(row), ' '.repeat(WIDTH), 'cold start pads blank rows')
}

ok('cold start: FILES header + r0 w0 e0 + 5 blank padded rows')

// ── 3. hand-built state with 12 files ───────────────────────────────────────

const files = Array.from({ length: 12 }, (_, i) => ({
  op: ['read', 'write', 'edit', 'search'][i % 4],
  path: `${HOME}/projects/deeply/nested/folder/number-${i}/module_${i}.ts`,
  reads: i,
  ts: 1762830000 + i,
  writes: i % 3
}))

const warm = {
  snapshot: {
    counts: { edit: 5, read: 12, write: 3 },
    events: [{ op: 'read', path: files[0].path, tool: 'read_file', ts: 1762830000 }],
    files,
    session_id: '20261111_101500_ab12cd',
    ts: 1762830000
  }
}

const tree = app.render({ cols: 120, rows: 40, state: warm, t: fakeTheme })
const rows = checkGeometry(tree, '12-file state')

assert.equal(rowText(rows[0]).slice(-9), 'r12 w3 e5', 'header carries live counts')
assert.ok(rowText(rows[1]).includes('module_0.ts'), 'newest file first')
assert.ok(rowText(rows[2]).includes('module_1.ts'), 'second newest second')
assert.ok(!rowText(rows[1]).includes('module_5.ts'), 'only 5 file rows fit the budget')
assert.ok(rowText(rows[1]).includes('…'), 'long parent dir shortened from the left')
assert.ok(rowText(rows[1]).endsWith('   0'), 'touch count right-aligned in the last 4 cells')
ok('12 files: 5 shown newest-first, left-shortened dir, right-aligned touch count')

// ── 4. real default path (no fake seed) ─────────────────────────────────────

const realPath = await mod.default.statePath()
assert.ok(realPath.endsWith('/state/filetree.json'), `default path shape: ${realPath}`)
ok(`real default path resolves to ${realPath}`)

const realState = await mod.default.readState()
assert.ok(realState === null || typeof realState === 'object')
ok(`readState() on the real path: ${realState === null ? 'null (file absent, no throw)' : 'parsed object'}`)

// ── 5. poller effect (one real poll against the real path) ──────────────────

const pollerElement = cold.children[0]
assert.equal(typeof pollerElement.type, 'function', 'first child is the poller component')
assert.equal(effects.length, 0, 'no effect runs until React renders the component')

pollerElement.type(pollerElement.props) // simulate React calling the component
assert.equal(effects.length, 1, 'poller registered one effect')

const cleanup = effects[0]()
assert.equal(typeof cleanup, 'function', 'effect returns an interval cleanup')
ok('poller registers one useEffect and returns a cleanup function')

await new Promise(resolve => setTimeout(resolve, 1300))

if (realState !== null) {
  assert.ok(updates.length >= 1, 'poll delivered at least one snapshot to updateWidget')

  const next = updates.at(-1)({ snapshot: null })
  assert.ok(next && typeof next.snapshot === 'object', 'updateWidget reducer returns a snapshot state')
  assert.ok(Array.isArray(next.snapshot.files), 'delivered snapshot carries a files array')
  ok(`live poll delivered ${updates.length} update(s) from the real state file`)
} else {
  console.log('note: state file absent, live poll path not exercised (cold start is covered above)')
}

cleanup()

// ── 6. explorer: modal navigation, preview, geometry ────────────────────────

assert.equal(explorer.mode, 'modal', 'explorer owns every keypress')
assert.equal(explorer.help, 'browse recently touched files')
assert.equal(explorer.zone, undefined, 'modal apps have no ambient zone')

const xInit = explorer.init('')
assert.ok(xInit && typeof xInit === 'object', "explorer init('') returns a state object")
assert.equal(xInit.sel, 0)
assert.equal(xInit.top, 0)
assert.equal(xInit.preview, null)
assert.ok(Array.isArray(xInit.files))
ok(`explorer: init returns state (${xInit.files.length} files from the real path ${mod.default.xPath()})`)

// Real fixtures, inside the stage dir.
const tmp = fs.mkdtempSync(path.join(here, '.tmp-explorer-'))
const bigFile = path.join(tmp, 'big.txt')
fs.writeFileSync(bigFile, Array.from({ length: 100 }, (_, i) => `line ${i + 1}`).join('\n') + '\n')

const xFiles = Array.from({ length: 5 }, (_, i) => ({
  op: ['read', 'write', 'edit', 'search'][i % 4],
  path: i === 1 ? bigFile : `${HOME}/projects/deeply/nested/folder-${i}/module_${i}.ts`,
  reads: i,
  ts: 1000 - i,
  writes: i % 3
}))

const xState = { counts: { edit: 1, read: 3, write: 1 }, files: xFiles, note: '', preview: null, sel: 0, top: 0 }

const down = explorer.reduce(xState, { ch: 'j', key: {} })
assert.notEqual(down, xState, 'j must return a NEW state object')
assert.equal(down.sel, 1)
ok("reduce {ch:'j'} moves selection 0 -> 1 and returns a new state")

const upAgain = explorer.reduce(down, { ch: 'k', key: {} })
assert.equal(upAgain.sel, 0)
assert.notEqual(upAgain, down)
ok("reduce {ch:'k'} moves back to 0 (new state)")

const bottom = explorer.reduce(xState, { ch: 'G', key: {} })
assert.equal(bottom.sel, xFiles.length - 1)
ok(`reduce {ch:'G'} selects the last row (index ${bottom.sel})`)

assert.equal(explorer.reduce(xState, { ch: '', key: { downArrow: true } }).sel, 1)
assert.equal(explorer.reduce(bottom, { ch: '', key: { upArrow: true } }).sel, 3)
assert.equal(explorer.reduce(bottom, { ch: 'g', key: {} }).sel, 0)
assert.equal(explorer.reduce(xState, { ch: 'g', key: {} }), xState, 'g at the top swallows the key (same ref)')
ok('arrow keys mirror j/k, g jumps to top')

const manyFiles = Array.from({ length: 25 }, (_, i) => ({
  op: 'read',
  path: `${HOME}/x/dir/f${i}.ts`,
  reads: 1,
  ts: 100 - i,
  writes: 0
}))
const manyBottom = explorer.reduce({ ...xState, files: manyFiles }, { ch: 'G', key: {} })
assert.equal(manyBottom.sel, 24)
assert.equal(manyBottom.top, 5, 'G scrolls the 20-row window so row 24 is visible')
assert.equal(explorer.reduce(manyBottom, { ch: 'g', key: {} }).top, 0)
ok('scroll window: 25 files, G -> sel 24 / top 5, g -> top 0')

assert.equal(explorer.reduce(xState, { ch: 'q', key: {} }), null)
ok("reduce {ch:'q'} on a closed preview returns null (closes)")

// preview path, through the reducer
const onBig = explorer.reduce(xState, { ch: 'j', key: {} }) // sel 1 = bigFile
const withPreview = explorer.reduce(onBig, { ch: ' ', key: {} })
assert.ok(withPreview.preview, 'space opens the preview')
assert.equal(withPreview.preview.lines.length, 60)
assert.equal(withPreview.preview.total, 100)
assert.equal(withPreview.preview.lines[0], 'line 1')
assert.equal(withPreview.preview.lines.at(-1), 'line 60')
assert.equal(withPreview.top, 0, 'selection stays visible in the tight window')
ok('space opens the preview: exactly 60 of 100 lines, first and last asserted')

const previewClosed = explorer.reduce(withPreview, { ch: '', key: { escape: true } })
assert.notEqual(previewClosed, null, 'first esc must not close the app')
assert.equal(previewClosed.preview, null)
assert.equal(previewClosed.sel, 1, 'selection survives the preview close')
assert.equal(explorer.reduce(previewClosed, { ch: '', key: { escape: true } }), null, 'second esc closes the app')
ok('esc twice: preview closes first, then esc returns null (app closes)')

// direct read path: never throws
const direct = mod.default.readPreview(bigFile)
assert.equal(direct.lines.length, 60, 'direct read returns exactly 60 lines')
assert.equal(direct.total, 100)
ok('readPreview(100-line file): 60 lines returned, total 100')

const missing = mod.default.readPreview(path.join(tmp, 'absent.txt'))
assert.equal(missing.lines.length, 1)
assert.ok(missing.lines[0].startsWith('cannot read: '), missing.lines[0])
ok(`readPreview on a missing file: one line "${missing.lines[0]}"`)

const emptyFile = path.join(tmp, 'empty.txt')
fs.writeFileSync(emptyFile, '')
assert.deepEqual(mod.default.readPreview(emptyFile).lines, ['(empty)'])
ok("readPreview on an empty file returns ['(empty)']")

const binFile = path.join(tmp, 'blob.bin')
fs.writeFileSync(binFile, Buffer.from([0x41, 0x00, 0x42]))
assert.deepEqual(mod.default.readPreview(binFile).lines, ['binary file, preview skipped'])
ok('readPreview on a binary file: one line, no throw')

const reloaded = explorer.reduce(xState, { ch: 'r', key: {} })
assert.ok(Array.isArray(reloaded.files))
assert.equal(typeof reloaded.note, 'string')
ok(`r reloads the JSON: ${reloaded.files.length} files, note "${reloaded.note || 'none'}"`)

// geometry: Overlay tree, width and row budget
const xTree = explorer.render({ cols: 120, rows: 40, state: withPreview, t: fakeTheme })
assert.equal(xTree.type, sdk.Overlay, 'render returns an Overlay')
assert.equal(xTree.props.zone, 'center')

const xDialog = xTree.children[0]
assert.equal(xDialog.type, sdk.Dialog, 'Overlay holds a Dialog')
assert.ok(xDialog.props.width <= 110, `dialog width ${xDialog.props.width} <= 110`)

const xTexts = strings(xTree)
const longest = xTexts.reduce((acc, s) => Math.max(acc, s.length), 0)
assert.ok(longest <= 110, `longest rendered line is ${longest} cells, limit 110`)
ok(`explorer render: Overlay > Dialog (${xDialog.props.width} wide), longest line ${longest} cells`)

const openRows = xDialog.children.length
assert.equal(openRows, 21, 'preview open: header + 10 list + gap + preview title + 8 lines = 21')
const closedRows = explorer.render({ cols: 120, rows: 40, state: xState, t: fakeTheme }).children[0].children.length
assert.equal(closedRows, 21, 'preview closed: header + 20 list rows = 21')
assert.ok(openRows + 8 <= 30 && closedRows + 8 <= 30, 'body + 8 rows of Dialog chrome stays inside 30 rows')
ok(`row budget: ${openRows} body rows + 8 chrome rows = 29 <= 30 (constant in both modes)`)

// selection + dimming tones
const closedDialog = explorer.render({ cols: 120, rows: 40, state: xState, t: fakeTheme }).children[0]
const selRow = closedDialog.children[1]
assert.equal(selRow.children[0].props.color, fakeTheme.color.accent, 'selected row uses accent')
assert.ok(strings(selRow)[0].includes('module_0.ts'))
const plainRow = closedDialog.children[2]
assert.equal(plainRow.children[1].props.color, fakeTheme.color.text, 'unselected name uses text')
assert.equal(plainRow.children[2].props.color, fakeTheme.color.muted, 'parent dir dimmed with muted')
ok('selected row accent, unselected name text, parent dir muted')

const narrow = explorer.render({ cols: 80, rows: 40, state: xState, t: fakeTheme })
const narrowMax = strings(narrow).reduce((acc, s) => Math.max(acc, s.length), 0)
const narrowWidth = narrow.children[0].props.width
assert.ok(narrowMax <= 110 && narrowMax <= narrowWidth, `narrow render width ${narrowWidth}, longest ${narrowMax}`)
ok(`narrow terminal (80 cols): dialog ${narrowWidth} wide, longest line ${narrowMax} cells`)

const savedHome = process.env.HERMES_HOME
process.env.HERMES_HOME = path.join(tmp, 'empty-home')
const coldExplorer = explorer.init('')
if (savedHome === undefined) delete process.env.HERMES_HOME
else process.env.HERMES_HOME = savedHome
assert.equal(coldExplorer.files.length, 0)
assert.ok(coldExplorer.note.startsWith('cannot read state: '), coldExplorer.note)
ok(`cold start with no state file: "${coldExplorer.note}"`)

fs.rmSync(tmp, { force: true, recursive: true })
ok('fixtures removed')

// ── preview ─────────────────────────────────────────────────────────────────

console.log('\ncard preview (12-file state, 40 columns, 6 rows):')
for (const row of rows) {
  console.log(`|${rowText(row)}|`)
}

console.log(`\nexplorer preview (110 wide, preview open on ${path.basename(bigFile)}), body rows only:`)
for (const bodyRow of xDialog.children) {
  console.log(`|${strings(bodyRow).join('')}|`)
}

// ── summary ─────────────────────────────────────────────────────────────────

console.log(`\n${checks} checks passed, 0 failed`)
