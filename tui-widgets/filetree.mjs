/**
 * filetree.mjs — two widgets over one snapshot.
 *
 * `filetree` (ambient, top-right): a 40-column, 6-row card (1 header + 5 file
 * rows, the ambient limit) polling $HERMES_HOME/state/filetree.json, written by
 * the `filetree-rail` Hermes plugin. `explorer` (modal): the same JSON as a
 * navigable list, launched on demand with /explorer. Ambient rails capture no
 * keys, so a keyboard-driven list must be modal. It never docks by itself.
 */

import { closeSync, openSync, readFileSync, readSync } from 'node:fs'
import { homedir } from 'node:os'

const WIDTH = 40
const FILE_ROWS = 5
const POLL_MS = 1000
const GLYPH_W = 2
const NAME_W = 20
const PARENT_W = 14
const COUNT_W = 4 // space + 3 right-aligned digits
const HOME = (process.env.HOME || '').replace(/\/+$/, '')

const OP_GLYPH = { edit: '◆', read: '○', search: '▹', write: '●' }

const blank = n => ' '.repeat(Math.max(0, n))
const padEnd = (s, n) => (s.length >= n ? s.slice(0, n) : s + blank(n - s.length))
const padStart = (s, n) => (s.length >= n ? s.slice(-n) : blank(n - s.length) + s)

/** Shorten from the LEFT with a leading ellipsis. Never pads. */
const clip = (s, n) => (n <= 0 ? '' : s.length <= n ? s : '…' + s.slice(s.length - (n - 1)))

/** Same, but cut on a path separator so a directory name is never half a word. */
const clipDir = (s, n) => {
  if (n <= 0) return ''
  if (s.length <= n) return s
  const tail = s.slice(s.length - (n - 1))
  const slash = tail.indexOf('/')
  return `…${slash >= 0 ? tail.slice(slash) : tail}`
}

const splitPath = path => {
  const cut = path.lastIndexOf('/')
  return cut <= 0 ? { base: path, parent: '' } : { base: path.slice(cut + 1), parent: path.slice(0, cut) }
}

/** Dim parent dir, with $HOME folded to ~. */
const prettyParent = dir => {
  if (!dir) return ''
  if (HOME && (dir === HOME || dir.startsWith(`${HOME}/`))) return `~${dir.slice(HOME.length)}`
  return dir === '/' ? '/' : dir.replace(/^\/+/, '/')
}

const touches = entry => (Number(entry && entry.reads) || 0) + (Number(entry && entry.writes) || 0)

const toneFor = (op, t) => {
  if (op === 'write') return t.color.ok
  if (op === 'edit') return t.color.warn
  return op === 'search' ? t.color.label : t.color.muted
}

// ── ambient geometry ────────────────────────────────────────────────────────

const headerRow = (Box, h, Text, t, counts) => {
  const c = counts || {}
  const head = 'FILES'
  const room = WIDTH - head.length
  const tail = padStart(clip(`r${Number(c.read) || 0} w${Number(c.write) || 0} e${Number(c.edit) || 0}`, room), room)

  return h(
    Box,
    { flexDirection: 'row', key: 'header', width: WIDTH },
    h(Text, { bold: true, color: t.color.accent }, head),
    h(Text, { color: t.color.muted }, tail)
  )
}

const fileRow = (Box, h, Text, t, entry, index) => {
  const op = OP_GLYPH[entry && entry.op] ? entry.op : 'read'
  const path = String((entry && entry.path) || '')
  const { base, parent } = splitPath(path)
  const name = padEnd(clip(base || path, NAME_W), NAME_W)
  const dir = padEnd(clipDir(prettyParent(parent), PARENT_W), PARENT_W)
  const count = padStart(String(touches(entry)), COUNT_W - 1)

  return h(
    Box,
    { flexDirection: 'row', key: `f${index}`, width: WIDTH },
    h(Text, { color: toneFor(op, t) }, padEnd(OP_GLYPH[op], GLYPH_W)),
    h(Text, { color: t.color.text }, name),
    h(Text, { color: t.color.muted }, dir),
    h(Text, { color: t.color.accent }, ` ${count}`)
  )
}

const emptyRow = (Box, h, Text, t, index) =>
  h(Box, { flexDirection: 'row', key: `e${index}`, width: WIDTH }, h(Text, { color: t.color.muted }, blank(WIDTH)))

/** Fixed-height body: header + FILE_ROWS rows, padded with blanks. */
const cardRows = (Box, h, Text, t, snapshot) => {
  const files = snapshot && Array.isArray(snapshot.files) ? snapshot.files : []
  const rows = [headerRow(Box, h, Text, t, snapshot && snapshot.counts)]

  for (let i = 0; i < FILE_ROWS; i += 1) {
    const entry = files[i]
    rows.push(entry ? fileRow(Box, h, Text, t, entry, i) : emptyRow(Box, h, Text, t, i))
  }

  return rows
}

// ── ambient data ────────────────────────────────────────────────────────────

async function statePath() {
  const home = (process.env.HERMES_HOME || '').trim()
  if (home) return `${home.replace(/\/+$/, '')}/state/filetree.json`

  const os = await import('node:os')
  return `${os.homedir()}/.hermes/state/filetree.json`
}

/** Read the plugin snapshot. Missing or broken file -> null (cold start), never throws. */
async function readState() {
  try {
    const fsp = await import('node:fs/promises')
    const raw = await fsp.readFile(await statePath(), 'utf8')
    const parsed = JSON.parse(raw)
    return parsed && typeof parsed === 'object' ? parsed : null
  } catch {
    return null
  }
}

// ── explorer helpers (modal) ────────────────────────────────────────────────
// Budget: width <= 110, 29 rows (Dialog chrome 8 + body 21) in a 30-row modal.
// Body = header 1 + list rows + preview title 1 + 8 preview rows (list 20 -> 10 when open).

const X_WIDTH = 110, X_ROWS = 20, X_ROWS_PV = 10
const X_PV_ROWS = 8, X_PV_LINES = 60, X_READ = 65536
const X_NAME = 30, X_COUNT = 8, X_WRAP = 100

const errCode = error => String((error && (error.code || error.name)) || 'error')
const xHome = () => (process.env.HERMES_HOME || '').trim().replace(/\/+$/, '') || `${homedir()}/.hermes`
const xPath = () => `${xHome()}/state/filetree.json`
const countsOf = c => ({ edit: Number(c && c.edit) || 0, read: Number(c && c.read) || 0, write: Number(c && c.write) || 0 })

/** Snapshot, synchronously. A missing or broken file is a note, never a throw. */
function loadExplorer() {
  try {
    const parsed = JSON.parse(readFileSync(xPath(), 'utf8'))
    const raw = Array.isArray(parsed && parsed.files) ? parsed.files : []
    const files = raw
      .filter(entry => entry && typeof entry.path === 'string' && entry.path)
      .map(entry => ({
        op: OP_GLYPH[entry.op] ? entry.op : 'read', path: entry.path, reads: Number(entry.reads) || 0,
        ts: Number(entry.ts) || 0, writes: Number(entry.writes) || 0
      }))
      .sort((a, b) => b.ts - a.ts)

    return { counts: countsOf(parsed && parsed.counts), files, note: files.length ? '' : 'no files recorded yet' }
  } catch (error) {
    return { counts: countsOf(null), files: [], note: `cannot read state: ${errCode(error)}` }
  }
}

/**
 * First 60 lines of one file. Never throws: a failure is one 'cannot read: CODE'
 * line. Reads at most 64 KiB, so a huge log cannot stall the TUI.
 */
function readPreview(filePath) {
  if (!filePath || typeof filePath !== 'string') return { failed: true, lines: ['cannot read: no path'], total: 0 }
  let fd

  try {
    fd = openSync(filePath, 'r')
    const buf = Buffer.alloc(X_READ)
    const chunk = buf.subarray(0, readSync(fd, buf, 0, X_READ, 0))
    if (chunk.includes(0)) return { binary: true, failed: true, lines: ['binary file, preview skipped'], total: 0 }

    const all = chunk.toString('utf8').replace(/[\u0000-\u0008\u000b\u000c\u000e-\u001f]+/g, ' ').split(/\r?\n/)
    if (all[all.length - 1] === '') all.pop() // trailing newline is not an extra line

    return all.length ? { lines: all.slice(0, X_PV_LINES), total: all.length } : { lines: ['(empty)'], total: 0 }
  } catch (error) {
    return { failed: true, lines: [`cannot read: ${errCode(error)}`], total: 0 }
  } finally {
    if (fd !== undefined) {
      try {
        closeSync(fd)
      } catch {}
    }
  }
}

const wrapLine = (line, width) => {
  const out = []
  let rest = String(line)
  while (rest.length > width) {
    out.push(rest.slice(0, width))
    rest = rest.slice(width)
  }
  out.push(rest)
  return out
}

const selOf = state => Math.max(0, Math.min(Number(state && state.sel) || 0, ((state && state.files) || []).length - 1))

/** Scroll window: keep the selected row inside [top, top + height). */
const keepVisible = (sel, top, height) => {
  const base = Math.max(0, Number(top) || 0)
  if (sel < base) return sel
  return sel >= base + height ? Math.max(0, sel - height + 1) : base
}

/** One full-width body line: clipped, padded, muted unless a tone is given. */
const xLine = (Box, h, Text, t, key, inner, text, color) =>
  h(Box, { flexDirection: 'row', key, width: inner }, h(Text, { color: color || t.color.muted }, padEnd(clip(text, inner), inner)))

const xHeader = (Box, h, Text, t, state, inner) => {
  const c = state.counts || {}
  const head = padEnd(`FILES ${(state.files || []).length}`, 12) + padEnd(`r${c.read || 0} w${c.write || 0} e${c.edit || 0}`, 16)
  const note = clip(state.note || 'j/k move  enter preview  esc close', Math.max(0, inner - 28))
  return h(Box, { flexDirection: 'row', key: 'xhead', width: inner },
    h(Text, { bold: true, color: t.color.accent }, head),
    h(Text, { color: t.color.muted }, note))
}

const xRow = (Box, h, Text, t, entry, index, selected, inner) => {
  const { base, parent } = splitPath(entry.path)
  const dirW = Math.max(8, inner - 2 - X_NAME - X_COUNT)
  const glyph = padEnd(OP_GLYPH[entry.op], 2)
  const name = padEnd(clip(base || entry.path, X_NAME), X_NAME)
  const dir = padEnd(clipDir(prettyParent(parent), dirW), dirW)
  const count = padStart(String(touches(entry)), X_COUNT - 1)
  if (selected) return xLine(Box, h, Text, t, `x${index}`, inner, `${glyph}${name}${dir}${count}`, t.color.accent)

  return h(Box, { flexDirection: 'row', key: `x${index}`, width: inner },
    h(Text, { color: toneFor(entry.op, t) }, glyph),
    h(Text, { color: t.color.text }, name),
    h(Text, { color: t.color.muted }, dir),
    h(Text, { color: t.color.accent }, count))
}

const xPreview = (Box, h, Text, t, state, inner) => {
  const preview = state.preview || {}
  const lines = Array.isArray(preview.lines) ? preview.lines : []
  const total = Number(preview.total) || lines.length
  const head = `PREVIEW  ${clip(String(preview.title || ''), 36)}  ${total} line${total === 1 ? '' : 's'}`
  const rows = [xLine(Box, h, Text, t, 'xpt', inner, head, preview.failed ? t.color.error : t.color.primary)]
  const wrapped = []

  for (const line of lines) {
    for (const segment of wrapLine(line, Math.max(20, Math.min(X_WRAP, inner - 2)))) wrapped.push(`  ${segment}`)
  }

  for (let i = 0; i < X_PV_ROWS; i += 1) rows.push(xLine(Box, h, Text, t, `xp${i}`, inner, wrapped[i] || '', t.color.text))

  return rows
}

// ── apps ────────────────────────────────────────────────────────────────────

export default function register(sdk) {
  const { Box, Dialog, Overlay, React, Text, defineWidgetApp, h, openWidget, updateWidget } = sdk
  let lastKey = ''

  // Polls off a component effect so the timer dies with the card on unmount.
  function Poller() {
    React.useEffect(() => {
      let live = true

      const poll = async () => {
        const snapshot = await readState()
        if (!live || !snapshot) return

        const key = JSON.stringify(snapshot)
        if (key === lastKey) return // unchanged: no repaint

        lastKey = key
        updateWidget(app, () => ({ snapshot }))
      }

      poll()
      const timer = setInterval(poll, POLL_MS)

      return () => {
        live = false
        clearInterval(timer)
      }
    }, [])

    return null
  }

  const app = defineWidgetApp({
    id: 'filetree',
    help: 'live rail: files this session read and edited',
    init: () => ({ snapshot: null }),
    mode: 'ambient',
    reduce: state => state,
    render: ({ state, t }) =>
      h(
        Box,
        { flexDirection: 'column', overflow: 'hidden', width: WIDTH },
        h(Poller, { key: 'poller' }),
        ...cardRows(Box, h, Text, t, state && state.snapshot)
      ),
    zone: 'top-right',
    width: WIDTH
  })

  openWidget(app, app.init('')) // live on load, no slash command needed

  // Modal sibling: same data, keyboard owned, launched only with /explorer.
  const explorerApp = defineWidgetApp({
    id: 'explorer',
    help: 'browse recently touched files',
    init: () => ({ ...loadExplorer(), preview: null, sel: 0, top: 0 }),
    mode: 'modal',
    reduce: (state, input) => {
      const key = (input && input.key) || {}
      const ch = (input && input.ch) || ''
      const s = state || {}
      const files = s.files || []
      const last = files.length - 1
      const rows = s.preview ? X_ROWS_PV : X_ROWS
      const sel = selOf(s)
      const top = Math.max(0, Number(s.top) || 0)
      const step = delta => {
        const next = Math.max(0, Math.min(sel + delta, last))
        const nextTop = keepVisible(next, top, rows)
        return next === sel && nextTop === top ? s : { ...s, sel: next, top: nextTop }
      }

      if (key.escape || ch === '\u001b') {
        return s.preview ? { ...s, preview: null, top: keepVisible(sel, top, X_ROWS) } : null
      }

      if (ch === 'q' || (key.ctrl && (ch === 'c' || ch === 'C'))) return null
      if (key.downArrow || ch === 'j') return step(1)
      if (key.upArrow || ch === 'k') return step(-1)
      if (ch === 'g') return sel === 0 && top === 0 ? s : { ...s, sel: 0, top: 0 }
      if (ch === 'G') return { ...s, sel: Math.max(0, last), top: keepVisible(Math.max(0, last), top, rows) }

      if (ch === ' ' || key.return || key.enter) {
        const entry = files[sel]
        if (!entry) return s

        const { base } = splitPath(entry.path)
        const preview = { ...readPreview(entry.path), path: entry.path, title: base || entry.path }
        return { ...s, preview, top: keepVisible(sel, top, X_ROWS_PV) }
      }

      if (ch === 'r') {
        const data = loadExplorer()
        const nextSel = Math.min(sel, Math.max(0, data.files.length - 1))
        return { ...s, ...data, sel: nextSel, top: keepVisible(nextSel, 0, rows) }
      }

      return s
    },
    render: ({ cols, state, t }) => {
      const width = Math.max(64, Math.min(X_WIDTH, (Number(cols) || 120) - 4))
      const inner = width - 6
      const s = state || {}
      const files = s.files || []
      const open = Boolean(s.preview)
      const listRows = open ? X_ROWS_PV : X_ROWS
      const top = Math.max(0, Number(s.top) || 0)
      const sel = selOf(s)
      const body = [xHeader(Box, h, Text, t, s, inner)]

      for (let i = 0; i < listRows; i += 1) {
        const entry = files[top + i]
        body.push(
          entry
            ? xRow(Box, h, Text, t, entry, top + i, top + i === sel, inner)
            : xLine(Box, h, Text, t, `xb${i}`, inner, '')
        )
      }

      if (open) {
        body.push(xLine(Box, h, Text, t, 'xgap', inner, ''))
        body.push(...xPreview(Box, h, Text, t, s, inner))
      }

      return h(Overlay, { backdrop: true, key: 'explorer', zone: 'center' },
        h(Dialog, { hint: 'j/k move  g/G top/bottom  enter preview  esc back, esc again close  r reload', title: 'EXPLORER', width }, ...body))
    }
  })

  return app
}

// Test seams: the widget test drives the real default path without seeding a fake.
register.loadExplorer = loadExplorer
register.readPreview = readPreview
register.readState = readState
register.statePath = statePath
register.xPath = xPath
