/**
 * operations — Hermes desktop plugin ("Operations").
 *
 * Overview tab: vitals for the chosen window, today's hourly strip, what needs
 * attention, hosts with the model pool, and recent activity.
 * Flow tab: requests by entry point, profile, model and what served them, with requests per
 * hour by what served them; selecting a node shows where its traffic came from and went.
 * Activity tab: errors, gateway changes, kanban task events, host check changes, merged MRs
 * and model loads, grouped by hour and filtered by kind; an entry opens to its raw lines.
 * Handoffs tab: who handed work to whom, as lanes per receiver over the window: kanban cards
 * with every run, and subagent delegations; selecting one shows its runs or subagents.
 * Trace tab: the window's sessions; one opens to its model calls in order (model, what served
 * it, time to first token, tokens, errors) with its subagents and the session that opened it,
 * and where the session's time went.
 * Merge requests tab: open MRs and those merged in the window, each with its newest pipeline's
 * stages, who it waits on, and counts by repo.
 * Topology tab: gateways, hosts and what served their requests (pool devices and cloud
 * providers), with live state and conformance drift; counts on chips, details in the
 * inspector or a collapsed sheet; selecting one shows its details and what stops if it goes down.
 * Conformance tab: the fleet standard's repo checks from CI and each host's checks from its
 * metrics forwarder, optional per install.
 *
 * The desktop half of the operations plugin package: Hermes copies this file from
 * plugins/operations/desktop/ into place. Backend: ../dashboard/plugin_api.py, mounted
 * at /api/plugins/operations/ and reached only through ctx.rest. Read-only except the
 * Handoffs card actions (hand an open card to a profile, or close it), the Conformance
 * actions (file a card to fix a host's drift or record fixing it by hand, propose a rule as a
 * merge request), saved roles, and Activity summaries when the install turns them on.
 *
 * Routes:
 *   GET /overview?hours=N -> { vitals, today, attention, hosts, activity, errors }
 *   GET /changes?hours=N  -> { items: merged MRs and model loads, errors }
 *   GET /mrs?hours=N      -> { items: open and merged MRs with pipeline stages, errors }
 *   GET /flow?hours=N     -> { total, nodes, paths, hourly, errors }
 *   GET /activity?hours=N -> { items: hourly groups with raw lines and a key, counts, truncated, summaries, errors }
 *   POST /activity/summaries {hours} -> { summaries: {key: {text, model, provider, events}}, pending, errors }
 *   GET /handoffs?hours=N -> { items: cards and subagent delegations, pairs, truncated, errors }
 *   GET /trace?hours=N    -> { sessions, truncated, errors }
 *   GET /find-session?prefix=&before= -> { session: {session_id, start, end} | null, errors } (a health finding's cut-short id)
 *   GET /trace/{session}  -> { calls, subagents, parent, why, platform, profile, truncated, errors }
 *   GET /topology?hours=N -> { hosts (with drift), gateways, edges (with models), served, timeline (replay), router, errors }
 *   GET /attention        -> { items: the attention list alone, errors } (the alert poll)
 *   GET /assignees        -> { assignees: profile names }
 *   POST /attention/fix {key, title, body, profile} -> a kanban card for one alert, started now: { ok, card, existing, status, started, warning }
 *   POST /cards/{id}/assign {profile, start_at?} -> { ok, status, started, warning, starts_at? }
 *   POST /cards/{id}/unblock {comment}       -> { ok, status, started, warning }
 *   POST /cards/{id}/close {outcome, result} -> { ok, status }
 *   GET /conformance      -> { project, standard, repo: [rule results], pipeline, errors }
 *   POST /conformance/hosts/{host}/fix {profile|null} -> a kanban card to fix the host's failing checks,
 *                         or with no profile a record that a person is fixing them; GET /conformance
 *                         returns each host's newest fix with its state from the host's next report
 *   POST /conformance/rules {kind, rule, to?} -> a merge request adding the rule to the standard, or with
 *                         to "org" (business mode, data.org) the org's copy: { status: published | proposal_pending }
 *   GET /roles, PUT /roles {roles}, DELETE /roles -> { roles, defaults, saved, errors }
 */

import { jsx, jsxs } from 'react/jsx-runtime'
import { createContext, useContext, useEffect, useLayoutEffect, useRef, useState } from 'react'
import {
  host, haptic,
  ROUTES_AREA, SIDEBAR_NAV_AREA, PALETTE_AREA,
  useQuery, useQueryClient,
  Badge, Button, Input, Select, SelectContent, SelectItem, SelectTrigger, SelectValue, Textarea,
  StatusDot, Skeleton, ErrorState,
  cn
} from '@hermes/plugin-sdk'

const ID = 'operations'
const PATH = '/operations'
const REFRESH_MS = 30_000

// set inside register(), the only place the plugin ctx exists
let api = null
// the desktop shell blocks target=_blank; external links go through the OS door instead
const openOut = e => { e.preventDefault(); api.os.openExternal(e.currentTarget.href) }

const useOverview = hours =>
  useQuery({
    queryKey: [ID, 'overview', hours],
    queryFn: () => api.rest(`/overview?hours=${hours}`, { timeoutMs: 45_000 }),
    refetchInterval: REFRESH_MS
  })

// GitLab is a separate call, so it gets its own query and a slower poll.
const useChanges = hours =>
  useQuery({
    queryKey: [ID, 'changes', hours],
    queryFn: () => api.rest(`/changes?hours=${hours}`, { timeoutMs: 45_000 }),
    refetchInterval: 60_000
  })

const useFlow = (hours, start) =>
  useQuery({
    queryKey: [ID, 'flow', hours, start],
    queryFn: () => api.rest(`/flow?hours=${hours}${start ? `&start=${start}` : ''}`, { timeoutMs: 45_000 }),
    refetchInterval: REFRESH_MS
  })

const useErrors = (hours, start) =>
  useQuery({
    queryKey: [ID, 'errors', hours, start],
    queryFn: () => api.rest(`/errors?hours=${hours}${start != null ? `&start=${start}` : ''}`, { timeoutMs: 45_000 }),
    refetchInterval: REFRESH_MS
  })

const useActivity = hours =>
  useQuery({
    queryKey: [ID, 'activity', hours],
    queryFn: () => api.rest(`/activity?hours=${hours}`, { timeoutMs: 45_000 }),
    refetchInterval: REFRESH_MS
  })

// Readable Activity summaries, when the install turned them on; asks again while some are pending.
const useSummaries = (hours, enabled) =>
  useQuery({
    queryKey: [ID, 'summaries', hours],
    queryFn: () => api.rest('/activity/summaries', { method: 'POST', body: { hours }, timeoutMs: 90_000 }),
    enabled: !!enabled,
    refetchInterval: q => (q.state.data?.pending ? 5_000 : REFRESH_MS)
  })

const useHandoffs = (hours, start) =>
  useQuery({
    queryKey: [ID, 'handoffs', hours, start],
    queryFn: () => api.rest(`/handoffs?hours=${hours}${start ? `&start=${start}` : ''}`, { timeoutMs: 45_000 }),
    refetchInterval: REFRESH_MS
  })

const useTrace = (hours, start) =>
  useQuery({
    queryKey: [ID, 'trace', hours, start],
    queryFn: () => api.rest(`/trace?hours=${hours}${start ? `&start=${start}` : ''}`, { timeoutMs: 45_000 }),
    refetchInterval: REFRESH_MS
  })

const useSessionTrace = sid =>
  useQuery({
    queryKey: [ID, 'trace', 'session', sid],
    queryFn: () => api.rest(`/trace/${encodeURIComponent(sid)}`, { timeoutMs: 45_000 }),
    enabled: !!sid,
    refetchInterval: REFRESH_MS
  })

const useTopology = hours =>
  useQuery({
    queryKey: [ID, 'topology', hours],
    queryFn: () => api.rest(`/topology?hours=${hours}`, { timeoutMs: 45_000 }),
    refetchInterval: REFRESH_MS
  })

const useRoles = () => useQuery({ queryKey: [ID, 'roles'], queryFn: () => api.rest('/roles'), staleTime: 5 * 60_000 })

const useConformance = () =>
  useQuery({
    queryKey: [ID, 'conformance'],
    queryFn: () => api.rest('/conformance', { timeoutMs: 45_000 }),
    refetchInterval: 5 * 60_000
  })

// Hermes' own projects and repos, as the sidebar lists them for the profile Desktop is on (the gateway
// answers for its own profile otherwise): the one the focused chat works in, then created projects, then
// repos found from sessions. The backend checks each against the repo standard.
const useProjectChecks = () =>
  useQuery({
    queryKey: [ID, 'conformance', 'projects'],
    queryFn: async () => {
      const profile = host.state?.profile?.get?.()
      const ask = async method => { try { return await host.request?.(method, profile ? { profile } : {}) } catch { return null } }
      const [listed, found] = await Promise.all([ask('projects.list'), ask('projects.discover_repos')])
      const names = new Map()
      for (const p of listed?.projects || []) if (!p.archived) for (const f of p.folders || []) if (!names.has(f.path)) names.set(f.path, f.label || p.name)
      for (const r of [...(found?.repos || [])].sort((a, b) => (b.last_active || 0) - (a.last_active || 0)))
        if (r.root && !names.has(r.root)) names.set(r.root, r.label || r.root.split(/[\\/]/).pop())
      const cwd = host.state?.cwd?.get?.() || ''
      const current = [...names.keys()].filter(r => cwd === r || cwd.startsWith(`${r}/`) || cwd.startsWith(`${r}\\`)).sort((a, b) => b.length - a.length)[0]
      const roots = [...(current ? [current] : []), ...[...names.keys()].filter(r => r !== current)].slice(0, 30)
      const data = await api.rest(`/conformance/projects?${roots.map(r => `root=${encodeURIComponent(r)}`).join('&')}`, { timeoutMs: 120_000 })
      const created = new Set((listed?.projects || []).filter(p => !p.archived).flatMap(p => (p.folders || []).map(f => f.path)))
      return { ...data, names: Object.fromEntries(names), current, created: [...created], listed: !!(listed || found) }
    },
    // A repo unchanged since its last check is not read again, so this is cheap.
    refetchInterval: 60_000
  })

// --- formatting ---------------------------------------------------------------------

function fmtAgo(s) {
  if (s == null) return ''
  const m = Math.round((Date.now() / 1000 - s) / 60)
  if (m < 1) return 'just now'
  if (m < 60) return `${m}m ago`
  const h = Math.round(m / 60)
  if (h < 24) return `${h}h ago`
  return new Date(s * 1000).toLocaleDateString()
}

const fmtClock = s => new Date(s * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })

function fmtDur(s) {
  if (s == null) return '-'
  if (s < 59.95) return `${s.toFixed(1)}s`
  // Round to the shown unit first, so 5d 23.6h reads 6d 0h rather than 5d 24h.
  if (s < 3599.5) { const r = Math.round(s); return `${Math.floor(r / 60)}m${r % 60}s` }
  if (s < 86370) { const r = Math.round(s / 60); return `${Math.floor(r / 60)}h ${r % 60}m` }
  const r = Math.round(s / 3600)
  return `${Math.floor(r / 24)}d ${r % 24}h`
}


const muted = text => jsx('div', { className: 'text-xs text-(--ui-text-quaternary)', children: text })

// --- layout pieces --------------------------------------------------------------------

function Section({ title, count, error, children }) {
  return jsxs('div', {
    className: 'overflow-hidden rounded-md border border-(--ui-stroke-secondary)',
    children: [
      jsxs('div', {
        className: 'flex items-center justify-between px-3 py-1',
        style: { backgroundColor: 'color-mix(in srgb, var(--ui-stroke-secondary) 40%, transparent)' },
        children: [
          jsx('span', { className: 'text-xs font-medium', children: title }),
          count != null
            ? jsx('span', { className: 'text-[0.6875rem] text-(--ui-text-quaternary)', children: String(count) })
            : null
        ]
      }),
      jsx('div', { className: 'p-3', children: error ? muted(`Unavailable: ${error}`) : children })
    ]
  })
}

// A linked page opens on the range of the page it came from (hours), else the shortest that still covers
// when its thing happened (at, unix seconds), never shorter than its own default.
const rangeFor = ({ at, hours }, base) => hours || (at ? [6, 12, 24, 72, 168].find(h => h >= base && h * 3600 >= Date.now() / 1000 - at) ?? 168 : base)

function RangePicker({ hours, onChange }) {
  const ranges = [
    { h: 6, label: '6h' },
    { h: 12, label: '12h' },
    { h: 24, label: '24h' },
    { h: 72, label: '3d' },
    { h: 168, label: '7d' }
  ]
  return jsx('div', {
    className: 'flex items-center gap-0.5 rounded border border-(--ui-stroke-secondary) p-0.5',
    children: ranges.map(r =>
      jsx('button', {
        type: 'button',
        key: r.h,
        onClick: () => { haptic('tap'); onChange(r.h) },
        className: cn(
          'rounded px-2 py-0.5 text-[0.6875rem] transition-colors',
          hours === r.h
            ? 'bg-(--ui-accent)/10 text-(--ui-accent)'
            : 'text-(--ui-text-quaternary) hover:bg-(--chrome-action-hover)'
        ),
        children: r.label
      })
    )
  })
}

// --- shared visuals ---------------------------------------------------------------------

const TONE = { bad: '#f85149', warn: '#d29922', good: '#3fb950', info: '#58a6ff', muted: '#8b949e', purple: '#bc8cff', orange: '#f0883e', pink: '#e275ad' }
const plural = (n, word) => `${n} ${word}${n === 1 ? '' : 's'}`
const MONO = 'ui-monospace, SFMono-Regular, Menlo, monospace'
// Motion for the live views, as in the metrics-dash demos: traffic flowing along a line, a pulse on what took it
// recently, a twinkling field behind a live map and a blinking live dot. Rendered inside each map's svg.
const MOTION_CSS = `
@keyframes ops-flow { to { stroke-dashoffset: -32 } }
@keyframes ops-pulse { 50% { opacity: .04; transform: scale(1.6) } }
@keyframes ops-twinkle { 50% { opacity: .08 } }
@keyframes ops-blink { 50% { opacity: .25 } }
.ops-flow { animation: ops-flow 1.4s linear infinite }
.ops-pulse { transform-box: fill-box; transform-origin: center; animation: ops-pulse 2.4s ease-in-out infinite }
.ops-twinkle { animation: ops-twinkle 4s ease-in-out infinite }
.ops-blink { animation: ops-blink 1.6s ease-in-out infinite }
@media (prefers-reduced-motion: reduce) { .ops-flow, .ops-pulse, .ops-twinkle, .ops-blink { animation: none } }`

// A small uppercase status tag with a dot: the label is the state, the color its tone.
function Tag({ tone = 'muted', children, title }) {
  const c = TONE[tone] || tone
  return jsxs('span', {
    title,
    className: 'inline-flex shrink-0 items-center gap-1 rounded-sm px-1.5 uppercase',
    style: { fontFamily: MONO, fontSize: '0.625rem', letterSpacing: '0.06em', lineHeight: '1.1rem', color: c, backgroundColor: `color-mix(in srgb, ${c} 14%, transparent)`, whiteSpace: 'nowrap' },
    children: [jsx('span', { style: { width: 6, height: 6, borderRadius: 3, backgroundColor: c } }), children]
  })
}

// A boxed uppercase label for a kind of thing (MR, model), no state implied; color marks what a
// banner is about.
const KindBox = ({ color, children }) =>
  jsx('span', {
    className: 'shrink-0 rounded-sm border border-(--ui-stroke-secondary) px-1 uppercase text-(--ui-text-quaternary)',
    style: { fontFamily: MONO, fontSize: '0.625rem', letterSpacing: '0.04em', lineHeight: '1rem', ...(color && { color, borderColor: color, fontWeight: 600 }) },
    children
  })

// go(tab, sel, when) switches tab with something selected there (a node, card or session id), on a range from when ({ at } or { hours }).
const Nav = createContext(() => {})
// the role the page is viewed as, and every role (see --- roles ---)
const Role = createContext({ role: null, roles: [] })

// A text link, or with btn an outlined button (a drawer's actions).
// note: what the link was clicked from, which the map shows above itself
function GoLink({ tab, sel = null, at, hours, start, note, btn, children }) {
  const go = useContext(Nav)
  const onClick = () => { haptic('tap'); go(tab, sel, { at, hours, start, note }) }
  return btn
    ? jsx('button', { type: 'button', className: 'hover:bg-(--chrome-action-hover)', style: BTN, onClick, children })
    : jsx('button', { type: 'button', className: 'text-[0.6875rem] text-(--ui-accent) hover:underline', onClick, children })
}

// Drawer parts: an outlined button, the title, a line of text, a labeled section, raw lines.
const BTN = { padding: '7px 12px', fontSize: 13, fontWeight: 500, border: '1px solid var(--ui-stroke-secondary)' }
const DIM = 'var(--ui-text-tertiary, var(--ui-text-quaternary))'
// The host shows no native tooltips, so hover text is drawn: a box at x% of its track, kept inside it.
const hoverTip = (x, top, children) => jsx('div', {
  role: 'tooltip',
  style: { position: 'absolute', left: `${x}%`, top, transform: `translateX(-${x}%)`, zIndex: 10, pointerEvents: 'none', width: 'max-content', maxWidth: '24rem',
           padding: '4px 8px', borderRadius: 4, fontSize: 12, border: '1px solid var(--ui-stroke-secondary)', background: 'var(--ui-bg, #0d1117)', color: 'var(--ui-text-primary, inherit)' },
  children
})
const drawerTitle = text => jsx('h2', { key: 'title', style: { margin: 0, fontSize: 18, lineHeight: 1.25, fontWeight: 600, textWrap: 'balance', overflowWrap: 'anywhere' }, children: text })
const drawerText = (key, text) => jsx('p', { key, style: { margin: 0, fontSize: 13.5, color: DIM }, children: text })
const LABEL = { margin: 0, fontFamily: MONO, fontSize: 10.5, lineHeight: 1, fontWeight: 500, letterSpacing: '0.08em', textTransform: 'uppercase', color: 'var(--ui-text-quaternary)' }
const drawerPart = (key, label, body) =>
  jsxs('div', {
    key,
    children: [jsx('h4', { style: { ...LABEL, marginBottom: 6 }, children: label }), body]
  })
// Labeled facts in a row, mono labels over values.
const facts = (key, pairs) =>
  jsx('dl', {
    key,
    className: 'flex flex-wrap',
    style: { margin: 0, gap: '8px 26px' },
    children: pairs.map(([k, v]) =>
      jsxs('div', { key: k, className: 'grid min-w-0', style: { gap: 3 }, children: [jsx('dt', { style: LABEL, children: k }), jsx('dd', { style: { margin: 0, fontSize: 13.5, overflowWrap: 'anywhere' }, children: v })] })
    )
  })
const RAW = { margin: 0, padding: '10px 12px', fontFamily: MONO, fontSize: 11.5, lineHeight: 1.7, color: DIM, whiteSpace: 'pre-wrap', overflowWrap: 'anywhere',
              backgroundColor: 'color-mix(in srgb, var(--ui-stroke-secondary) 18%, transparent)', border: '1px solid var(--ui-stroke-secondary)' }
const rawBlock = raw =>
  jsx('pre', {
    style: RAW,
    children: raw.map(([ts, line]) => `${new Date(ts * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' })}  ${line}`).join('\n')
  })

// A panel over the right of the plugin's pane (not the window, whose title bar holds Desktop's
// own controls); Esc or a click outside closes it.
function Drawer({ label, head, onClose, children }) {
  const ref = useRef(null)
  const panel = useRef(null)
  const shade = useRef(null)
  const [box, setBox] = useState(null)
  const shown = box != null
  // the demo's slide-in, once the panel has its place; skipped for reduced motion
  useEffect(() => {
    if (!shown || window.matchMedia?.('(prefers-reduced-motion: reduce)').matches) return
    panel.current?.animate?.([{ transform: 'translateX(24px)', opacity: 0 }, { transform: 'none', opacity: 1 }], { duration: 220, easing: 'ease-out' })
    shade.current?.animate?.([{ opacity: 0 }, { opacity: 1 }], { duration: 220, easing: 'ease-out' })
  }, [shown])
  useLayoutEffect(() => {
    const pane = ref.current.closest('[data-ops-pane]')
    const fit = () => {
      const r = pane ? pane.getBoundingClientRect() : { top: 0, left: 0, right: window.innerWidth, bottom: window.innerHeight, width: window.innerWidth }
      setBox({ top: r.top, left: r.left, right: window.innerWidth - r.right, bottom: window.innerHeight - r.bottom, width: r.width })
    }
    fit()
    window.addEventListener('resize', fit)
    return () => window.removeEventListener('resize', fit)
  }, [])
  useEffect(() => {
    const key = e => { if (e.key === 'Escape') onClose() }
    window.addEventListener('keydown', key)
    return () => window.removeEventListener('keydown', key)
  }, [onClose])
  const at = box ? { top: box.top, right: box.right, bottom: box.bottom } : { top: 0, right: 0, bottom: 0 }
  return jsxs('div', {
    ref,
    style: box ? undefined : { visibility: 'hidden' },
    children: [
      jsx('div', { ref: shade, onClick: onClose, style: { position: 'fixed', ...at, left: box ? box.left : 0, zIndex: 60, backgroundColor: 'rgb(0 0 0 / 0.25)' } }),
      jsxs('aside', {
        ref: panel,
        role: 'dialog',
        'aria-label': label,
        className: 'text-sm',
        style: {
          position: 'fixed', ...at, zIndex: 61, width: box ? `min(27rem, ${Math.round(box.width * 0.92)}px)` : '27rem', overflowY: 'auto',
          display: 'grid', gridTemplateColumns: 'minmax(0, 1fr)', gap: 16, alignContent: 'start', padding: '16px 18px 24px',
          backgroundColor: 'color-mix(in srgb, var(--ui-surface-background, var(--ui-bg-elevated, #0d1117)) 86%, transparent)',
          backdropFilter: 'blur(14px)', WebkitBackdropFilter: 'blur(14px)',
          borderLeft: '1px solid var(--ui-stroke-secondary)', boxShadow: '-24px 0 48px -24px rgb(0 0 0 / 0.55)'
        },
        children: [
          jsxs('div', {
            className: 'flex items-center',
            style: { gap: 10 },
            children: [
              ...head,
              jsx('span', { className: 'flex-1' }),
              jsx('button', {
                type: 'button', 'aria-label': 'Close', title: 'Close (Esc)', onClick: onClose,
                className: 'shrink-0 hover:bg-(--chrome-action-hover)',
                style: { width: 28, height: 28, display: 'grid', placeItems: 'center', marginRight: -4, color: DIM },
                children: jsx('svg', { width: 14, height: 14, viewBox: '0 0 14 14', stroke: 'currentColor', strokeWidth: 1.6, strokeLinecap: 'round', children: jsx('path', { d: 'M2 2l10 10M12 2L2 12' }) })
              })
            ]
          }),
          ...children
        ]
      })
    ]
  })
}

// Clickable card props: a click or Enter/Space opens it.
const opens = onOpen => ({
  role: 'button', tabIndex: 0, style: { cursor: 'pointer' },
  onClick: () => { haptic('tap'); onOpen() },
  onKeyDown: e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); onOpen() } }
})

function Sparkline({ values, color = TONE.info }) {
  if (!values || values.length < 2) return null
  const max = Math.max(1, ...values)
  const pts = values.map((v, i) => [(i / (values.length - 1)) * 100, 22 - (v / max) * 20])
  const line = pts.map(([x, y]) => `${x.toFixed(1)},${y.toFixed(1)}`).join(' ')
  const [lx, ly] = pts.at(-1)
  return jsxs('svg', {
    viewBox: '0 0 100 24', preserveAspectRatio: 'none', 'aria-hidden': true,
    style: { width: '100%', height: '1.75rem', display: 'block', overflow: 'visible' },
    children: [
      jsx('polygon', { points: `0,24 ${line} 100,24`, fill: color, opacity: 0.15 }),
      jsx('polyline', { points: line, fill: 'none', stroke: color, strokeWidth: 1.5, vectorEffect: 'non-scaling-stroke' }),
      jsx('circle', { cx: lx, cy: ly, r: 1.6, fill: color })
    ]
  })
}

function StatCard({ label, value, unit, sub, tone, spark, sparkTone }) {
  return jsxs('div', {
    className: 'flex flex-col gap-1 rounded-md border border-(--ui-stroke-secondary) px-3 py-2',
    children: [
      jsx('div', { className: 'text-[0.6875rem] text-(--ui-text-quaternary)', children: label }),
      jsxs('div', {
        className: 'flex items-baseline gap-1',
        children: [
          jsx('span', { className: 'text-2xl font-semibold tabular-nums leading-tight', children: String(value) }),
          unit ? jsx('span', { className: 'text-xs text-(--ui-text-quaternary)', children: unit }) : null
        ]
      }),
      jsx('div', { className: 'truncate text-[0.6875rem]', style: { color: tone ? TONE[tone] : 'var(--ui-text-quaternary)' }, title: sub, children: sub || ' ' }),
      spark ? jsx(Sparkline, { values: spark, color: TONE[sparkTone] || TONE.info }) : null
    ]
  })
}

// --- sections -------------------------------------------------------------------------

function Vitals({ v, hours, today = [], hosts }) {
  const p95 = v.p95_s == null ? null : v.p95_s < 60 ? [v.p95_s.toFixed(1), 's'] : [fmtDur(v.p95_s), '']
  const stale = (hosts?.hosts || []).filter(h => h.stale).map(h => h.host)
  const poolOnly = hosts?.pool_only || []
  const todayReq = today.reduce((a, h) => a + h.requests, 0)
  const todayErr = today.reduce((a, h) => a + h.errors, 0)
  return jsx('div', {
    // inline grid template: the desktop app's CSS only has the utility classes its own code uses
    className: 'grid gap-2',
    style: { gridTemplateColumns: 'repeat(auto-fit, minmax(10rem, 1fr))' },
    children: [
      jsx(StatCard, { key: 'r', label: `Requests, last ${hours}h`, value: v.requests.toLocaleString(), sub: `${todayReq.toLocaleString()} today, by hour below`, spark: today.map(h => h.requests) }),
      jsx(StatCard, {
        key: 'e', label: 'Errors', value: v.errors.toLocaleString(), tone: v.errors ? 'bad' : undefined,
        sub: v.requests ? `${((v.errors / v.requests) * 100).toFixed(1)}% of requests · ${todayErr} today` : `${todayErr} today`,
        spark: today.map(h => h.errors), sparkTone: 'bad'
      }),
      jsx(StatCard, { key: 'p', label: 'p95 latency', value: p95 ? p95[0] : '-', unit: p95?.[1], sub: `model calls, last ${hours}h` }),
      jsx(StatCard, {
        key: 'h', label: 'Hosts reporting',
        // the total counts pool-only devices too; amber only when a host that sends stats went quiet
        value: `${v.hosts_reporting} of ${v.hosts_total + poolOnly.length}`,
        tone: stale.length ? 'warn' : undefined,
        sub: stale.length ? `${stale.join(', ')} quiet` : poolOnly.length ? `${poolOnly.length} pool only, no host stats` : 'all sending stats'
      }),
      jsx(StatCard, { key: 't', label: 'Open tasks', value: v.open_tasks, sub: 'on the kanban board' })
    ]
  })
}

// Today from midnight to midnight as lanes: requests per hour, errors, task outcomes and changes,
// with a line at now and the rest of the day hatched. Hovering a mark says what it is; clicking it
// opens its details with links to where it shows.
function TodayStrip({ hours, changes = [], incidents = [] }) {
  const [hover, setHover] = useState(null)
  const [open, setOpen] = useState(null)
  if (!hours.length) return null
  const day0 = hours[0].ts
  const now = Date.now() / 1000
  const at = ts => `${Math.min(100, Math.max(0, ((ts - day0) / 86400) * 100))}%`
  const maxReq = Math.max(1, ...hours.map(h => h.requests))
  const maxErr = Math.max(1, ...hours.map(h => h.errors))
  const slot = h => ({ position: 'absolute', left: at(h.ts), width: `calc(${100 / 24}% - 2px)` })
  const today = changes.filter(c => c.ts >= day0)
  const what = c => `${c.kind === 'merged' ? 'Merged' : 'Model'}: ${c.text}`
  const hourOf = h => ({
    head: `${fmtClock(h.ts)} to ${fmtClock(h.ts + 3600)}`, tone: h.errors ? TONE.bad : TONE.info,
    title: `${plural(h.requests, 'request')}, ${plural(h.errors, 'error')}`,
    lines: h.tasks_done || h.tasks_failed ? [`${h.tasks_done} tasks done, ${h.tasks_failed} failed`] : [],
    changes: today.filter(c => c.ts >= h.ts && c.ts < h.ts + 3600),
    errorsFrom: h.errors ? h.ts : null,
    // the map, Flow, Trace and Handoffs over just this hour; the map as the hour ended (or now)
    go: [['See the map at this hour', 'topology', null, { at: Math.min(h.ts + 3600, now), start: h.ts }], ['See the traffic in Flow', 'flow', null, { hours: 1, start: h.ts }],
         ['See the sessions in Trace', 'trace', null, { hours: 1, start: h.ts }], ...(h.tasks_done || h.tasks_failed ? [['See the tasks in Handoffs', 'handoffs', null, { hours: 1, start: h.ts }]] : [])]
  })
  // a host or a gateway is selected on the map
  const nodeOf = it => (it.source === 'host' ? `h:${it.key.slice('host:'.length)}` : it.source === 'gateway' && it.host ? `g:${it.host}:${it.key.slice('gateway:'.length, it.key.lastIndexOf(':'))}` : null)
  const incidentOf = (it, node = nodeOf(it)) => ({
    head: `since ${fmtClock(it.ts)}`, tone: it.sev === 'crit' ? TONE.bad : TONE.warn, title: it.text,
    lines: [`ongoing for ${fmtDur(now - it.ts)}`, ...(it.action ? [`${it.source === 'gateway' ? 'Error' : 'What to do'}: ${it.action}`] : []), `from ${it.source}`],
    go: [...(node ? [[`Show the ${it.source} on the map`, 'topology', node]] : []), ['See the map when it started', 'topology', node, { at: it.ts }]]
  })
  const changeOf = c => ({
    head: fmtClock(c.ts), tone: c.kind === 'merged' ? TONE.info : TONE.purple, title: what(c),
    lines: [c.by ? `${c.where}, merged by ${c.by}` : c.kind === 'merged' ? c.where : `on ${c.where}`],
    go: c.kind === 'merged' ? [['See merge requests', 'mrs']] : [['See the map at that time', 'topology', `s:${c.where}`, { at: c.ts }]],
    url: c.url
  })
  // a mark is a button: hover or focus shows its tip, placed on the window since the section clips, and a click opens its drawer
  const show = (e, item) => {
    const r = e.currentTarget.getBoundingClientRect()
    setHover({ x: (100 * (r.left + r.width / 2)) / window.innerWidth, top: r.bottom + 6, item })
  }
  const mark = (key, item, style, child) => jsx('button', {
    key, type: 'button', 'aria-label': `${item.head}: ${item.title}`,
    onMouseEnter: e => show(e, item), onMouseLeave: () => setHover(null), onFocus: e => show(e, item), onBlur: () => setHover(null),
    onClick: () => { setHover(null); setOpen(item) },
    style: { padding: 0, border: 0, backgroundColor: 'transparent', cursor: 'pointer', ...style }, children: child
  })
  const lane = (label, height, children) => [
    jsx('span', { key: `${label}-l`, className: 'self-center text-[0.6875rem] text-(--ui-text-quaternary)', style: { fontFamily: MONO }, children: label }),
    jsx('div', { key: `${label}-t`, className: 'relative', style: { height }, children })
  ]
  const ticks = Array.from({ length: 13 }, (_, i) => i * 2)
  const small = (ts, color, round) => ({ style: { position: 'absolute', left: `calc(${at(ts)} - 7px)`, top: -1, width: 14, height: 14 },
                                         child: jsx('div', { style: { margin: 3, width: 8, height: 8, backgroundColor: color, ...(round ? { borderRadius: 4 } : { transform: 'rotate(45deg)' }) } }) })
  return jsxs('div', {
    className: 'relative',
    children: [
      jsx('div', {
        className: 'grid items-stretch gap-y-1.5',
        style: { gridTemplateColumns: '4.5rem 1fr' },
        children: [
          // the whole hour is the target, so a short bar is as easy to hover as a tall one
          ...lane('requests', '2.25rem', hours.map(h => mark(h.ts, hourOf(h), { ...slot(h), top: 0, bottom: 0 },
            jsx('div', { style: { position: 'absolute', left: 0, right: 0, bottom: 0, height: `${Math.max(h.requests ? 6 : 0, (h.requests / maxReq) * 100)}%`, backgroundColor: TONE.info, opacity: 0.75, borderRadius: '2px 2px 0 0' } })))),
          ...lane('incidents', `${Math.max(1, incidents.length) * 0.5}rem`, incidents.map((it, i) => mark(it.key || i, incidentOf(it), {
            position: 'absolute', left: at(Math.max(it.ts, day0)), right: `calc(100% - ${at(now)})`, minWidth: 4, top: `${i * 0.5}rem`, height: '0.375rem', borderRadius: 1, backgroundColor: it.sev === 'crit' ? TONE.bad : TONE.warn
          }))),
          ...lane('errors', '0.5rem', hours.filter(h => h.errors).map(h => mark(h.ts, hourOf(h), { ...slot(h), top: 0, bottom: 0, backgroundColor: TONE.bad, opacity: 0.35 + 0.65 * (h.errors / maxErr), borderRadius: 2 }))),
          ...lane('tasks', '0.75rem', hours.filter(h => h.tasks_done || h.tasks_failed).map(h => {
            const m = small(h.ts + 1800, h.tasks_failed ? TONE.bad : TONE.good, true)
            return mark(h.ts, hourOf(h), m.style, m.child)
          })),
          ...lane('changes', '0.75rem', today.map((c, i) => {
            const m = small(c.ts, c.kind === 'merged' ? TONE.info : TONE.purple)
            return mark(i, changeOf(c), m.style, m.child)
          })),
          jsx('span', { key: 'axis-l' }),
          jsx('div', {
            key: 'axis', className: 'relative h-4 text-[0.625rem] text-(--ui-text-quaternary)', style: { fontFamily: MONO },
            children: ticks.map(t => jsx('span', { key: t, style: { position: 'absolute', left: `${(t / 24) * 100}%`, transform: t === 0 ? undefined : t === 24 ? 'translateX(-100%)' : 'translateX(-50%)' }, children: String(t).padStart(2, '0') }))
          })
        ]
      }),
      // now line and the rest of the day, over the lanes only
      jsxs('div', {
        className: 'pointer-events-none absolute top-0',
        style: { left: '4.5rem', right: 0, bottom: '1.25rem' },
        children: [
          jsx('div', { style: { position: 'absolute', left: at(now), right: 0, top: 0, bottom: 0, backgroundImage: 'repeating-linear-gradient(135deg, color-mix(in srgb, var(--ui-stroke-secondary) 45%, transparent) 0 1px, transparent 1px 7px)' } }),
          jsx('div', { style: { position: 'absolute', left: at(now), top: -4, bottom: 0, width: 1, backgroundColor: 'var(--ui-text-quaternary)' } }),
          jsx('span', { className: 'text-[0.625rem] font-semibold', style: { position: 'absolute', left: `calc(${at(now)} + 4px)`, top: -6, fontFamily: MONO }, children: fmtClock(now) })
        ]
      }),
      hover ? jsx('div', {
        style: { position: 'fixed', left: 0, right: 0, top: 0, height: 0, zIndex: 50 },
        children: hoverTip(hover.x, hover.top, [
          jsx('div', { key: 'h', style: { fontFamily: MONO, color: DIM }, children: hover.item.head }),
          jsx('div', { key: 't', style: { color: hover.item.tone, fontWeight: 500 }, children: hover.item.title }),
          // the drawer has the rest
          ...hover.item.lines.slice(0, 3).map((l, i) => jsx('div', { key: i, children: l })),
          hover.item.lines.length > 3 ? jsx('div', { key: 'more', children: `and ${hover.item.lines.length - 3} more` }) : null,
          hover.item.changes?.length ? jsx('div', { key: 'ch', children: plural(hover.item.changes.length, 'change') }) : null,
          jsx('div', { key: 'c', style: { color: DIM }, children: 'click for details' })
        ])
      }) : null,
      open ? jsx(StripDrawer, { item: open, onClose: () => setOpen(null) }) : null
    ]
  })
}

function StripDrawer({ item, onClose }) {
  return jsx(Drawer, {
    label: item.title,
    onClose,
    head: [
      jsx('i', { key: 'k', style: { width: 10, height: 10, flex: 'none', backgroundColor: item.tone } }),
      jsx('span', { key: 't', style: { fontFamily: MONO, fontSize: 12, color: DIM }, children: item.head })
    ],
    children: [
      drawerTitle(item.title),
      item.errorsFrom != null ? jsx(HourErrors, { key: 'e', start: item.errorsFrom }) : null,
      item.lines.length ? drawerPart('d', 'Details', jsx('div', { className: 'grid gap-1', children: item.lines.map((l, i) => jsx('div', { key: i, style: { fontSize: 13.5, overflowWrap: 'anywhere' }, children: l })) })) : null,
      item.changes?.length ? drawerPart('c', `Changes this hour (${item.changes.length})`, jsx('div', {
        className: 'divide-y divide-(--ui-stroke-secondary)',
        children: item.changes.map((c, i) => jsxs('div', { key: i, className: 'grid gap-0.5 py-1.5', children: [
          jsxs('div', { className: 'flex items-center gap-2 text-[0.6875rem] text-(--ui-text-quaternary)', children: [
            jsx(KindBox, { children: c.kind === 'merged' ? 'MR' : 'Model' }),
            jsx('span', { className: 'tabular-nums', style: { fontFamily: MONO }, children: fmtClock(c.ts) }),
            c.url ? jsx('a', { href: c.url, onClick: openOut, className: 'ml-auto text-(--ui-accent) hover:underline', children: 'Open merge request' }) : null
          ] }),
          jsx('div', { style: { fontSize: 13.5, overflowWrap: 'anywhere' }, children: c.text })
        ] }))
      })) : null,
      jsxs('div', { key: 'go', className: 'flex flex-wrap gap-2', children: [
        item.url ? jsx('a', { key: 'url', href: item.url, onClick: openOut, className: 'hover:bg-(--chrome-action-hover)', style: BTN, children: 'Open merge request' }) : null,
        ...item.go.map(([label, tab, sel, when]) => jsx(GoLink, { key: label, btn: true, tab, sel, ...when, children: label }))
      ] })
    ]
  })
}

// The hour's failed calls grouped by what failed and where, each linking to that place over the hour.
function HourErrors({ start }) {
  const { data, isLoading, isError } = useErrors(1, start)
  const groups = data?.groups || []
  const total = groups.reduce((n, g) => n + g.count, 0)
  return drawerPart('e', isLoading ? 'Errors this hour' : `Errors this hour (${total})`,
    isError || data?.errors?.errors ? drawerText('x', `Could not load them: ${data?.errors?.errors || 'the backend did not respond'}.`)
    : isLoading ? jsx(Skeleton, { className: 'h-16 w-full' })
    : jsx('div', { className: 'divide-y divide-(--ui-stroke-secondary)', children: errorRows(groups, { hours: 1, start, flow: true }) }))
}

// Failed calls, one row per what failed and where, linking to that place over the window
// (from start, else the last hours); flow adds the model's traffic, for a list shown off Flow.
function errorRows(groups, { hours, start, flow }) {
  const at = start != null ? Math.min(start + hours * 3600, Date.now() / 1000) : undefined
  return groups.map((g, i) => jsxs('div', { key: i, className: 'grid gap-1 py-2', children: [
    jsxs('div', { className: 'flex flex-wrap items-center gap-2 text-[0.6875rem] text-(--ui-text-quaternary)', children: [
      jsx(KindBox, { color: TONE.bad, children: plural(g.count, 'error') }),
      jsx('span', { className: 'font-medium text-(--ui-text-primary)', children: `${g.platform ? `${g.platform} calls` : 'router'} on ${g.host || 'unknown host'}` }),
      jsx('span', { className: 'ml-auto tabular-nums', style: { fontFamily: MONO }, children: g.first === g.last ? fmtClock(g.last) : `${fmtClock(g.first)} to ${fmtClock(g.last)}` })
    ] }),
    jsx('div', { style: { fontFamily: MONO, fontSize: 12, color: TONE.bad, overflowWrap: 'anywhere' }, children: g.error || `${g.event} failed` }),
    jsx('div', { style: { fontSize: 12.5, color: DIM }, children: [
      g.profile && `profile ${g.profile}`, g.model && `model ${g.model}`, g.served && `served by ${g.served}`,
      g.sessions.length ? plural(g.sessions.length, 'session') : 'no session'].filter(Boolean).join(' · ') }),
    jsxs('div', { className: 'flex flex-wrap gap-3', children: [
      g.sessions.length ? jsx(GoLink, { key: 't', tab: 'trace', sel: g.sessions[0], hours, start,
        children: g.sessions.length > 1 ? `Open the newest of ${g.sessions.length} sessions in Trace` : 'Open the session in Trace' }) : null,
      // only a client's failed calls (with a platform) are counted in Flow
      flow && g.platform && g.model ? jsx(GoLink, { key: 'f', tab: 'flow', sel: `model:${g.model}`, hours, start, children: 'Show the model in Flow' }) : null,
      g.host ? jsx(GoLink, { key: 'm', tab: 'topology', sel: g.platform && g.profile ? `p:${g.host}:${g.profile}` : `h:${g.host}`, at, start,
        note: `${plural(g.count, 'error')}, ${g.platform ? `${g.platform} calls` : 'router'} on ${g.host}: ${g.error || `${g.event} failed`}`,
        children: g.platform && g.profile ? `Show ${g.profile} on ${g.host} on the map` : 'Show the host on the map' }) : null
    ] })
  ] }))
}

// What the tag on an attention card says: the state, in the source's own terms.
function attentionTag(it) {
  if (it.source === 'gateway') return it.sev === 'crit' ? ['bad', 'down'] : ['warn', 'degraded']
  if (it.source === 'host') return ['warn', 'quiet']
  if (it.source === 'kanban') return it.card ? ['orange', 'needs you'] : ['bad', 'failing']
  return it.sev === 'crit' ? ['bad', 'critical'] : it.sev === 'warn' ? ['warn', 'warning'] : ['muted', 'notice']
}

// A health finding names a session by the start of its id ("session 20261006_101700_96faea… at 86k tok").
const findingSession = it => it.source?.startsWith('health') && it.ts ? /\bsession (\S{8,}?)(?:…|\s|$)/.exec(it.text)?.[1] : null

const useFindSession = it => {
  const prefix = findingSession(it)
  return useQuery({ queryKey: [ID, 'find-session', prefix, it.ts], enabled: !!prefix, staleTime: Infinity,
                    queryFn: () => api.rest(`/find-session?prefix=${encodeURIComponent(prefix)}&before=${it.ts}`) })
}

// The finding's session, found by that id, opened in Trace on the hours it ran.
function SessionLink({ it }) {
  const s = useFindSession(it).data?.session
  if (!s) return null
  const start = Math.floor(s.start / 3600) * 3600
  return jsx(GoLink, { tab: 'trace', sel: s.session_id, start, hours: Math.min(168, Math.max(1, Math.ceil((s.end - start) / 3600))), children: 'Open the session in Trace' })
}

// Where an attention item can be seen in Operations: its card in Handoffs, its host or gateway on the map, or its session in Trace.
const attentionLink = it => it.card
  ? jsx(GoLink, { tab: 'handoffs', sel: it.card, children: 'Open the card in Handoffs' })
  : it.source === 'gateway' || it.source === 'host'
    // a gateway item names only its platform, so the map opens without a selection
    ? jsx(GoLink, { tab: 'topology', sel: it.source === 'host' ? `h:${it.key.slice('host:'.length)}` : null, children: 'Show on the map' })
    : findingSession(it) ? jsx(SessionLink, { it }) : null

function Attention({ items }) {
  const [fixing, setFixing] = useState(null)
  if (!items.length) return muted('Nothing needs attention.')
  return jsx('div', {
    className: 'grid gap-2',
    style: { gridTemplateColumns: 'repeat(auto-fill, minmax(16rem, 1fr))' },
    children: items.map((it, i) => {
      const [tone, label] = attentionTag(it)
      return jsxs('div', {
        key: it.key || i,
        className: 'flex flex-col gap-1 rounded-md border border-(--ui-stroke-secondary) p-3 text-xs',
        children: [
          jsxs('div', { className: 'flex items-center gap-2', children: [jsx(Tag, { tone, children: label }), jsx('span', { className: 'truncate text-[0.6875rem] text-(--ui-text-quaternary)', children: it.source })] }),
          jsx('div', { className: 'text-sm font-medium leading-snug', children: it.text }),
          it.action ? jsx('div', { className: 'leading-snug text-(--ui-text-quaternary)', title: it.action, style: { display: '-webkit-box', WebkitBoxOrient: 'vertical', WebkitLineClamp: 5, overflow: 'hidden' }, children: it.action }) : null,
          it.ts ? jsx('div', { className: 'text-[0.6875rem] text-(--ui-text-quaternary)', style: { fontFamily: MONO }, children: `since ${fmtClock(it.ts)} · ongoing for ${fmtDur(Date.now() / 1000 - it.ts)}` }) : null,
          jsxs('div', { className: 'mt-auto flex flex-wrap items-center gap-x-3', children: [
            attentionLink(it),
            // a kanban item already has its card; the rest can be handed to a chat or a card
            it.source !== 'kanban' ? jsx('button', { type: 'button', className: 'text-[0.6875rem] text-(--ui-accent) hover:underline', 'aria-expanded': fixing === it.key, onClick: () => setFixing(fixing === it.key ? null : it.key), children: fixing === it.key ? 'Hide fix options' : 'Fix this' }) : null
          ] }),
          fixing === it.key ? jsx(AlertActions, { a: it }) : null
        ]
      })
    })
  })
}

function Changes({ hours }) {
  const { data, isLoading, isError } = useChanges(hours)
  if (isError) return muted('Changes unavailable: the /api/plugins/operations backend did not respond.')
  if (isLoading || !data) return jsx(Skeleton, { className: 'h-24 w-full' })
  const errors = Object.entries(data.errors || {}).map(([k, v]) =>
    jsx('div', { key: k, className: 'text-xs text-(--ui-text-quaternary)', children: `${k === 'gitlab' ? 'Merged MRs' : 'Model changes'} unavailable: ${v}` })
  )
  const startOfToday = new Date().setHours(0, 0, 0, 0) / 1000
  return jsxs('div', {
    className: 'divide-y divide-(--ui-stroke-secondary)',
    children: [
      ...errors,
      !data.items.length ? muted('No merged MRs or model loads in this window.') : null,
      ...data.items.map((c, i) =>
        jsxs('div', {
          key: i,
          className: 'space-y-0.5 py-1.5 text-xs',
          children: [
            jsxs('div', {
              className: 'flex items-center gap-2 text-[0.6875rem] text-(--ui-text-quaternary)',
              children: [
                jsx(KindBox, { children: c.kind === 'merged' ? 'MR' : 'Model' }),
                jsx('span', { className: 'whitespace-nowrap tabular-nums', style: { fontFamily: MONO }, children: c.ts >= startOfToday ? fmtClock(c.ts) : new Date(c.ts * 1000).toLocaleDateString([], { month: 'short', day: 'numeric' }) }),
                c.url ? jsx('a', { href: c.url, onClick: openOut, className: 'ml-auto shrink-0 text-(--ui-accent) hover:underline', children: 'Open merge request' }) : null
              ]
            }),
            jsx('div', { className: 'truncate font-medium', title: c.text, children: c.text }),
            jsx('div', { className: 'truncate text-(--ui-text-quaternary)', children: c.by ? `${c.where} · merged by ${c.by}` : c.kind === 'merged' ? c.where : `on ${c.where}` })
          ]
        })
      )
    ]
  })
}

function Hosts({ hosts, pool, pool_only = [], onOpen }) {
  const models = Object.fromEntries(pool.map(p => [p.device, p.models]))
  const cards = [
    ...hosts.map(h => ({ name: h.host, device: h.device || (models[h.host] ? h.host : null), h })),
    ...pool_only.map(d => ({ name: d, device: d, h: null }))
  ]
  if (!cards.length) return muted('No host stats in the last day and no devices in the pool.')
  return jsx('div', {
    className: 'grid gap-2',
    style: { gridTemplateColumns: 'repeat(auto-fill, minmax(13rem, 1fr))' },
    children: cards.map(({ name, device, h }) => {
      const ms = (device && models[device]) || []
      const mem = h?.mem_total_gb ? h.mem_used_gb / h.mem_total_gb : null
      const color = !h ? TONE.muted : h.stale ? TONE.warn : TONE.good
      return jsxs('div', {
        key: name,
        ...opens(() => onOpen({ name, device, h, ms })),
        className: 'flex flex-col gap-1.5 rounded-md border border-(--ui-stroke-secondary) p-3 text-xs hover:bg-(--chrome-action-hover)',
        children: [
          jsxs('div', {
            className: 'flex items-center gap-2',
            children: [
              jsx('span', { style: { width: 7, height: 7, borderRadius: 4, flex: 'none', backgroundColor: color } }),
              jsx('span', { className: 'truncate text-sm font-medium', children: name }),
              jsx('span', {
                className: 'ml-auto shrink-0 text-[0.6875rem] text-(--ui-text-quaternary)', style: { fontFamily: MONO },
                children: !h ? 'pool only' : h.stale ? `quiet ${fmtAgo(h.last_seen)}` : `load ${h.cpu_load?.toFixed(1) ?? '-'}`
              })
            ]
          }),
          jsx('div', {
            className: 'truncate text-[0.6875rem] text-(--ui-text-quaternary)', style: { fontFamily: MONO },
            children: !h ? 'serving models, no host stats' : device && device !== name ? `pool device ${device}` : device ? 'in the model pool' : 'not in the model pool'
          }),
          mem != null
            ? jsx('div', {
                className: 'h-1 overflow-hidden rounded-sm', style: { backgroundColor: 'color-mix(in srgb, var(--ui-stroke-secondary) 70%, transparent)' },
                title: `memory ${Math.round(mem * 100)}% used`,
                children: jsx('div', { style: { height: '100%', width: `${Math.min(100, mem * 100)}%`, backgroundColor: mem > 0.85 ? TONE.warn : TONE.info } })
              })
            : null,
          jsxs('div', {
            className: 'grid gap-x-2 gap-y-0.5', style: { gridTemplateColumns: 'auto 1fr' },
            children: [
              ...(h ? [
                jsx('span', { key: 'mk', className: 'text-(--ui-text-quaternary)', children: 'Memory' }),
                jsx('span', { key: 'mv', className: 'tabular-nums', children: `${h.mem_used_gb?.toFixed(0) ?? '-'} / ${h.mem_total_gb?.toFixed(0) ?? '-'} GB` })
              ] : []),
              jsx('span', { key: 'ok', className: 'text-(--ui-text-quaternary)', children: 'Models' }),
              jsx('span', {
                key: 'ov', style: { overflowWrap: 'anywhere' },
                className: ms.length ? undefined : 'text-(--ui-text-quaternary)',
                children: ms.length ? ms.map(m => (m.count > 1 ? `${m.name} x${m.count}` : m.name)).join(', ') : device ? 'none loaded' : '-'
              })
            ]
          })
        ]
      })
    })
  })
}

const activityTone = kind => (kind === 'error' || /crashed|blocked|timed out|gave up|fatal/.test(kind) ? 'bad' : /completed/.test(kind) ? 'good' : /created/.test(kind) ? 'orange' : 'info')

function Activity({ items, onOpen }) {
  const [kind, setKind] = useState('all')
  if (!items.length) return muted('No errors or task events in this window.')
  const counts = {}
  for (const a of items) counts[a.kind] = (counts[a.kind] || 0) + 1
  const shown = kind === 'all' || !counts[kind] ? items : items.filter(a => a.kind === kind)
  const chip = (k, label, tone) =>
    jsxs('button', {
      type: 'button', key: k,
      'aria-pressed': kind === k,
      onClick: () => { haptic('tap'); setKind(k) },
      className: cn('flex items-center gap-1.5 rounded border px-2 py-0.5 text-[0.6875rem] transition-colors',
        kind === k ? 'border-(--ui-accent) text-(--ui-accent)' : 'border-(--ui-stroke-secondary) text-(--ui-text-quaternary) hover:bg-(--chrome-action-hover)'),
      children: [tone ? jsx('span', { style: { width: 6, height: 6, borderRadius: 3, backgroundColor: TONE[tone] } }) : null, label]
    })
  return jsxs('div', {
    children: [
      jsx('div', {
        className: 'mb-1 flex flex-wrap gap-1',
        children: [chip('all', `All ${items.length}`), ...Object.entries(counts).map(([k, n]) => chip(k, `${k} ${n}`, activityTone(k)))]
      }),
      jsx('div', {
        className: 'divide-y divide-(--ui-stroke-secondary)',
        children: shown.map((a, i) =>
          jsxs('div', {
            key: i,
            ...opens(() => onOpen(a)),
            className: 'grid gap-x-2 px-1 py-1.5 text-xs hover:bg-(--chrome-action-hover)',
            style: { gridTemplateColumns: '4.25rem 1fr', cursor: 'pointer' },
            children: [
              jsx('span', { className: 'whitespace-nowrap tabular-nums text-(--ui-text-quaternary)', style: { fontFamily: MONO, fontSize: '0.6875rem', lineHeight: '1.1rem' }, children: fmtClock(a.ts) }),
              jsxs('div', {
                className: 'min-w-0 space-y-0.5',
                children: [
                  jsxs('div', {
                    className: 'flex items-center gap-2',
                    children: [
                      jsx(Tag, { tone: activityTone(a.kind), children: a.kind }),
                      a.where ? jsx('span', { className: 'ml-auto shrink-0 truncate text-[0.6875rem] text-(--ui-text-quaternary)', children: a.where }) : null
                    ]
                  }),
                  jsx('div', { className: 'leading-snug', style: { overflowWrap: 'anywhere' }, title: a.text, children: a.text.length > 220 ? `${a.text.slice(0, 219)}...` : a.text }),
                  a.count > 1
                    ? jsx('div', { className: 'text-[0.6875rem] text-(--ui-text-quaternary)', style: { fontFamily: MONO }, children: `${a.count} times since ${fmtClock(a.first_ts)}` })
                    : null
                ]
              })
            ]
          })
        )
      })
    ]
  })
}

// --- flow ----------------------------------------------------------------------------

const FLOW_COLS = [['entry', 'Entry points'], ['profile', 'Profiles'], ['model', 'Models'], ['served', 'Served by']]
// no red: red marks failed requests
const SERVED_COLORS = ['#58a6ff', '#3fb950', '#d29922', '#bc8cff', '#f778ba', '#39c5cf', '#f0883e', '#8b949e']
// Each device or provider keeps its color from day to day: the first time one is seen it takes the
// least used color (busiest first) and this Desktop remembers it. Past eight, colors repeat.
const SERVED_SEEN = 'flow.colors'
const servedColors = ids => {
  const seen = storeGet(SERVED_SEEN, {})
  const fresh = ids.filter(id => !(id in seen))
  for (const id of fresh) {
    const uses = SERVED_COLORS.map((_, i) => Object.values(seen).filter(x => x === i).length)
    seen[id] = uses.indexOf(Math.min(...uses))
  }
  if (fresh.length) try { api.storage.set(SERVED_SEEN, seen) } catch {}
  return Object.fromEntries(ids.map(id => [id, SERVED_COLORS[seen[id]]]))
}
const pct = (n, of) => (of ? Math.round((n / of) * 100) : 0)

function FlowChart({ nodes, paths, total, sel, onSelect, color, live }) {
  // LABEL: a small node still takes a label's height in the layout, so labels never overlap
  const NW = 10, GAP = 6, LABEL = 14
  const cols = FLOW_COLS.map(([c]) => nodes.filter(n => n.col === c))
  // the served column's labels sit right of its bars, so the model labels have the whole gap to
  // them; the bands give up the longest one's width (about 6.6px a character at 12px)
  const R = Math.max(0, ...cols[3].map(n => Math.max(`${n.label} ${n.requests}${n.errors ? ` · ${n.errors} failed` : ''}`.length,
                                                     `${n.label} ${n.requests} of ${n.requests}`.length))) * 6.6 + 8
  const W = 960 - R
  const k = 240 / total
  const slot = n => Math.max(LABEL, n.requests * k)
  const span = col => col.reduce((a, n) => a + slot(n), 0) + GAP * (col.length - 1)
  const H = Math.max(...cols.map(span))
  const pos = {}
  cols.forEach((col, c) => {
    let y = (H - span(col)) / 2
    col.forEach(n => {
      const h = n.requests * k
      pos[n.id] = { n, x: (c * (W - NW)) / 3, y: y + (slot(n) - h) / 2, h, out: 0, in: 0 }
      y += slot(n) + GAP
    })
  })
  // ribbons between neighbouring columns, summed over the paths that cross them
  const byKey = {}
  for (const p of paths)
    for (let j = 0; j < 3; j++) {
      const key = `${p.ids[j]}\n${p.ids[j + 1]}`
      const l = (byKey[key] ||= { a: p.ids[j], b: p.ids[j + 1], n: 0, s: 0, by: {} })
      l.n += p.requests
      if (sel && p.ids.includes(sel)) { l.s += p.requests; l.by[p.ids[3]] = (l.by[p.ids[3]] || 0) + p.requests }
    }
  // with a selection: each node's requests that also went through it
  const thru = {}
  if (sel) for (const p of paths) if (p.ids.includes(sel)) for (const id of p.ids) thru[id] = (thru[id] || 0) + p.requests
  const links = Object.values(byKey)
  ;[...links].sort((p, q) => pos[p.a].y - pos[q.a].y || pos[p.b].y - pos[q.b].y).forEach(l => { l.y0 = pos[l.a].y + pos[l.a].out; pos[l.a].out += l.n * k })
  ;[...links].sort((p, q) => pos[p.b].y - pos[q.b].y || pos[p.a].y - pos[q.a].y).forEach(l => { l.y1 = pos[l.b].y + pos[l.b].in; pos[l.b].in += l.n * k })
  const f = v => v.toFixed(1)
  return jsx('div', {
    className: 'overflow-x-auto',
    ref: dragScroll,
    children: jsxs('svg', {
      viewBox: `-2 -2 ${W + R + 4} ${H + 4}`,
      role: 'img',
      'aria-label': 'Request flow',
      style: { width: '100%', minWidth: '36rem', height: 'auto', display: 'block' },
      children: [
        live ? jsx('style', { key: 'motion', children: MOTION_CSS }) : null,
        ...links.flatMap(l => {
          const x0 = pos[l.a].x + NW, x1 = pos[l.b].x, m = (x0 + x1) / 2
          const band = (h, o = 0) => { const y0 = l.y0 + o, y1 = l.y1 + o; return `M${f(x0)} ${f(y0)}C${f(m)} ${f(y0)} ${f(m)} ${f(y1)} ${f(x1)} ${f(y1)}L${f(x1)} ${f(y1 + h)}C${f(m)} ${f(y1 + h)} ${f(m)} ${f(y0 + h)} ${f(x0)} ${f(y0 + h)}Z` }
          const tip = `${pos[l.a].n.label} to ${pos[l.b].n.label}: ${l.n} requests${sel ? `, ${l.s} through ${pos[sel].n.label}` : ''}`
          // the selection's share is drawn over the band, along its top edge, in the color of what served it
          let o = 0
          return [
            jsx('path', { key: `${l.a}>${l.b}`, fill: sel ? 'currentColor' : 'var(--ui-accent)', opacity: sel ? 0.05 : 0.22, d: band(l.n * k), children: jsx('title', { children: tip }) }),
            // requests running along the band's middle, left to right
            live && !sel ? jsx('path', { key: `${l.a}>${l.b}:flow`, className: 'ops-flow', fill: 'none', stroke: 'var(--ui-accent)', strokeWidth: Math.min(3, Math.max(1, l.n * k / 3)),
                                         strokeDasharray: '2 14', strokeLinecap: 'round', opacity: 0.55, pointerEvents: 'none',
                                         d: `M${f(x0)} ${f(l.y0 + l.n * k / 2)}C${f(m)} ${f(l.y0 + l.n * k / 2)} ${f(m)} ${f(l.y1 + l.n * k / 2)} ${f(x1)} ${f(l.y1 + l.n * k / 2)}` }) : null,
            ...Object.entries(l.by).map(([id, n]) => {
              const d = band(n * k, o)
              o += n * k
              return jsx('path', { key: `${l.a}>${l.b}:${id}`, fill: color(id), opacity: 0.7, d, children: jsx('title', { children: `${tip}; ${n} served by ${pos[id].n.label}` }) })
            })
          ]
        }),
        ...Object.values(pos).map(p => {
          const last = p.n.col === 'served'
          const off = sel && !thru[p.n.id]
          // a model label sits across from the served column's: a long one drops its provider prefix, then is capped; the tooltip has it whole
          const short = p.n.label.length > 20 ? p.n.label.slice(p.n.label.lastIndexOf('/') + 1) : p.n.label
          const name = p.n.col === 'model' && short.length > 20 ? `${short.slice(0, 19)}…` : p.n.col === 'model' ? short : p.n.label
          return jsxs('g', {
            key: p.n.id,
            role: 'button',
            tabIndex: 0,
            'aria-label': `${p.n.label}, ${p.n.requests} requests`,
            style: { cursor: 'pointer', outline: 'none' },
            onClick: () => { haptic('tap'); onSelect(p.n.id === sel ? null : p.n.id) },
            onKeyDown: e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); onSelect(p.n.id === sel ? null : p.n.id) } },
            children: [
              jsx('rect', {
                x: p.x, y: f(p.y), width: NW, height: f(Math.max(2, p.h)),
                fill: last ? color(p.n.id) : 'currentColor',
                opacity: off ? 0.15 : last || p.n.id === sel ? 1 : 0.55,
                stroke: p.n.id === sel ? 'var(--ui-accent)' : 'none',
                strokeWidth: 3
              }),
              // failed requests as a red share at the foot of the bar
              p.n.errors
                ? jsx('rect', { x: p.x, y: f(p.y + p.h - Math.max(2, p.n.errors * k)), width: NW, height: f(Math.max(2, p.n.errors * k)), fill: TONE.bad })
                : null,
              jsx('text', {
                x: p.x + NW + 6,
                y: f(p.y + Math.max(2, p.h) / 2 + 4),
                fill: p.n.id === sel ? 'var(--ui-accent)' : 'currentColor',
                // a halo in the page's color keeps the label readable where it crosses a band
                stroke: 'var(--ui-bg, #0d1117)', strokeWidth: 3, strokeLinejoin: 'round', paintOrder: 'stroke',
                fontSize: 12,
                fontWeight: p.n.id === sel ? 600 : 400,
                opacity: off ? 0.3 : 1,
                children: sel && !off && thru[p.n.id] < p.n.requests
                  ? [`${name} ${thru[p.n.id]}`, jsx('tspan', { key: 'of', opacity: 0.5, children: ` of ${p.n.requests}` })]
                  : [`${name} ${p.n.requests}`, p.n.errors ? jsx('tspan', { key: 'e', fill: TONE.bad, children: ` · ${p.n.errors} failed` }) : null]
              }),
              jsx('title', { children: `${p.n.label}: ${p.n.requests} requests, ${p.n.errors} errors, p95 ${fmtDur(p.n.p95_s)}` })
            ]
          })
        })
      ]
    })
  })
}

function Breakdown({ title, items, of, onSelect }) {
  return jsxs('div', {
    className: 'space-y-1',
    children: [
      jsx('div', { className: 'text-[0.6875rem] font-medium text-(--ui-text-quaternary)', children: title }),
      ...items.map(it =>
        jsxs('button', {
          type: 'button',
          key: it.id,
          onClick: () => { haptic('tap'); onSelect(it.id) },
          className: 'block w-full rounded px-1 py-0.5 text-left text-xs hover:bg-(--chrome-action-hover)',
          children: [
            jsxs('div', {
              className: 'flex items-center gap-2',
              children: [
                jsx('span', { className: 'min-w-0 flex-1 truncate', title: it.label, children: it.label }),
                jsx('span', { className: 'shrink-0 tabular-nums text-(--ui-text-quaternary)', children: `${it.n} · ${pct(it.n, of)}%` })
              ]
            }),
            jsx('div', {
              style: { height: 3, marginTop: 2, backgroundColor: 'color-mix(in srgb, var(--ui-stroke-secondary) 60%, transparent)' },
              children: jsx('div', { style: { height: '100%', width: `${pct(it.n, of)}%`, backgroundColor: it.color || 'var(--ui-accent)' } })
            })
          ]
        })
      )
    ]
  })
}

function FlowDetail({ nodes, paths, total, sel, onSelect, color, hours, start }) {
  const byId = Object.fromEntries(nodes.map(n => [n.id, n]))
  const node = sel && byId[sel]
  if (!node)
    return jsxs('div', {
      className: 'space-y-3',
      children: [
        jsx('div', { className: 'text-xs', children: `${total} requests. Select any bar to see where its requests came from and went.` }),
        jsx(Breakdown, {
          title: 'Served by',
          of: total,
          onSelect,
          items: nodes.filter(n => n.col === 'served').map(n => ({ id: n.id, label: n.label, n: n.requests, color: color(n.id) }))
        })
      ]
    })
  const through = paths.filter(p => p.ids.includes(sel))
  const colName = Object.fromEntries(FLOW_COLS)
  return jsxs('div', {
    className: 'space-y-3',
    children: [
      jsxs('div', {
        className: 'space-y-0.5 text-xs',
        children: [
          jsxs('div', {
            className: 'flex items-center gap-2',
            children: [
              node.col === 'served' ? jsx('span', { style: { width: 8, height: 8, flex: 'none', backgroundColor: color(node.id) } }) : null,
              jsx('span', { className: 'font-semibold', style: { color: 'var(--ui-accent)' }, children: node.label }),
              node.col === 'served' ? jsx(KindBox, { children: node.pool_device ? 'pool device' : 'provider' }) : null
            ]
          }),
          jsxs('div', { className: 'text-(--ui-text-quaternary)', children: [
            `${node.requests} requests, ${pct(node.requests, total)}% of the window · `,
            jsx('span', { style: node.errors ? { color: TONE.bad, fontWeight: 500 } : undefined, children: plural(node.errors, 'error') }),
            ` · p95 ${fmtDur(node.p95_s)}`
          ] }),
          node.col === 'served' ? jsx(GoLink, { tab: 'topology', sel: `s:${node.id.slice('served:'.length)}`, hours, children: 'Show on the map' }) : null
        ]
      }),
      node.errors ? jsx(FlowErrors, { node, hours, start }) : null,
      // every request took one route: name it in a line instead of lists that each read 100%
      through.length === 1
        ? jsxs('div', {
            className: 'text-xs leading-relaxed',
            children: [
              muted('Every request took one route:'),
              jsx('div', {
                children: through[0].ids.flatMap((id, i) => [
                  i ? jsx('span', { key: `t${i}`, className: 'text-(--ui-text-quaternary)', children: ', then ' }) : null,
                  id === sel
                    ? jsx('span', { key: id, className: 'font-medium', children: byId[id]?.label ?? id })
                    : jsx('button', { key: id, type: 'button', className: 'text-(--ui-accent) hover:underline', style: byId[id]?.col === 'served' ? { color: color(id) } : undefined, onClick: () => { haptic('tap'); onSelect(id) }, children: byId[id]?.label ?? id })
                ])
              })
            ]
          })
        : null,
      ...(through.length === 1 ? [] : FLOW_COLS.filter(([c]) => c !== node.col).map(([c, title]) => {
        const j = FLOW_COLS.findIndex(([x]) => x === c)
        const sums = {}
        for (const p of through) sums[p.ids[j]] = (sums[p.ids[j]] || 0) + p.requests
        const items = Object.entries(sums).sort((a, b) => b[1] - a[1])
          .map(([id, n]) => ({ id, label: byId[id]?.label ?? id, n, color: c === 'served' ? color(id) : undefined }))
        const before = j < FLOW_COLS.findIndex(([x]) => x === node.col)
        return jsx(Breakdown, { key: c, title: `${before ? 'From' : 'To'}: ${colName[c].toLowerCase()}`, items, of: node.requests, onSelect })
      }))
    ]
  })
}

// What failed among the selection's requests: the window's errors from a client on the same entry point, profile, model or server.
function FlowErrors({ node, hours, start }) {
  const { data, isLoading, isError } = useErrors(hours, start)
  // matched on the label Flow gives a missing value
  const value = g => ({ entry: g.platform, profile: g.profile || 'no profile', model: g.model || 'no model', served: g.served || 'unknown' })[node.col]
  // the router's own errors (no platform) are not in Flow
  const groups = (data?.groups || []).filter(g => g.platform && value(g) === node.label)
  return jsxs('div', {
    className: 'space-y-1',
    children: [
      jsx('div', { className: 'text-[0.6875rem] font-medium text-(--ui-text-quaternary)', children: 'What failed' }),
      isError || data?.errors?.errors ? muted(`Could not load the errors: ${data?.errors?.errors || 'the backend did not respond'}.`)
      : isLoading ? jsx(Skeleton, { className: 'h-16 w-full' })
      : groups.length ? jsx('div', { className: 'divide-y divide-(--ui-stroke-secondary)', children: errorRows(groups, { hours, start }) })
      : muted('These calls were recorded as failed without an error message.')
    ]
  })
}

function FlowHourly({ hourly, paths, served, sel, color }) {
  // each hour's requests (key 'paths') or failed requests ('failed') by what served them, through the selection if any
  const mine = (h, key = 'paths') => {
    const by = {}
    for (const [i, n] of Object.entries(h[key] || {})) if (!sel || paths[i]?.ids.includes(sel)) by[paths[i].ids[3]] = (by[paths[i].ids[3]] || 0) + n
    return by
  }
  const label = sel && (served.find(n => n.id === sel)?.label ?? sel.slice(sel.indexOf(':') + 1))
  const used = new Set(sel ? hourly.flatMap(h => Object.keys(mine(h))) : served.map(n => n.id))
  const anyFailed = hourly.some(h => Object.keys(mine(h, 'failed')).length)
  // a selection is often a sliver of the whole, so it is scaled to its own busiest hour and the rest is left out
  const max = Math.max(1, ...hourly.map(h => Object.values(mine(h)).reduce((a, n) => a + n, 0)))
  const wide = hourly.length > 30
  const counts = sel && !wide
  return jsxs('div', {
    className: 'space-y-1',
    children: [
      jsx('div', {
        className: 'flex items-end',
        // the count sits over its bar, in the top padding when the bar is the tallest
        style: { height: '5rem', gap: wide ? 0 : 2, paddingTop: counts ? '0.875rem' : 0, boxSizing: 'content-box' },
        children: hourly.map(h => {
          const sum = Object.values(h.by).reduce((a, n) => a + n, 0)
          const on = mine(h)
          const onSum = Object.values(on).reduce((a, n) => a + n, 0)
          const bad = mine(h, 'failed')
          const badSum = Object.values(bad).reduce((a, n) => a + n, 0)
          const tip = [`${new Date(h.ts * 1000).toLocaleString([], { weekday: wide ? 'short' : undefined, hour: '2-digit', minute: '2-digit' })}: ${sel ? `${onSum} of ${sum} requests through ${label}` : `${sum} requests`}`,
            ...served.filter(n => on[n.id]).map(n => `${n.label} ${on[n.id]}${bad[n.id] ? `, ${bad[n.id]} failed` : ''}`)].join('\n')
          return jsx('div', {
            key: h.ts,
            title: tip,
            className: 'flex h-full flex-1 flex-col justify-end',
            children: [
              counts && onSum ? jsx('div', { key: 'n', className: 'text-center text-[0.625rem] tabular-nums', style: { flex: 'none', color: DIM, lineHeight: '0.875rem' }, children: onSum }) : null,
              // the hour's failed requests in red on top; each server's share below counts only what it answered
              badSum ? jsx('div', { key: 'failed', style: { flex: 'none', height: `${(badSum / max) * 100}%`, backgroundColor: TONE.bad, opacity: 0.85 } }) : null,
              ...[...served].reverse().flatMap(n => {
                const part = (on[n.id] || 0) - (bad[n.id] || 0)
                // flex none: a full-height bar pushes its count up into the padding instead of shrinking
                return [
                  part ? jsx('div', { key: n.id, style: { flex: 'none', height: `${(part / max) * 100}%`, backgroundColor: color(n.id), opacity: 0.85 } }) : null
                ]
              })
            ]
          })
        })
      }),
      jsx('div', {
        className: 'flex text-[0.6875rem] text-(--ui-text-quaternary)',
        style: { gap: wide ? 0 : 2 },
        children: hourly.map(h => {
          const d = new Date(h.ts * 1000)
          const label = wide ? (d.getHours() === 0 ? d.toLocaleDateString([], { weekday: 'short' }) : '') : d.getHours() % 3 === 0 ? d.toLocaleTimeString([], { hour: 'numeric' }) : ''
          return jsx('div', { key: h.ts, className: 'flex-1 whitespace-nowrap', style: { minWidth: 0, overflow: 'visible' }, children: label })
        })
      }),
      jsx('div', {
        className: 'flex flex-wrap items-center gap-3 text-[0.6875rem] text-(--ui-text-quaternary)',
        children: served.map(n =>
          jsxs('span', {
            key: n.id,
            className: 'flex items-center gap-1',
            style: { opacity: used.has(n.id) ? 1 : 0.35 },
            children: [jsx('span', { style: { width: 8, height: 8, display: 'inline-block', backgroundColor: color(n.id) } }), n.label]
          })
        ).concat(anyFailed ? [jsxs('span', { key: 'failed', className: 'flex items-center gap-1', children: [
          jsx('span', { style: { width: 8, height: 8, display: 'inline-block', backgroundColor: TONE.bad } }), 'failed'] })] : [])
      })
    ]
  })
}

function FlowPage({ sel: initial, when = {}, remember }) {
  const [hours, setHours] = useState(rangeFor(when, 24))
  // set by a link to one hour (the Today strip); picking a range goes back to the last hours
  const [start, setStart] = useState(when.start ?? null)
  const pickHours = h => { setHours(h); setStart(null) }
  const [selected, setSelected] = useState(initial)
  remember?.({ sel: selected, when: { hours, start } })
  const { data, isLoading, isError } = useFlow(hours, start)
  if (isError) return jsx('div', { className: 'p-4', children: muted('Flow unavailable: the /api/plugins/operations backend did not respond.') })
  if (isLoading || !data) return jsx('div', { className: 'p-4', children: jsx(Skeleton, { className: 'h-48 w-full' }) })
  const err = data.errors || {}
  const nodes = data.nodes || []
  const served = nodes.filter(n => n.col === 'served')
  const colors = servedColors(served.map(n => n.id))
  const color = id => colors[id] || SERVED_COLORS[SERVED_COLORS.length - 1]
  const sel = nodes.some(n => n.id === selected) ? selected : null
  const args = { nodes, paths: data.paths || [], total: data.total, sel, onSelect: setSelected, color, hours, start }
  const node = sel && nodes.find(n => n.id === sel)
  const KINDS = { entry: 'entry point', profile: 'profile', model: 'model', served: 'served by' }
  const kind = node && (node.col === 'served' ? (node.pool_device ? 'pool device' : 'provider') : KINDS[node.col])
  // a link can name something with no requests in this window: say so rather than quietly show everything
  const missing = selected && !node && selected.slice(selected.indexOf(':') + 1)
  const missingKind = missing && KINDS[selected.slice(0, selected.indexOf(':'))]
  return jsxs('div', {
    className: 'space-y-3 p-4',
    children: [
      jsxs('div', {
        className: 'flex flex-wrap items-center gap-2',
        children: [
          jsx('h2', { className: 'text-sm font-semibold', children: 'Flow' }),
          jsx('span', { className: 'text-[0.6875rem] text-(--ui-text-quaternary)', children: 'requests by entry point, profile, model and what served them' }),
          jsx('span', { className: 'ml-auto' }),
          start ? jsx('span', { className: 'text-[0.6875rem] tabular-nums', style: { fontFamily: MONO, color: 'var(--ui-accent)' }, children: `${fmtClock(start)} to ${fmtClock(start + hours * 3600)}` }) : null,
          jsx(RangePicker, { hours: start ? null : hours, onChange: pickHours })
        ]
      }),
      err.metrics ? muted(`No metrics collector data (${err.metrics}): the flow reads as empty.`) : null,
      node
        ? jsxs('div', {
            className: 'flex flex-wrap items-center gap-2 rounded px-3 py-2 text-xs',
            style: { border: '1px solid color-mix(in srgb, var(--ui-accent) 50%, transparent)', backgroundColor: 'color-mix(in srgb, var(--ui-accent) 10%, transparent)' },
            children: [
              jsx('span', { children: 'Showing traffic through' }),
              jsx(KindBox, { color: 'var(--ui-accent)', children: kind }),
              jsx('span', { className: 'font-semibold', style: { color: 'var(--ui-accent)' }, children: node.label }),
              jsx('span', { className: 'tabular-nums', style: { color: DIM }, children: `${node.requests} of ${data.total} requests (${pct(node.requests, data.total)}%)` }),
              jsx('button', { type: 'button', className: 'ml-auto font-medium text-(--ui-accent) hover:underline', onClick: () => { haptic('tap'); setSelected(null) }, children: 'Show all traffic' })
            ]
          })
        : missing
          ? jsxs('div', {
              className: 'flex flex-wrap items-center gap-2 rounded px-3 py-2 text-xs',
              style: { border: `1px dashed ${CHANGE_COLOR.warn}`, backgroundColor: `color-mix(in srgb, ${CHANGE_COLOR.warn} 8%, transparent)` },
              children: [
                jsx('span', { children: 'You asked for' }),
                missingKind ? jsx(KindBox, { color: CHANGE_COLOR.warn, children: missingKind }) : null,
                jsx('span', { className: 'font-semibold', style: { color: CHANGE_COLOR.warn }, children: missing }),
                jsx('span', { style: { color: DIM }, children: `but it has no requests ${start ? `from ${fmtClock(start)} to ${fmtClock(start + hours * 3600)}` : `in the last ${hours}h`}. Showing all traffic instead.` }),
                jsx('span', { className: 'ml-auto' }),
                hours < 168 || start ? jsx('button', { type: 'button', className: 'font-medium text-(--ui-accent) hover:underline', onClick: () => { haptic('tap'); pickHours(168) }, children: 'Look back 7 days' }) : null,
                jsx('button', { type: 'button', className: 'text-(--ui-text-quaternary) hover:underline', onClick: () => { haptic('tap'); setSelected(null) }, children: 'Dismiss' })
              ]
            })
          : null,
      jsx(Section, {
        title: 'Request flow',
        count: `${node ? `${node.requests} of ${data.total} requests through ${node.label}` : `${data.total ?? 0} requests`} · ${start ? `${fmtClock(start)} to ${fmtClock(start + hours * 3600)}` : `last ${hours}h`} · band width is request count`,
        error: err.flow,
        children: data.total
          ? jsxs('div', {
              className: 'flex flex-wrap gap-4',
              children: [
                jsx('div', { style: { flex: '3 1 32rem', minWidth: 0 }, children: jsx(FlowChart, { ...args, live: start == null }) }),
                jsx('div', { style: { flex: '1 1 14rem', minWidth: 0 }, children: jsx(FlowDetail, args) })
              ]
            })
          : muted('No requests in this window.')
      }),
      data.total
        ? jsx(Section, {
            title: 'Per hour, by what served it',
            count: node ? `only requests through ${node.label}, scaled to its busiest hour` : 'pool devices and cloud providers',
            children: jsx(FlowHourly, { hourly: data.hourly, paths: args.paths, served, sel, color })
          })
        : null
    ]
  })
}

// --- activity --------------------------------------------------------------------------

const LOG_KINDS = [['all', 'All'], ['incident', 'Incidents'], ['change', 'Changes'], ['task', 'Tasks'], ['conformance', 'Host checks']]
const BAD = /error|fatal|stale|fail|crashed|blocked|timed out|gave up|rate limited/

// An Activity entry's raw lines and where it leads: its card, its host on the map, its traffic.
function EntryDetail({ e, drawer }) {
  const hosts = useOverview(24).data?.hosts
  const task = e.category === 'task'
  const card = task ? e.raw.map(([, line]) => line.split(' · ')[0]).find(id => id.startsWith('t_')) : null
  // a task's where is its assignee profile; anything else names a host or a pool device
  const h = !task && e.where ? hosts?.hosts.find(x => x.host === e.where || x.device === e.where) : null
  const device = h ? h.device : !task && hosts?.pool.some(x => x.device === e.where) ? e.where : null
  const btn = drawer
  const at = e.first_ts || e.ts
  // an error's raw line is "event · model · error": its traffic is that model's, not the host it was logged on
  const model = e.kind === 'error' ? e.raw.map(([, line]) => line.split(' · ')[1]).find(x => x && x !== 'no model') : null
  const actions = [
    e.url ? jsx('a', { key: 'u', href: e.url, onClick: openOut, className: btn ? 'hover:bg-(--chrome-action-hover)' : 'text-[0.6875rem] text-(--ui-accent) hover:underline', style: btn ? BTN : undefined, children: 'Open in GitLab' }) : null,
    card ? jsx(GoLink, { key: 'c', btn, tab: 'handoffs', sel: card, children: 'Open the card in Handoffs' }) : null,
    h || device ? jsx(GoLink, { key: 'm', btn, tab: 'topology', sel: h ? `h:${h.host}` : `s:${device}`, at, children: 'Show on the map' }) : null,
    task && e.where ? jsx(GoLink, { key: 'f', btn, tab: 'flow', sel: `profile:${e.where}`, at, children: 'See its traffic' })
      : model ? jsx(GoLink, { key: 'f', btn, tab: 'flow', sel: `model:${model}`, at, children: `See ${model} traffic` })
      : device ? jsx(GoLink, { key: 'f', btn, tab: 'flow', sel: `served:${device}`, at, children: 'See its traffic' }) : null,
    e.category === 'conformance' ? jsx(GoLink, { key: 'k', btn, tab: 'conformance', children: 'Open Conformance' }) : null
  ]
  if (drawer)
    return jsxs('div', {
      style: { display: 'grid', gap: 16 },
      children: [
        drawerPart('raw', `Raw events${e.count > e.raw.length ? ` (newest ${e.raw.length} of ${e.count})` : ''}`, e.raw.length ? rawBlock(e.raw) : drawerText('n', 'No raw lines for this entry in the Activity log.')),
        jsx('div', { key: 'go', className: 'flex flex-wrap gap-2', children: actions })
      ]
    })
  return jsxs('div', {
    className: 'space-y-0.5',
    children: [
      e.count > 1 ? muted(`${e.count} times between ${fmtClock(e.first_ts)} and ${fmtClock(e.ts)}${e.count > e.raw.length ? `; the newest ${e.raw.length} are below` : ''}.`) : null,
      ...e.raw.map(([ts, line], i) =>
        jsxs('div', {
          key: i,
          className: 'flex gap-2 text-(--ui-text-quaternary)',
          children: [
            jsx('span', { className: 'shrink-0 tabular-nums', children: new Date(ts * 1000).toLocaleTimeString() }),
            jsx('span', { className: 'min-w-0 flex-1', style: { overflowWrap: 'anywhere' }, children: line })
          ]
        })
      ),
      jsx('div', { className: 'flex flex-wrap gap-x-3 pt-0.5', children: actions })
    ]
  })
}

// A model-written line always names its model and how many events it read.
const sumNote = sum => `Summarized from ${plural(sum.events, 'event')} by ${sum.model}${sum.provider && sum.provider !== 'auto' ? ` (${sum.provider})` : ''}`

function LogEntry({ e, sum, open, onToggle }) {
  // an MR titled "Fix failing ..." is not bad news; only state text from incidents and host checks counts
  const bad = BAD.test(e.kind) || (['incident', 'conformance'].includes(e.category) && BAD.test(e.text.split('->').pop()))
  return jsxs('div', {
    className: 'text-xs',
    children: [
      jsxs('button', {
        type: 'button',
        onClick: () => { haptic('tap'); onToggle() },
        className: cn('flex w-full items-center gap-2 rounded px-1 py-0.5 text-left transition-colors', open ? 'bg-(--ui-accent)/10' : 'hover:bg-(--chrome-action-hover)'),
        children: [
          jsx('span', { className: 'shrink-0 whitespace-nowrap tabular-nums text-(--ui-text-quaternary)', style: { minWidth: '3.5rem' }, children: fmtClock(e.ts) }),
          jsx(Tag, { tone: bad ? 'bad' : e.kind === 'merged' ? 'good' : activityTone(e.kind), children: e.kind }),
          jsx('span', { className: 'min-w-0 flex-1 truncate', title: sum ? `${sumNote(sum)}. Logged as: ${e.text}` : e.text, children: sum ? sum.text : e.text }),
          sum ? jsx('span', { className: 'shrink-0 truncate text-[0.6875rem] text-(--ui-text-quaternary)', style: { maxWidth: '8rem' }, title: sumNote(sum), children: `by ${sum.model.split('/').pop()}` }) : null,
          e.count > 1 ? jsx('span', { className: 'shrink-0 whitespace-nowrap text-(--ui-text-quaternary)', children: `x${e.count}` }) : null,
          e.where ? jsx('span', { className: 'shrink-0 truncate text-(--ui-text-quaternary)', style: { maxWidth: '10rem' }, children: e.where }) : null
        ]
      }),
      open ? jsxs('div', { className: 'space-y-0.5 py-1', style: { paddingLeft: '4.5rem' }, children: [
        sum ? muted(`${sumNote(sum)}. Logged as: ${e.text}`) : null,
        jsx(EntryDetail, { e })
      ] }) : null
    ]
  })
}

function ActivityPage() {
  const [hours, setHours] = useState(24)
  // a role that watches only some kinds opens on those
  const { role } = useContext(Role)
  const partial = role && role.watch.length && role.watch.length < WATCH.length
  const [picked, setKind] = useState(null)
  const kind = (picked === 'watch' && !partial ? null : picked) || (partial ? 'watch' : 'all')
  const [open, setOpen] = useState(null)
  const log = useActivity(hours)
  const changes = useChanges(hours)
  const sums = useSummaries(hours, log.data?.summaries)
  if (log.isError) return jsx('div', { className: 'p-4', children: muted('Activity unavailable: the /api/plugins/operations backend did not respond.') })
  if (log.isLoading || !log.data) return jsx('div', { className: 'p-4', children: jsx(Skeleton, { className: 'h-48 w-full' }) })
  const changeItems = (changes.data?.items || []).map(c => ({
    ts: c.ts, first_ts: c.ts, count: 1, category: 'change', kind: c.kind, text: c.text, url: c.url,
    where: c.by ? `${c.where} · ${c.by}` : c.where, raw: [[c.ts, c.by ? `${c.text} (${c.by})` : c.text]]
  }))
  const all = [...log.data.items, ...changeItems].sort((a, b) => b.ts - a.ts)
  const watched = partial ? all.filter(e => role.watch.includes(e.category)) : []
  const counts = { ...log.data.counts, change: changeItems.length, all: all.length, watch: watched.length }
  const shown = kind === 'all' ? all : kind === 'watch' ? watched : all.filter(e => e.category === kind)
  const hoursOf = []
  for (const e of shown) {
    const h = Math.floor(e.ts / 3600) * 3600
    if (hoursOf[hoursOf.length - 1]?.ts !== h) hoursOf.push({ ts: h, items: [] })
    hoursOf[hoursOf.length - 1].items.push(e)
  }
  const keyOf = e => `${e.category}|${e.kind}|${e.text}|${e.where}|${e.ts}`
  const notes = Object.entries({ ...log.data.errors, ...(changes.data?.errors || {}) }).map(([k, v]) =>
    jsx('div', { key: k, className: 'text-xs text-(--ui-text-quaternary)', children: `${{ metrics: 'Metrics collector data', kanban: 'Kanban board', incidents: 'Errors and gateway changes', conformance: 'Host check changes', tasks: 'Task events', gitlab: 'Merged MRs', models: 'Model changes' }[k] || k} unavailable: ${v}` })
  )
  return jsxs('div', {
    className: 'space-y-3 p-4',
    children: [
      jsxs('div', {
        className: 'flex flex-wrap items-center gap-2',
        children: [
          jsx('h2', { className: 'text-sm font-semibold', children: 'Activity' }),
          jsx('span', { className: 'text-[0.6875rem] text-(--ui-text-quaternary)', children: 'grouped by hour · select an entry for its raw lines' }),
          jsx('span', { className: 'ml-auto' }),
          jsx(RangePicker, { hours, onChange: setHours })
        ]
      }),
      ...notes,
      changes.isError ? muted('Merged MRs and model loads unavailable: /changes did not respond.') : null,
      log.data.summaries
        ? muted(sums.isError ? `Summaries unavailable: ${sums.error?.message || 'the backend did not respond'}.`
          : sums.data?.errors?.model ? `Summaries paused: ${sums.data.errors.model}`
          : sums.data?.pending ? `Summarizing: ${sums.data.pending} ${sums.data.pending === 1 ? 'entry still shows its' : 'entries still show their'} logged text.`
          : null)
        : null,
      jsx('div', {
        className: 'flex flex-wrap items-center gap-1',
        children: [...(partial ? [['watch', `${role.name} watches`]] : []), ...LOG_KINDS].map(([id, label]) =>
          jsx('button', {
            type: 'button',
            key: id,
            onClick: () => { haptic('tap'); setKind(id) },
            className: cn(
              'rounded px-2 py-0.5 text-[0.6875rem] transition-colors',
              kind === id ? 'bg-(--ui-accent)/10 text-(--ui-accent)' : 'text-(--ui-text-quaternary) hover:bg-(--chrome-action-hover)'
            ),
            children: `${label} ${counts[id] || 0}`
          })
        )
      }),
      log.data.truncated ? muted('Showing the newest entries; pick a shorter range for the rest.') : null,
      hoursOf.length
        ? jsx('div', {
            className: 'space-y-3',
            children: hoursOf.map(h =>
              jsx(Section, {
                key: h.ts,
                title: new Date(h.ts * 1000).toLocaleString([], { weekday: 'short', hour: 'numeric', minute: '2-digit' }),
                count: h.items.length,
                children: jsx('div', {
                  className: 'space-y-0.5',
                  children: h.items.map(e =>
                    jsx(LogEntry, { key: keyOf(e), e, sum: e.key && sums.data?.summaries?.[e.key], open: open === keyOf(e), onToggle: () => setOpen(open === keyOf(e) ? null : keyOf(e)) })
                  )
                })
              })
            )
          })
        : muted(kind === 'all' ? 'Nothing happened in this window.' : kind === 'watch' ? `Nothing ${role.name} watches happened in this window.` : 'Nothing of this kind in this window.')
    ]
  })
}

// --- handoffs --------------------------------------------------------------------------

const OUTCOME_COLOR = { completed: '#3fb950', crashed: '#f85149', timed_out: '#f85149', gave_up: '#f85149', spawn_failed: '#f85149', blocked: '#d29922', rate_limited: '#d29922', reclaimed: '#8b949e' }
const HKIND = { card: ['Kanban card', '#3fb9a0'], subagent: ['Subagents', TONE.purple] }
const handoffTone = i => (i.status === 'blocked' ? 'bad' : i.status === 'triage' ? 'warn' : i.open ? 'info' : i.status === 'done' ? 'good' : 'muted')
const hatch = c => `repeating-linear-gradient(135deg, color-mix(in srgb, ${c} 60%, transparent) 0 5px, transparent 5px 9px)`

// One lane per agent. A dot is the sender, a line drops to the receiver's lane, and the bar is the
// receiver: a card's runs are solid on a thin line from filing to result, so a card that waited days
// does not read as days of work; an open card is hatched up to now.
function HandoffLanes({ items, now, since, sel, onSelect }) {
  const LH = 40
  const lanes = []
  for (const i of [...items].sort((p, q) => p.start - q.start))
    for (const [who, side] of [[i.from, 'sent'], [i.to, 'got']]) {
      let l = lanes.find(l => l.id === who)
      if (!l) lanes.push((l = { id: who, sent: 0, got: 0 }))
      l[side]++
    }
  const lane = id => lanes.findIndex(l => l.id === id)
  const mid = j => j * LH + LH / 2
  // the axis spans the handoffs themselves, not the whole window, with a little room at each end; it never
  // starts before the window, so a card still open from earlier is clipped at the left edge
  const lo = Math.max(Math.min(...items.map(i => i.start)), since)
  const hi = items.some(i => i.open) ? now : Math.max(...items.map(i => i.end || i.start))
  const pad = Math.max((hi - lo) * 0.03, 300)
  const t0 = Math.max(lo - pad, since), t1 = Math.min(hi + pad, now)
  const pct = t => (Math.min(Math.max(t, t0), t1) - t0) / (t1 - t0) * 100
  // at most six labels, so a 12-hour clock label (about 50px) clears the next at the axis's narrowest (27rem)
  const step = [1800, 3600, 7200, 14400, 43200, 86400].find(st => (t1 - t0) / st <= 6) || 86400
  const d0 = new Date(t0 * 1000)
  d0.setHours(0, 0, 0, 0)
  const ticks = []
  for (let t = d0 / 1000; t <= t1 - step / 4; t += step) if (t >= t0 + step / 4) ticks.push(t)
  // marks that start at about the same moment on a shared lane step 12px right of each other
  const nudge = {}
  const placed = []
  for (const i of [...items].sort((p, q) => p.start - q.start)) {
    const at = pct(i.start), ls = [lane(i.from), lane(i.to)]
    nudge[i.id] = placed.filter(o => Math.abs(o.at - at) < 0.8 && o.ls.some(l => ls.includes(l))).length
    placed.push({ at, ls })
  }
  return jsx('div', {
    className: 'overflow-x-auto',
    ref: dragScroll,
    children: jsxs('div', {
      style: { display: 'grid', gridTemplateColumns: '9rem minmax(0, 1fr)', minWidth: '36rem' },
      children: [
        jsx('div', {
          className: 'relative',
          style: { gridColumn: 2, height: 20, fontFamily: MONO, fontSize: '0.65625rem', color: 'var(--ui-text-quaternary)' },
          children: ticks.map(t =>
            jsx('span', { key: t, className: 'whitespace-nowrap', style: { position: 'absolute', left: `${pct(t)}%`, transform: 'translateX(-50%)' }, children: step >= 86400 ? new Date(t * 1000).toLocaleDateString([], { month: 'short', day: 'numeric' }) : fmtClock(t) })
          )
        }),
        jsx('div', {
          children: lanes.map(l =>
            jsxs('div', {
              key: l.id,
              className: 'flex min-w-0 flex-col justify-center pr-2',
              style: { height: LH, borderTop: '1px solid var(--ui-stroke-secondary)' },
              children: [
                jsx('span', { className: 'truncate text-xs font-semibold', title: l.id, children: l.id }),
                jsx('span', { className: 'truncate', style: { fontFamily: MONO, fontSize: '0.6875rem', color: 'var(--ui-text-quaternary)' }, children: [l.sent && `sent ${l.sent}`, l.got && `got ${l.got}`].filter(Boolean).join(' · ') })
              ]
            })
          )
        }),
        jsxs('div', {
          className: 'relative',
          style: { height: lanes.length * LH, borderLeft: '1px solid color-mix(in srgb, var(--ui-stroke-secondary) 35%, transparent)', background: `repeating-linear-gradient(180deg, var(--ui-stroke-secondary) 0 1px, transparent 1px ${LH}px)` },
          children: [
            t1 === now ? jsx('i', { style: { position: 'absolute', top: 0, bottom: 0, right: 0, borderRight: '1px dashed var(--ui-text-quaternary)' } }) : null,
            ...items.flatMap(i => {
              const a = lane(i.from), b = lane(i.to), c = HKIND[i.kind]?.[1] || TONE.muted
              const end = i.end || (i.open ? now : i.start)
              const from = Math.max(i.start, t0), early = i.start < t0
              const x0 = pct(i.start), x1 = pct(end), span = Math.max(end - from, 1)
              const left = `calc(${x0}% + ${nudge[i.id] * 12}px)`
              const dim = sel && sel !== i.id ? 0.25 : 1
              const tip = `${HKIND[i.kind]?.[0] || i.kind}: ${i.from} to ${i.to}, ${i.title} · ${fmtClock(i.start)}${i.end ? ` to ${fmtClock(i.end)}` : i.open ? ', still open' : `, ${i.status}`}`
              const runs = i.kind === 'card' ? i.runs.filter(r => (r.end || now) > from) : []
              return [
                early ? null : jsx('i', { key: `${i.id}-l`, style: { position: 'absolute', left, top: Math.min(mid(a), mid(b)), height: Math.abs(mid(a) - mid(b)), borderLeft: `2px ${i.open ? 'dashed' : 'solid'} ${c}`, transform: 'translateX(-1px)', opacity: dim, pointerEvents: 'none' } }),
                early ? null : jsx('button', { key: `${i.id}-d`, type: 'button', title: tip, 'aria-label': tip, onClick: () => { haptic('tap'); onSelect(sel === i.id ? null : i.id) }, style: { position: 'absolute', left, top: mid(a), width: 10, height: 10, borderRadius: '50%', backgroundColor: c, transform: 'translate(-50%, -50%)', opacity: dim, outline: sel === i.id ? '2px solid var(--ui-accent)' : 'none', outlineOffset: 2 } }),
                jsx('button', {
                  key: i.id,
                  type: 'button',
                  title: tip,
                  'aria-label': tip,
                  onClick: () => { haptic('tap'); onSelect(sel === i.id ? null : i.id) },
                  style: {
                    position: 'absolute', left, width: `max(9px, ${x1 - x0}%)`, top: mid(b) - 7, height: 14, opacity: dim,
                    background: i.open ? hatch(c) : i.kind === 'card' ? `linear-gradient(${c}, ${c}) center / 100% 2px no-repeat` : c,
                    border: i.open ? `1px solid ${c}` : 'none',
                    outline: sel === i.id ? '2px solid var(--ui-accent)' : 'none', outlineOffset: 2
                  },
                  children: runs.map((r, j) =>
                    jsx('i', { key: j, style: { position: 'absolute', top: 0, bottom: 0, left: `${(Math.max(r.start, from) - from) / span * 100}%`, width: `max(3px, ${((r.end || now) - Math.max(r.start, from)) / span * 100}%)`, backgroundColor: r.outcome && r.outcome !== 'completed' ? OUTCOME_COLOR[r.outcome] || c : c } })
                  )
                })
              ]
            })
          ]
        })
      ]
    })
  })
}

// Hand an open card to a profile, whose own worker starts on a ready card at once and picks it
// up from the card's history, or close it: done needs a result, archive is for a card no longer needed.
function CardActions({ i }) {
  const qc = useQueryClient()
  const [profile, setProfile] = useState('')
  const [closing, setClosing] = useState(null)
  const [result, setResult] = useState('')
  const [busy, setBusy] = useState(false)
  const [note, setNote] = useState(null)
  const [when, setWhen] = useState('')
  const [reply, setReply] = useState('')
  const { data } = useQuery({ queryKey: [ID, 'assignees'], queryFn: () => api.rest('/assignees'), staleTime: 5 * 60_000 })
  const profiles = data?.assignees || []
  const startAt = when ? Math.floor(new Date(when).getTime() / 1000) : null
  const send = async (path, body, done) => {
    setBusy(true)
    setNote(null)
    try {
      const out = await api.rest(path, { method: 'POST', body })
      setNote({ bad: !!out.warning, text: out.warning || (typeof done === 'function' ? done(out) : done) })
      setClosing(null)
      qc.invalidateQueries({ queryKey: [ID] })
    } catch (e) {
      setNote({ bad: true, text: e?.message || String(e) })
    }
    setBusy(false)
  }
  const path = `/cards/${encodeURIComponent(i.id)}`
  const started = out => out.started ? `${i.to} started working on it.` : out.status === 'ready' ? `Unblocked; ${i.to} starts on the dispatcher's next tick.` : `Unblocked; it is ${out.status} now.`
  return jsxs('div', {
    className: 'space-y-2 rounded border border-(--ui-border) p-2',
    children: [
      jsx('div', { className: 'text-[0.6875rem] font-medium text-(--ui-text-quaternary)', children: 'Move it forward' }),
      i.status === 'blocked'
        ? jsxs('div', {
            className: 'flex flex-wrap items-center gap-2',
            children: [
              jsx(Input, { className: 'h-7 min-w-0 text-xs', style: { flex: '1 1 100%' }, placeholder: 'Reply to the worker (optional), e.g. the go-ahead it asked for', value: reply, onChange: e => setReply(e.target.value) }),
              jsx(Button, {
                size: 'xs', variant: 'default', disabled: busy,
                onClick: () => send(`${path}/unblock`, { comment: reply }, out => { setReply(''); return started(out) }),
                children: 'Unblock and start'
              })
            ]
          })
        : null,
      jsxs('div', {
        className: 'flex flex-wrap items-center gap-2',
        children: [
          jsxs(Select, {
            value: profile,
            onValueChange: setProfile,
            children: [
              jsx(SelectTrigger, { className: 'h-7 min-w-0 text-xs', style: { flex: '1 1 100%' }, children: jsx(SelectValue, { placeholder: 'Hand to a profile' }) }),
              jsx(SelectContent, { children: profiles.map(p => jsx(SelectItem, { key: p, value: p, children: p })) })
            ]
          }),
          jsx(Input, { type: 'datetime-local', className: 'h-7 min-w-0 text-xs', style: { flex: '1 1 100%' }, title: 'Leave empty to start now', value: when, onChange: e => setWhen(e.target.value) }),
          jsx(Button, {
            size: 'xs', variant: 'outline', disabled: !profile || busy,
            onClick: () => send(`${path}/assign`, { profile, start_at: startAt }, out => out.starts_at ? `Scheduled: ${profile} starts it at ${new Date(out.starts_at * 1000).toLocaleString([], { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' })} (within about two minutes of that).` : out.started ? `${profile} started working on it.` : out.status === 'ready' ? `Handed to ${profile}; it starts on the dispatcher's next tick.` : `Handed to ${profile}; it starts once the card is ready.`),
            children: when ? 'Schedule' : i.status === 'ready' || i.status === 'scheduled' ? 'Assign and start now' : 'Assign'
          }),
          jsx('span', { style: { flexBasis: '100%', height: 0 } }),
          jsx(Button, { size: 'xs', variant: 'outline', disabled: busy, onClick: () => setClosing(closing === 'done' ? null : 'done'), children: 'Mark done' }),
          jsx(Button, { size: 'xs', variant: 'outline', disabled: busy, onClick: () => setClosing(closing === 'archived' ? null : 'archived'), children: 'Close as not needed' })
        ]
      }),
      closing
        ? jsxs('div', {
            className: 'flex flex-wrap items-center gap-2',
            children: [
              closing === 'done'
                ? jsx(Input, { className: 'h-7 min-w-0 flex-1 text-xs', placeholder: 'What was the result? (required)', value: result, onChange: e => setResult(e.target.value) })
                : jsx('span', { className: 'flex-1 text-(--ui-text-quaternary)', children: 'Archive this card? It leaves the board; nothing runs it.' }),
              jsx(Button, {
                size: 'xs', variant: closing === 'done' ? 'default' : 'destructive', disabled: busy || (closing === 'done' && !result.trim()),
                onClick: () => send(`${path}/close`, { outcome: closing, result }, closing === 'done' ? 'Marked done.' : 'Archived.'),
                children: closing === 'done' ? 'Confirm done' : 'Archive'
              }),
              jsx(Button, { size: 'xs', variant: 'ghost', disabled: busy, onClick: () => setClosing(null), children: 'Cancel' })
            ]
          })
        : null,
      note ? jsx('div', { style: { color: note.bad ? '#f85149' : '#3fb950', whiteSpace: 'pre-wrap' }, children: note.text }) : null
    ]
  })
}

function HandoffDetail({ i }) {
  const [full, setFull] = useState(false)
  const rows = i.kind === 'card'
    ? i.runs.map(r => ({ color: OUTCOME_COLOR[r.outcome] || 'var(--ui-accent)', left: r.profile || 'worker', mid: r.outcome || 'running', start: r.start, end: r.end, extra: r.error }))
    : i.children.map(c => ({ color: '#bc8cff', left: c.model || 'model not recorded', mid: `${c.requests} request${c.requests === 1 ? '' : 's'}`, start: c.start, end: c.end }))
  return jsxs('div', {
    className: 'space-y-2 text-xs',
    children: [
      jsxs('div', {
        className: 'flex flex-wrap items-center gap-2',
        children: [
          jsx('span', { className: 'font-medium', children: i.title }),
          jsx(KindBox, { children: i.kind === 'card' ? 'kanban card' : 'subagents' }),
          i.kind === 'card' ? jsx(Tag, { tone: handoffTone(i), children: i.status }) : null
        ]
      }),
      muted(`${i.from} to ${i.to}${i.where ? ` on ${i.where}` : ''} · ${new Date(i.start * 1000).toLocaleString([], { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' })} · ${i.end ? `took ${fmtDur(i.end - i.start)}` : i.open ? `open for ${fmtDur(Date.now() / 1000 - i.start)}` : 'closed'}`),
      i.kind === 'card' && i.id ? muted(`Card ${i.id}`) : null,
      i.blocked_reason
        ? jsxs('div', {
            children: [
              jsx('div', { className: 'whitespace-pre-wrap', style: { color: TONE.warn, ...(full ? {} : { display: '-webkit-box', WebkitLineClamp: 6, WebkitBoxOrient: 'vertical', overflow: 'hidden' }) }, children: `Blocked: ${i.blocked_reason}` }),
              i.blocked_reason.length > 240 ? jsx('button', { type: 'button', className: 'text-[0.6875rem] text-(--ui-accent) hover:underline', onClick: () => setFull(!full), children: full ? 'Show less' : 'Show all' }) : null
            ]
          })
        : null,
      i.starts_at ? muted(`Scheduled: ${i.to} starts it at ${new Date(i.starts_at * 1000).toLocaleString([], { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' })}`) : null,
      i.kind === 'card' && i.open && i.id ? jsx(CardActions, { i }, i.id) : null,
      rows.length
        ? jsxs('div', {
            className: 'space-y-1',
            children: [
              jsx('div', { className: 'text-[0.6875rem] font-medium text-(--ui-text-quaternary)', children: i.kind === 'card' ? `Runs (${rows.length})` : `Subagents (${rows.length})` }),
              ...rows.map((r, j) =>
                jsxs('div', {
                  key: j,
                  children: [
                    jsxs('div', {
                      className: 'flex items-center gap-2',
                      children: [
                        jsx(StatusDot, { tone: 'muted', style: { backgroundColor: r.color } }),
                        jsx('span', { className: 'min-w-0 flex-1 truncate', children: r.left }),
                        jsx('span', { className: 'shrink-0 text-(--ui-text-quaternary)', children: r.mid }),
                        jsx('span', { className: 'shrink-0 tabular-nums text-(--ui-text-quaternary)', children: r.end ? fmtDur(r.end - r.start) : 'running' })
                      ]
                    }),
                    r.extra ? jsx('div', { className: 'truncate text-(--ui-text-quaternary)', style: { paddingLeft: '1rem' }, title: r.extra, children: r.extra }) : null
                  ]
                })
              )
            ]
          })
        : muted(i.kind === 'card' ? 'No worker has picked this card up yet.' : 'No subagent details recorded.')
    ]
  })
}

function HandoffSummary({ items, pairs }) {
  const open = items.filter(i => i.open).length
  const subs = items.reduce((n, i) => n + (i.kind === 'subagent' ? i.children.length : 0), 0)
  const row = (key, left, right) =>
    jsxs('div', {
      key,
      className: 'flex items-center gap-2',
      children: [jsx('span', { className: 'min-w-0 flex-1 truncate', children: left }), jsx('span', { className: 'shrink-0 tabular-nums', style: { fontFamily: MONO, color: 'var(--ui-text-quaternary)' }, children: right })]
    })
  const head = t => jsx('div', { className: 'text-[0.6875rem] font-medium text-(--ui-text-quaternary)', children: t })
  return jsxs('div', {
    className: 'space-y-3 text-xs',
    children: [
      jsxs('div', {
        className: 'space-y-1',
        children: [
          jsx('div', { className: 'text-sm font-semibold', children: `${plural(items.length, 'handoff')} in this window` }),
          jsx('div', { children: `${items.length - open} finished, ${open} open.${subs ? ` ${plural(subs, 'subagent')} ran under them.` : ''}` })
        ]
      }),
      jsxs('div', {
        className: 'space-y-1',
        children: [head('By kind'), ...Object.entries(HKIND).map(([k, [label, c]]) => [label, c, items.filter(i => i.kind === k).length]).filter(r => r[2]).map(([label, c, n]) =>
          row(label, jsxs('span', { className: 'flex items-center gap-1.5', children: [jsx('i', { style: { width: 8, height: 8, backgroundColor: c, flex: 'none' } }), label] }), n))]
      }),
      pairs?.length
        ? jsxs('div', {
            className: 'space-y-1',
            children: [head('Who handed work to whom'), ...pairs.map(p => row(`${p.from}|${p.to}|${p.kind}`, `${p.from} to ${p.to}`, `${p.count}${p.open ? ` · ${p.open} open` : ''}`))]
          })
        : null,
      muted('Select a handoff in the lanes or the list for its runs and actions.')
    ]
  })
}

// A card's own kanban events, found in the Activity log by the card id each raw line starts with.
// /activity reads back from now, so ask for the hours since the card was created, not the page's window
function HandoffEvents({ i }) {
  const [open, setOpen] = useState(null)
  const { data, isLoading, isError } = useActivity(Math.min(168, Math.max(1, Math.ceil((Date.now() / 1000 - i.start) / 3600))))
  if (isError) return muted('Activity unavailable: the /api/plugins/operations backend did not respond.')
  if (isLoading || !data) return jsx(Skeleton, { className: 'h-24 w-full' })
  const mine = (data.items || []).filter(e => e.category === 'task' && e.raw.some(([, line]) => line.startsWith(`${i.id} ·`)))
  if (!mine.length) return muted('No events for this card in this window.')
  return jsx('div', {
    className: 'space-y-0.5',
    children: mine.map((e, j) => jsx(LogEntry, { key: j, e, open: open === j, onToggle: () => setOpen(open === j ? null : j) }))
  })
}

function HandoffDrawer({ i, onClose }) {
  const [sid, setSid] = useState(i.kind === 'subagent' ? i.id.slice('subagents:'.length) : null)
  return jsx(Drawer, {
    label: i.title,
    onClose,
    head: [
      jsx(Tag, { key: 'k', tone: HKIND[i.kind]?.[1] || 'muted', children: HKIND[i.kind]?.[0] || i.kind }),
      jsx('span', { key: 't', className: 'tabular-nums', style: { fontFamily: MONO, fontSize: 12, color: DIM }, children: new Date(i.start * 1000).toLocaleString([], { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' }) })
    ],
    children: [
      drawerTitle(i.title),
      drawerText('sub', `${i.from} to ${i.to}${i.where ? ` on ${i.where}` : ''} · ${i.end ? `took ${fmtDur(i.end - i.start)}` : i.open ? 'still open' : i.status}`),
      i.kind === 'card'
        ? drawerPart('b', 'Card events', jsx(HandoffEvents, { i }))
        : sid ? jsx(SessionTrace, { key: sid, sid, onOpen: setSid, drawer: true }) : drawerText('n', 'No session recorded for these subagents.'),
      sid ? jsx('div', { key: 'go', className: 'flex flex-wrap gap-2', children: jsx(GoLink, { btn: true, tab: 'trace', sel: sid, at: i.start, children: 'Open in Trace' }) }) : null
    ]
  })
}

function HandoffsPage({ sel: initial, when: link = {}, remember }) {
  const [hours, setHours] = useState(rangeFor(link, 168))
  // set by a link to one hour (the Today strip); picking a range goes back to the last hours
  const [start, setStart] = useState(link.start ?? null)
  // 'hour' stands for the newest card that finished or failed in that hour, what the strip counted
  const [selected, setSelected] = useState(initial ?? (link.start ? 'hour' : null))
  const [drawer, setDrawer] = useState(null)
  remember?.({ sel: selected, when: { hours, start } })
  const { data, isLoading, isError } = useHandoffs(hours, start)
  if (isError) return jsx('div', { className: 'p-4', children: muted('Handoffs unavailable: the /api/plugins/operations backend did not respond.') })
  if (isLoading || !data) return jsx('div', { className: 'p-4', children: jsx(Skeleton, { className: 'h-48 w-full' }) })
  const items = [...(data.items || [])].sort((p, q) => q.start - p.start)
  const now = start ? Math.min(start + hours * 3600, data.generated_at) : data.generated_at
  const inHour = t => t >= start && t < start + hours * 3600
  const sel = selected === 'hour'
    ? items.find(i => i.kind === 'card' && (inHour(i.end) || i.runs.some(r => inHour(r.end))))
    : items.find(i => i.id === selected)
  const when = t => (hours > 24 ? new Date(t * 1000).toLocaleString([], { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' }) : fmtClock(t))
  const kinds = Object.entries(HKIND).filter(([k]) => items.some(i => i.kind === k))
  const legend = (key, style, label) => jsxs('span', { key, className: 'flex items-center gap-1.5', children: [jsx('i', { style: { display: 'inline-block', width: 9, height: 9, ...style } }), label] })
  return jsxs('div', {
    className: 'space-y-3 p-4',
    children: [
      jsxs('div', {
        className: 'flex flex-wrap items-center gap-2',
        children: [
          jsx('h2', { className: 'text-sm font-semibold', children: 'Handoffs' }),
          jsx('span', { className: 'text-[0.6875rem] text-(--ui-text-quaternary)', children: 'kanban cards and subagent delegations' }),
          jsx('span', { className: 'ml-auto' }),
          start ? jsx('span', { className: 'text-[0.6875rem] tabular-nums', style: { fontFamily: MONO, color: 'var(--ui-accent)' }, children: `${fmtClock(start)} to ${fmtClock(start + hours * 3600)}` }) : null,
          jsx(RangePicker, { hours: start ? null : hours, onChange: h => { setHours(h); setStart(null) } })
        ]
      }),
      ...Object.entries(data.errors || {}).map(([k, v]) =>
        jsx('div', { key: k, className: 'text-xs text-(--ui-text-quaternary)', children: `${{ metrics: 'Metrics collector data', kanban: 'Kanban board', subagents: 'Subagent runs' }[k] || k} unavailable: ${v}` })
      ),
      !items.length
        ? muted('No kanban cards or subagent runs in this window.')
        : jsxs('div', {
            className: 'flex flex-wrap items-start gap-3',
            children: [
              jsx('div', {
                className: 'min-w-0',
                style: { flex: '999 1 36rem' },
                children: jsx(Section, {
                  title: 'Lanes by agent',
                  count: 'a dot is the sender, the bar is the receiver working until a result',
                  children: jsxs('div', {
                    className: 'space-y-2',
                    children: [
                      jsx(HandoffLanes, { items, now, since: start ?? now - hours * 3600, sel: sel?.id, onSelect: setSelected }),
                      jsxs('div', {
                        className: 'flex flex-wrap gap-x-4 gap-y-1',
                        style: { fontFamily: MONO, fontSize: '0.6875rem', color: 'var(--ui-text-quaternary)' },
                        children: [
                          ...kinds.map(([k, [label, c]]) => legend(k, { backgroundColor: c }, label)),
                          legend('wait', { height: 2, backgroundColor: 'currentColor' }, 'Waiting for a worker'),
                          legend('open', { background: hatch('currentColor') }, 'Still open'),
                          legend('fail', { backgroundColor: TONE.bad }, 'Run failed or blocked')
                        ]
                      }),
                      data.truncated ? muted('Showing the newest handoffs; pick a shorter range for the rest.') : null
                    ]
                  })
                })
              }),
              jsx('div', {
                className: 'min-w-0',
                style: { flex: '1 1 18rem' },
                children: jsx(Section, {
                  title: sel ? 'Handoff' : 'Summary',
                  children: sel
                    ? jsxs('div', {
                        className: 'space-y-2',
                        children: [
                          jsx(HandoffDetail, { i: sel }, sel.id),
                          jsxs('div', {
                            className: 'flex flex-wrap gap-2',
                            children: [
                              jsx(Button, { size: 'xs', variant: 'outline', onClick: () => { haptic('tap'); setDrawer(sel) }, children: sel.kind === 'card' ? 'Card events' : 'Session trace' }),
                              jsx(Button, { size: 'xs', variant: 'ghost', onClick: () => setSelected(null), children: 'Clear selection' })
                            ]
                          })
                        ]
                      })
                    : jsx(HandoffSummary, { items, pairs: data.pairs })
                })
              })
            ]
          }),
      items.length
        ? jsx(Section, {
            title: 'All handoffs',
            count: `${items.length} · newest first`,
            children: jsx('div', {
              className: 'space-y-1.5',
              style: { maxHeight: '22rem', overflowY: 'auto', paddingRight: 4 },
              children: items.map(i =>
                jsxs('button', {
                  key: i.id,
                  type: 'button',
                  'aria-pressed': sel?.id === i.id,
                  onClick: () => { haptic('tap'); setSelected(sel?.id === i.id ? null : i.id) },
                  className: 'w-full text-left text-xs hover:bg-(--chrome-action-hover)',
                  style: {
                    display: 'grid', gridTemplateColumns: `${hours > 24 ? '7.5rem' : '3.5rem'} 7rem minmax(0, 1fr) 5rem 5.5rem`, alignItems: 'center', gap: '0.75rem', padding: '0.5rem 0.75rem',
                    border: `1px solid ${sel?.id === i.id ? 'var(--ui-accent)' : 'var(--ui-stroke-secondary)'}`,
                    backgroundColor: sel?.id === i.id ? 'color-mix(in srgb, var(--ui-accent) 7%, transparent)' : undefined
                  },
                  children: [
                    jsx('span', { className: 'tabular-nums', style: { fontFamily: MONO, color: 'var(--ui-text-quaternary)' }, children: when(i.start) }),
                    jsxs('span', { className: 'flex items-center gap-1.5 text-(--ui-text-quaternary)', children: [jsx('i', { style: { width: 8, height: 8, flex: 'none', backgroundColor: HKIND[i.kind]?.[1] || TONE.muted } }), HKIND[i.kind]?.[0] || i.kind] }),
                    jsxs('span', {
                      className: 'flex min-w-0 flex-col',
                      children: [
                        jsx('span', { className: 'truncate font-semibold', title: i.title, children: i.title }),
                        jsx('span', { className: 'truncate', style: { fontFamily: MONO, fontSize: '0.6875rem', color: 'var(--ui-text-quaternary)' }, children: `${i.from} to ${i.to}${i.where ? ` on ${i.where}` : ''}` })
                      ]
                    }),
                    jsx('span', { className: 'tabular-nums', style: { fontFamily: MONO, color: 'var(--ui-text-quaternary)' }, children: i.end ? fmtDur(i.end - i.start) : i.open ? 'open' : '-' }),
                    jsx('span', { children: jsx(Tag, { tone: handoffTone(i), children: i.status }) })
                  ]
                })
              )
            })
          })
        : null,
      // the latest poll's copy, so its status keeps up; the snapshot once it leaves the window
      drawer ? jsx(HandoffDrawer, { i: items.find(i => i.id === drawer.id) || drawer, onClose: () => setDrawer(null) }, drawer.id) : null
    ]
  })
}

// --- trace ---------------------------------------------------------------------------

const fmtTok = n => (n == null ? '-' : n >= 1e6 ? `${(n / 1e6).toFixed(1)}M` : n >= 1000 ? `${(n / 1000).toFixed(1)}k` : String(n))

function SessionTrace({ sid, onOpen, drawer, fitH }) {
  const { data, isLoading, isError } = useSessionTrace(sid)
  const [pick, setPick] = useState(null)
  if (isError) return muted('This session did not load.')
  if (isLoading || !data) return jsx(Skeleton, { className: 'h-32 w-full' })
  const calls = data.calls || []
  if (!calls.length) return muted(data.errors?.trace ? `Unavailable: ${data.errors.trace}` : 'No model calls recorded for this session.')
  const t0 = Math.min(...calls.map(c => c.start)), t1 = Math.max(...calls.map(c => c.end))
  const failed = calls.filter(c => c.error).length
  const name = (c, i) => `${c.turn}.${i + 1}`
  const color = c => (c.error ? TONE.bad : c.pool_device ? TONE.good : TONE.info)
  // The axis runs to a round number of ticks past the last call, so the gridlines fall on the ticks.
  const DAY = 86400
  const step = [1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600, 7200, 21600, 43200, DAY, 2 * DAY, 7 * DAY].find(x => (t1 - t0) / x <= 8) ||
    7 * DAY * Math.ceil((t1 - t0) / (8 * 7 * DAY))
  const n = Math.max(1, Math.ceil((t1 - t0) / step)), max = n * step
  const at = s => `${(s / max) * 100}%`
  // Day steps read in days, so a week-long session's axis is 0d 1d 2d rather than 24h 48h ... 216h.
  const tick = s => (s < 60 ? `${s}s` : s < 3600 ? `${s / 60}m` : step < DAY ? `${s / 3600}h` : `${s / DAY}d`)
  const cur = pick == null ? null : calls[pick]
  const choose = i => { haptic('tap'); setPick(pick === i ? null : i) }
  const cols = { display: 'grid', gridTemplateColumns: 'minmax(7rem, 30%) minmax(0, 1fr)', alignItems: 'center' }
  // "Call 1.32 took ..." in the explanation selects that call.
  const cited = [...new Set((data.why || []).flatMap(w => [...w.matchAll(/call (\d+\.\d+)/gi)].map(m => m[1])))]
    .map(label => [label, calls.findIndex((c, i) => name(c, i) === label)]).filter(([, i]) => i >= 0)
  const models = [...new Set(calls.map(c => c.model || 'no model'))]
  const served = [...new Set(calls.map(c => c.served))]
  const sum = k => calls.reduce((a, c) => a + (c[k] || 0), 0)
  const modelTime = sum('duration_s')
  const kv = pairs => jsx('dl', {
    key: 'kv',
    className: 'grid',
    style: { margin: 0, gridTemplateColumns: 'max-content minmax(0, 1fr)', gap: '5px 16px' },
    children: pairs.filter(([, v]) => v).flatMap(([k, v]) => [
      jsx('dt', { key: `${k}-k`, style: { color: 'var(--ui-text-quaternary)' }, children: k }),
      jsx('dd', { key: `${k}-v`, style: { margin: 0, overflowWrap: 'anywhere', color: k === 'Error' ? TONE.bad : undefined }, children: v })
    ])
  })
  const tokens = (i, o, cached, reasoning) => `${fmtTok(i)} in, ${fmtTok(o)} out${cached ? `, ${fmtTok(cached)} cached` : ''}${reasoning ? `, ${fmtTok(reasoning)} reasoning` : ''}`
  const btn = (key, label, onClick) => jsx('button', { key, type: 'button', className: 'hover:bg-(--chrome-action-hover)', style: BTN, onClick, children: label })
  const summary = [
    sideTitle(`${data.platform || 'unknown'} · ${data.profile || 'no profile'} session at ${fmtClock(t0)}`),
    drawer ? null : jsx('span', { key: 'sid', style: { marginTop: -6, fontFamily: MONO, fontSize: 11, color: 'var(--ui-text-quaternary)', overflowWrap: 'anywhere' }, children: sid }),
    kv([
      ['Total', fmtDur(t1 - t0)],
      ['Calls', `${calls.length}${failed ? `, ${failed} failed` : ''}`],
      ['Model time', fmtDur(modelTime)],
      ['Between calls', fmtDur(Math.max(0, t1 - t0 - modelTime))],
      ['Tokens', tokens(sum('input_tokens'), sum('output_tokens'), sum('cache_read_tokens'), sum('reasoning_tokens'))],
      [models.length === 1 ? 'Model' : 'Models', models.join(', ')],
      ['Served by', served.join(', ')]
    ]),
    data.parent || (!drawer && data.subagents?.length)
      ? jsxs('div', {
          key: 'go',
          className: 'flex flex-wrap gap-2',
          children: [
            data.parent ? btn('p', 'Open the session that started it', () => onOpen(data.parent)) : null,
            !drawer && data.subagents?.length ? jsx(GoLink, { key: 's', btn: true, tab: 'handoffs', sel: `subagents:${sid}`,
              // the hours the session ran, from the hour it began, not the last 7 days
              start: Math.floor(t0 / 3600) * 3600, hours: Math.min(168, Math.ceil((t1 - Math.floor(t0 / 3600) * 3600) / 3600)), children: 'Open its subagents in Handoffs' }) : null
          ]
        })
      : null,
    jsx('p', { key: 'hint', style: { margin: 0, color: DIM }, children: 'Select a call for its details.' })
  ]
  const detail = cur && [
    sideTitle(`Call ${name(cur, pick)}`),
    jsx('span', { key: 'm', style: { marginTop: -6, fontFamily: MONO, fontSize: 13, fontWeight: 600, overflowWrap: 'anywhere' }, children: cur.model || 'no model' }),
    kv([
      ['Served by', `${cur.served} (${cur.pool_device ? 'pool device' : 'cloud provider'})`],
      ['From host', cur.host],
      ['Starts at', `+${fmtDur(cur.start - t0)}`],
      ['Took', `${fmtDur(cur.duration_s)}${cur.usual_s != null ? `, usually ${fmtDur(cur.usual_s)}` : ''}`],
      ['First token', cur.ttft_s != null ? fmtDur(cur.ttft_s) : null],
      ['Tokens', tokens(cur.input_tokens, cur.output_tokens, cur.cache_read_tokens, cur.reasoning_tokens)],
      ['Speed', cur.tps != null ? `${cur.tps} tokens/s` : null],
      ['Finish', cur.finish_reason],
      ['Error', cur.error]
    ]),
    // This call against calls like it as of when it ran (the backend's _why says which), on one scale.
    cur.usual_s != null && cur.duration_s != null
      ? jsxs('div', {
          key: 'vs',
          className: 'grid items-center',
          title: `Typical: the median of ${cur.model || 'this model'} calls on ${cur.served} with ${Math.ceil(cur.output_tokens / 2)} to ${2 * Math.max(cur.output_tokens, 1)} output tokens, from other sessions, that ended in the week before this call started`,
          style: { gridTemplateColumns: 'auto minmax(0, 1fr) auto', gap: '6px 10px', fontFamily: MONO, fontSize: 11.5 },
          children: [
            ...[['This call', cur.duration_s, color(cur)], ['Typical', cur.usual_s, DIM]].flatMap(([label, s, c]) => [
              jsx('span', { key: `${label}-l`, style: { color: DIM }, children: label }),
              jsx('span', { key: `${label}-b`, style: { height: 8, borderRadius: 2, backgroundColor: c, width: `${Math.max(1, (s / Math.max(cur.duration_s, cur.usual_s)) * 100)}%` } }),
              jsx('span', { key: `${label}-v`, children: fmtDur(s) })
            ]),
            jsx('span', { key: 'n', style: { gridColumn: '1 / -1', color: 'var(--ui-text-quaternary)' }, children: `Typical: median of ${cur.usual_n} similar calls in the week before` })
          ]
        })
      : cur.usual_n != null
        ? drawerText('vs', `No typical time yet: ${plural(cur.usual_n, 'call')} like this one (same model and server, similar output length) in the week before it; it takes 5.`)
        : null,
    jsxs('div', {
      key: 'go',
      className: 'flex flex-wrap gap-2',
      children: [
        jsx(GoLink, { key: 'map', btn: true, tab: 'topology', sel: `s:${cur.served}`, at: cur.start, children: 'Show on the map' }),
        jsx(GoLink, { key: 'flow', btn: true, tab: 'flow', sel: `served:${cur.served}`, at: cur.start, children: 'See its traffic' }),
        btn('back', 'Back to the session', () => setPick(null))
      ]
    })
  ]
  const side = jsx('div', { key: 'side', style: { ...SIDE, flex: '1 1 17rem', minWidth: 0, ...(fitH ? { maxHeight: fitH, overflowY: 'auto' } : {}) }, children: detail || summary })
  const main = jsxs('div', {
    key: 'main',
    className: 'grid',
    style: { flex: '999 1 26rem', minWidth: 0, gap: drawer ? 16 : 12, ...(fitH ? { maxHeight: fitH, overflowY: 'auto', alignContent: 'start' } : {}) },
    children: [
      data.why?.length
        ? jsx(Section, {
            title: `Why it took ${fmtDur(t1 - t0)}`,
            count: failed ? `${failed} failed` : null,
            children: jsxs('div', {
              className: 'grid',
              style: { gap: 10 },
              children: [
                jsx('div', { key: 'w', className: 'grid', style: { gap: 4, maxWidth: '72ch', fontSize: 13.5, lineHeight: 1.6, textWrap: 'pretty' }, children: data.why.map((w, i) => jsx('p', { key: i, style: { margin: 0 }, children: w })) }),
                cited.length
                  ? jsx('div', {
                      key: 'c',
                      className: 'flex flex-wrap gap-2',
                      children: cited.map(([label, i]) =>
                        jsx('button', {
                          key: label,
                          type: 'button',
                          onClick: () => choose(i),
                          className: 'hover:border-(--ui-accent)',
                          style: { padding: '4px 10px', fontSize: 12.5, border: `1px solid ${i === pick ? 'var(--ui-accent)' : 'var(--ui-stroke-secondary)'}` },
                          children: `Call ${label}`
                        })
                      )
                    })
                  : null
              ]
            })
          })
        : null,
      jsx(Section, {
        title: 'Waterfall',
        count: 'Select a call for its details.',
        children: jsxs('div', {
          children: [
            jsxs('div', {
              key: 'axis',
              style: cols,
              children: [
                jsx('span', {}),
                jsx('div', {
                  className: 'relative',
                  style: { height: 16, fontFamily: MONO, fontSize: 10.5, color: 'var(--ui-text-quaternary)' },
                  children: Array.from({ length: n + 1 }, (_, k) =>
                    jsx('span', { key: k, className: 'absolute', style: { left: at(k * step), transform: k === 0 ? undefined : k === n ? 'translateX(-100%)' : 'translateX(-50%)' }, children: tick(k * step) })
                  )
                })
              ]
            }),
            jsx('div', {
              key: 'rows',
              // on the page the whole column scrolls instead
              style: drawer ? { maxHeight: '20rem', overflowY: 'auto' } : undefined,
              children: calls.map((c, i) => {
                const end = (c.end - t0) / max
                return jsxs('button', {
                  type: 'button',
                  key: i,
                  onClick: () => choose(i),
                  title: c.error || `${c.finish_reason || ''}${c.usual_s != null ? ` · usually ${fmtDur(c.usual_s)}` : ''}`,
                  className: cn('w-full text-left', i === pick ? 'bg-(--ui-accent)/10' : 'hover:bg-(--chrome-action-hover)'),
                  style: { ...cols, alignItems: 'stretch' },
                  children: [
                    jsxs('span', {
                      className: 'min-w-0',
                      style: { padding: '3px 10px 3px 0', fontFamily: MONO, fontSize: 12, lineHeight: 1.3 },
                      children: [
                        jsx('span', { className: 'block truncate', children: `${name(c, i)}  ${c.model || 'no model'}` }),
                        jsx('span', { className: 'block truncate', style: { fontSize: 11, color: 'var(--ui-text-quaternary)' }, children: `on ${c.served}` })
                      ]
                    }),
                    jsxs('span', {
                      className: 'relative block',
                      style: { background: `repeating-linear-gradient(90deg, color-mix(in srgb, var(--ui-stroke-secondary) 70%, transparent) 0 1px, transparent 1px ${100 / n}%)` },
                      children: [
                        jsx('i', { className: 'absolute', style: { top: 'calc(50% - 6px)', height: 12, left: at(c.start - t0), width: at(c.end - c.start), minWidth: 3, backgroundColor: color(c) } }),
                        jsx('span', {
                          className: 'absolute whitespace-nowrap',
                          style: { top: 'calc(50% - 10px)', fontFamily: MONO, fontSize: 11, lineHeight: '20px', color: i === pick ? undefined : DIM,
                                   ...(end > 0.85 ? { right: `calc(${100 - ((c.start - t0) / max) * 100}% + 6px)` } : { left: `calc(${end * 100}% + 6px)` }) },
                          children: c.error ? 'failed' : fmtDur(c.duration_s)
                        })
                      ]
                    })
                  ]
                })
              })
            }),
            jsx('div', {
              key: 'legend',
              className: 'flex flex-wrap',
              style: { gap: '6px 16px', marginTop: 10, fontFamily: MONO, fontSize: 11.5, color: DIM },
              children: [['Pool device', TONE.good], ['Cloud provider', TONE.info], ['Failed', TONE.bad]].map(([l, c]) =>
                jsxs('span', { key: l, children: [jsx('i', { style: { display: 'inline-block', width: 9, height: 9, marginRight: 6, verticalAlign: -1, backgroundColor: c } }), l] })
              )
            }),
            data.truncated ? jsx('div', { key: 'more', className: 'mt-2', children: muted('Showing the first calls of a long session.') }) : null
          ]
        })
      }),
      data.subagents?.length
        ? jsx(Section, {
            title: 'Subagents it started',
            children: jsx('div', {
              className: 'grid',
              style: { gap: 6 },
              children: data.subagents.map(a =>
                jsxs('div', {
                  key: a.session_id || a.start,
                  className: 'flex items-center gap-2',
                  children: [
                    jsx('span', { className: 'min-w-0 flex-1 truncate', children: `${a.profile || a.label || 'subagent'} · ${plural(a.calls, 'call')} · ${fmtDur(a.duration_s)} · ended ${fmtClock(a.end)}` }),
                    a.session_id && a.calls ? jsx('button', { type: 'button', className: 'hover:bg-(--chrome-action-hover)', style: { ...BTN, padding: '4px 10px', fontSize: 12 }, onClick: () => onOpen(a.session_id), children: 'Open' }) : null
                  ]
                })
              )
            })
          })
        : null
    ]
  })
  // In the drawer the panel goes first, above the waterfall; on the page it sits at the top right.
  return jsx('div', { className: cn('text-xs', drawer ? 'grid' : 'flex flex-wrap items-start'), style: { gap: drawer ? 16 : 12 }, children: drawer ? [side, main] : [main, side] })
}

function TracePage({ sel: initial, when = {}, remember }) {
  const [hours, setHours] = useState(rangeFor(when, 24))
  // set by a link to one hour (the Today strip); picking a range goes back to the last hours
  const [start, setStart] = useState(when.start ?? null)
  const [selected, setSelected] = useState(initial)
  const scrolled = useRef(null)
  const { data, isLoading, isError } = useTrace(hours, start)
  const rowRef = useRef(null)
  const [fitH, setFitH] = useState(null)
  // Each column scrolls within what is left of the pane below it, so the page need not.
  useLayoutEffect(() => {
    const el = rowRef.current
    if (!el) return
    const fit = () => {
      const pane = el.closest('[data-ops-pane]')
      setFitH(Math.max(320, (pane ? pane.getBoundingClientRect().bottom : window.innerHeight) - el.getBoundingClientRect().top - 16))
    }
    fit()
    window.addEventListener('resize', fit)
    return () => window.removeEventListener('resize', fit)
  }, [data])
  if (isError) return jsx('div', { className: 'p-4', children: muted('Trace unavailable: the /api/plugins/operations backend did not respond.') })
  if (isLoading || !data) return jsx('div', { className: 'p-4', children: jsx(Skeleton, { className: 'h-48 w-full' }) })
  const rows = data.sessions || []
  const failing = rows.filter(r => r.errors).length
  // an hour opened from the Today strip starts on its newest failed session, the likeliest reason to look
  const sid = selected || (start && rows.find(r => r.errors)?.session_id) || rows[0]?.session_id
  remember?.({ sel: sid, when: { hours, start } })
  return jsxs('div', {
    className: 'space-y-3 p-4',
    children: [
      jsxs('div', {
        className: 'flex flex-wrap items-center gap-2',
        children: [
          jsx('h2', { className: 'text-sm font-semibold', children: 'Trace' }),
          jsx('span', { className: 'text-[0.6875rem] text-(--ui-text-quaternary)', children: 'a session\'s model calls in order' }),
          jsx('span', { className: 'ml-auto' }),
          start ? jsx('span', { className: 'text-[0.6875rem] tabular-nums', style: { fontFamily: MONO, color: 'var(--ui-accent)' }, children: `${fmtClock(start)} to ${fmtClock(start + hours * 3600)}` }) : null,
          jsx(RangePicker, { hours: start ? null : hours, onChange: h => { setHours(h); setStart(null) } })
        ]
      }),
      ...Object.entries(data.errors || {}).map(([k, v]) =>
        jsx('div', { key: k, className: 'text-xs text-(--ui-text-quaternary)', children: `${{ metrics: 'Metrics collector data' }[k] || k} unavailable: ${v}` })
      ),
      jsxs('div', {
        ref: rowRef,
        className: 'flex flex-wrap items-start gap-3',
        children: [
          jsx('div', {
            style: { flex: '1 1 16rem', maxWidth: '100%', minWidth: 0 },
            children: jsx(Section, {
              title: 'Sessions',
              // short enough to stay on the header's one line beside the title
              count: `${rows.length}${data.truncated ? '+' : ''}${failing ? ` · ${failing} failed` : ''}`,
              children: rows.length
                ? jsx('div', {
                    className: 'grid',
                    // less the section's header and padding
                    style: { gap: 8, maxHeight: fitH ? fitH - 52 : '75vh', overflowY: 'auto' },
                    children: rows.map(r => {
                      const on = sid === r.session_id
                      return jsxs('button', {
                        type: 'button',
                        key: r.session_id,
                        // each newly selected session (a link, Open on a subagent or its parent) is scrolled to once, not again on each refresh
                        ref: on && scrolled.current !== sid ? el => { if (el && scrolled.current !== sid) { scrolled.current = sid; el.scrollIntoView({ block: 'nearest' }) } } : undefined,
                        onClick: () => { haptic('tap'); setSelected(r.session_id) },
                        className: cn('grid min-w-0 text-left transition-colors', on ? '' : 'hover:border-(--ui-text-quaternary)'),
                        style: { gap: 4, padding: '10px 12px', border: `1px solid ${on ? 'var(--ui-accent)' : 'var(--ui-stroke-secondary)'}`, backgroundColor: on ? 'color-mix(in srgb, var(--ui-accent) 7%, transparent)' : undefined },
                        children: [
                          jsxs('span', {
                            className: 'flex items-center gap-2 tabular-nums',
                            style: { fontFamily: MONO, fontSize: 12, color: DIM },
                            children: [fmtClock(r.start), jsx('b', { key: 'd', className: 'ml-auto', style: { fontWeight: 500 }, children: fmtDur(r.end - r.start) })]
                          }),
                          jsx('span', { className: 'truncate', style: { fontSize: 13, fontWeight: 600 }, children: `${r.platform || 'unknown'} · ${r.profile || 'no profile'}` }),
                          jsx('span', { className: 'truncate', style: { fontSize: 12, color: DIM }, children: r.models.join(', ') || 'no model' }),
                          jsxs('span', {
                            className: 'flex flex-wrap items-center gap-1.5',
                            children: [
                              r.errors ? jsx(Tag, { tone: 'bad', children: `${r.errors} failed` }) : jsx(Tag, { tone: 'good', children: 'ok' }),
                              r.parent ? jsx(KindBox, { children: 'subagent' }) : null,
                              r.subagents ? jsx(KindBox, { children: plural(r.subagents, 'subagent') }) : null,
                              jsx('span', { style: { fontFamily: MONO, fontSize: 11, color: 'var(--ui-text-quaternary)' }, children: plural(r.calls, 'call') })
                            ]
                          })
                        ]
                      })
                    })
                  })
                : muted('No sessions made model calls in this window.')
            })
          }),
          jsx('div', {
            style: { flex: '999 1 34rem', minWidth: 0 },
            children: sid ? jsx(SessionTrace, { key: sid, sid, onOpen: setSelected, fitH }) : null
          })
        ]
      })
    ]
  })
}

// --- topology ------------------------------------------------------------------------

const STATE_COLOR = { connected: '#3fb950', fatal: '#f85149' }
const DRIFT = '#e275ad'

// What stops if the selected node goes down. Per model, from the window's traffic: "also went to"
// means the same model was served somewhere still up. Per router role, from the router's own rules:
// the role moves to its first candidate still loaded and fit somewhere up; with none, a strict role
// stops and any other falls through to its last candidate.
// Pool devices in the pool now with the model loaded.
const roleDevices = (data, model) =>
  data.served.filter(s => s.pool_device && s.live && s.models.some(m => m.name === model)).map(s => s.id)

function blastRadius(data, sel) {
  const [kind, ...rest] = sel.split(':')
  const id = rest.join(':')
  if (kind === 'g') return { cut: 1, alt: 0, lines: [{ text: `Requests arriving through ${id.slice(id.indexOf(':') + 1)} would stop. Nothing else depends on it.`, cut: true }] }
  const lines = []
  const down = new Set()
  let host = null
  if (kind === 'h') {
    host = data.hosts.find(h => h.host === id)
    if (host?.device) down.add(host.device)
    for (const g of data.gateways.filter(g => g.host === id)) lines.push({ text: `Entry point ${g.platform} goes with it`, cut: true })
    const own = data.edges.filter(e => e.host === id).reduce((a, e) => a + e.requests, 0)
    if (own) lines.push({ text: `Requests made on ${id} stop (${own} in the window)`, cut: true })
  } else down.add(id)
  for (const e of data.edges.filter(e => down.has(e.served) && e.host !== host?.host))
    for (const [model, n] of Object.entries(e.models || {})) {
      const other = data.edges.find(x => !down.has(x.served) && x.host !== host?.host && x.models?.[model])
      lines.push(other
        ? { text: `${e.host}: ${model} (${n} requests) also went to ${other.served}`, cut: false }
        : { text: `${e.host}: ${model} (${n} requests) has no other server`, cut: true })
    }
  const up = model => roleDevices(data, model).filter(d => !down.has(d))
  for (const r of data.router?.roles || []) {
    const on = roleDevices(data, r.resolved)
    if (!on.length || on.some(d => !down.has(d))) continue
    const next = r.candidates.find(c => c.live && c.fit && up(c.model).length)
    lines.push(next
      ? { text: `Role ${r.name} moves from ${r.resolved} to ${next.model} on ${up(next.model).join(', ')}`, cut: false }
      : r.strict
        ? { text: `Role ${r.name} stops: it is strict and no other candidate is loaded`, cut: true }
        : { text: `Role ${r.name} falls through to its last candidate, ${r.candidates.at(-1)?.model}, which is not loaded anywhere still up`, cut: true })
  }
  return { cut: lines.filter(l => l.cut).length, alt: lines.filter(l => !l.cut).length, lines }
}

// The map as it stood at `at` (a time inside the loaded window), rebuilt from the window's timeline
// the way Ops Timeline's Architecture page does: requests up to then, each node's state then, and
// nodes not seen yet left out. Pool devices keep the models loaded now; the router is shown as now.
const stateAt = (series, at) => series?.filter(s => s[0] <= at).at(-1)?.[1]

// from, when set, drops the buckets before it: the map of one hour instead of the window up to at
function topologyAt(data, at, from) {
  const t = data.timeline
  if ((!at && !from) || !t) return data
  const by = {}
  for (const [ts, host, served, pool, model, n, errors] of t.buckets) {
    if (ts > (at || Infinity) || ts < (from || 0)) continue
    const e = (by[`${host}>${served}`] ||= { host, served, pool_device: false, requests: 0, errors: 0, models: {} })
    e.pool_device ||= !!pool
    e.requests += n
    e.errors += errors
    e.models[model] = (e.models[model] || 0) + n
  }
  const edges = Object.values(by).sort((a, b) => b.requests - a.requests)
  if (!at) return { ...data, from, edges }
  const gateways = data.gateways.flatMap(g => {
    const state = stateAt(t.states[`g:${g.host}:${g.platform}`], at)
    return state ? [{ ...g, state, error: null, ts: null }] : []
  })
  const used = new Set([...edges.flatMap(e => [e.host, e.served]), ...gateways.map(g => g.host)])
  const hosts = data.hosts.flatMap(h => {
    const live = stateAt(t.states[`h:${h.host}`], at)
    const drift = stateAt(t.states[`d:${h.host}`], at)
    if (live === undefined && drift === undefined && !used.has(h.host)) return []
    return [{ ...h, stale: !live, drift: drift || [] }]
  })
  const served = data.served.flatMap(s => {
    const live = s.pool_device ? !!stateAt(t.states[`s:${s.id}`], at) : false
    return live || used.has(s.id) ? [{ ...s, live }] : []
  })
  return { ...data, at, from, gateways, hosts, served, edges }
}

// The map as nodes in four tiers (entry points, hosts, pool devices, cloud providers) and edges from
// what depends to what it depends on: a gateway on its host, a host on what served its requests.
const TIERS = [['Entry points', TONE.orange], ['Hosts', TONE.purple], ['Model pool', TONE.good], ['Cloud providers', TONE.info]]

function topoGraph(data) {
  // a platform on two hosts needs its host named wherever the layout does not group by host
  const twice = new Set(data.gateways.map(g => g.platform).filter((p, i, all) => all.indexOf(p) !== i))
  const nodes = [
    ...data.gateways.map(g => ({ id: `g:${g.host}:${g.platform}`, label: g.platform, full: `${g.platform} on ${g.host}`, tier: 0, group: g.host,
                                long: twice.has(g.platform) ? `${g.platform} on ${g.host}` : g.platform,
                                color: STATE_COLOR[g.state] || TONE.warn, broken: g.state !== 'connected' })),
    ...data.hosts.map(h => ({ id: `h:${h.host}`, label: h.host, full: h.host, tier: 1, color: h.stale ? TONE.warn : TONE.good, drift: h.drift?.length > 0, broken: h.stale })),
    ...data.served.map(s => ({ id: `s:${s.id}`, label: s.models.length ? `${s.id} (${s.models.length} model${s.models.length > 1 ? 's' : ''})` : s.id, full: s.id,
                               tier: s.pool_device ? 2 : 3, color: s.pool_device ? (s.live ? TONE.good : TONE.muted) : TONE.info, broken: s.pool_device && !s.live }))
  ]
  const ids = new Set(nodes.map(n => n.id))
  const max = Math.max(1, ...data.edges.map(e => e.requests))
  const edges = [
    ...nodes.filter(n => n.tier === 0).map(n => ({ a: n.id, b: `h:${n.group}`, w: 1.5, color: n.color, title: `Gateway ${n.full}` })),
    ...data.edges.map(e => ({ a: `h:${e.host}`, b: `s:${e.served}`, w: 1 + 6 * Math.sqrt(e.requests / max), color: e.errors ? TONE.bad : 'var(--ui-accent)',
                              title: `${e.host} to ${e.served}: ${e.requests} requests${e.errors ? `, ${e.errors} failed` : ''}` }))
  ].filter(e => ids.has(e.a) && ids.has(e.b))
  return { nodes, edges }
}

// Everything reachable from `from` along edges: what it depends on (down) or what depends on it (up).
function topoWalk(edges, from, up) {
  const seen = new Set([from])
  const queue = [from]
  while (queue.length) {
    const n = queue.shift()
    for (const e of edges) {
      const [here, next] = up ? [e.b, e.a] : [e.a, e.b]
      if (here === n && !seen.has(next)) { seen.add(next); queue.push(next) }
    }
  }
  return seen
}

const W = 900, BH = 22, ROW = 30
const boxCurve = (x0, y0, x1, y1) => { const m = (x0 + x1) / 2; return `M${x0} ${y0}C${m} ${y0} ${m} ${y1} ${x1} ${y1}` }
// a box-to-box edge, from the right side of whichever node is further left
const boxEdge = (A, B) => {
  const [L, R] = A.x <= B.x ? [A, B] : [B, A]
  return boxCurve(L.x + L.w, L.y + BH / 2, R.x, R.y + BH / 2)
}
const tierLegend = (tiers, y) => tiers.map(([name, color], i) => jsxs('g', { key: `k${i}`, children: [
  jsx('rect', { x: i * 130, y, width: 10, height: 4, rx: 1, fill: color }),
  jsx('text', { x: i * 130 + 15, y: y + 5, fontSize: 11, fill: 'currentColor', opacity: 0.6, children: name })
] }))

// Tiers as columns. A host's entry points sit in a region named for it, so a second gateway host is its
// own region; what served requests splits into the model pool and the cloud providers.
function layoutColumns({ nodes }) {
  const BW = 220, HEAD = 18, top = 22
  const pos = {}, back = []
  const region = (x, y, h, label, key) => back.push(jsxs('g', { key, children: [
    jsx('rect', { x: x - 6, y, width: BW + 12, height: h, rx: 6, fill: 'currentColor', fillOpacity: 0.03, stroke: 'currentColor', strokeOpacity: 0.12 }),
    jsx('text', { x, y: y + 13, fontSize: 10.5, fill: 'currentColor', opacity: 0.55, children: label })
  ] }))
  const stack = (x, groups) => {
    let y = top
    for (const [label, list] of groups) {
      if (!list.length) continue
      const y0 = y
      if (label) y += HEAD
      for (const n of list) { pos[n.id] = { x, y: y + (ROW - BH) / 2, w: BW }; y += ROW }
      if (label) { region(x, y0, y - y0 + 4, label, `r${x}:${label}`); y += 12 }
    }
    return y
  }
  const by = t => nodes.filter(n => n.tier === t)
  const hosts = [...new Set(by(0).map(n => n.group))]
  const xs = [6, (W - BW) / 2, W - BW - 6]
  const heights = [
    stack(xs[0], hosts.map(h => [h, by(0).filter(n => n.group === h)])),
    stack(xs[1], [[null, by(1)]]),
    stack(xs[2], [['Model pool', by(2)], ['Cloud providers', by(3)]])
  ]
  const H = Math.max(...heights) + 4
  // centre the shorter columns against the tallest
  xs.forEach((x, c) => { const dy = (H - heights[c]) / 2; if (dy > 0) for (const p of Object.values(pos)) if (p.x === x) p.y += dy })
  back.forEach((r, i) => {
    const c = xs.findIndex(x => r.key.startsWith(`r${x}:`))
    const dy = (H - heights[c]) / 2
    if (dy > 0) back[i] = jsx('g', { key: r.key, transform: `translate(0 ${dy})`, children: r })
  })
  ;['Entry points', 'Hosts', 'Served by'].forEach((t, c) => back.push(jsx('text', { key: `h${c}`, x: xs[c], y: 10, fontSize: 11, fontWeight: 600, fill: 'currentColor', opacity: 0.6, children: t })))
  return { H, pos, back, edge: boxEdge, grouped: true }
}

// Nodes around an ellipse in tier order with a gap between tiers, spaced evenly top to bottom so boxes do
// not overlap; an arc segment per tier, and chords bowed toward the centre.
function layoutRadial({ nodes }) {
  const BW = 210
  const list = [0, 1, 2, 3].map(t => nodes.filter(n => n.tier === t))
  const slots = list.reduce((a, l) => a + (l.length ? l.length + 1 : 0), 0) || 1
  const H = Math.max(380, slots * 13 + 60), cx = W / 2, cy = H / 2, rx = W / 2 - BW - 12, ry = H / 2 - 24
  const at = k => {
    const u = (((k - 0.5) / slots) % 1 + 1) % 1, y = u < 0.5 ? 4 * u - 1 : 3 - 4 * u
    return { px: cx + (u < 0.5 ? 1 : -1) * rx * Math.sqrt(Math.max(0, 1 - y * y)), py: cy + ry * y, right: u < 0.5, a: Math.atan2(ry * y, (u < 0.5 ? 1 : -1) * rx * Math.sqrt(Math.max(0, 1 - y * y))) }
  }
  const pos = {}, back = []
  let k = 0
  list.forEach((l, t) => {
    if (!l.length) return
    const k0 = k
    for (const n of l) {
      const p = at(k + 0.5)
      pos[n.id] = { x: p.right ? p.px + 6 : p.px - BW - 6, y: p.py - BH / 2, w: BW, px: p.px, py: p.py }
      k++
    }
    const a0 = at(k0 + 0.2).a, a1 = at(k - 0.2).a
    const span = ((a1 - a0) % (2 * Math.PI) + 2 * Math.PI) % (2 * Math.PI)
    // the segment runs through the points the lines end on, so each line lands on its tier's arc beside its label
    const pt = a => { const r = 1 / Math.sqrt((Math.cos(a) / rx) ** 2 + (Math.sin(a) / ry) ** 2); return `${(cx + r * Math.cos(a)).toFixed(1)} ${(cy + r * Math.sin(a)).toFixed(1)}` }
    back.push(jsx('path', { key: `seg${t}`, d: `M${pt(a0)}A${rx} ${ry} 0 ${span > Math.PI ? 1 : 0} 1 ${pt(a1)}`, fill: 'none', stroke: TIERS[t][1], strokeWidth: 3, strokeLinecap: 'round', opacity: 0.7,
                          children: jsx('title', { children: TIERS[t][0] }) }))
    for (const n of l) back.push(jsx('circle', { key: `pin${n.id}`, cx: pos[n.id].px, cy: pos[n.id].py, r: 3.5, fill: TIERS[t][1] }))
    k++
  })
  back.push(jsx('g', { key: 'legend', children: tierLegend(TIERS, H - 6) }))
  const edge = (A, B) => `M${A.px.toFixed(1)} ${A.py.toFixed(1)}Q${(cx + ((A.px + B.px) / 2 - cx) * 0.2).toFixed(1)} ${(cy + ((A.py + B.py) / 2 - cy) * 0.2).toFixed(1)} ${B.px.toFixed(1)} ${B.py.toFixed(1)}`
  return { H, pos, back, edge }
}

// Every node on one baseline in tier order, an arc per dependency above it. Nodes keep their place as the
// as-of slider moves, and long-range dependencies stand out as tall arcs.
function layoutArc({ nodes, edges }) {
  const L = 24, R = 120
  const list = [0, 1, 2, 3].map(t => nodes.filter(n => n.tier === t))
  const slots = list.reduce((a, l) => a + l.length, 0) + Math.max(0, list.filter(l => l.length).length - 1) * 0.8
  const ux = {}, segs = []
  let k = 0.5
  list.forEach((l, t) => {
    if (!l.length) return
    const k0 = k
    for (const n of l) { ux[n.id] = k / slots; k++ }
    segs.push([(k0 - 0.4) / slots, (k - 0.6) / slots, t])
    k += 0.8
  })
  const ax = u => L + u * (W - L - R)
  const span = Math.max(0, ...edges.map(e => Math.abs(ax(ux[e.a]) - ax(ux[e.b]))))
  const B = 16 + Math.min(240, span / 2), H = B + 150
  const pos = {}
  for (const n of nodes) pos[n.id] = { x: ax(ux[n.id]), y: B, w: 0 }
  const back = [
    ...segs.map(([u0, u1, t]) => jsx('path', { key: `b${t}`, d: `M${ax(u0).toFixed(1)} ${B}H${ax(u1).toFixed(1)}`, stroke: TIERS[t][1], strokeWidth: 3, strokeLinecap: 'round', opacity: 0.7,
                                                children: jsx('title', { children: TIERS[t][0] }) })),
    jsx('g', { key: 'legend', children: tierLegend(TIERS, H - 6) })
  ]
  const edge = (A, B0) => {
    const x0 = Math.min(A.x, B0.x), x1 = Math.max(A.x, B0.x), r = (x1 - x0) / 2
    return `M${x0.toFixed(1)} ${B}A${r.toFixed(1)} ${Math.min(r, B - 8).toFixed(1)} 0 0 1 ${x1.toFixed(1)} ${B}`
  }
  return { H, pos, back, edge, arc: true }
}

// A blast radius around one node: what depends on it to the left, by hop, each marked as cut off, with
// another route (it still reaches something serving that is up without the centre) or already broken;
// what it needs to the right. With nothing selected it starts on the node with the most dependents.
function layoutBurst({ nodes, edges }, sel) {
  const byId = Object.fromEntries(nodes.map(n => [n.id, n]))
  const c = byId[sel] ? sel : nodes.reduce((b, n) => (topoWalk(edges, n.id, true).size > topoWalk(edges, b.id, true).size ? n : b), nodes[0]).id
  const tree = up => {
    const kids = { [c]: [] }, depth = { [c]: 0 }, queue = [c]
    while (queue.length) {
      const n = queue.shift()
      kids[n] ||= []
      for (const e of edges) {
        const [here, next] = up ? [e.b, e.a] : [e.a, e.b]
        if (here === n && !(next in depth)) { depth[next] = depth[n] + 1; kids[n].push(next); queue.push(next) }
      }
    }
    return { kids, depth }
  }
  const left = tree(true), right = tree(false)
  const lvL = Math.max(...Object.values(left.depth)), lvR = Math.max(...Object.values(right.depth))
  // the centre box, then a column per hop on each side: a node `step` wide less a 22px gap
  const CW = 170, top = 40, GAP = 22
  const step = Math.min(220, (W - CW) / Math.max(1, lvL + lvR)), nw = step - GAP
  const cx = (W - CW - (lvL + lvR) * step) / 2 + CW / 2 + lvL * step
  const colX = (side, d) => (side < 0 ? cx - CW / 2 - d * step : cx + CW / 2 + (d - 1) * step + GAP)
  const leaves = (t, n) => (t.kids[n]?.length ? t.kids[n].reduce((a, m) => a + leaves(t, m), 0) : 1)
  const rows = Math.max(leaves(left, c), leaves(right, c), 3), H = rows * 36 + top + 40, cy = top + rows * 18
  const pos = {}
  const place = (t, n, y0, side) => {
    const h = leaves(t, n) * 36, d = t.depth[n], w = d ? nw : CW
    pos[n] = { x: d ? colX(side, d) : cx - CW / 2, y: y0 + h / 2 - BH / 2, w }
    let y = y0
    for (const m of t.kids[n]) { place(t, m, y, side); y += leaves(t, m) * 36 }
  }
  place(left, c, cy - leaves(left, c) * 18, -1)
  place(right, c, cy - leaves(right, c) * 18, 1)
  // a dependent still has a route if it reaches something serving (pool or cloud) that is up, avoiding the centre
  const reach = (n, seen = new Set()) => {
    if (n === c || seen.has(n)) return false
    seen.add(n)
    if (byId[n].tier >= 2) return !byId[n].broken
    return edges.some(e => e.a === n && reach(e.b, seen))
  }
  const fate = {}
  for (const id of Object.keys(left.depth)) if (id !== c) fate[id] = byId[id].broken ? 'was' : reach(id) ? 'alt' : 'cut'
  const count = f => Object.values(fate).filter(x => x === f).length
  const safe = nodes.filter(n => !(n.id in pos)).map(n => n.full)
  // a label over each hop's column: dependents to the left, what it needs to the right
  const hop = (side, d) => jsx('text', {
    key: `hop${side}${d}`, x: colX(side, d) + nw / 2, y: top - 14, textAnchor: 'middle', fontSize: 10.5, fill: 'currentColor', opacity: 0.5,
    children: side < 0 ? `${d === 1 ? 'Depends on it' : `${d} hops away`}` : `${d === 1 ? 'Needs' : `${d} hops away`}`
  })
  const back = [...Array.from({ length: lvL }, (_, i) => hop(-1, i + 1)), ...Array.from({ length: lvR }, (_, i) => hop(1, i + 1))]
  const head = `${byId[c].broken ? `${byId[c].full} is down now` : `If ${byId[c].full} went down`}: ${plural(count('cut'), 'dependent')} cut off, ${count('alt')} with another route${count('was') ? `, ${count('was')} already broken` : ''}`
  const foot = `${byId[sel] ? '' : 'Nothing selected, so this starts on the node with the most dependents. '}${safe.length ? `Unaffected: ${safe.join(', ')}.` : 'Every node is in its radius.'}`
  return { H, pos, back, edge: boxEdge, fate, centre: c, head, foot }
}

// The map as the path a request takes: entry point, the profile that took it, the router role it asked
// for, and what served it. Built from the window's traffic up to the map's time, each gateway's profile
// (a gateway named "profile:platform" is that profile's entry point) and the router's roles as now.
// `collapse` folds every profile into one orchestrator; `router` folds the roles into one router node.
const FLOW_EDGE = [
  ['entry', 'entry to profile', TONE.muted], ['alias', 'profile to role alias', TONE.orange], ['primary', 'role to its first choice', '#2ec4b6'],
  ['fallback', 'role on fallback', TONE.warn], ['pool', 'direct to pool', TONE.good], ['cloud', 'cloud direct', TONE.purple],
  ['harness', 'harness sampling', TONE.pink], ['fatal', 'bot down', TONE.bad]
]
const EDGE_COLOR = Object.fromEntries(FLOW_EDGE.map(([k, , c]) => [k, c]))
const EDGE_DASH = { fallback: '5 3', harness: '2 3', fatal: '5 3' }
const onFallback = r => r.candidates.length > 0 && r.resolved !== r.candidates[0].model

function flowGraph(data, { collapse = false, router = false } = {}) {
  const t = data.timeline || {}
  const at = data.at || Infinity, from = data.from || 0
  // the last two buckets before the map's time count as recent: they get a glow and moving traffic
  const recent = Math.min(at, data.generated_at || Infinity) - 2 * (t.step || 600)
  const roles = Object.fromEntries((data.router?.roles || []).map(r => [r.name, r]))
  const served = Object.fromEntries(data.served.map(s => [s.id, s]))
  const nodes = {}, edges = {}
  const node = (id, n) => (nodes[id] ||= { id, requests: 0, tokens: 0, ...n })
  const edge = (a, b, kind) => (edges[`${a}>${b}`] ||= { a, b, kind, requests: 0, errors: 0 })
  const entry = p => node(`e:${p}`, { label: p, full: `Entry point ${p}`, tier: 0, color: TONE.orange, gateways: [] })
  const profile = (host, p) => (collapse
    ? node('p:hermes', { label: 'Hermes orchestrator', full: 'Hermes orchestrator, every profile', tier: 1, color: TONE.purple })
    : node(`p:${host}:${p}`, { label: p, full: `${p} on ${host}`, tier: 1, group: host, color: TONE.purple }))
  const role = name => {
    const r = roles[name]
    if (router) return node('r:router', { label: 'Router', full: 'Model router', tier: 2, color: TONE.warn, shape: 'diamond' })
    const devs = roleDevices(data, r.resolved)
    const none = !devs.length && data.served.some(s => s.pool_device)
    return node(`r:${name}`, { label: name.replace(/^hermes\//, ''), full: `Role ${name}`, tier: 2, role: r,
                               color: none ? TONE.bad : onFallback(r) ? TONE.warn : TONE.good,
                               sub: none ? ['nothing in the pool serves it'] : onFallback(r) ? [`fallback ${devs.join(', ') || r.resolved}`] : [] })
  }
  const server = (id, pool) => {
    const s = served[id] || { id, pool_device: !!pool, live: false, models: [] }
    return node(`s:${id}`, { label: id, full: s.pool_device ? `${id}, pool device${s.live ? '' : ', offline'}` : `${id}, cloud provider`,
                             tier: s.pool_device ? 3 : 4, color: s.pool_device ? (s.live ? TONE.good : TONE.bad) : TONE.info,
                             broken: s.pool_device && !s.live, sub: s.models.map(m => m.name) })
  }
  for (const g of data.gateways) {
    const i = g.platform.indexOf(':')
    const e = entry(g.platform.slice(i + 1))
    e.gateways.push(g)
    if (g.state !== 'connected' && e.state !== 'fatal') Object.assign(e, { state: g.state, color: STATE_COLOR[g.state] || TONE.warn, broken: true })
    if (i > 0) edge(e.id, profile(g.host, g.platform.slice(0, i)).id, g.state === 'connected' ? 'entry' : 'fatal')
  }
  for (const [ts, host, platform, prof, model, by, pool, n, errors, tokens] of t.flow || []) {
    if (ts > at || ts < from) continue
    const path = [entry(platform), profile(host, prof)]
    if (roles[model]) path.push(role(model))
    const s = server(by, pool)
    path.push(s)
    if (!pool && !s.sub.includes(model)) s.sub.push(model)
    for (const x of path) { x.requests += n; x.tokens += tokens; x.recent ||= ts >= recent }
    for (let k = 1; k < path.length; k++) {
      const A = path[k - 1], B = path[k]
      const e = edge(A.id, B.id, k === 1 ? 'entry' : B.tier === 2 ? 'alias' : A.tier === 2 ? (onFallback(roles[model]) ? 'fallback' : 'primary') : pool ? 'pool' : 'cloud')
      e.requests += n
      e.errors += errors
      e.recent ||= ts >= recent
    }
  }
  for (const [ts, host, model, n] of t.harness || []) {
    if (ts > at || ts < from) continue
    const x = node(`x:${host}`, { label: 'harness', full: `Harness on ${host}`, tier: 1, group: host, color: TONE.pink, shape: 'diamond' })
    x.requests += n
    x.recent ||= ts >= recent
    for (const d of roleDevices(data, model)) {
      const e = edge(x.id, server(d, true).id, 'harness')
      e.requests += n
      e.recent ||= ts >= recent
    }
  }
  // where each role sends work now, with or without traffic in the window
  // every role, served or not
  for (const r of Object.values(roles)) role(r.name)
  // the one router line to a device can carry roles on fallback and roles on their first choice:
  // it draws as fallback with the first choice underneath
  for (const r of Object.values(roles))
    for (const d of roleDevices(data, r.resolved)) {
      const e = edge(role(r.name).id, server(d, true).id, 'primary')
      ;(e.roles ||= { primary: [], fallback: [] })[onFallback(r) ? 'fallback' : 'primary'].push(r.name.replace(/^hermes\//, ''))
      e.kind = e.roles.fallback.length ? 'fallback' : 'primary'
      e.under = e.roles.fallback.length && e.roles.primary.length ? 'primary' : undefined
    }
  const r = nodes['r:router']
  if (r && !r.requests) Object.assign(r, { state: 'no requests', color: TONE.muted, full: 'Model router, no requests through it in the window' })
  const list = Object.values(nodes).sort((a, b) => a.tier - b.tier || (a.group || '').localeCompare(b.group || '') || b.requests - a.requests || a.label.localeCompare(b.label))
  // harness samples are not requests, so they stay off the request scale
  const most = Math.max(1, ...list.map(n => n.tokens)), busiest = Math.max(1, ...Object.values(edges).filter(e => e.kind !== 'harness').map(e => e.requests))
  for (const n of list) n.size = Math.sqrt(n.tokens / most)
  const name = Object.fromEntries(FLOW_EDGE.map(([k, text]) => [k, text]))
  return {
    nodes: list,
    edges: Object.values(edges).map(e => ({
      ...e, color: EDGE_COLOR[e.kind], dash: EDGE_DASH[e.kind] || (e.requests ? undefined : '4 3'),
      w: e.kind === 'harness' ? 1.5 : e.requests ? 1 + 5 * Math.sqrt(e.requests / busiest) : 1.2,
      title: `${nodes[e.a].full} to ${nodes[e.b].full}: ${e.requests ? `${e.requests} ${e.kind === 'harness' ? 'samples' : 'requests'}${e.errors ? `, ${e.errors} failed` : ''}` : `${name[e.kind]}, no requests in the window`}`
             + (e.under ? `\nOn fallback: ${e.roles.fallback.join(', ')}\nFirst choice: ${e.roles.primary.join(', ')}` : '')
    }))
  }
}

// What is wrong at the map's time, each with the nodes it points at in either graph (an id ending in
// ":" matches every node starting with it), when it started, from the timeline's states, and for the
// issue panel: why (`cause`), what was seen (`facts`), where to look (`go`, [label, tab, sel]) and a
// command that usually fixes it (`cmd`). A role on fallback with no requests is `quiet` and goes last.
function topoIssues(data, now) {
  const states = data.timeline?.states || {}
  const since = key => states[key]?.filter(s => s[0] <= now).at(-1)?.[0]
  const dur = key => (since(key) ? ` for ${fmtDur(now - since(key))}` : '')
  const out = []
  for (const g of data.gateways.filter(g => g.state !== 'connected')) {
    const key = `g:${g.host}:${g.platform}`
    out.push({ key, tone: 'bad', since: since(key), nodes: [key, `e:${g.platform.split(':').at(-1)}`], text: `${g.platform} on ${g.host} ${g.state}${dur(key)}`,
               cause: `The ${g.platform} gateway on ${g.host} is ${g.state}, so messages sent to it are not answered.`,
               facts: g.error ? [`Last error: ${g.error}`] : [],
               go: [['See its traffic', 'flow', `entry:${g.platform.split(':').at(-1)}`]],
               cmd: { text: '~/.hermes/hermes-agent/.venv/bin/hermes -p default gateway restart', note: `Run on ${g.host} once the cause is fixed. It restarts the gateway for every profile.` } })
  }
  for (const s of data.served.filter(s => s.pool_device && !s.live))
    out.push({ key: `s:${s.id}`, tone: 'bad', since: since(`s:${s.id}`), nodes: [`s:${s.id}`], text: `${s.id} offline${dur(`s:${s.id}`)}`,
               cause: `${s.id} is a pool device that is not answering. Roles whose model is loaded only there fall back or stop.`,
               facts: [], go: [['See its traffic', 'flow', `served:${s.id}`]] })
  for (const h of data.hosts.filter(h => h.stale)) {
    const up = data.served.some(s => s.id === h.device && s.live)
    out.push({ key: `h:${h.host}`, tone: 'warn', since: since(`h:${h.host}`), nodes: [`h:${h.host}`, `p:${h.host}:`, `x:${h.host}`], text: `${h.host} not reporting${dur(`h:${h.host}`)}`,
               cause: up ? `${h.device} is still serving in the pool, so ${h.host} is up and its metrics forwarder has stopped sending.`
                         : `Neither ${h.host} nor its pool device is answering. It may be off, asleep or off the network.`,
               facts: h.last_seen ? [`Last report ${fmtDur(now - h.last_seen)} ago`] : [],
               go: [['Host checks', 'conformance', null]],
               cmd: up ? { text: 'launchctl kickstart -k gui/$UID/ai.hermes.metricsfwd', note: `Run on ${h.host}.` } : undefined })
  }
  const asked = r => (data.timeline?.flow || []).filter(f => f[4] === r.name && f[0] <= now).reduce((a, f) => a + f[7], 0)
  // with no candidate loaded on any pool device the role is not on fallback: nothing serves it
  const pool = data.served.some(s => s.pool_device)
  for (const r of (data.router?.roles || []).filter(r => pool && !roleDevices(data, r.resolved).length)) {
    const n = asked(r)
    out.push({ key: `r:${r.name}`, tone: n ? 'bad' : 'muted', quiet: !n, nodes: [`r:${r.name}`, 'r:router'],
               text: `${r.name.replace(/^hermes\//, '')}: nothing in the pool serves it, ${plural(n, 'request')}`,
               cause: `None of the role's candidates (${r.candidates.map(c => c.model).join(', ')}) is loaded on a pool device now, so the router has nowhere to send its requests. Load one of them on a pool device, or point the role at a model that is loaded.`,
               facts: r.candidates.map(c => `${c.model}: not loaded`),
               go: n ? [['See its traffic', 'flow', `model:${r.name}`]] : [] })
  }
  // roles falling back to the same place are one issue
  const fallback = {}
  for (const r of (data.router?.roles || []).filter(onFallback)) {
    const on = roleDevices(data, r.resolved)
    if (pool && !on.length) continue
    ;(fallback[on.join(', ') || r.resolved] ||= []).push({ r, on, n: asked(r) })
  }
  for (const [to, list] of Object.entries(fallback)) {
    const n = list.reduce((a, x) => a + x.n, 0), one = list.length === 1
    const short = r => r.name.replace(/^hermes\//, '')
    out.push({ key: `r:${list.map(x => x.r.name).join('+')}`, tone: n ? 'warn' : 'muted', quiet: !n, roles: list.length, to,
               nodes: [...list.map(x => `r:${x.r.name}`), 'r:router', ...list[0].on.map(d => `s:${d}`)],
               text: one ? `${short(list[0].r)} on fallback to ${to}, ${plural(n, 'request')}`
                         : `${list.length} roles on fallback to ${to} (${list.map(x => short(x.r)).join(', ')}), ${plural(n, 'request')}`,
               cause: list.map(({ r }) => {
                 const first = r.candidates[0]
                 return `${one ? '' : `${short(r)}: `}${first.model}, the role's first choice, ${!first.live ? 'is not loaded on any pool device' : 'is loaded but does not fit'}, so the router sends it to ${r.resolved}.`
               }).join(' ') + (one ? ` Load ${list[0].r.candidates[0].model} on a pool device, or put ${list[0].r.resolved} first if it is the better choice now.`
                                   : ' Load each first choice on a pool device, or put what serves it now first if that is the better choice.'),
               facts: list.flatMap(({ r }) => r.candidates.map(c => `${one ? '' : `${short(r)}, `}${c.model}: ${c.model === r.resolved ? 'serving now' : !c.live ? 'not loaded' : c.fit ? 'loaded' : 'loaded, does not fit'}`)),
               go: list.filter(x => x.n).map(x => [one ? 'See its traffic' : `See ${short(x.r)} traffic`, 'flow', `model:${x.r.name}`]) })
  }
  for (const h of data.hosts.filter(h => h.drift?.length))
    out.push({ key: `d:${h.host}`, tone: 'drift', since: since(`d:${h.host}`), nodes: [`h:${h.host}`, `p:${h.host}:`, `x:${h.host}`], text: `${h.host} drifted: ${h.drift.join(', ')} failing`,
               cause: `${h.host} no longer matches the fleet standard on ${plural(h.drift.length, 'check')}.${h.stale ? ` It has stopped reporting, so these are the results of its last report.` : ''}`,
               facts: [], go: h.drift.map(rule => [`Open ${rule}`, 'conformance', `host:${h.host}:${rule}`]) })
  return out.sort((a, b) => !!a.quiet - !!b.quiet)
}
const issueHits = (issue, id) => issue.nodes.some(x => x === id || (x.endsWith(':') && id.startsWith(x)))

// each swatch a fixed gap after the last label, measured in the page's font where there is a canvas,
// else at about 5.6px a character at this size
let legendCtx
const legendWidth = text => {
  try {
    legendCtx ||= document.createElement('canvas').getContext('2d')
    legendCtx.font = `10px ${getComputedStyle(document.body).fontFamily}`
    return legendCtx.measureText(text).width
  } catch { return text.length * 5.6 }
}
const flowLegend = (y, edges) => {
  const kinds = [...FLOW_EDGE.filter(([k]) => edges.some(e => e.kind === k || e.under === k)),
                 ...(edges.some(e => e.errors && e.kind !== 'fatal') ? [['failed', 'failed calls', TONE.bad]] : [])]
  return kinds.map(([k, text, color], i) => {
  const x = kinds.slice(0, i).reduce((a, f) => a + 34 + legendWidth(f[1]), 0)
  return jsxs('g', { key: `f${k}`, children: [
    jsx('path', { d: `M${x} ${y}h14`, stroke: color, strokeWidth: 2, strokeDasharray: EDGE_DASH[k] }),
    jsx('text', { x: x + 18, y: y + 4, fontSize: 10, fill: 'currentColor', opacity: 0.6, children: text })
  ] })
  })
}

// Circles: entry points down the left, a circle per host holding its profiles and harness, the router
// as one diamond, then the model pool and the cloud each as a circle of its members. Node area is
// token volume.
function layoutCircles({ nodes, edges }) {
  const pos = {}, back = []
  const rad = n => (n.tier === 0 ? 6 : n.shape === 'diamond' ? 11 : 7 + 15 * (n.size || 0))
  // members spaced round a ring so their labels clear each other; the circle's radius
  const ringOf = list => {
    const big = Math.max(0, ...list.map(rad))
    const r = list.length < 2 ? 0 : 84 / (2 * Math.sin(Math.PI / list.length))
    return { r, R: r + big + 26 }
  }
  const ring = (list, cx, cy, label, key) => {
    const { r, R } = ringOf(list)
    list.forEach((n, i) => {
      const a = -Math.PI / 2 + (2 * Math.PI * i) / list.length
      pos[n.id] = { x: cx + r * Math.cos(a), y: cy + r * Math.sin(a) - 4, w: 0, r: rad(n), shape: n.shape || 'circle' }
    })
    if (label) back.push(jsxs('g', { key, children: [
      jsx('circle', { cx, cy, r: R, fill: 'currentColor', fillOpacity: 0.025, stroke: 'currentColor', strokeOpacity: 0.25, strokeDasharray: '4 4' }),
      jsx('text', { x: cx, y: cy - R + 14, textAnchor: 'middle', fontSize: 10.5, fill: 'currentColor', opacity: 0.6, children: label })
    ] }))
    return R
  }
  const by = t => nodes.filter(n => n.tier === t)
  const groups = []
  for (const n of [...by(1), ...by(2)]) {
    const k = n.tier === 2 ? 'router' : n.group || n.id
    ;(groups.find(g => g[0] === k) || groups[groups.push([k, []]) - 1])[1].push(n)
  }
  const hosts = groups.filter(([k]) => k !== 'router'), routed = groups.find(([k]) => k === 'router')?.[1] || []
  const cols = [
    by(0).length * 44,
    hosts.reduce((a, [k, l]) => a + 2 * (l[0].group ? ringOf(l).R : rad(l[0]) + 20) + 16, 0),
    routed.length ? 2 * ringOf(routed).R : 0,
    [by(3), by(4)].reduce((a, l) => a + (l.length ? 2 * ringOf(l).R + 16 : 0), 0)
  ]
  const H = Math.max(240, ...cols) + 40
  // columns move right, and the map widens, when a ring is too big for its usual place
  const hostR = Math.max(0, ...hosts.map(([, l]) => (l[0].group ? ringOf(l).R : rad(l[0]) + 20)))
  const routerR = routed.length > 1 ? ringOf(routed).R : 20
  const poolR = Math.max(0, ...[by(3), by(4)].filter(l => l.length).map(l => ringOf(l).R))
  const routerX = Math.max(470, 260 + hostR + 30 + routerR), poolX = Math.max(720, routerX + routerR + 30 + poolR)
  by(0).forEach((n, i) => { pos[n.id] = { x: 30, y: (H - 40 - cols[0]) / 2 + 22 + i * 44, w: 0, r: rad(n), shape: 'circle', side: true } })
  let y = (H - 40 - cols[1]) / 2
  for (const [, list] of hosts) {
    const R = list[0].group ? ringOf(list).R : rad(list[0]) + 20
    if (list[0].group) ring(list, 260, y + R, list[0].group, `h${list[0].group}`)
    else pos[list[0].id] = { x: 260, y: y + R, w: 0, r: rad(list[0]), shape: 'circle' }
    y += 2 * R + 16
  }
  if (routed.length) ring(routed, routerX, (H - 40) / 2, routed.length > 1 ? 'Router' : null, 'router')
  y = (H - 40 - cols[3]) / 2
  for (const [list, label] of [[by(3), 'Model pool'], [by(4), 'Cloud']]) {
    if (!list.length) continue
    const R = ring(list, poolX, y + ringOf(list).R, label, label)
    y += 2 * R + 16
  }
  back.push(jsx('g', { key: 'legend', children: flowLegend(H - 8, edges) }))
  const edge = (A, B) => {
    const mx = (A.x + B.x) / 2, my = (A.y + B.y) / 2 - Math.abs(B.x - A.x) * 0.08
    return `M${A.x.toFixed(1)} ${A.y.toFixed(1)}Q${mx.toFixed(1)} ${my.toFixed(1)} ${B.x.toFixed(1)} ${B.y.toFixed(1)}`
  }
  return { H, W: Math.max(W, poolX + 180), pos, back, edge, grouped: true }
}

// Columns as a switchboard: entry points, the profiles taking them by host with each host's harness
// under them, the router's roles inside one router box, then the pool devices and cloud providers
// with the models each has loaded or served.
function layoutSwitchboard({ nodes, edges }) {
  const pos = {}, back = []
  // a wider gap after the entry points, where every entry edge fans out, and room for a role's fallback line
  const col = [[4, 92], [128, 144], [298, 172], [496, 210], [732, 164]]
  const LINE = 12, HEAD = 18
  const region = (x, w, y, h, label, key) => back.push(jsxs('g', { key, children: [
    jsx('rect', { x: x - 6, y, width: w + 12, height: h, rx: 6, fill: 'currentColor', fillOpacity: 0.03, stroke: 'currentColor', strokeOpacity: 0.12 }),
    jsx('text', { x, y: y + 13, fontSize: 10.5, fill: 'currentColor', opacity: 0.55, children: label })
  ] }))
  const stack = (c, groups) => {
    const [x, w] = col[c]
    let y = 22
    for (const [label, list] of groups) {
      if (!list.length) continue
      const y0 = y
      if (label) y += HEAD
      for (const n of list) {
        const sub = n.sub || []
        const h = BH + sub.length * LINE
        pos[n.id] = { x, y: y + 4, w, h, sub, shape: n.shape === 'diamond' ? 'diamond-box' : undefined }
        y += h + 8
      }
      if (label) { region(x, w, y0, y - y0 + 2, label, `r${c}:${label}`); y += 12 }
    }
    return y
  }
  const by = t => nodes.filter(n => n.tier === t)
  const prof = by(1).filter(n => n.shape !== 'diamond'), harness = by(1).filter(n => n.shape === 'diamond')
  const hosts = [...new Set(prof.map(n => n.group))]
  const heights = [
    stack(0, [[null, by(0)]]),
    stack(1, [...hosts.map(h => [h || null, prof.filter(n => n.group === h)]), ['Harness', harness]]),
    stack(2, [['Router', by(2)]]),
    stack(3, [[null, by(3)]]),
    stack(4, [[null, by(4)]])
  ]
  // a lane under the columns for lines that skip one, so a profile's cloud-direct line does not cross the router and pool
  const top = Math.max(...heights), lane = top + 6, H = top + 36
  col.forEach(([x], c) => {
    const dy = (top - heights[c]) / 2
    if (dy <= 0) return
    for (const p of Object.values(pos)) if (p.x === x) p.y += dy
    back.forEach((r, i) => { if (r.key.startsWith(`r${c}:`)) back[i] = jsx('g', { key: r.key, transform: `translate(0 ${dy})`, children: r }) })
  })
  ;['Entry', 'Gateways', 'Role aliases', 'Model pool', 'Cloud'].forEach((t, c) => back.push(jsx('text', { key: `h${c}`, x: col[c][0], y: 10, fontSize: 11, fontWeight: 600, fill: 'currentColor', opacity: 0.6, children: t })))
  back.push(jsx('g', { key: 'legend', children: flowLegend(H - 6, edges) }))
  const colOf = p => col.findIndex(([x]) => x === p.x)
  const edge = (A, B) => {
    const [L, R] = A.x <= B.x ? [A, B] : [B, A]
    const a = colOf(L), b = colOf(R)
    if (b - a < 2) return boxEdge(A, B)
    // down the gap after its own column, along the lane, and up the gap before its target's
    const ga = (col[a][0] + col[a][1] + col[a + 1][0]) / 2, gb = (col[b - 1][0] + col[b - 1][1] + R.x) / 2
    const y0 = L.y + BH / 2, y1 = R.y + BH / 2, r = 6
    return `M${L.x + L.w} ${y0}H${ga - r}Q${ga} ${y0} ${ga} ${y0 + r}V${lane - r}Q${ga} ${lane} ${ga + r} ${lane}` +
           `H${gb - r}Q${gb} ${lane} ${gb} ${lane - r}V${y1 + r}Q${gb} ${y1} ${gb + r} ${y1}H${R.x}`
  }
  return { H, pos, back, edge, grouped: true }
}

// Area grouping and the switchboard draw the request path; the rest draw hosts and what served them.
const FLOW_LAYOUT = { area: { router: true }, switchboard: {} }
const LAYOUTS = [['columns', 'Columns', layoutColumns], ['area', 'Area grouping', layoutCircles], ['radial', 'Segmented radial', layoutRadial],
                 ['switchboard', 'Router switchboard', layoutSwitchboard], ['arc', 'Arc diagram', layoutArc], ['burst', 'Blast radius', layoutBurst]]
const FATE = { cut: TONE.bad, alt: TONE.warn, was: TONE.muted }

function TopologyMap({ g, sel, onSelect, layout = 'area', traffic, live }) {
  const [tip, setTip] = useState(null)
  if (!g.nodes.length) return null
  const still = window.matchMedia?.('(prefers-reduced-motion: reduce)').matches
  const L = (LAYOUTS.find(l => l[0] === layout) || LAYOUTS[0])[2](g, sel)
  const { pos } = L
  const rel = sel && !L.fate && pos[sel] ? new Set([...topoWalk(g.edges, sel, false), ...topoWalk(g.edges, sel, true)]) : null
  const pick = id => { haptic('tap'); onSelect(id === sel ? null : id) }
  // drawn hover text, on the window so the scrolling map does not clip it: at the pointer, or under a focused node
  const show = (e, lines) => {
    const r = e.clientX == null ? e.currentTarget.getBoundingClientRect() : null
    setTip({ x: r ? r.left + r.width / 2 : e.clientX, y: r ? r.bottom : e.clientY + 8, lines })
  }
  const hover = lines => ({ onMouseEnter: e => show(e, lines), onMouseLeave: () => setTip(null) })
  const nodeEl = n => {
    const p = pos[n.id]
    if (!p) return null
    const on = n.id === sel, dim = rel && !rel.has(n.id), f = L.fate?.[n.id]
    const lines = f ? [`${n.full}: ${{ cut: 'cut off', alt: 'has another route', was: 'already broken' }[f]}`]
                    : [n.full + (n.requests ? `, ${plural(n.requests, n.id.startsWith('x:') ? 'sample' : 'request')}` : ''), ...(p.sub || [])]
    const label = L.grouped ? n.label : n.long || n.label
    const stroke = on || n.id === L.centre ? 'var(--ui-accent)' : f ? FATE[f] : n.drift ? DRIFT : 'currentColor'
    const common = {
      key: n.id, role: 'button', tabIndex: 0, 'aria-label': n.full, 'aria-pressed': on,
      style: { cursor: 'pointer', outline: 'none', opacity: dim ? 0.35 : 1 },
      onClick: () => pick(n.id),
      onKeyDown: e => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); pick(n.id) } },
      ...hover(lines), onFocus: e => show(e, lines), onBlur: () => setTip(null)
    }
    // a circle or diamond in Area grouping: label under it, or beside it for an entry point
    if (p.r) {
      const { x, y, r } = p
      const lab = p.side ? { x: x + r + 6, y: y + 4 } : { x, y: y + r + 12, textAnchor: 'middle' }
      return jsxs('g', { ...common, children: [
        traffic && n.recent ? jsx('circle', { key: 'glow', className: 'ops-pulse', cx: x, cy: y, r: r + 6, fill: n.color, opacity: 0.2 }) : null,
        p.shape === 'diamond'
          ? jsx('path', { key: 'n', d: `M${x} ${y - r}L${x + r} ${y}L${x} ${y + r}L${x - r} ${y}Z`, fill: 'var(--ui-bg, #0d1117)', stroke: on ? 'var(--ui-accent)' : n.color, strokeWidth: on ? 2.5 : 1.5 })
          : jsx('circle', { key: 'n', cx: x, cy: y, r, fill: 'var(--ui-bg, #0d1117)', stroke: on ? 'var(--ui-accent)' : n.color, strokeWidth: on ? 2.5 : 1.5 }),
        p.shape === 'diamond' ? null : jsx('circle', { key: 'f', cx: x, cy: y, r, fill: n.color, fillOpacity: 0.3, pointerEvents: 'none' }),
        n.broken ? jsx('path', { key: 'x', d: `M${x - 4} ${y - 4}l8 8M${x + 4} ${y - 4}l-8 8`, stroke: TONE.bad, strokeWidth: 2 }) : null,
        jsx('text', { key: 't', ...lab, fontSize: 10.5, fill: 'currentColor', children: label.length > 22 ? `${label.slice(0, 21)}...` : label }),
        n.state ? jsx('text', { key: 's', ...lab, y: lab.y + 11, fontSize: 9.5, fill: n.color, children: n.state }) : null
      ] })
    }
    if (L.arc) {
      return jsxs('g', { ...common, children: [
        jsx('circle', { cx: p.x, cy: p.y, r: on ? 7 : 5.5, fill: n.color, stroke: on ? 'var(--ui-accent)' : n.drift ? DRIFT : 'none', strokeWidth: 2, strokeDasharray: n.drift && !on ? '3 2' : undefined }),
        jsx('text', { x: p.x + 4, y: p.y + 14, fontSize: 11, fill: 'currentColor', transform: `rotate(40 ${p.x + 4} ${p.y + 14})`, children: label.length > 26 ? `${label.slice(0, 25)}...` : label })
      ] })
    }
    // about 6.6px a character at the label's size, 5.6px at the sub lines'
    const max = Math.floor((p.w - 26) / 6.6), subMax = Math.floor((p.w - 26) / 5.6)
    const cy = p.y + BH / 2
    return jsxs('g', { ...common, children: [
      jsx('rect', { key: 'b', x: p.x, y: p.y, width: p.w, height: p.h || BH, rx: 4, fill: 'var(--ui-bg, #0d1117)', stroke,
                    strokeOpacity: on || f || n.drift || n.id === L.centre ? 1 : 0.3, strokeWidth: on || n.id === L.centre ? 2 : 1,
                    strokeDasharray: (n.drift && !on && !f) || f === 'was' ? '4 3' : undefined }),
      p.shape === 'diamond-box'
        ? jsx('path', { key: 'd', d: `M${p.x + 11} ${cy - 5}l5 5l-5 5l-5 -5Z`, fill: n.color })
        : jsx('circle', { key: 'd', cx: p.x + 11, cy, r: 4, fill: n.color }),
      traffic && n.recent ? jsx('circle', { key: 'h', className: 'ops-pulse', cx: p.x + 11, cy, r: 6.5, fill: n.color, opacity: 0.35, pointerEvents: 'none' }) : null,
      jsx('text', { key: 't', x: p.x + 21, y: cy + 4, fill: n.broken ? TONE.bad : 'currentColor', fontSize: 12, children: label.length > max ? `${label.slice(0, max - 1)}...` : label }),
      ...(p.sub || []).map((t, i) => jsx('text', { key: `s${i}`, x: p.x + 21, y: p.y + BH + 8 + i * 12, fill: n.tier === 2 ? TONE.warn : 'currentColor', opacity: n.tier === 2 ? 1 : 0.6, fontSize: 10,
                                                  children: t.length > subMax ? `${t.slice(0, subMax - 1)}...` : t }))
    ] })
  }
  const H = L.H
  return jsxs('div', {
    className: 'space-y-1',
    children: [
      L.head ? jsx('div', { className: 'text-xs font-medium', children: L.head }) : null,
      jsx('div', {
        className: 'overflow-x-auto',
    ref: dragScroll,
        children: jsxs('svg', {
          viewBox: `-2 -2 ${(L.W || W) + 4} ${H + 4}`,
          role: 'img',
          'aria-label': `Topology, ${(LAYOUTS.find(l => l[0] === layout) || LAYOUTS[0])[1].toLowerCase()}`,
          style: { width: '100%', minWidth: '36rem', height: 'auto', display: 'block' },
          children: [
            traffic ? jsx('style', { key: 'motion', children: MOTION_CSS }) : null,
            // a faint twinkling field while the map follows now, spread by the golden angle so it holds still between renders
            ...(traffic && live ? Array.from({ length: 70 }, (_, i) => jsx('circle', {
              key: `star${i}`, className: 'ops-twinkle', cx: ((i * 0.618034) % 1) * (L.W || W), cy: ((i * 0.381966 + i * i * 0.0137) % 1) * H,
              r: i % 3 ? 0.8 : 1.2, fill: 'currentColor', opacity: 0.3, pointerEvents: 'none', style: { animationDelay: `${-(i * 0.37) % 4}s` }
            })) : []),
            ...L.back,
            ...g.edges.filter(e => pos[e.a] && pos[e.b]).map(e => {
              const d = L.edge(pos[e.a], pos[e.b])
              // a line with no requests in the window is faint unless it marks a bot down
              const opacity = rel ? (rel.has(e.a) && rel.has(e.b) ? 0.7 : 0.08) : !e.requests && e.kind !== 'fatal' ? 0.25 : 0.55
              return jsxs('g', { key: `${e.a}>${e.b}`, children: [
                e.under ? jsx('path', { d, fill: 'none', stroke: EDGE_COLOR[e.under], strokeWidth: e.w, opacity }) : null,
                jsx('path', {
                  d, fill: 'none', stroke: e.color, strokeWidth: e.w, strokeDasharray: e.dash, opacity
                }),
                // the failed share of a line's requests drawn red along its middle
                e.errors && e.requests ? jsx('path', { d, fill: 'none', stroke: TONE.bad, strokeWidth: Math.max(1.5, e.w * e.errors / e.requests), opacity: Math.min(1, opacity + 0.3) }) : null,
                // thin lines are hard to hover: a wider invisible stroke takes the hover
                jsx('path', { d, fill: 'none', stroke: 'transparent', strokeWidth: Math.max(10, e.w + 6), ...hover(e.title.split('\n')) }),
                // dashes and a dot with a short trail running along each edge with traffic in the last two buckets
                ...(traffic && e.recent && (!rel || (rel.has(e.a) && rel.has(e.b))) ? [
                  jsx('path', { key: 'flow', className: 'ops-flow', d, fill: 'none', stroke: e.color, strokeWidth: Math.max(1, e.w / 2), strokeDasharray: '3 13', strokeLinecap: 'round', opacity: 0.9, pointerEvents: 'none' }),
                  // the dots move by SMIL, which the reduced-motion rule in MOTION_CSS does not reach
                  ...(still ? [] : [0, 1, 2, 3]).map(k => jsx('circle', { key: `dot${k}`, r: 2.5 - k * 0.5, fill: e.color, opacity: 1 - k * 0.25, pointerEvents: 'none',
                                                          children: jsx('animateMotion', { dur: `${(3 - e.w / 3).toFixed(1)}s`, begin: `${k * 0.06}s`, repeatCount: 'indefinite', path: d }) }))
                ] : [])
              ] })
            }),
            ...g.nodes.map(nodeEl)
          ]
        })
      }),
      L.foot ? muted(L.foot) : null,
      tip ? jsx('div', {
        style: { position: 'fixed', left: 0, right: 0, top: 0, height: 0, zIndex: 50 },
        children: hoverTip((100 * tip.x) / window.innerWidth, tip.y + 8,
                           tip.lines.map((l, i) => jsx('div', { key: i, style: i ? undefined : { fontWeight: 500 }, children: l })))
      }) : null
    ]
  })
}


function BlastRadius({ data, sel, name }) {
  const b = blastRadius(data, sel)
  return jsxs('div', {
    className: 'space-y-0.5 text-xs',
    children: [
      jsx('div', { className: 'font-medium', children: `If ${name} went down: ${plural(b.cut, 'dependent')} cut off, ${b.alt} with another route` }),
      ...b.lines.map((l, i) => jsx('div', { key: i, style: { color: l.cut ? '#f85149' : '#d29922' }, children: l.text })),
      b.lines.length ? null : muted('Nothing in this window depended on it.'),
      b.alt ? muted('Another route means the same model was also served elsewhere in this window, not that the router is set to fail over.') : null
    ]
  })
}

function TopologyDetail({ data, sel, g }) {
  const { hours } = data
  const line = (k, text, color) => jsx('div', { key: k, style: color ? { color } : undefined, children: text })
  const sends = list => list.map(e => line(`${e.host}>${e.served}`, `${e.host} to ${e.served}: ${e.requests} requests${e.errors ? `, ${e.errors} failed` : ''}`, e.errors ? '#f85149' : undefined))
  const [kind, ...rest] = sel.split(':')
  const id = rest.join(':')
  // an entry point, profile, role or harness from the request-path layouts: its volume and every edge it has
  if ('eprx'.includes(kind)) {
    const n = g.nodes.find(x => x.id === sel)
    if (!n) return null
    const r = n.role
    return jsxs('div', { className: 'space-y-1 text-xs', children: [
      line('h', `${n.full}: ${n.requests ? `${plural(n.requests, kind === 'x' ? 'sample' : 'request')}${n.tokens ? `, ${fmtTok(n.tokens)} tokens` : ''}` : 'no requests'} ${data.at ? 'up to then' : `in the last ${hours}h`}`),
      ...(n.gateways || []).map(gw => line(`${gw.host}:${gw.platform}`, `Gateway ${gw.platform} on ${gw.host}: ${gw.state}${gw.ts ? `, reported ${fmtAgo(gw.ts)}` : ''}`, STATE_COLOR[gw.state] || TONE.warn)),
      r ? line('r', `Resolves to ${r.resolved || 'nothing'}${onFallback(r) ? `, not its first choice ${r.candidates[0].model}` : ''}${r.strict ? '; strict' : ''}`, onFallback(r) ? TONE.warn : undefined) : null,
      ...g.edges.filter(e => e.a === sel || e.b === sel).map(e => line(`${e.a}>${e.b}`, e.title, e.kind === 'fatal' ? TONE.bad : e.kind === 'fallback' ? TONE.warn : e.errors ? TONE.bad : undefined)),
      kind === 'e' ? jsx(GoLink, { key: 'f', tab: 'flow', sel: `entry:${id}`, hours, children: 'See its traffic' }) : null
    ] })
  }
  if (kind === 'g') {
    const g = data.gateways.find(x => `${x.host}:${x.platform}` === id)
    return g ? jsxs('div', { className: 'space-y-1 text-xs', children: [
      line('h', `Gateway ${g.platform} on ${g.host}: ${g.state}${g.ts ? `, reported ${fmtAgo(g.ts)}` : ''}`, STATE_COLOR[g.state] || '#d29922'),
      g.error ? line('e', g.error) : null,
      jsx(GoLink, { key: 'f', tab: 'flow', sel: `entry:${g.platform.split(':').at(-1)}`, hours, children: 'See its traffic' }),
      jsx(BlastRadius, { key: 'b', data, sel, name: `${g.platform} on ${g.host}` })
    ] }) : null
  }
  if (kind === 'h') {
    const h = data.hosts.find(x => x.host === id)
    if (!h) return null
    return jsxs('div', { className: 'space-y-1 text-xs', children: [
      data.at ? line('r', `${h.host} at that time: ${h.stale ? 'not reporting' : 'reporting'}. Load and memory below are from now.`, h.stale ? '#d29922' : undefined) : null,
      line('h', h.stats ? `${h.host}: load ${h.cpu_load?.toFixed(1) ?? '-'} · mem ${h.mem_used_gb?.toFixed(0) ?? '-'} / ${h.mem_total_gb?.toFixed(0) ?? '-'} GB · host stats ${fmtAgo(h.last_seen)}`
                        : `${h.host}: no host stats in the last day`, h.stale ? '#d29922' : undefined),
      h.device ? line('d', `Pool device: ${h.device}`) : null,
      h.drift?.length ? jsxs('div', { key: 'c', className: 'flex flex-wrap items-baseline gap-x-2', children: [
        jsx('span', { style: { color: DRIFT }, children: 'Drifted from its standard, failing:' }),
        ...h.drift.map(rule => jsx(GoLink, { key: rule, tab: 'conformance', sel: `host:${h.host}:${rule}`, children: rule }))
      ] }) : null,
      ...data.gateways.filter(g => g.host === h.host).map(g => line(g.platform, `Gateway ${g.platform}: ${g.state}`, STATE_COLOR[g.state] || '#d29922')),
      ...sends(data.edges.filter(e => e.host === h.host)),
      jsxs('div', { key: 'go', className: 'flex flex-wrap gap-x-3', children: [
        h.device ? jsx(GoLink, { tab: 'flow', sel: `served:${h.device}`, hours, children: 'See its traffic' }) : null
      ] }),
      jsx(BlastRadius, { key: 'b', data, sel, name: h.host })
    ] })
  }
  const s = data.served.find(x => x.id === id)
  if (!s) return null
  return jsxs('div', { className: 'space-y-1 text-xs', children: [
    line('h', s.pool_device ? `${s.id}: pool device, ${s.live ? 'in the pool now' : 'not in the pool now'}` : `${s.id}: cloud provider`),
    s.models.length ? jsx('div', { className: 'flex flex-wrap gap-1', children: s.models.map(m => jsx(Badge, { key: m.name, variant: 'outline', children: m.count > 1 ? `${m.name} x${m.count}` : m.name })) }) : null,
    ...sends(data.edges.filter(e => e.served === s.id)),
    jsx(GoLink, { key: 'f', tab: 'flow', sel: `served:${s.id}`, hours, children: 'See its traffic' }),
    jsx(BlastRadius, { key: 'b', data, sel, name: s.id })
  ] })
}

// The router's role aliases: candidates best first, the one each role resolves to now and where it is loaded.
function RouterRoles({ data, hours, onPick, open, onToggle }) {
  const r = data.router
  if (!r) return null
  const s = rolesSummary(data)
  return jsxs('div', {
    className: 'overflow-hidden rounded-md border border-(--ui-stroke-secondary)',
    children: [
      jsxs('button', {
        type: 'button',
        'aria-expanded': !!open,
        onClick: () => { haptic('tap'); onToggle?.() },
        className: 'flex w-full flex-wrap items-center gap-x-3 gap-y-1 px-3 py-1.5 text-left text-xs hover:bg-(--chrome-action-hover)',
        children: [
          jsx('span', { className: 'font-medium', children: 'Roles' }),
          r.roles.length ? jsx('span', { className: 'text-(--ui-text-quaternary)', children: `${s.n} aliases · green serving now, outlined loaded, faded not loaded` }) : null,
          s.fallback ? jsx('span', { className: 'text-(--ui-text-quaternary)', children: `${s.fallback} on fallback` }) : null,
          s.requests ? jsx('span', { className: 'text-(--ui-text-quaternary)', children: plural(s.requests, 'request') }) : null,
          jsx('span', { className: 'ml-auto text-(--ui-text-quaternary)', children: open ? 'Hide' : 'Show' })
        ]
      }),
      open ? jsx('div', {
        className: 'border-t border-(--ui-stroke-secondary) p-3',
        children: r.error
          ? muted(`No model router answered at ${r.url} (${r.error}). Without one, Blast radius uses observed traffic only. The router plugin's MODEL_ROUTER_URL setting points it elsewhere.`)
          : !r.roles.length
            ? muted('The model router has no role aliases.')
            : jsx('div', {
                className: 'grid gap-x-3 gap-y-1 text-xs',
                style: { gridTemplateColumns: 'minmax(8rem, auto) 1fr' },
                children: r.roles.flatMap(role => {
                  const on = roleDevices(data, role.resolved)
                  const n = (data.timeline?.flow || []).filter(f => f[4] === role.name).reduce((a, f) => a + f[7], 0)
                  return [
                    jsxs('div', { key: `${role.name}-n`, className: 'truncate font-medium', children: [
                      onPick
                        ? jsx('button', { type: 'button', className: 'hover:text-(--ui-accent) hover:underline', title: `Select ${role.name} on the map`, onClick: () => { haptic('tap'); onPick(`r:${role.name}`) }, children: role.name })
                        : jsx('span', { title: role.name, children: role.name }),
                      role.strict ? jsx('span', { className: 'ml-1 text-(--ui-text-quaternary)', children: 'strict' }) : null] }),
                    jsxs('div', {
                      key: `${role.name}-c`,
                      className: 'flex min-w-0 flex-wrap items-center gap-1',
                      children: [
                        ...role.candidates.map((c, i) =>
                          jsx(Badge, {
                            key: `${i}-${c.model}`,
                            variant: c.model === role.resolved && c.live ? 'success' : 'outline',
                            title: !c.live ? 'not loaded' : !c.fit ? 'loaded, but does not fit the role' : 'loaded',
                            style: c.live ? undefined : { opacity: 0.5 },
                            children: c.model
                          })),
                        jsx('span', { className: 'text-(--ui-text-quaternary)', children: on.length ? `on ${on.join(', ')}` : role.resolved ? `${role.resolved} is not in the pool now` : 'nothing to serve it' }),
                        n ? jsx(GoLink, { tab: 'flow', sel: `model:${role.name}`, hours, children: `See its traffic (${n})` }) : null
                      ]
                    })
                  ]
                })
              })
      }) : null
    ]
  })
}

// A gateway restart takes every platform on the host down and back at the same moment: one change,
// listing the platforms (`list`), at the node of the worst outcome.
const mergeRestarts = changes => {
  const out = []
  for (const c of changes) {
    const host = c.node.split(':')[1], at = c.text.indexOf(` on ${host} restarted`)
    const prev = out.at(-1)
    if (!c.node.startsWith('g:') || at < 0) out.push(c)
    else if (prev?.parts && prev.ts === c.ts && prev.host === host) prev.parts.push(c)
    else out.push({ ts: c.ts, host, parts: [c] })
  }
  return out.map(c => {
    if (!c.parts) return c
    if (c.parts.length === 1) return c.parts[0]
    const worst = c.parts.find(p => p.tone !== 'info') || c.parts[0]
    const list = c.parts.map(p => p.text.replace(` on ${c.host} restarted`, '').replace(' and went', ' went')).join(', ')
    return { ts: c.ts, node: worst.node, tone: worst.tone, text: `${c.host} gateway restarted`, list }
  })
}

// What changed on the map in the window, counted by kind, with where traffic concentrated and the
// newest changes as chips that replay the map to that moment with the node selected.
const CHANGE_KIND = [
  ['g', 'bad', 'gateway problem', 'gateway problems'], ['g', 'warn', 'gateway problem', 'gateway problems'],
  ['h', 'warn', 'host went quiet', 'hosts went quiet'], ['s', 'warn', 'device left the pool', 'devices left the pool'],
  ['d', 'drift', 'host drifted', 'hosts drifted'], ['g', 'info', 'gateway restart', 'gateway restarts'], ['', 'good', 'recovery', 'recoveries']
]
const CHANGE_COLOR = { bad: TONE.bad, warn: TONE.warn, good: TONE.good, info: TONE.info, drift: DRIFT, muted: TONE.muted }
const changeNode = c => (c.node.startsWith('d:') ? `h:${c.node.slice(2)}` : c.node)

// Counts first: gateway/host problems, each fallback group, drift, then how many role aliases.
// A role on fallback with no requests stays off the chips (`quiet`).
function chromeChips(issues, router) {
  const loud = issues.filter(i => !i.quiet)
  const fallbacks = loud.filter(i => i.tone === 'warn' && i.key.startsWith('r:'))
  const drifts = loud.filter(i => i.tone === 'drift')
  const skip = new Set([...fallbacks, ...drifts])
  const problems = loud.filter(i => !skip.has(i))
  const chips = []
  if (problems.length) chips.push({ key: 'issues', tone: 'bad', label: plural(problems.length, 'issue'), items: problems })
  for (const i of fallbacks) chips.push({ key: i.key, tone: 'warn', label: `${i.roles} fallback · ${i.to}`, items: [i] })
  if (drifts.length) chips.push({ key: 'drift', tone: 'drift', label: `${drifts.length} drift`, items: drifts })
  const n = router?.roles?.length || 0
  if (n) chips.push({ key: 'roles', tone: 'info', label: plural(n, 'role'), items: [] })
  return chips
}

function changePills(changes) {
  const counts = {}
  for (const c of changes) {
    const k = CHANGE_KIND.find(([n, tone]) => tone === c.tone && (!n || c.node.startsWith(`${n}:`)))
    if (k) counts[k[2]] = [(counts[k[2]]?.[0] || 0) + 1, k]
  }
  return CHANGE_KIND.filter((k, i, a) => counts[k[2]] && a.findIndex(x => x[2] === k[2]) === i)
    .map(k => [counts[k[2]][0], k[2], k[3], counts[k[2]][1][1]])
}

function rolesSummary(data) {
  const roles = data.router?.roles || []
  const names = new Set(roles.map(r => r.name))
  return {
    n: roles.length,
    fallback: roles.filter(onFallback).length,
    requests: (data.timeline?.flow || []).filter(f => names.has(f[4])).reduce((a, f) => a + f[7], 0)
  }
}

function TopologyChanges({ data, at, fmt, hours, start, span, onPick }) {
  const [open, setOpen] = useState(false)
  const changes = mergeRestarts(data.timeline?.changes || [])
  const [all, setAll] = useState(false)
  // the same change to the same node again, a gateway restarted four times, is one chip at its newest
  const groups = []
  for (const c of changes) {
    const g = groups.find(g => g[0].node === c.node && g[0].text === c.text && g[0].list === c.list)
    if (g) g.push(c)
    else groups.push([c])
  }
  const shown = all ? groups : groups.slice(0, 8)
  const pills = changePills(changes)
  const n = changes.length + (data.timeline?.more_changes || 0)
  const total = data.edges.reduce((a, e) => a + e.requests, 0)
  const share = {}
  for (const e of data.edges) share[e.served] = (share[e.served] || 0) + e.requests
  const [top, req] = Object.entries(share).sort((a, b) => b[1] - a[1])[0] || []
  // the profiles sending it the most
  const from = {}
  for (const f of data.timeline?.flow || []) if (f[5] === top) from[f[3]] = (from[f[3]] || 0) + f[7]
  const by = Object.entries(from).sort((a, b) => b[1] - a[1]).slice(0, 2)
  return jsxs('div', {
    className: 'overflow-hidden rounded-md border border-(--ui-stroke-secondary)',
    children: [
      jsxs('button', {
        type: 'button',
        'aria-expanded': open,
        onClick: () => { haptic('tap'); setOpen(v => !v) },
        className: 'flex w-full flex-wrap items-center gap-x-3 gap-y-1 px-3 py-1.5 text-left text-xs hover:bg-(--chrome-action-hover)',
        children: [
          jsx('span', { className: 'font-medium', children: 'What changed' }),
          jsx('span', { className: 'text-(--ui-text-quaternary)', children: `${start ? span : `last ${hours}h`} · ${plural(n, 'change')}` }),
          ...pills.map(([count, one, many, tone]) => jsxs('span', {
            key: one,
            className: 'inline-flex items-center gap-1 text-(--ui-text-quaternary)',
            children: [
              jsx('i', { style: { width: 6, height: 6, borderRadius: 3, background: CHANGE_COLOR[tone] || TONE.muted } }),
              `${count} ${count === 1 ? one : many}`
            ]
          })),
          jsx('span', { className: 'ml-auto text-(--ui-text-quaternary)', children: open ? 'Hide' : 'Show' })
        ]
      }),
      open ? jsxs('div', {
        className: 'space-y-1.5 border-t border-(--ui-stroke-secondary) p-3 text-xs',
        children: [
          !changes.length ? jsx('div', { children: 'No gateway, host, pool or conformance changes in this window.' }) : null,
          total && req / total > 0.6 ? jsxs('div', { className: 'flex flex-wrap items-baseline gap-x-2', children: [
            jsx('span', { children: `${top} served ${Math.round((100 * req) / total)}% of the window's ${total} requests${by[0]?.[1] === req ? `, all from ${by[0][0]}` : by.length ? `, mostly from ${by.map(([p, k]) => `${p} (${Math.round((100 * k) / req)}%)`).join(' and ')}` : ''}.` }),
            jsx(GoLink, { tab: 'flow', sel: `served:${top}`, ...(start ? { hours: 1, start } : { hours }), children: 'See its traffic' })
          ] }) : null,
          changes.length ? jsx('div', {
            className: 'flex flex-wrap gap-1',
            children: [
              ...shown.map((g, i) => {
                const c = g[0], later = at && c.ts > at
                return jsxs('button', {
                  key: i,
                  type: 'button',
                  onClick: () => onPick(c),
                  className: cn('flex items-center gap-1 rounded border border-(--ui-stroke-secondary) px-1.5 py-0.5 hover:bg-(--chrome-action-hover)', later && 'border-dashed opacity-50'),
                  title: later ? `After ${fmt(at)}, the time the map is replayed to. Replay to this moment instead.` : 'Replay the map to this moment',
                  children: [
                    jsx('span', { style: { width: 6, height: 6, borderRadius: 3, background: CHANGE_COLOR[c.tone] || TONE.muted } }),
                    jsx('span', { className: 'tabular-nums text-(--ui-text-quaternary)', children: g.length > 1 ? `${fmt(g.at(-1).ts)} to ${fmt(c.ts)}` : fmt(c.ts) }),
                    jsx('span', { children: `${c.text}${g.length > 1 ? ` ${g.length} times` : ''}${c.list ? ` (${c.list})` : ''}` }),
                    later ? jsx('span', { className: 'text-(--ui-text-quaternary)', children: 'later' }) : null
                  ]
                })
              }),
              groups.length > 8 ? jsx('button', {
                key: 'all',
                type: 'button',
                onClick: () => setAll(!all),
                className: 'rounded px-1.5 py-0.5 text-(--ui-accent) hover:bg-(--chrome-action-hover)',
                children: all ? 'Show the newest 8' : `Show all ${groups.length}`
              }) : null
            ]
          }) : null,
          data.timeline?.more_changes ? muted(`${plural(data.timeline.more_changes, 'older change')} not listed.`) : null
        ]
      }) : null
    ]
  })
}

// The fifteen layouts of the topology atlas, as the demo scores them: fit for tiers of entry points,
// hosts and what serves them with positions that hold still while the as-of slider moves. Four are
// Topology layouts (the id), Flow and Handoffs use two more (the tab); the rest are why-nots.
const VERDICT = { Primary: 'var(--ui-accent)', Supporting: TONE.purple, 'Drill-down': TONE.orange, Encoding: TONE.info, Skip: TONE.muted }
const STYLES = [
  ['Area Grouping', 5, 'Primary', 'Hosts, the model pool and cloud providers become regions. Gateways sit inside their host, so a second gateway host shows up as its own region.', 'area',
    'M4 5h34v36H4zM44 5h32v17H44zM44 28h32v13H44zM14 17l18 8M32 25l22-12M54 13h12M54 34h12', [[14, 17, 3], [32, 25, 3], [54, 13, 3], [66, 13, 3], [54, 34, 3], [66, 34, 3]]],
  ['Flow Chart', 5, 'Primary', 'Entry point to profile to model to what served it, as bands sized by volume. One read answers where each entry point\'s traffic ends up.', 'flow',
    'M4 6v34M76 6v34M4 10c30 0 30 16 72 16M4 23c30 0 30-13 72-13M4 36c30 0 30-2 72-2', []],
  ['Segmented Radial Convergence', 4, 'Supporting', 'A ring segment per tier keeps the tiers; chords show who depends on whom. Compact, so it suits narrow panes.', 'radial',
    'M42 4.1A19 19 0 0 1 55.5 12M57.5 15.5A19 19 0 0 1 57.5 30.5M55.5 34A19 19 0 0 1 42 41.9M38 41.9A19 19 0 0 1 24.5 34M22.5 30.5A19 19 0 0 1 22.5 15.5M24.5 12A19 19 0 0 1 38 4.1M44 8Q40 23 52 30M28 30Q40 23 30 10M48 36Q40 23 24 22', []],
  ['Arc Diagram', 4, 'Supporting', 'Every node on one line, an arc per dependency. Nodes never move, so it pairs with the as-of slider; Handoffs unrolls the same idea over time as lanes.', 'arc',
    'M4 38h72M10 38a10 10 0 0 1 20 0M20 38a20 20 0 0 1 40 0M30 38a15 15 0 0 1 30 0M50 38a10 10 0 0 1 20 0', [[10, 38, 2.5], [20, 38, 2.5], [30, 38, 2.5], [50, 38, 2.5], [60, 38, 2.5], [70, 38, 2.5]]],
  ['Ramification', 3, 'Drill-down', 'A spawn tree: a session to its subagents and theirs. Matches the data when one run fans out; its branching is what each side of the blast radius uses.', null,
    'M8 23h12M20 23L34 11M20 23L34 35M34 11h10M34 35h24M44 11L58 5M44 11L58 17M58 5h12M58 17h12', [[8, 23, 3], [34, 11, 2.5], [34, 35, 2.5], [58, 5, 2.5], [58, 17, 2.5], [58, 35, 2.5]]],
  ['Scaling Circles', 3, 'Encoding', 'Not a layout: circle area for volume, usable inside whichever layout is drawn.', null,
    'M16 30L36 18L60 26L70 9', [[16, 30, 8], [36, 18, 5], [60, 26, 11], [70, 9, 3]]],
  ['Circular Ties', 3, 'Drill-down', 'Clear for profile-to-profile messaging between about 6 and 10 nodes, but it drops the host and entry tiers, so it only works as an inset.', null,
    'M40 7L53.9 31M40 7L26.1 31M53.9 15H26.1M53.9 15L40 39M26.1 31H53.9', [[40, 7, 2.5], [53.9, 15, 2.5], [53.9, 31, 2.5], [40, 39, 2.5], [26.1, 31, 2.5], [26.1, 15, 2.5]]],
  ['Centralized Burst', 3, 'Drill-down', 'With a hub in the middle it hides what the hub does. Centered on the node you select instead, it becomes a blast radius: what breaks to one side, what it needs to the other.', 'burst',
    'M40 23V4M40 23L62 9M40 23H70M40 23L62 37M40 23V42M40 23L18 37M40 23H10M40 23L18 9', [[40, 23, 5], [40, 4, 2.5], [62, 9, 2.5], [70, 23, 2.5], [62, 37, 2.5], [40, 42, 2.5], [18, 37, 2.5], [10, 23, 2.5], [18, 9, 2.5]]],
  ['Centralized Ring', 2, 'Skip', 'Assumes one hub, which stops being true as soon as a second host runs a gateway.', null,
    'M40 6a17 17 0 1 0 .01 0M40 23V6M40 23L54.7 31.5M40 23L25.3 31.5', [[40, 23, 4], [40, 6, 2.5], [54.7, 14.5, 2.5], [54.7, 31.5, 2.5], [40, 40, 2.5], [25.3, 31.5, 2.5], [25.3, 14.5, 2.5]]],
  ['Radial Convergence', 2, 'Skip', 'The segmented version without segments. The segments carry the tier meaning, so use that one.', null,
    'M40 5Q40 23 58 23M58 23Q40 23 40 41M40 41Q40 23 22 23M22 23Q40 23 40 5M52.7 10.3Q40 23 27.3 35.7', [[40, 5, 2.5], [58, 23, 2.5], [40, 41, 2.5], [22, 23, 2.5], [52.7, 10.3, 2.5], [27.3, 35.7, 2.5]]],
  ['Radial Implosion', 2, 'Skip', 'Many to one. It shows that everything lands in one place and hides which entry point fed which host.', null,
    'M6 6L36 21M74 6L44 21M6 40L36 25M74 40L44 25M4 23H34M76 23H46M40 3V19M40 43V27', [[40, 23, 4]]],
  ['Elliptical Implosion', 1, 'Skip', 'A decorative implosion. The ellipse adds no axis this data can fill.', null,
    'M4 23a36 19 0 1 0 72 0a36 19 0 1 0-72 0M4 23H35M76 23H45M40 4V18M40 42V28M14 10L35 20M66 36L45 26', [[40, 23, 3]]],
  ['Organic Rhizome', 1, 'Skip', 'Force layouts move when the data changes, so scrubbing the as-of slider would make nodes jump.', null,
    'M8 30C16 12 26 36 34 20S50 8 56 22 70 34 74 14M20 38C24 28 30 30 34 20M56 22C52 32 60 40 66 40M34 20C36 10 44 6 48 8', [[8, 30, 2.5], [34, 20, 3], [56, 22, 3], [74, 14, 2.5], [20, 38, 2.5], [66, 40, 2.5], [48, 8, 2.5]]],
  ['Circled Globe', 1, 'Skip', 'Geographic. Hosts on one site or network have no geography to show, and the data carries no location.', null,
    'M40 4a19 19 0 1 0 .01 0M21 23H59M40 4c-10 6-10 32 0 38M40 4c10 6 10 32 0 38M24 13H56M24 33H56', [[33, 17, 2.5], [50, 28, 2.5]]],
  ['Sphere', 1, 'Skip', 'Occlusion hides nodes and labels, and the data has no third dimension.', null,
    'M40 4a19 19 0 1 0 .01 0M21 23c0 6 38 6 38 0M23 14c2 4 32 4 34 0M23 32c2 4 32 4 34 0M40 4c-14 8-14 30 0 38', [[30, 20, 2.5], [48, 30, 2.5], [44, 12, 2.5]]]
]

const styleGlyph = (st, size) => jsxs('svg', {
  viewBox: '0 0 80 46', width: size, height: (size * 46) / 80, fill: 'none', stroke: VERDICT[st[2]], strokeWidth: 1.5, strokeLinecap: 'round', 'aria-hidden': true,
  children: [jsx('path', { d: st[5] }), ...st[6].map(([x, y, r], i) => jsx('circle', { key: i, cx: x, cy: y, r, fill: 'var(--ui-bg, #0d1117)' }))]
})
const fitBar = n => jsx('span', {
  title: `Fit ${n} of 5`, className: 'inline-flex gap-0.5',
  children: [1, 2, 3, 4, 5].map(k => jsx('i', { key: k, style: { width: 8, height: 4, borderRadius: 1, background: k <= n ? 'currentColor' : 'var(--ui-stroke-secondary)' } }))
})

function LayoutStyles({ layout, onLayout }) {
  const qc = useQueryClient()
  const go = useContext(Nav)
  const { roles } = useContext(Role)
  const [pick, setPick] = useState(null)
  const [note, setNote] = useState(null)
  const st = STYLES.find(s => s[0] === pick)
  const isLayout = id => LAYOUTS.some(l => l[0] === id)
  const defaultsOf = id => roles.filter(r => r.layout === id).map(r => r.name)
  const setDefault = async (role, id) => {
    setNote(null)
    try {
      await saveRoles(qc, roles.map(r => (r.id === role.id ? { ...r, layout: id } : r)))
    } catch (e) {
      setNote(e?.message || String(e))
    }
  }
  const side = !st
    ? [
        jsx('h3', { key: 'h', className: 'text-sm font-semibold', children: 'Default view per role' }),
        muted('What each role sees first on this tab. Select a style to see why it fits or not, and to change a default.'),
        jsx('div', { key: 'l', className: 'space-y-0.5 text-xs', children: roles.map(r => jsxs('div', { key: r.id, className: 'flex justify-between gap-2', children: [jsx('span', { children: r.name }), jsx('span', { className: 'text-(--ui-text-quaternary)', children: LAYOUT_NAMES[r.layout] })] })) })
      ]
    : [
        jsx('span', { key: 'v', className: 'text-[0.6875rem] font-medium uppercase', style: { color: VERDICT[st[2]], letterSpacing: '0.06em' }, children: st[2] }),
        jsx('h3', { key: 'h', className: 'text-sm font-semibold', children: st[0] }),
        jsx('p', { key: 'p', className: 'text-xs', children: st[3] }),
        jsx('div', { key: 'g', children: styleGlyph(st, 200) }),
        jsxs('div', { key: 'f', className: 'flex items-center gap-2 text-xs', children: ['Fit', fitBar(st[1]), `${st[1]} of 5`] }),
        isLayout(st[4])
          ? jsxs('div', { key: 'd', className: 'space-y-1', children: [
              jsx('div', { className: 'text-[0.6875rem] font-medium text-(--ui-text-quaternary)', children: 'Default for' }),
              jsx('div', { className: 'flex flex-wrap gap-1', children: roles.map(r => jsx('button', {
                type: 'button', key: r.id, 'aria-pressed': r.layout === st[4], disabled: r.layout === st[4],
                title: r.layout === st[4] ? `${r.name} opens with this` : `${r.name} opens with ${LAYOUT_NAMES[r.layout]} now`,
                onClick: () => { haptic('tap'); setDefault(r, st[4]) },
                className: cn('rounded border px-2 py-0.5 text-[0.6875rem]', r.layout === st[4] ? 'border-(--ui-accent) text-(--ui-accent)' : 'border-(--ui-stroke-secondary) text-(--ui-text-quaternary) hover:bg-(--chrome-action-hover)'),
                children: r.name
              })) })
            ] })
          : muted(st[2] === 'Skip' ? 'Not offered: see why above.' : 'Not a Topology layout, so it cannot be a default.'),
        jsxs('div', { key: 'a', className: 'flex flex-wrap gap-2', children: [
          isLayout(st[4]) && st[4] !== layout ? jsx(Button, { size: 'xs', variant: 'outline', onClick: () => onLayout(st[4]), children: 'Show it now' }) : null,
          st[4] === 'flow' ? jsx(Button, { size: 'xs', variant: 'outline', onClick: () => go(st[4]), children: 'Open Flow' }) : null,
          st[0] === 'Arc Diagram' ? jsx(Button, { size: 'xs', variant: 'outline', onClick: () => go('handoffs'), children: 'Open Handoffs' }) : null,
          jsx(Button, { size: 'xs', variant: 'outline', onClick: () => setPick(null), children: 'Clear selection' })
        ] })
      ]
  return jsx(Section, {
    title: 'Layout styles',
    count: 'fit is how well a style shows tiers of entry points, hosts and what serves them, with nodes that hold still over time',
    children: jsxs('div', {
      className: 'flex flex-wrap items-start gap-3',
      children: [
        jsx('div', {
          className: 'grid gap-2',
          style: { gridTemplateColumns: 'repeat(auto-fill, minmax(8.5rem, 1fr))', flex: '3 1 26rem' },
          children: STYLES.map(s => {
            const defs = isLayout(s[4]) ? defaultsOf(s[4]) : []
            return jsxs('button', {
              type: 'button', key: s[0], 'aria-pressed': s === st,
              onClick: () => { haptic('tap'); setPick(s === st ? null : s[0]) },
              className: 'grid content-start gap-1 rounded border p-2 text-left text-xs transition-colors hover:bg-(--chrome-action-hover)',
              style: { borderColor: s === st ? 'var(--ui-accent)' : 'var(--ui-stroke-secondary)', opacity: s[2] === 'Skip' && s !== st ? 0.7 : 1 },
              children: [
                styleGlyph(s, 80),
                jsx('b', { className: 'font-medium', children: s[0] }),
                jsxs('span', { className: 'flex items-center gap-2 text-[0.6875rem]', style: { color: VERDICT[s[2]] }, children: [fitBar(s[1]), s[2]] }),
                defs.length ? jsx('small', { className: 'text-[0.6875rem] text-(--ui-text-quaternary)', children: `Default: ${defs.join(', ')}` }) : null
              ]
            })
          })
        }),
        jsxs('div', { style: { ...SIDE, flex: '1 1 16rem' }, children: [...side, note ? jsx('div', { key: 'n', className: 'text-xs', style: { color: TONE.bad }, children: note }) : null] })
      ]
    })
  })
}

// --- roles ---------------------------------------------------------------------------

// A role is a job description the page reads: what Activity shows first (the kinds it watches) and which
// layout Topology opens with. The built-in five ship with the backend; edits save for everyone on this
// Hermes install, and the role you view as is remembered per viewer.
const WATCH = LOG_KINDS.filter(([id]) => id !== 'all')
const LAYOUT_NAMES = Object.fromEntries(LAYOUTS.map(([id, name]) => [id, name]))

// PUT saves the whole list; null puts back the built-in roles. Either answers as GET /roles does.
const saveRoles = async (qc, roles) => {
  const out = await api.rest('/roles', roles ? { method: 'PUT', body: { roles } } : { method: 'DELETE' })
  qc.setQueryData([ID, 'roles'], out)
  return out
}

const lines = text => text.split('\n').map(s => s.trim()).filter(Boolean)
const asForm = r => ({ ...r, responsibilities: r.responsibilities.join('\n'), skills: r.skills.join('\n') })
const fromForm = f => ({ ...f, responsibilities: lines(f.responsibilities), skills: lines(f.skills) })

function Seg({ options, value, onChange, multi }) {
  return jsx('div', {
    className: 'flex flex-wrap gap-1',
    children: options.map(([id, label]) => {
      const on = multi ? value.includes(id) : value === id
      return jsx('button', {
        type: 'button',
        key: id,
        'aria-pressed': on,
        onClick: () => { haptic('tap'); onChange(multi ? (on ? value.filter(v => v !== id) : [...value, id]) : id) },
        className: cn('rounded border px-2 py-0.5 text-[0.6875rem] transition-colors',
          on ? 'border-(--ui-accent) bg-(--ui-accent)/10 text-(--ui-accent)' : 'border-(--ui-stroke-secondary) text-(--ui-text-quaternary) hover:bg-(--chrome-action-hover)'),
        children: label
      })
    })
  })
}

function RolesDrawer({ data, current, onClose }) {
  const qc = useQueryClient()
  const [id, setId] = useState(current)
  const roles = data.roles
  const base = roles.find(r => r.id === id) || roles[0]
  const [f, setF] = useState(() => asForm(base))
  const [busy, setBusy] = useState(false)
  const [note, setNote] = useState(null)
  const open = r => { setId(r.id); setF(asForm(r)); setNote(null) }
  const builtIn = data.defaults.find(d => d.id === f.id)
  const isNew = !roles.some(r => r.id === f.id)
  const run = async (list, text, after) => {
    setBusy(true)
    setNote(null)
    try {
      const out = await saveRoles(qc, list)
      setNote({ text })
      after?.(out)
    } catch (e) {
      setNote({ bad: true, text: e?.message || String(e) })
    }
    setBusy(false)
  }
  const set = k => e => setF({ ...f, [k]: e.target.value })
  const label = text => jsx('div', { className: 'text-[0.6875rem] font-medium text-(--ui-text-quaternary)', children: text })
  const save = () => run(isNew ? [...roles, fromForm(f)] : roles.map(r => (r.id === f.id ? fromForm(f) : r)), `Saved ${f.name}.`, () => setId(f.id))
  return jsx(Drawer, {
    label: 'Edit roles',
    onClose,
    head: [jsx('h3', { key: 'h', className: 'text-sm font-semibold', children: 'Roles' })],
    children: [
      jsx('p', { key: 'p', className: 'text-xs text-(--ui-text-quaternary)', children: 'Each role is a job description: what it is responsible for, the skills it brings, what Activity shows it first and the layout Topology opens with. Changes apply to everyone using this Hermes install.' }),
      jsxs('div', { key: 'list', className: 'flex flex-wrap gap-1', children: [
        ...roles.map(r => jsx('button', {
          type: 'button', key: r.id, 'aria-pressed': r.id === f.id, onClick: () => open(r),
          className: cn('rounded border px-2 py-0.5 text-xs', r.id === f.id ? 'border-(--ui-accent) text-(--ui-accent)' : 'border-(--ui-stroke-secondary) hover:bg-(--chrome-action-hover)'),
          children: r.name
        })),
        jsx('button', {
          type: 'button', key: 'add', className: 'rounded border border-dashed border-(--ui-stroke-secondary) px-2 py-0.5 text-xs text-(--ui-text-quaternary) hover:bg-(--chrome-action-hover)',
          onClick: () => { let n = 1; while (roles.some(r => r.id === `role-${n}`)) n++; setF(asForm({ id: `role-${n}`, name: 'New role', summary: '', responsibilities: [], skills: [], watch: WATCH.map(([k]) => k), layout: 'area' })); setNote(null) },
          children: 'Add role'
        })
      ] }),
      jsxs('div', { key: 'form', className: 'space-y-2', children: [
        label('Name'),
        jsx(Input, { className: 'h-7 text-xs', value: f.name, maxLength: 60, onChange: set('name') }),
        label('Job description'),
        jsx(Textarea, { className: 'min-h-16 text-xs', value: f.summary, maxLength: 600, onChange: set('summary') }),
        label('Responsibilities, one per line'),
        jsx(Textarea, { className: 'min-h-20 text-xs', value: f.responsibilities, onChange: set('responsibilities') }),
        label('Skills, one per line'),
        jsx(Textarea, { className: 'min-h-20 text-xs', value: f.skills, onChange: set('skills') }),
        label('Watches in Activity'),
        jsx(Seg, { options: WATCH, value: f.watch, multi: true, onChange: watch => setF({ ...f, watch: WATCH.map(([k]) => k).filter(k => watch.includes(k)) }) }),
        label('Topology opens with'),
        jsx(Seg, { options: LAYOUTS.map(([k, name]) => [k, name]), value: f.layout, onChange: layout => setF({ ...f, layout }) })
      ] }),
      jsxs('div', { key: 'acts', className: 'flex flex-wrap gap-2', children: [
        jsx(Button, { size: 'xs', variant: 'default', disabled: busy || !f.name.trim(), onClick: save, children: isNew ? 'Add' : 'Save' }),
        builtIn && !isNew ? jsx(Button, { size: 'xs', variant: 'outline', disabled: busy, onClick: () => setF(asForm(builtIn)), children: 'Back to built-in' }) : null,
        !builtIn && !isNew ? jsx(Button, {
          size: 'xs', variant: 'outline', disabled: busy || roles.length < 2,
          onClick: () => run(roles.filter(r => r.id !== f.id), `Deleted ${f.name}.`, out => open(out.roles[0])),
          children: 'Delete'
        }) : null,
        data.saved ? jsx(Button, {
          size: 'xs', variant: 'outline', disabled: busy,
          onClick: () => run(null, 'All roles are back to the built-in five.', () => open(data.defaults[0])),
          children: 'Reset all'
        }) : null
      ] }),
      builtIn && !isNew ? jsx('p', { key: 'n', className: 'text-[0.6875rem] text-(--ui-text-quaternary)', children: '"Back to built-in" fills the form with the shipped description; Save keeps it.' }) : null,
      note ? jsx('div', { key: 'note', className: 'text-xs', style: { color: note.bad ? TONE.bad : undefined }, children: note.text }) : null,
      ...Object.values(data.errors || {}).map((v, i) => jsx('div', { key: `e${i}`, className: 'text-xs', style: { color: TONE.warn }, children: v }))
    ]
  })
}

// A node id from another tab or a change, as the graph on screen names it: a gateway becomes its entry
// point and a host the first profile in its circle when the layout draws the request path.
const nodeIn = (g, id) => {
  if (!id || g.nodes.some(n => n.id === id)) return id
  if (id.startsWith('g:')) return `e:${id.split(':').at(-1)}`
  if (id.startsWith('h:')) return (g.nodes.find(n => n.group === id.slice(2) && n.id.startsWith('p:')) || g.nodes.find(n => n.group === id.slice(2)))?.id || null
  if (id.startsWith('p:')) return g.nodes.find(n => n.id === `h:${id.split(':')[1]}`)?.id || null
  if (id.startsWith('r:')) return g.nodes.find(n => n.id === 'r:router')?.id || null
  return null
}
const typing = el => el && (/^(INPUT|TEXTAREA|SELECT)$/.test(el.tagName) || el.isContentEditable)
const check = (label, on, onChange, disabled, title) => jsxs('label', {
  className: cn('flex items-center gap-1 text-[0.6875rem]', disabled ? 'opacity-40' : 'cursor-pointer'), title,
  children: [jsx('input', { type: 'checkbox', checked: on, disabled, onChange: e => onChange(e.target.checked), className: 'accent-(--ui-accent)' }), label]
})

// An open issue in a drawer: why, what was seen, where to look and a command that usually fixes it.
function IssueDrawer({ issue, fmt, onClose }) {
  const [copied, setCopied] = useState(false)
  const copy = async () => {
    try { await navigator.clipboard.writeText(issue.cmd.text); setCopied(true) } catch { setCopied(false) }
  }
  return jsx(Drawer, {
    label: issue.text,
    onClose,
    head: [
      jsx('i', { key: 'k', style: { width: 10, height: 10, flex: 'none', backgroundColor: CHANGE_COLOR[issue.tone] || TONE.muted } }),
      jsx('span', { key: 't', style: { fontFamily: MONO, fontSize: 12, color: DIM }, children: issue.since ? `since ${fmt(issue.since)}` : 'issue' })
    ],
    children: [
      drawerTitle(issue.text),
      drawerPart('c', 'Why', jsx('p', { style: { margin: 0, fontSize: 13.5 }, children: issue.cause })),
      issue.facts.length ? drawerPart('f', 'Seen', jsx('div', { className: 'grid gap-1', children: issue.facts.map((f, i) => jsx('div', { key: i, style: { fontFamily: MONO, fontSize: 12, color: DIM, overflowWrap: 'anywhere' }, children: f })) })) : null,
      issue.cmd ? drawerPart('cmd', 'Usually fixes it', jsxs('div', { className: 'grid gap-1.5', children: [
        jsxs('div', { className: 'flex items-center gap-2', children: [
          jsx('code', { className: 'min-w-0 flex-1 overflow-x-auto rounded border border-(--ui-stroke-secondary) px-2 py-1 font-mono text-[0.6875rem]', style: { userSelect: 'all', whiteSpace: 'nowrap' }, children: issue.cmd.text }),
          jsx('button', { type: 'button', onClick: copy, className: 'rounded px-2 py-0.5 text-(--ui-accent) hover:bg-(--chrome-action-hover)', children: copied ? 'Copied' : 'Copy' })
        ] }),
        drawerText('n', issue.cmd.note)
      ] })) : null,
      issue.go.length ? jsx('div', { key: 'go', className: 'flex flex-wrap gap-2', children: issue.go.map(([label, tab, sel]) => jsx(GoLink, { key: label, btn: true, tab, sel, children: label })) }) : null
    ]
  })
}

function TopologyPage({ sel: initial, when = {}, remember }) {
  const [hours, setHours] = useState(rangeFor(when, 24))
  const [selected, setSel] = useState(initial)
  // null follows now; a time inside the loaded window freezes the map there.
  const [at, setAt] = useState(when.at ?? null)
  // an hour from the Today strip: only its requests and changes, until Live, Play or a range clears it
  const [from, setFrom] = useState(when.start ?? null)
  // a link from another page says what it came from and whether what it asked for is on the map
  const [linked, setLinked] = useState(!!(initial || when.note) && !when.back)
  remember?.({ sel: selected, when: { hours, at, start: from } })
  const [playing, setPlaying] = useState(false)
  // null follows the role's default layout
  const { role } = useContext(Role)
  const [chosen, setLayout] = useState(null)
  const [styles, setStyles] = useState(false)
  const [collapse, setCollapse] = useState(false)
  const [traffic, setTraffic] = useState(() => !window.matchMedia?.('(prefers-reduced-motion: reduce)').matches)
  const [issuesOnly, setIssuesOnly] = useState(false)
  const [openIssue, setOpenIssue] = useState(null)
  const [tip, setTip] = useState(null)
  const [eachRole, setEachRole] = useState(false)
  const [view, setView] = useState(false)
  const [rolesOpen, setRolesOpen] = useState(false)
  const layout = chosen || role?.layout || 'area'
  const { data: live, isLoading, isError } = useTopology(hours)
  // replay steps one timeline bucket every quarter second and goes back to live at the end
  const step = live?.timeline?.step || 600
  const end = live ? Math.floor(live.generated_at) : 0
  useEffect(() => {
    if (!playing) return
    const id = setInterval(() => setAt(a => (a == null || a + step >= end ? null : a + step)), 250)
    return () => clearInterval(id)
  }, [playing, step, end])
  useEffect(() => { if (playing && at == null) setPlaying(false) }, [playing, at])
  useEffect(() => {
    if (!view) return
    const key = e => { if (e.key === 'Escape') setView(false) }
    window.addEventListener('keydown', key)
    return () => window.removeEventListener('keydown', key)
  }, [view])
  // the keys read this render's handlers through a ref, so one listener serves every render
  const keys = useRef({})
  const mapRef = useRef(null)
  useEffect(() => {
    const key = e => {
      if (e.defaultPrevented || e.metaKey || e.ctrlKey || e.altKey || typing(e.target)) return
      const k = keys.current
      if (e.key === ' ' && !e.target.closest?.('button, [role="button"], a')) { e.preventDefault(); k.play() }
      else if (e.key === 'l' || e.key === 'L') k.live()
      else if (e.key === 'i' || e.key === 'I') k.issues()
      else if (e.key === 'ArrowLeft' || e.key === 'ArrowRight') { e.preventDefault(); k.jump(e.key === 'ArrowRight' ? 1 : -1) }
    }
    window.addEventListener('keydown', key)
    return () => window.removeEventListener('keydown', key)
  }, [])
  if (isError) return jsx('div', { className: 'p-4', children: muted('Topology unavailable: the /api/plugins/operations backend did not respond.') })
  if (isLoading || !live) return jsx('div', { className: 'p-4', children: jsx(Skeleton, { className: 'h-48 w-full' }) })
  const t0 = from ?? Math.floor(live.timeline?.since || end - hours * 3600)
  const top = from ? Math.min(from + 3600, end) : end
  const data = topologyAt(live, at && at < end ? at : null, from)
  const fmt = ts => (hours > 24 ? new Date(ts * 1000).toLocaleString([], { weekday: 'short', hour: '2-digit', minute: '2-digit' }) : fmtClock(ts))
  const flow = !!FLOW_LAYOUT[layout]
  const full = flow ? flowGraph(data, { ...FLOW_LAYOUT[layout], ...(eachRole && { router: false }), collapse }) : topoGraph(data)
  const issues = topoIssues(data, data.at || end)
  const shown = issues.find(i => i.key === openIssue)
  // issues only: the nodes an issue points at and their neighbours
  const hit = new Set(full.nodes.filter(n => issues.some(i => issueHits(i, n.id))).map(n => n.id))
  const keep = new Set([...hit, ...full.edges.filter(e => hit.has(e.a) || hit.has(e.b)).flatMap(e => [e.a, e.b])])
  const g = issuesOnly ? { nodes: full.nodes.filter(n => keep.has(n.id)), edges: full.edges.filter(e => keep.has(e.a) && keep.has(e.b)) } : full
  const want = nodeIn(g, selected)
  const sel = g.nodes.some(n => n.id === want) ? want : null
  // what the link asked for, while it is still the selection
  const picked = initial && selected === initial ? g.nodes.find(n => n.id === sel) : null
  const lost = initial && selected === initial && !sel
  const empty = !data.hosts?.length && !data.served?.length
  const marks = mergeRestarts(live.timeline?.changes || []).filter(c => c.ts >= t0 && c.ts <= top)
  const changes = from && live.timeline
    ? { ...data, timeline: { ...live.timeline, more_changes: 0, changes: live.timeline.changes.filter(c => c.ts >= from && c.ts <= top),
                             flow: live.timeline.flow?.filter(f => f[0] >= from && f[0] <= top) } }
    : live
  const span = `${fmt(t0)} to ${fmt(top)}`
  const pickChange = c => { setPlaying(false); setAt(c.ts); setSel(changeNode(c)) }
  const chips = chromeChips(issues, data.router)
  const pickChip = chip => {
    haptic('tap')
    if (chip.key === 'roles') return setRolesOpen(v => !v)
    const items = chip.items || []
    if (!items.length) return
    const next = items[items.findIndex(i => i.key === openIssue) + 1]
    setOpenIssue(next?.key ?? null)
    if (next) setSel(full.nodes.find(n => issueHits(next, n.id))?.id || null)
  }
  const play = () => {
    if (playing) return setPlaying(false)
    setFrom(null)
    if (at == null) {
      if (hours < 24) setHours(24)
      setAt(end - 86400)
    }
    setPlaying(true)
  }
  const goLive = () => { setPlaying(false); setAt(null); setFrom(null) }
  keys.current = {
    play, live: goLive, issues: () => setIssuesOnly(v => !v),
    // the previous or next change or issue start, from the time on screen
    jump: dir => {
      const ts = [...new Set([...marks.map(c => c.ts), ...issues.map(i => i.since).filter(s => s >= t0 && s <= top)])].sort((a, b) => a - b)
      const now = at ?? end
      const next = dir > 0 ? ts.find(s => s > now) : ts.filter(s => s < now).at(-1)
      setPlaying(false)
      if (next != null) setAt(next)
      else if (dir > 0) goLive()
    }
  }
  const btn = (text, onClick, disabled, title) => jsx('button', {
    type: 'button', onClick, disabled, title,
    className: `rounded px-2 py-0.5 transition-colors ${disabled ? 'opacity-40' : 'text-(--ui-accent) hover:bg-(--chrome-action-hover)'}`,
    children: text
  })
  return jsxs('div', {
    className: 'space-y-3 p-4',
    children: [
      jsxs('div', {
        className: 'flex flex-wrap items-center gap-2',
        children: [
          jsx('h2', { className: 'text-sm font-semibold', children: 'Topology' }),
          jsx('span', { className: 'text-[0.6875rem] text-(--ui-text-quaternary)', children: 'gateways, hosts and what served their requests' }),
          jsx('span', { className: 'ml-auto' }),
          from ? jsx('span', { className: 'text-[0.6875rem] tabular-nums', style: { fontFamily: MONO, color: 'var(--ui-accent)' }, children: span }) : null,
          jsx(RangePicker, { hours: from ? null : hours, onChange: h => { setHours(h); goLive() } })
        ]
      }),
      ...Object.entries(data.errors || {}).map(([k, v]) =>
        jsx('div', { key: k, className: 'text-xs text-(--ui-text-quaternary)', children: `${{ metrics: 'Metrics collector data', replay: 'Replay' }[k] || k} unavailable: ${v}` })
      ),
      linked
        ? jsxs('div', {
            className: 'flex flex-wrap items-center gap-2 rounded px-3 py-2 text-xs',
            style: lost
              ? { border: `1px dashed ${CHANGE_COLOR.warn}`, backgroundColor: `color-mix(in srgb, ${CHANGE_COLOR.warn} 8%, transparent)` }
              : { border: '1px solid color-mix(in srgb, var(--ui-accent) 50%, transparent)', backgroundColor: 'color-mix(in srgb, var(--ui-accent) 10%, transparent)' },
            children: [
              when.note ? jsx('span', { children: 'From' }) : null,
              when.note && when.start != null ? jsx('span', { className: 'tabular-nums', style: { fontFamily: MONO, color: 'var(--ui-accent)' }, children: `${fmtClock(when.start)} to ${fmtClock(when.start + 3600)}` }) : null,
              when.note ? jsx('span', { className: 'min-w-0', style: { overflowWrap: 'anywhere' }, children: `${when.note}.` }) : null,
              picked
                ? jsx('span', { children: ['Selected ', jsx('b', { key: 'n', style: { color: 'var(--ui-accent)' }, children: picked.full }), ` as of ${data.at ? fmt(data.at) : 'now'}.`] })
                : lost
                  ? jsx('span', { style: { color: CHANGE_COLOR.warn }, children: `${initial.slice(2).split(':').reverse().join(' on ')} is not on this map as of ${data.at ? fmt(data.at) : 'now'}: it had no requests by then, or this layout does not draw it.` })
                  : null,
              jsx('button', { type: 'button', className: 'ml-auto text-(--ui-text-quaternary) hover:underline', onClick: () => { haptic('tap'); setLinked(false) }, children: 'Dismiss' })
            ]
          })
        : null,
      live.timeline ? jsxs('div', {
        className: 'flex items-center gap-2 text-[0.6875rem] text-(--ui-text-quaternary)',
        children: [
          jsx('span', { children: 'As of' }),
          jsxs('div', {
            className: 'relative min-w-0 flex-1',
            children: [
              jsx('input', {
                type: 'range',
                'aria-label': 'Show the topology as of this time',
                min: t0,
                max: top,
                step: 60,
                value: data.at || end,
                onChange: e => { setPlaying(false); setAt(Number(e.target.value) >= end ? null : Number(e.target.value)) },
                className: 'w-full accent-(--ui-accent)'
              }),
              // a tick per change on the track; each replays to its moment. The mark is 4px, its target 10px.
              ...marks.map((c, i) => jsx('button', {
                key: i,
                type: 'button',
                'aria-label': `${fmt(c.ts)} ${c.text}${c.list ? ` (${c.list})` : ''}`,
                onMouseEnter: () => setTip(i), onMouseLeave: () => setTip(null), onFocus: () => setTip(i), onBlur: () => setTip(null),
                onClick: () => pickChange(c),
                style: { position: 'absolute', left: `calc(${(100 * (c.ts - t0)) / Math.max(1, top - t0)}% - 5px)`, top: -8, width: 10, height: 10,
                         boxSizing: 'border-box', padding: '0 3px 4px', backgroundClip: 'content-box', borderRadius: 1,
                         backgroundColor: CHANGE_COLOR[c.tone] || TONE.muted, border: 0, cursor: 'pointer' }
              })),
              marks[tip] ? (() => {
                const c = marks[tip]
                return hoverTip((100 * (c.ts - t0)) / Math.max(1, top - t0), 16, [
                  jsx('span', { key: 't', className: 'tabular-nums', style: { color: DIM, marginRight: 6 }, children: fmt(c.ts) }),
                  jsx('span', { key: 'c', style: { color: CHANGE_COLOR[c.tone] || TONE.muted }, children: `${c.text}${c.list ? ` (${c.list})` : ''}` }),
                  jsx('span', { key: 'h', style: { color: DIM, marginLeft: 6 }, children: 'click to replay' })
                ])
              })() : null
            ]
          }),
          jsx('span', { className: 'w-24 tabular-nums', style: data.at ? { color: 'var(--ui-accent)' } : undefined, children: data.at ? fmt(data.at) : 'now' }),
          !data.at && !playing ? jsx('span', { className: traffic ? 'ops-blink' : undefined, title: 'Following now', style: { display: 'inline-block', width: 6, height: 6, borderRadius: '50%', background: TONE.good } }) : null,
          btn('Live', goLive, !data.at && !playing, 'Back to now (L)'),
          btn(playing ? 'Pause' : data.at ? 'Play' : 'Replay a day', play, false, playing ? 'Pause the replay (Space)' : data.at ? 'Play on from here (Space)' : 'Play the last 24 hours from the start (Space)')
        ]
      }) : null,
      jsxs('div', {
        className: 'flex flex-wrap items-center gap-2',
        children: [
          ...chips.map(c => {
            const on = c.key === 'roles' ? rolesOpen : c.items.some(i => i.key === openIssue)
            const color = CHANGE_COLOR[c.tone] || TONE.info
            return jsxs('button', {
              key: c.key, type: 'button', 'aria-pressed': on,
              onClick: () => pickChip(c),
              title: c.items.map(i => i.text).filter(Boolean).join(' · ') || (c.key === 'roles' ? 'Open the roles list' : undefined),
              className: 'inline-flex items-center gap-1.5 rounded-full border px-2.5 py-1 font-medium hover:bg-(--chrome-action-hover)',
              style: {
                fontFamily: MONO, fontSize: 12, color,
                borderColor: `color-mix(in srgb, ${color} 45%, var(--ui-stroke-secondary))`,
                background: on ? `color-mix(in srgb, ${color} 12%, transparent)` : undefined
              },
              children: [
                jsx('i', { style: { width: 6, height: 6, borderRadius: 3, background: 'currentColor' } }),
                c.label
              ]
            })
          }),
          !issues.some(i => !i.quiet) ? jsx('span', { className: 'text-[0.6875rem] text-(--ui-text-quaternary)', children: 'No issues' }) : null,
          jsx('span', { className: 'ml-auto text-[0.6875rem] text-(--ui-text-quaternary)', children: `${LAYOUT_NAMES[layout] || layout}${role && !chosen ? ` · ${role.name} default` : ''}` }),
          jsxs('div', {
            className: 'relative',
            children: [
              jsx('button', {
                type: 'button', 'aria-expanded': view,
                onClick: () => { haptic('tap'); setView(v => !v) },
                className: 'rounded border border-(--ui-stroke-secondary) px-2.5 py-1 text-[0.6875rem] text-(--ui-text-secondary) hover:bg-(--chrome-action-hover)',
                children: 'View'
              }),
              view ? jsxs('div', {
                className: 'absolute right-0 z-10 mt-1 w-[min(26rem,calc(100vw-2rem))] space-y-2 rounded-md border border-(--ui-stroke-secondary) p-3 text-xs shadow-lg',
                style: { background: 'var(--ui-bg, var(--chrome-bg))' },
                children: [
                  jsx('div', { className: 'font-medium', children: 'View' }),
                  jsx(Seg, { options: LAYOUTS.map(([k, name]) => [k, name]), value: layout, onChange: setLayout }),
                  jsxs('div', {
                    className: 'flex flex-wrap items-center gap-3',
                    children: [
                      check('Collapse to one orchestrator', collapse, setCollapse, !flow, flow ? 'Fold every profile into one node' : 'Area grouping and Router switchboard only'),
                      check('Show each role', eachRole, setEachRole, layout !== 'area', layout === 'area' ? 'Split the router into its role aliases, as a ring' : 'Area grouping only; the switchboard always shows each role'),
                      check('Traffic', traffic, setTraffic, false, 'Moving dots and a glow on what had requests in the last two buckets'),
                      check(`Issues only (${issues.length} active)`, issuesOnly, setIssuesOnly, false, 'Only what an issue points at and its neighbours (I)')
                    ]
                  }),
                  jsx('button', {
                    type: 'button', 'aria-expanded': styles,
                    onClick: () => { haptic('tap'); setStyles(!styles) },
                    className: 'text-[0.6875rem] text-(--ui-accent) hover:underline',
                    children: styles ? 'Hide layout styles' : 'Compare all fifteen'
                  }),
                  jsx('div', { className: 'text-[0.6875rem] text-(--ui-text-quaternary)', children: 'Space play · L live · I issues only · arrows step through changes' })
                ]
              }) : null
            ]
          })
        ]
      }),
      shown ? jsx(IssueDrawer, { issue: shown, fmt, onClose: () => setOpenIssue(null) }, shown.key) : null,
      jsx(Section, {
        title: data.at ? `Topology as of ${fmt(data.at)}` : 'Map',
        count: flow
          ? `entry point · profile · role alias · what served it (${from ? `requests from ${fmt(from)} to ${fmt(data.at || top)}` : data.at ? `requests from the window's start to ${fmt(data.at)}` : `last ${hours}h`}) · ${layout === 'area' ? 'circle area is token volume, a glow is recent activity' : 'line width is request count'}`
          : `gateways · hosts · served by (${from ? `requests from ${fmt(from)} to ${fmt(data.at || top)}` : data.at ? `requests from the window's start to ${fmt(data.at)}` : `last ${hours}h`}) · green live, amber stale, red down, blue cloud, grey not in the pool, pink dashed drifted`,
        children: empty
          ? muted(data.at ? 'Nothing had reported yet at this time.' : 'No hosts, gateways or requests reported yet.')
          : jsxs('div', {
              className: 'space-y-3',
              children: [
                g.nodes.length
                  ? jsx('div', { ref: mapRef, children: jsx(TopologyMap, { g, sel, onSelect: setSel, layout, traffic, live: !data.at && !playing }) })
                  : muted(issuesOnly ? 'No issues on this layout at this time.' : 'Nothing to draw.'),
                sel ? jsx(TopologyDetail, { data, sel, g }) : muted('Select a node for details. Line width is request count.')
              ]
            })
      }),
      styles ? jsx(LayoutStyles, { layout, onLayout: setLayout }) : null,
      live.timeline ? jsx(TopologyChanges, { data: changes, at: data.at, fmt, hours, start: from, span, onPick: pickChange }) : null,
      data.at && data.router ? muted('Roles show the model router as it is now, not as of the replayed time.') : null,
      jsx(RouterRoles, { data, hours, open: rolesOpen, onToggle: () => setRolesOpen(v => !v),
        onPick: flow ? id => { setSel(id); mapRef.current?.scrollIntoView({ behavior: 'smooth', block: 'center' }) } : null })
    ]
  })
}

// --- conformance ---------------------------------------------------------------------

// A check result as a matrix cell: its state in color over what was found.
const CELL = { pass: ['Passing', TONE.good], enforced_by_job: ['In CI', TONE.good], warn: ['Warning', TONE.warn], fail: ['Failing', TONE.bad], stale: ['Stale', TONE.muted], na: ['N/A'], none: ['Not run'] }
const hostCell = (h, res) => (!h.ts || !res ? 'none' : res.status === 'na' ? 'na' : h.stale ? 'stale' : res.status)
const repoValue = r => (r.findings.length ? plural(r.findings.length, 'line') : r.status === 'enforced_by_job' ? `job ${r.ci_job || ''}` : 'clean')
const SIDE = { display: 'grid', gridTemplateColumns: 'minmax(0, 1fr)', gap: 12, alignContent: 'start', padding: 16, fontSize: 13, border: '1px solid var(--ui-stroke-secondary)', borderRadius: 6 }
// One finding in a Where list: its file and line over the line itself, wrapped to the panel's width.
const FINDING = { padding: '5px 12px', borderTop: '1px solid var(--ui-stroke-secondary)' }
const sideTitle = text => jsx('h3', { key: 'h', style: { margin: 0, fontSize: 15, lineHeight: 1.3, fontWeight: 600, overflowWrap: 'anywhere' }, children: text })

function CheckCell({ st, value, on, title, onClick }) {
  const [label, c] = CELL[st] || [st, TONE.muted]
  const flat = st === 'na' || st === 'none'
  return jsxs('button', {
    type: 'button',
    title,
    disabled: flat,
    onClick: () => { haptic('tap'); onClick() },
    className: 'grid min-w-0 content-center text-left',
    style: {
      gap: 3, minHeight: 46, padding: '5px 8px', fontFamily: MONO, fontSize: 11.5, cursor: flat ? 'default' : 'pointer',
      border: `1px ${flat ? 'dashed' : 'solid'} ${on ? 'var(--ui-accent)' : flat ? 'var(--ui-stroke-secondary)' : `color-mix(in srgb, ${c} 55%, var(--ui-stroke-secondary))`}`,
      boxShadow: on ? '0 0 0 1px var(--ui-accent)' : undefined,
      background: flat ? 'none' : st === 'stale'
        ? 'repeating-linear-gradient(135deg, transparent 0 8px, color-mix(in srgb, var(--ui-stroke-secondary) 45%, transparent) 8px 9px)'
        : `color-mix(in srgb, ${c} 12%, transparent)`,
      color: flat || st === 'stale' ? 'var(--ui-text-quaternary)' : undefined
    },
    children: [
      jsx('span', { style: { fontSize: 10, lineHeight: 1, fontWeight: 500, letterSpacing: '0.06em', textTransform: 'uppercase', color: flat ? undefined : c }, children: label }),
      jsx('span', { style: { overflowWrap: 'anywhere', lineHeight: 1.35 }, children: flat ? '' : value || '' })
    ]
  })
}

// Press, hold and drag a cut-off grid or map sideways (ref callback for an overflow-x-auto box).
// Under 5px of movement stays a click; after a real drag the click that follows is swallowed so
// letting go over a cell or bar does not open it.
function dragScroll(el) {
  if (!el || el.dataset.drag) return
  el.dataset.drag = '1'
  el.addEventListener('pointerenter', () => { el.style.cursor = el.scrollWidth > el.clientWidth ? 'grab' : '' })
  el.addEventListener('pointerdown', e => {
    if (e.button !== 0 || el.scrollWidth <= el.clientWidth || e.target.closest('input, textarea, select')) return
    const x0 = e.clientX, left = el.scrollLeft
    let moved = false
    const move = ev => {
      if (!moved && Math.abs(ev.clientX - x0) < 5) return
      moved = true
      el.style.cursor = 'grabbing'
      el.style.userSelect = 'none'
      el.scrollLeft = left - (ev.clientX - x0)
    }
    const up = () => {
      window.removeEventListener('pointermove', move)
      window.removeEventListener('pointerup', up)
      el.style.cursor = 'grab'
      el.style.userSelect = ''
      if (!moved) return
      const swallow = ev => { ev.stopPropagation(); ev.preventDefault() }
      el.addEventListener('click', swallow, { capture: true, once: true })
      setTimeout(() => el.removeEventListener('click', swallow, { capture: true }), 0)
    }
    window.addEventListener('pointermove', move)
    window.addEventListener('pointerup', up)
  })
}

// Rows against checks: a column per check (a square after one that blocks), a row name over a mono line.
function CheckGrid({ cols, rows }) {
  return jsx('div', {
    className: 'overflow-x-auto',
    ref: dragScroll,
    children: jsxs('div', {
      className: 'grid gap-1',
      style: { gridTemplateColumns: `minmax(9rem, 1.4fr) repeat(${cols.length}, minmax(5.5rem, 1fr))`, minWidth: `calc(10rem + ${cols.length} * 6rem)` },
      children: [
        jsx('span', {}, 'corner'),
        ...cols.map(c =>
          jsxs('span', {
            key: c.key,
            title: c.title,
            className: 'self-end',
            style: { ...LABEL, lineHeight: 1.3, letterSpacing: '0.06em', padding: '0 2px 4px', overflowWrap: 'anywhere' },
            children: c.blocks
              ? [c.key.replace(/[^-\s]+$/, ''), jsxs('span', { key: 'b', style: { whiteSpace: 'nowrap' }, children: [c.key.match(/[^-\s]*$/)[0], jsx('span', { style: { display: 'inline-block', width: 6, height: 6, marginLeft: 5, backgroundColor: DIM, verticalAlign: 1 } })] })]
              : c.key
          })
        ),
        ...rows.flatMap(r => [
          jsxs('div', {
            key: `${r.key}-name`,
            className: 'grid min-w-0 content-center',
            style: { gap: 1, padding: '6px 8px 6px 0' },
            children: [
              // a long name wraps; the line under it wraps to two lines (an error in full), the tooltip has it whole
              jsx('span', { title: r.name, style: { fontSize: 13, fontWeight: 600, lineHeight: 1.3, overflowWrap: 'anywhere' }, children: r.name }),
              jsx('span', { title: r.sub, style: { ...(r.subFull ? {} : { display: '-webkit-box', WebkitBoxOrient: 'vertical', WebkitLineClamp: 2, overflow: 'hidden' }), fontFamily: MONO, fontSize: 11, lineHeight: 1.35, overflowWrap: 'anywhere', color: r.subColor || 'var(--ui-text-quaternary)' }, children: r.sub })
            ]
          }),
          ...r.cells
        ])
      ]
    })
  })
}

// Stale is a host's state (no recent report); projects are checked on the spot, so their key leaves it out.
const checkLegend = stale => jsx('div', {
  className: 'flex flex-wrap',
  style: { gap: '6px 16px', marginTop: 12, fontFamily: MONO, fontSize: 11.5, color: DIM },
  children: [['Passing', TONE.good], ['Warning', TONE.warn], ['Failing', TONE.bad], ...(stale ? [['Stale, host not reporting', TONE.muted]] : []), ['Not applicable']].map(([l, c]) =>
    jsxs('span', { key: l, children: [jsx('i', { style: { display: 'inline-block', width: 9, height: 9, marginRight: 6, verticalAlign: -1, backgroundColor: c || 'transparent', outline: c ? undefined : '1px dashed var(--ui-stroke-secondary)' } }), l] })
  )
})

// The dashboard does not reach into other hosts: fixing drift files a kanban card with the failing
// checks, their evidence and fixes, for a profile whose worker does the fix. One open card per host.
// Hands failing checks to a profile as one kanban card, after a confirm step: a host's drift, or a project's findings.
// Who fixes is a choice, never the dashboard itself: a profile's worker from a kanban card, or (for a
// host, with `steps`) the person asking, who runs the standard's fix there. Either way the confirm step
// shows what will be fixed and how, and the host's next report says whether it worked (`fix`).
const FIX_STATE = { waiting: TONE.warn, fixed: TONE.good, 'still failing': TONE.bad }

function FixCard({ name, where, path, body, label, failing, steps, fix }) {
  const qc = useQueryClient()
  const [profile, setProfile] = useState('')
  const [confirm, setConfirm] = useState(null)  // 'card' or 'hand'
  const [busy, setBusy] = useState(false)
  const [note, setNote] = useState(null)
  const { data } = useQuery({ queryKey: [ID, 'assignees'], queryFn: () => api.rest('/assignees'), staleTime: 5 * 60_000 })
  const setup = useSetup().data
  // a card needs a kanban board; a host follows the "When a host drifts" setting, a project always offers the card
  const cards = !setup || setup.kanban
  const mode = steps ? setup?.drift_action || 'button' : 'button'
  const send = async () => {
    setBusy(true)
    setNote(null)
    try {
      const out = await api.rest(path, { method: 'POST', body: { ...body, profile: confirm === 'hand' ? null : profile } })
      setNote(confirm === 'hand'
        ? { text: `Recorded. Fix these on ${name}; its next report shows whether they pass.`, commands: out.commands }
        : { bad: !!out.warning, text: out.existing ? `Card ${out.card} for ${name} is already open (${out.status}); it is on the Handoffs tab.` : out.warning || (out.started ? `Card ${out.card} filed; ${profile} started working on it.` : `Card ${out.card} filed for ${profile} (${out.status}).`) })
      qc.invalidateQueries({ queryKey: [ID] })
    } catch (e) {
      setNote({ bad: true, text: e?.message || String(e) })
    }
    setConfirm(null)
    setBusy(false)
  }
  const cmds = (steps || []).filter(r => r.remediate)
  const pre = text => jsx('pre', { style: { ...RAW, userSelect: 'text' }, children: text })
  return jsxs('div', {
    className: 'space-y-2 rounded border border-(--ui-border) p-2 text-xs',
    children: [
      jsx('div', { className: 'text-[0.6875rem] font-medium text-(--ui-text-quaternary)', children: label }),
      fix
        ? jsx('div', { style: { color: FIX_STATE[fix.state] }, children:
            `Fix asked for ${fmtAgo(fix.ts)}, ${fix.via === 'hand' ? 'to run by hand' : `as card ${fix.card} for ${fix.via}`}: ${
              fix.state === 'waiting' ? `waiting for ${name}'s next report.`
              : fix.state === 'fixed' ? `fixed, ${name}'s report from ${fmtAgo(fix.report_ts)} passes ${fix.rules.length === 1 ? 'it' : 'them all'}.`
              : `still failing ${fix.failing.join(', ')} in ${name}'s report from ${fmtAgo(fix.report_ts)}.`}` })
        : null,
      confirm
        ? jsxs('div', {
            className: 'space-y-2',
            children: [
              steps?.length
                ? jsx('div', { className: 'space-y-1', children: steps.map(r => jsxs('div', { key: r.rule, children: [
                    jsx('div', { className: 'font-medium', children: r.title || r.rule }),
                    r.fix ? jsx('div', { className: 'text-(--ui-text-quaternary)', children: r.fix }) : null,
                    r.remediate ? pre(r.remediate) : null
                  ] })) })
                : null,
              jsxs('div', { className: 'flex flex-wrap items-center gap-2', children: [
                jsx('span', { className: 'flex-1', children: confirm === 'hand'
                  ? `Record that you are fixing ${plural(failing.length, 'failing check')} ${where} yourself? The dashboard runs nothing on ${name}.`
                  : `File a kanban card for ${profile} to fix ${failing.length} failing check${failing.length === 1 ? '' : 's'} ${where} (${failing.join(', ')}), and start it now?` }),
                jsx(Button, { size: 'xs', variant: 'default', disabled: busy, onClick: send, children: confirm === 'hand' ? 'Record' : 'File and start' }),
                jsx(Button, { size: 'xs', variant: 'ghost', disabled: busy, onClick: () => setConfirm(null), children: 'Cancel' })
              ] })
            ]
          })
        : mode === 'off'
        ? jsxs('div', { className: 'flex flex-wrap items-center gap-2 text-(--ui-text-quaternary)', children: [
            jsx('span', { className: 'flex-1', style: { userSelect: 'text' }, children: 'Fix buttons are off: When a host drifts is set to off.' + settingsHint(setup, 'drift_action', 'button') }),
            setup.settings_page ? jsx(Button, { size: 'xs', variant: 'ghost', onClick: () => host.navigate(SETTINGS_PATH), children: 'Open plugin settings' }) : null
          ] })
        : jsxs('div', {
            className: 'flex flex-wrap items-center gap-2',
            children: [
              cards && mode === 'button' ? jsxs(Select, {
                value: profile,
                onValueChange: setProfile,
                children: [
                  jsx(SelectTrigger, { className: 'h-7 w-48 text-xs', children: jsx(SelectValue, { placeholder: 'Hand to a profile' }) }),
                  jsx(SelectContent, { children: (data?.assignees || []).map(p => jsx(SelectItem, { key: p, value: p, children: p })) })
                ]
              }) : null,
              cards && mode === 'button' ? jsx(Button, { size: 'xs', variant: 'outline', disabled: !profile || busy, onClick: () => setConfirm('card'), children: 'File a fix card' }) : null,
              steps ? jsx(Button, { size: 'xs', variant: 'ghost', disabled: busy, onClick: () => setConfirm('hand'), children: "I'll run it" }) : null
            ]
          }),
      !confirm && !cards ? jsx('div', { className: 'text-[0.6875rem] text-(--ui-text-quaternary)', children: 'Fix cards need a kanban board: run hermes kanban init, or open the Kanban page.' }) : null,
      !confirm && cards && mode === 'automatic' ? jsx('div', { className: 'text-[0.6875rem] text-(--ui-text-quaternary)', children: setup.drift_profile
        ? `The gateway files the fix card for ${setup.drift_profile} automatically, at most once a day for ${name} (When a host drifts).`
        : 'When a host drifts is automatic, but Profile for automatic fix cards is empty, so no card is filed. Set it in the plugin settings.' + settingsHint(setup, 'drift_profile', '<profile>') }) : null,
      steps && !confirm && !cmds.length ? jsx('div', { className: 'text-[0.6875rem] text-(--ui-text-quaternary)', children: 'The standard gives no command to run for these checks; a rule can add one as remediate.' }) : null,
      note ? jsx('div', { style: { color: note.bad ? '#f85149' : '#3fb950', whiteSpace: 'pre-wrap' }, children: note.text }) : null,
      note?.commands?.length ? pre(note.commands.join('\n')) : null
    ]
  })
}

// A new rule goes to the standard as a merge request, never straight to main; the repo's CI
// checks it on the MR and a person merges it. Check kinds and severities are the ones the
// standard already uses, each with an existing rule's check as the starting point. In business mode
// (an organisation with Hermes skill sync on) it can go to the org's copy instead; the sync server
// decides whether it publishes or waits for an admin, and the role only picks the button's label.
function ProposeRule({ data }) {
  const org = data.org || {}
  const biz = org.mode === 'business'
  const verb = ['OWNER', 'ADMIN'].includes(org.role) ? 'Publish' : 'Propose'
  const [to, setTo] = useState(biz ? 'org' : 'repo')
  const examples = data.check_examples || {}
  const severities = data.severities?.length ? data.severities : ['warns', 'blocks']
  const blank = { kind: 'host_rules', id: '', title: '', severity: severities[0], applies_to: '', check: '', why: '', fix: '' }
  const [open, setOpen] = useState(false)
  const [f, setF] = useState(blank)
  const [busy, setBusy] = useState(false)
  const [note, setNote] = useState(null)
  const set = k => e => setF({ ...f, [k]: e.target.value })
  const send = async () => {
    setBusy(true)
    setNote(null)
    try {
      const { kind, applies_to, ...rule } = f
      if (kind === 'host_rules') rule.applies_to = applies_to.split(',').map(x => x.trim()).filter(Boolean)
      const out = await api.rest('/conformance/rules', { method: 'POST', body: { kind, rule, to: biz ? to : 'repo' } })
      setNote(out.mr_url
        ? { url: out.mr_url, text: 'Merge request opened. The rule applies once it is merged.' }
        : out.path ? { good: true, text: `Added to ${out.path}. Hosts check it on their next report.` }
        : { good: true, text: out.status === 'published'
            ? `Published to ${out.org}: the rule is in its ${out.skill} standard, and members' hosts check it after their next sync.`
            : `Proposed to ${out.org} as proposal ${out.proposal_id ?? ''}: an admin approves or rejects it, and nothing applies until then.` })
      setF(blank)
    } catch (e) {
      setNote({ bad: true, text: e?.message || String(e) })
    }
    setBusy(false)
  }
  const field = (k, placeholder) => jsx(Input, { className: 'h-7 text-xs', placeholder, value: f[k], onChange: set(k) })
  const area = (k, placeholder) => jsx(Textarea, { className: 'min-h-12 text-xs', placeholder, value: f[k], onChange: set(k) })
  return jsx(Section, {
    title: 'Propose a rule',
    children: !open
      ? jsxs('div', {
          className: 'flex flex-wrap items-center gap-2 text-xs',
          children: [
            muted(biz
              ? `Add a check to ${data.standard || 'the standard'}, ${data.local ? 'written into' : 'as a merge request on'} ${data.project} or shared with ${org.org_name} (your role: ${org.role}).`
              : data.local
                ? `Add a check to ${data.standard || 'the standard'}. It is written into ${data.project}.`
                : `Add a check to ${data.standard || 'the standard'}. It opens a merge request on ${data.project}; nothing changes until it is merged.`),
            jsx(Button, { size: 'xs', variant: 'outline', onClick: () => setOpen(true), children: 'New rule' }),
            note?.url ? jsx('a', { href: note.url, onClick: openOut, className: 'hover:underline', style: { color: '#3fb950' }, children: note.url }) : null,
            note?.good ? jsx('span', { style: { color: '#3fb950' }, children: note.text }) : null
          ]
        })
      : jsxs('div', {
          className: 'grid gap-2 text-xs',
          style: { gridTemplateColumns: 'repeat(auto-fill, minmax(14rem, 1fr))' },
          children: [
            biz ? jsx('div', { style: { gridColumn: '1 / -1' }, children: jsx(Seg, { value: to, onChange: setTo, options: [['org', `${verb} to ${org.org_name}`], ['repo', `${data.local ? 'Write to' : 'Merge request on'} ${data.project}`]] }) }) : null,
            jsxs(Select, {
              value: f.kind,
              onValueChange: v => setF({ ...f, kind: v }),
              children: [
                jsx(SelectTrigger, { className: 'h-7 text-xs', children: jsx(SelectValue, {}) }),
                jsx(SelectContent, { children: [jsx(SelectItem, { value: 'host_rules', children: 'Host rule (checked on each host)' }, 'h'), jsx(SelectItem, { value: 'repo_rules', children: 'Repo rule (checked in CI)' }, 'r')] })
              ]
            }),
            field('id', 'id, e.g. disk-space'),
            field('title', 'Title, e.g. Disk has room'),
            jsxs(Select, {
              value: f.severity,
              onValueChange: v => setF({ ...f, severity: v }),
              children: [
                jsx(SelectTrigger, { className: 'h-7 text-xs', children: jsx(SelectValue, {}) }),
                jsx(SelectContent, { children: severities.map(v => jsx(SelectItem, { key: v, value: v, children: `Severity: ${v}` })) })
              ]
            }),
            f.kind === 'host_rules' ? field('applies_to', 'Applies to: roles or OS, comma separated') : null,
            jsxs(Select, {
              value: '',
              onValueChange: v => setF({ ...f, check: examples[v] }),
              children: [
                jsx(SelectTrigger, { className: 'h-7 text-xs', children: jsx(SelectValue, { placeholder: 'Start the check from a kind' }) }),
                jsx(SelectContent, { children: Object.keys(examples).map(v => jsx(SelectItem, { key: v, value: v, children: v })) })
              ]
            }),
            jsx('div', { style: { gridColumn: '1 / -1' }, children: area('check', 'Check, as YAML: { kind: port, url: "http://127.0.0.1:9119/api/status", timeout_s: 2 }') }),
            area('why', 'Why it matters: what breaks, and how it showed up'),
            area('fix', 'Fix: the steps that bring a failing host or file back'),
            jsxs('div', {
              className: 'flex flex-wrap items-center gap-2',
              style: { gridColumn: '1 / -1' },
              children: [
                jsx(Button, { size: 'xs', variant: 'default', disabled: busy || !f.id || !f.title || !f.check || !f.why || !f.fix, onClick: send, children: biz && to === 'org' ? `${verb} to ${org.org_name}` : 'Open merge request' }),
                jsx(Button, { size: 'xs', variant: 'ghost', disabled: busy, onClick: () => { setOpen(false); setF(blank) }, children: 'Cancel' }),
                note?.url
                  ? jsx('a', { href: note.url, onClick: openOut, className: 'hover:underline', style: { color: '#3fb950' }, children: `${note.text} ${note.url}` })
                  : note ? jsx('span', { style: { color: note.good ? '#3fb950' : '#f85149', whiteSpace: 'pre-wrap' }, children: note.text }) : null
              ]
            })
          ]
        })
  })
}

// Which mode this install is in. It is detected, not chosen: business needs a Hermes account in a
// shared organisation with skill sync on, so the bar shows both modes, marks the one in force and
// says what the other would take.
function ModeBar({ data }) {
  const org = data.org || {}
  const biz = org.mode === 'business'
  const pill = (on, text) => jsx('span', {
    'aria-current': on ? 'true' : undefined,
    className: cn('rounded border px-2 py-0.5 text-[0.6875rem]', on ? 'border-(--ui-accent) bg-(--ui-accent)/10 text-(--ui-accent)' : 'border-(--ui-stroke-secondary) text-(--ui-text-quaternary)'),
    children: text
  })
  const std = data.project ? `conformance.yaml in ${data.project}` : 'the built-in checks (no Conformance project set)'
  return jsxs('div', {
    className: 'flex flex-wrap items-center gap-2 text-xs',
    children: [
      jsx('span', { className: 'text-(--ui-text-quaternary)', children: 'Mode' }),
      pill(!biz, 'Single user'),
      pill(biz, 'Business'),
      jsx('span', { className: 'text-(--ui-text-quaternary)', children: biz
        ? `${org.org_name}, signed in as ${org.role}. Rules can go to the organisation's copy of ${data.standard || 'the standard'}, or as a merge request on ${data.project}.`
        : org.org_id
          ? `You are in ${org.org_name} (${org.role}), but business mode is off: ${org.reason}. Turn on Hermes skill sync to share the standard with the organisation.`
          : `The standard is ${std}. Business mode turns on by itself when this Hermes account is in a shared organisation with skill sync on${org.reason ? ` (now: ${org.reason})` : ''}.` })
    ]
  })
}

// A label over a value; with onPick it is a button that selects what it counts.
const sumRows = list =>
  jsx('div', {
    className: 'grid',
    style: { gap: 6 },
    children: list.map(([k, v, onPick]) =>
      jsxs(onPick ? 'button' : 'div', {
        key: k,
        type: onPick ? 'button' : undefined,
        onClick: onPick ? () => { haptic('tap'); onPick() } : undefined,
        className: cn('grid text-left', onPick && 'hover:underline'),
        style: { gridTemplateColumns: 'minmax(0, 1fr) auto', gap: 10 },
        children: [jsx('span', { style: { overflowWrap: 'anywhere' }, children: k }), jsx('span', { style: { fontFamily: MONO, fontSize: 12, color: onPick ? 'var(--ui-accent)' : DIM }, children: v })]
      })
    )
  })

// The side panel with nothing selected: how many hosts conform, which need a look, the standard, and each host check's count.
function ConformanceSummary({ data, hosts, hrules, bad, onPick }) {
  const rules = data.repo || []
  const quiet = hosts.filter(h => !h.ts || h.stale)
  const drifted = hosts.filter(h => h.ts && !h.stale && hrules.some(r => bad(h, r)))
  const ok = hosts.filter(h => h.declared && h.ts && !h.stale && !hrules.some(r => bad(h, r))).length
  const rfail = rules.filter(r => r.status === 'fail').length, rwarn = rules.filter(r => r.status === 'warn').length
  const look = [
    ...drifted.map(h => {
      const failing = hrules.filter(r => bad(h, r))
      return [h.host, `drifted, ${plural(failing.length, 'check')}`, () => onPick({ host: h.host, rule: failing[0].rule })]
    }),
    ...quiet.map(h => [h.host, h.ts ? `last report ${fmtAgo(h.ts)}` : 'never reported', () => onPick({ host: h.host, rule: hrules[0].rule })])
  ]
  return [
    sideTitle(hosts.length ? `${ok} of ${plural(hosts.length, 'host')} conform` : 'Repository checks'),
    drawerText('p', `${hosts.length ? `${drifted.length} drifted, ${quiet.length} not reporting. ` : ''}The repository has ${plural(rfail, 'failing check')} and ${plural(rwarn, 'warning')}.`),
    look.length ? drawerPart('look', 'Hosts to look at', sumRows(look)) : null,
    quiet.length ? drawerText('quiet', 'A host reports every few minutes once its metrics forwarder\'s CONFORMANCE_STANDARD points at a checkout of the standard.') : null,
    drawerPart('std', 'Standard', sumRows([[`${data.standard || 'unnamed'}, ${data.project}`, data.pipeline?.sha || ''], ['Host checks', hrules.length], ['Repository checks', rules.length]])),
    hrules.length
      ? drawerPart('by', 'By check', sumRows(hrules.map(r => {
          const n = hosts.filter(h => !h.stale && bad(h, r)).length
          return [r.title || r.rule, n ? plural(n, 'host') : 'ok', n ? () => onPick({ check: 'host', rule: r.rule }) : null]
        })))
      : null
  ]
}

const homeShort = path => path.replace(/^\/(Users|home)\/[^/]+/, '~')

// The projects part of the summary: how many pass, and each rule's count of projects that fail it.
// A rule a project does not check (another project's conformance.yaml rule) is not a failure there.
const pbad = (p, r) => ['fail', 'warn'].includes(p.results[r.id]?.status)

function ProjectsSummary({ pc, alone, onPick }) {
  const rows = pc.projects.filter(p => !p.error)
  const ok = rows.filter(p => !pc.rules.some(r => pbad(p, r))).length
  const byRule = sumRows(pc.rules.map(r => {
    const n = rows.filter(p => pbad(p, r)).length
    return [r.title || r.id, n ? plural(n, 'project') : 'ok', n ? () => onPick({ check: 'proj', rule: r.id }) : null]
  }))
  return [
    alone ? sideTitle(`${ok} of ${plural(rows.length, 'project')} pass`) : null,
    drawerPart('proj', alone ? 'By check' : `Projects, ${ok} of ${rows.length} pass`, byRule)
  ]
}

function ProjectCellSide({ p, rule, name, failing, onClear }) {
  const res = p.results[rule.id]
  return [
    jsx('div', { key: 'tag', children: jsx(Tag, { tone: CELL[res.status][1] || 'muted', children: CELL[res.status][0] }) }),
    jsxs('div', { key: 'head', className: 'grid', style: { gap: 4 }, children: [sideTitle(`${rule.title || rule.id} in ${name}`), drawerText('sub', homeShort(p.root))] }),
    facts('f', [['Found', res.count ? plural(res.count, 'line') : 'clean'], ['Severity', rule.severity], ['Rule from', rule.source === 'built-in' ? 'the built-in standard' : rule.source === 'conformance.yaml' ? "this project's conformance.yaml" : rule.source], ['Checked', fmtAgo(p.scanned_at)]]),
    drawerText('how', 'Checked here over the files git tracks in this folder, as they are on disk now.'),
    res.findings.length
      ? drawerPart('where', res.count > res.findings.length ? `Where, the first ${res.findings.length}` : 'Where', jsx('div', {
          style: { ...RAW, padding: '2px 0', lineHeight: 1.45, maxHeight: '18rem', overflowY: 'auto' },
          children: res.findings.map((f, i) => jsxs('div', { key: i, style: { ...FINDING, borderTop: i ? FINDING.borderTop : 'none' }, children: [jsx('div', { style: { color: 'var(--ui-accent)' }, children: `${f.file}:${f.line}` }), jsx('div', { children: f.text })] }))
        }))
      : null,
    rule.fix && res.status !== 'pass' ? drawerPart('fix', 'How to fix', drawerText('t', rule.fix)) : null,
    failing.length ? jsx(FixCard, { key: `fix-${p.root}`, name, where: `in ${name}`, path: '/conformance/projects/fix', body: { root: p.root }, label: `Fix findings in ${name}`, failing }) : null,
    jsx('div', { key: 'go', className: 'flex flex-wrap gap-2', children: jsx('button', { type: 'button', className: 'hover:bg-(--chrome-action-hover)', style: BTN, onClick: onClear, children: 'Clear selection' }) })
  ]
}

// One check across the fleet or the projects: what fails it, each opening to its evidence.
function CheckListSide({ rule, unit, items, onPick, onClear }) {
  return [
    jsxs('div', { key: 'head', className: 'grid', style: { gap: 4 }, children: [sideTitle(rule.title || rule.id || rule.rule), drawerText('sub', items.length ? `${plural(items.length, unit)} ${items.length === 1 ? 'fails' : 'fail'} this check${rule.severity ? `, which ${rule.severity}` : ''}.` : `No ${unit} fails this check now.`)] }),
    items.length ? drawerPart('who', 'Failing', sumRows(items.map(x => [x.name, x.value, () => onPick(x.sel)]))) : null,
    rule.fix ? drawerPart('fix', 'How to fix', drawerText('t', rule.fix)) : null,
    jsx('div', { key: 'go', className: 'flex flex-wrap gap-2', children: jsx('button', { type: 'button', className: 'hover:bg-(--chrome-action-hover)', style: BTN, onClick: onClear, children: 'Clear selection' }) })
  ]
}

function HostCellSide({ host, rule, failing, steps, fix, onClear }) {
  const res = host.results[rule.rule]
  const st = hostCell(host, res)
  return [
    jsx('div', { key: 'tag', children: jsx(Tag, { tone: CELL[st][1] || 'muted', children: CELL[st][0] }) }),
    jsxs('div', { key: 'head', className: 'grid', style: { gap: 4 }, children: [sideTitle(`${rule.title || rule.rule} on ${host.host}`), drawerText('sub', [host.os, ...(host.roles || [])].filter(Boolean).join(', '))] }),
    facts('f', [['Found', res?.value || res?.status || 'not run'], ['Severity', rule.severity || 'none'], ['Checked', host.ts ? fmtAgo(host.ts) : 'never']]),
    host.stale ? drawerText('stale', `No report since ${fmtAgo(host.ts)}, so this is the last known result against standard ${host.standard_rev || 'unknown'}, not a current one.`) : null,
    host.verified ? null : drawerText('unv', 'Unverified: the sender\'s ingest token may not report as this host.'),
    !host.ts
      ? drawerText('nr', 'This host has never reported. It reports every few minutes once its metrics forwarder\'s CONFORMANCE_STANDARD points at a checkout of the standard.')
      : res ? null : drawerText('nr', 'This host\'s last report did not include this rule; its checkout of the standard may be older than the rule.'),
    res?.evidence?.length ? drawerPart('ev', 'Evidence', jsx('pre', { style: RAW, children: res.evidence.join('\n') })) : null,
    rule.fix && (st === 'fail' || st === 'warn') ? drawerPart('fix', 'How to fix', drawerText('t', rule.fix)) : null,
    failing.length ? jsx(FixCard, { key: `fix-${host.host}`, name: host.host, where: `on ${host.host}`, path: `/conformance/hosts/${encodeURIComponent(host.host)}/fix`, body: {}, label: `Fix drift on ${host.host}`, failing, steps, fix }) : null,
    jsxs('div', {
      key: 'go',
      className: 'flex flex-wrap gap-2',
      children: [
        jsx(GoLink, { key: 'map', btn: true, tab: 'topology', sel: `h:${host.host}`, children: 'Show on the map' }),
        jsx('button', { key: 'clear', type: 'button', className: 'hover:bg-(--chrome-action-hover)', style: BTN, onClick: onClear, children: 'Clear selection' })
      ]
    })
  ]
}

function RepoCellSide({ r, data, onClear }) {
  const st = CELL[r.status] ? r.status : 'none'
  return [
    jsx('div', { key: 'tag', children: jsx(Tag, { tone: CELL[st][1] || 'muted', children: CELL[st][0] }) }),
    jsxs('div', { key: 'head', className: 'grid', style: { gap: 4 }, children: [sideTitle(r.title || r.rule), drawerText('sub', `${data.project}, main ${data.pipeline?.sha || ''}`)] }),
    facts('f', [['Found', repoValue(r)], ['Severity', r.severity || 'none'], ['Checked by', r.ci_job ? `CI job ${r.ci_job}` : 'the standard job']]),
    r.ci_job ? drawerText('ci', `A failure in the CI job "${r.ci_job}" fails the pipeline.`) : null,
    r.findings.length
      ? drawerPart('where', 'Where in the repo', jsx('div', {
          style: { ...RAW, padding: '2px 0', lineHeight: 1.45, maxHeight: '18rem', overflowY: 'auto' },
          children: r.findings.map((f, i) =>
            jsxs('div', {
              key: i,
              style: { ...FINDING, borderTop: i ? FINDING.borderTop : 'none' },
              children: [
                jsx('a', { href: data.repo_url ? `${data.repo_url}/-/blob/main/${f.file}#L${f.line}` : undefined, onClick: openOut, className: 'block hover:underline', style: { color: 'var(--ui-accent)' }, children: `${f.file}:${f.line}` }),
                jsx('div', { children: f.text })
              ]
            })
          )
        }))
      : null,
    r.fix && r.status !== 'pass' && r.status !== 'enforced_by_job' ? drawerPart('fix', 'How to fix', drawerText('t', r.fix)) : null,
    jsxs('div', {
      key: 'go',
      className: 'flex flex-wrap gap-2',
      children: [
        data.repo_url ? jsx('a', { key: 'gl', href: data.repo_url, onClick: openOut, className: 'hover:bg-(--chrome-action-hover)', style: BTN, children: 'Open in GitLab' }) : null,
        jsx('button', { key: 'clear', type: 'button', className: 'hover:bg-(--chrome-action-hover)', style: BTN, onClick: onClear, children: 'Clear selection' })
      ]
    })
  ]
}

// No GitLab needed: a starter conformance.yaml under ~/.hermes/conformance, used while the setting is empty.
function StartLocalStandard() {
  const qc = useQueryClient()
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState('')
  const start = async () => {
    setBusy(true)
    try {
      await api.rest('/conformance/local', { method: 'POST', body: {} })
      qc.invalidateQueries({ queryKey: [ID] })
    } catch (e) {
      setErr(e?.message || String(e))
    }
    setBusy(false)
  }
  return jsxs('div', {
    className: 'flex flex-wrap items-center gap-2 text-xs',
    children: [
      jsx(Button, { size: 'xs', variant: 'outline', disabled: busy, onClick: start, children: 'Start a local standard' }),
      err ? jsx('span', { style: { color: TONE.bad }, children: err }) : null
    ]
  })
}

// sel `host:<name>:<rule>` opens on that host's check, as Topology links a drifted host's failing checks.
function ConformancePage({ sel }) {
  const { data, isLoading, isError } = useConformance()
  const projects = useProjectChecks()
  const [selected, setSelected] = useState(() => {
    const [kind, host, ...rule] = (sel || '').split(':')
    return kind === 'host' && host && rule.length ? { host, rule: rule.join(':') } : null
  })
  if (isError) return jsx('div', { className: 'p-4', children: muted('Conformance unavailable: the /api/plugins/operations backend did not respond.') })
  if (isLoading || !data) return jsx('div', { className: 'p-4', children: jsx(Skeleton, { className: 'h-24 w-full' }) })
  const fleet = !!data.project
  const pc = projects.data
  const err = { ...(data.errors || {}), ...(pc?.errors?.rules ? { prules: pc.errors.rules } : {}) }
  const rules = data.repo || []
  const hrules = data.host_rules || []
  const hosts = hrules.length ? data.hosts || [] : []
  const bad = (h, r) => ['fail', 'warn'].includes(h.results[r.rule]?.status)
  const p = data.pipeline
  const selHost = selected?.host && hosts.find(h => h.host === selected.host)
  const selRule = selHost && hrules.find(r => r.rule === selected.rule)
  const selRepo = selected?.repo && rules.find(r => r.rule === selected.repo)
  const selProj = selected?.proj && pc?.projects.find(x => x.root === selected.proj && !x.error)
  const selPRule = selProj && pc.rules.find(r => r.id === selected.rule)
  const selCheck = selected?.check === 'host' ? hrules.find(r => r.rule === selected.rule) : selected?.check === 'proj' ? pc?.rules.find(r => r.id === selected.rule) : null
  const projName = x => pc.names[x.root] || x.root.split(/[\\/]/).pop()
  const projRows = (pc?.projects || []).map(x => ({
    key: x.root,
    name: projName(x),
    sub: x.rules_error || `${x.root === pc.current ? 'current, ' : pc.created?.includes(x.root) ? 'project, ' : ''}${homeShort(x.root)}`,
    subColor: x.rules_error ? TONE.warn : x.root === pc.current ? 'var(--ui-accent)' : undefined,
    subFull: !!x.rules_error,
    cells: x.error
      ? [jsx('div', { key: `${x.root}-err`, className: 'self-center text-xs', style: { gridColumn: `span ${pc.rules.length}`, color: 'var(--ui-text-quaternary)' }, children: `Not checked: ${x.error}` })]
      : pc.rules.map(r => {
          const res = x.results[r.id]
          return jsx(CheckCell, { key: `${x.root}-${r.id}`, st: res?.status || 'na', value: res ? (res.count ? plural(res.count, 'line') : 'clean') : '', on: selected?.proj === x.root && selected?.rule === r.id, title: `${r.title || r.id} in ${projName(x)}`, onClick: () => setSelected({ proj: x.root, rule: r.id }) })
        })
  }))
  const isOn = (host, rule) => selected?.host === host && selected?.rule === rule
  const clear = () => setSelected(null)
  const cols = list => list.map(r => ({ key: r.rule, title: `${r.title || r.rule}, ${r.severity || 'no severity'}`, blocks: r.severity === 'blocks' }))
  const hostRows = hosts.map(h => ({
    key: h.host,
    name: h.host,
    sub: [h.os, ...(h.roles || []), !h.ts ? 'never reported' : `${h.stale ? 'last report' : 'reported'} ${fmtAgo(h.ts)}`, h.verified ? null : 'unverified'].filter(Boolean).join(', '),
    subColor: h.stale && h.ts ? TONE.warn : undefined,
    cells: h.results.unclassified
      ? [jsx('div', { key: `${h.host}-unclassified`, className: 'self-center text-xs', style: { gridColumn: `span ${hrules.length}`, color: TONE.warn }, children: 'Unclassified: this host reports but is not in the standard\'s hosts:' })]
      : hrules.map(r => {
          const res = h.results[r.rule]
          return jsx(CheckCell, { key: `${h.host}-${r.rule}`, st: hostCell(h, res), value: res?.value || res?.status, on: isOn(h.host, r.rule), title: `${r.title || r.rule} on ${h.host}`, onClick: () => setSelected({ host: h.host, rule: r.rule }) })
        })
  }))
  const repoRow = {
    key: 'repo',
    name: (data.project || '').split('/').pop(),
    sub: `main ${p?.sha || ''}, checked in CI`,
    cells: rules.map(r =>
      jsx(CheckCell, { key: r.rule, st: CELL[r.status] ? r.status : 'none', value: repoValue(r), on: selected?.repo === r.rule, title: r.title || r.rule, onClick: () => setSelected({ repo: r.rule }) })
    )
  }
  const sub = text => jsx('span', { className: 'text-[0.6875rem] text-(--ui-text-quaternary)', children: text })
  const projSection = jsx(Section, {
    title: 'Projects',
    count: `The built-in standard${fleet ? `, ${data.standard || data.project}` : ''} and each project's own conformance.yaml, checked on this machine.`,
    children: projects.isError
      ? muted('Projects unavailable: the backend did not answer.')
      : !pc
        ? jsx(Skeleton, { className: 'h-24 w-full' })
        : !pc.listed
          ? muted('This Desktop build does not list projects to plugins.')
          : projRows.length
            ? [jsx(CheckGrid, { key: 'g', cols: pc.rules.map(r => ({ key: r.id, title: `${r.title || r.id}, ${r.severity}${r.source === 'built-in' ? ', built in' : `, from ${r.source}`}`, blocks: r.severity === 'blocks' })), rows: projRows }), jsx('div', { key: 'legend', children: checkLegend(false) })]
            : muted('No projects yet. Open a folder in Hermes and it shows here.')
  })
  const side = selRule
    ? jsx(HostCellSide, { key: `${selHost.host}|${selRule.rule}`, host: selHost, rule: selRule, failing: hrules.filter(r => bad(selHost, r)).map(r => r.rule), steps: hrules.filter(r => bad(selHost, r)), fix: data.fixes?.[selHost.host], onClear: clear })
    : selRepo
      ? jsx(RepoCellSide, { key: selRepo.rule, r: selRepo, data, onClear: clear })
      : selPRule
        ? jsx(ProjectCellSide, { key: `${selProj.root}|${selPRule.id}`, p: selProj, rule: selPRule, name: projName(selProj), failing: pc.rules.filter(r => pbad(selProj, r)).map(r => r.id), onClear: clear })
        : selCheck && selected.check === 'host'
          ? CheckListSide({ rule: selCheck, unit: 'host', onPick: setSelected, onClear: clear, items: hosts.filter(h => !h.stale && bad(h, selCheck)).map(h => ({ name: h.host, value: h.results[selCheck.rule].value || h.results[selCheck.rule].status, sel: { host: h.host, rule: selCheck.rule } })) })
        : selCheck
          ? CheckListSide({ rule: selCheck, unit: 'project', onPick: setSelected, onClear: clear, items: pc.projects.filter(x => !x.error && pbad(x, selCheck)).map(x => ({ name: projName(x), value: plural(x.results[selCheck.id].count, 'line'), sel: { proj: x.root, rule: selCheck.id } })) })
        : [
            ...(fleet ? ConformanceSummary({ data, hosts, hrules, bad, onPick: setSelected }) : []),
            ...(pc?.projects.length ? ProjectsSummary({ pc, alone: !fleet, onPick: setSelected }) : []),
            fleet ? null : drawerText('off', 'Fleet checks are off. Set "Conformance project" in Capabilities > Plugins > Operations to a GitLab repo (group/repo) or a local conformance.yaml, or start a local standard here, to add host checks and repository rules of your own.'),
            fleet ? null : jsx(StartLocalStandard, { key: 'local' }),
            drawerText('n', 'Select a cell for the rule, the evidence and the fix.')
          ]
  return jsxs('div', {
    className: 'space-y-3 p-4',
    children: [
      fleet ? jsxs('div', {
        className: 'flex flex-wrap items-center gap-2 text-xs',
        children: [
          jsx('span', { className: 'text-(--ui-text-quaternary)', children: 'Fleet standard' }),
          jsx('span', { className: 'font-medium', children: data.standard || 'unnamed' }),
          jsx('span', { className: 'text-(--ui-text-quaternary)', children: 'from' }),
          jsx('a', { href: data.repo_url, onClick: openOut, className: 'text-(--ui-text-quaternary) hover:underline', children: data.project }),
          jsx('span', { className: 'text-(--ui-text-quaternary)', children: data.local ? '(a local file)' : '(the Conformance project setting)' }),
          jsx('span', { className: 'ml-auto' }),
          p
            ? jsx('a', {
                href: p.url,
                onClick: openOut,
                className: 'text-[0.6875rem] text-(--ui-text-quaternary) hover:underline',
                style: { color: p.status === 'success' ? undefined : TONE.warn },
                children: `main pipeline ${p.status} · ${p.sha} · ${fmtAgo(p.ts)}`
              })
            : null
        ]
      }) : null,
      data.org ? jsx(ModeBar, { key: 'mode', data }) : null,
      ...Object.entries(err).map(([k, v]) =>
        jsx('div', { key: k, className: 'text-xs text-(--ui-text-quaternary)', children: `${{ report: 'Report', rules: 'Rule text', pipeline: 'Pipeline', checker: 'Checker', hosts: 'Host checks', prules: 'The standard\'s rules for projects' }[k] || k} unavailable: ${v}` })
      ),
      jsxs('div', {
        className: 'flex flex-wrap items-start gap-3',
        children: [
          jsxs('div', {
            className: 'grid',
            style: { flex: '999 1 34rem', minWidth: 0, gap: 12, gridTemplateColumns: 'minmax(0, 1fr)' },
            children: [fleet ? jsx(Section, {
              key: 'fleet',
              title: hosts.length ? 'Hosts against the standard' : 'Repository',
              count: hosts.length ? 'Each cell is one check on one host. A square after a name means it blocks.' : 'The standard, checked in CI on main.',
              children: [
                hosts.length ? jsx(CheckGrid, { key: 'hosts', cols: cols(hrules), rows: hostRows }) : null,
                hosts.length
                  ? jsxs('div', { key: 'rh', className: 'flex flex-wrap items-baseline gap-2', style: { margin: '18px 0 8px' }, children: [jsx('span', { className: 'text-xs font-medium', children: 'Repository' }), sub('The same standard, checked in CI on main.')] })
                  : null,
                rules.length ? jsx(CheckGrid, { key: 'repo', cols: cols(rules), rows: [repoRow] }) : jsx('div', { key: 'none', children: muted('No report yet.') }),
                jsx('div', { key: 'legend', children: checkLegend(hosts.length > 0) })
              ]
            }) : null, jsx('div', { key: 'proj', children: projSection })]
          }),
          jsx('div', { style: { ...SIDE, flex: '1 1 18rem', minWidth: 0 }, children: side })
        ]
      }),
      fleet ? jsx(ProposeRule, { data }) : null
    ]
  })
}

// --- page -----------------------------------------------------------------------------

// A Recent activity entry, with its raw lines from the Activity log (same window, same kind, text and place).
function ActivityDrawer({ a, hours, onClose }) {
  const { data, isLoading } = useActivity(hours)
  const e = data?.items.find(x => x.kind === a.kind && x.text === a.text && x.where === a.where)
  return jsx(Drawer, {
    label: a.text,
    onClose,
    head: [
      jsx(Tag, { key: 'k', tone: activityTone(a.kind), children: a.kind }),
      jsx('span', { key: 't', className: 'tabular-nums', style: { fontFamily: MONO, color: 'var(--ui-text-quaternary)' }, children: fmtClock(a.ts) })
    ],
    children: [
      drawerTitle(a.text),
      drawerText('sub', `${a.where ? `${a.where} · ` : ''}${a.count > 1 ? `${a.count} times since ${fmtClock(a.first_ts)}` : 'once'} · last ${hours}h`),
      isLoading
        ? jsx(Skeleton, { key: 'b', className: 'h-16 w-full' })
        : jsx(EntryDetail, { key: 'b', drawer: true, e: e || { ...a, category: a.kind.startsWith('task') ? 'task' : 'incident', raw: [] } })
    ]
  })
}

// A host or pool device: its stats, models and recent activity, and where to see more.
function HostDrawer({ c, activity, hours, onOpen, onClose }) {
  const { name, device, h, ms } = c
  const mem = h?.mem_total_gb ? h.mem_used_gb / h.mem_total_gb : null
  const mine = activity.filter(a => a.where === name || (device && a.where === device))
  return jsx(Drawer, {
    label: name,
    onClose,
    head: [
      jsx('span', { key: 'd', style: { width: 8, height: 8, borderRadius: 4, flex: 'none', backgroundColor: !h ? TONE.muted : h.stale ? TONE.warn : TONE.good } }),
      jsx('span', { key: 's', style: { fontFamily: MONO, fontSize: 12, color: DIM }, children: !h ? 'pool only' : device && device !== name ? `pool device ${device}` : device ? 'in the model pool' : 'not in the model pool' })
    ],
    children: [
      drawerTitle(name),
      drawerText('sub', !h ? 'Serving models; sends no host stats.' : h.stale ? `Quiet: no host stats for ${fmtAgo(h.last_seen).replace(/ ago$/, '')}.` : `Live, load ${h.cpu_load?.toFixed(1) ?? '-'}, memory ${h.mem_used_gb?.toFixed(0) ?? '-'} of ${h.mem_total_gb?.toFixed(0) ?? '-'} GB.`),
      mem != null
        ? jsx('div', { key: 'mem', title: `memory ${Math.round(mem * 100)}% used`, style: { height: 5, overflow: 'hidden', backgroundColor: 'color-mix(in srgb, var(--ui-stroke-secondary) 70%, transparent)' },
                       children: jsx('div', { style: { height: '100%', width: `${Math.min(100, mem * 100)}%`, backgroundColor: mem > 0.85 ? TONE.warn : TONE.info } }) })
        : null,
      drawerPart('m', 'Models loaded', drawerText('mv', ms.length ? ms.map(m => (m.count > 1 ? `${m.name} x${m.count}` : m.name)).join(', ') : device ? 'None loaded.' : 'Not in the model pool.')),
      drawerPart('a', 'Recent activity here', mine.length
        ? jsx('div', {
            className: 'divide-y divide-(--ui-stroke-secondary)',
            children: mine.map((a, i) =>
              jsxs('div', {
                key: i,
                ...opens(() => onOpen(a)),
                className: 'hover:bg-(--chrome-action-hover)',
                style: { display: 'grid', gridTemplateColumns: '4.5rem minmax(0, 1fr)', gap: 10, padding: '10px 4px', cursor: 'pointer' },
                children: [
                  jsx('span', { className: 'tabular-nums', style: { fontFamily: MONO, fontSize: 12, color: DIM }, children: fmtClock(a.ts) }),
                  jsxs('div', { className: 'space-y-1', children: [jsx(Tag, { tone: activityTone(a.kind), children: a.kind }), jsx('div', { style: { fontWeight: 600, overflowWrap: 'anywhere' }, children: a.text })] })
                ]
              }))
          })
        : drawerText('n', 'No activity on this host in the window.')),
      jsxs('div', {
        key: 'go',
        className: 'flex flex-wrap gap-2',
        children: [
          jsx(GoLink, { btn: true, tab: 'topology', sel: h ? `h:${name}` : `s:${device}`, hours, children: 'Show on the map' }),
          device ? jsx(GoLink, { btn: true, tab: 'flow', sel: `served:${device}`, hours, children: 'See its traffic' }) : null,
          h ? jsx(GoLink, { btn: true, tab: 'conformance', children: 'Host checks' }) : null
        ]
      })
    ]
  })
}

// --- first-run setup ------------------------------------------------------------------

const SETTINGS_PATH = `/settings?tab=plugins&plugin=${ID}`
// Desktop builds the settings form only in a profile with its own copy of the plugin; elsewhere a
// setting is one hermes config set away, so say that instead of linking to an empty page.
const settingsCmd = (s, key, value) => `hermes -p ${s.profile} config set plugins.entries.${ID}.settings.${key} ${value}`
const settingsHint = (s, key, value) => s.settings_page ? '' : ` In this profile, run: ${settingsCmd(s, key, value)}`
const SETUP_HIDDEN = 'setup.hidden'
const useSetup = () => useQuery({ queryKey: [ID, 'setup'], queryFn: () => api.rest('/setup'), staleTime: 60_000 })

// What a first run still needs, each with where to fix it. Discord alerts are optional and never
// count as left; the drift step only exists with a kanban board, since cards need one.
function setupSteps(s) {
  const missing = s.profiles.filter(p => p.state === 'missing').map(p => p.profile)
  // no settings form in this profile: the button copies the command the step names instead
  const settings = (key, value) => s.settings_page
    ? { label: 'Open plugin settings', onClick: () => host.navigate(SETTINGS_PATH) }
    : { label: 'Copy command', onClick: () => navigator.clipboard?.writeText(settingsCmd(s, key, value)).catch(() => {}) }
  const fromDefault = key => (s.from_default || []).includes(key) ? ' Set in the default profile.' : ''
  return [
    { id: 'profiles', done: !missing.length, title: 'Turn Operations on in every profile',
      text: missing.length
        ? `Not on in ${missing.join(', ')}. Desktop shows this page only for profiles that list it: add "- operations" under plugins: enabled: in each one's config.yaml (~/.hermes/profiles/<profile>/config.yaml, or ~/.hermes/config.yaml for default), then restart Hermes Desktop. A profile that lists it under plugins: disabled: is left as it is.`
        : 'On in every profile.' },
    { id: 'metrics', done: s.metrics, title: 'Connect the metrics collector',
      text: s.metrics ? 'metrics.db found.' : 'No metrics.db under ~/.hermes/metrics yet. Install hermes-metrics-dash (github.com/c-pompa/hermes-metrics-dash) on this machine and its forwarder on each host; until then requests, hosts and the model pool read as empty.' },
    { id: 'kanban', done: s.kanban, title: 'Create the kanban board',
      text: s.kanban ? 'Board found.' : 'No kanban board yet. Run hermes kanban init (or open the Kanban page) so cards can be handed to profiles and fix cards can be filed.' },
    { id: 'conformance', done: !!s.conformance_project, title: 'Pick the fleet standard',
      text: s.conformance_project ? `Checking ${s.conformance_project}.${fromDefault('conformance_project')}` : 'Set Conformance project to a GitLab repo whose CI publishes conformance-report.json or to a local conformance.yaml, or start a local standard on the Conformance tab; its fleet checks stay off until then.' + settingsHint(s, 'conformance_project', '<group/project>'),
      action: s.conformance_project ? null : settings('conformance_project', '<group/project>') },
    s.kanban ? { id: 'drift', done: s.drift_action !== 'automatic' || !!s.drift_profile, title: 'Choose what happens when a host drifts',
      text: s.drift_action === 'automatic'
        ? (s.drift_profile ? `Fix cards are filed automatically for ${s.drift_profile}.${fromDefault('drift_action')}` : 'Automatic is on but no profile is set, so no card is filed. Set Profile for automatic fix cards.')
        : (s.drift_action === 'off' ? 'Off: the Conformance tab only shows failing checks.' : 'The Conformance tab offers a fix-by-card button (the default). Automatic files and starts the card for a profile; off hides the buttons.') + settingsHint(s, 'drift_action', 'button|automatic|off'),
      action: settings('drift_action', 'button') } : null,
    { id: 'alerts', done: true, optional: true, title: 'Discord alerts (optional)',
      text: s.alert_target ? `Posting to ${s.alert_target}.${fromDefault('alert_target')}` : 'Alerts show in Desktop while it runs. Set Discord alerts so the gateway posts them with Desktop closed.' + settingsHint(s, 'alert_target', 'discord'),
      action: s.alert_target ? null : settings('alert_target', 'discord') }
  ].filter(Boolean)
}

// Once per install: a toast saying how many setup steps are left (or that this profile does not
// have Operations on), pointing at the checklist on the Notifications tab.
const SETUP_TOASTED = 'setup.toasted'

function setupToast(ctx) {
  if (ctx.storage.get(SETUP_TOASTED, false)) return
  const open = { label: 'Open Operations', onClick: () => host.navigate(PATH) }
  ctx.rest('/setup').then(s => {
    const left = setupSteps(s).filter(x => !x.done).length
    if (left) host.notify({ kind: 'info', title: 'Operations', message: `${plural(left, 'setup step')} left`, detail: 'The Notifications tab lists each one and where to do it; Overview links to it.', action: open })
    ctx.storage.set(SETUP_TOASTED, true)
  }, e => {
    if (!/not enabled/.test(e?.message || '')) return
    host.notify({ kind: 'info', title: 'Operations is installed', message: 'It is not on for this profile yet.', detail: 'Open it to see the one line to add.', action: open })
    ctx.storage.set(SETUP_TOASTED, true)
  })
}

function SetupChecklist() {
  const { data } = useSetup()
  const [hidden, setHidden] = useState(() => { try { return api.storage.get(SETUP_HIDDEN, false) } catch { return false } })
  if (!data || hidden) return null
  const steps = setupSteps(data)
  const left = steps.filter(x => !x.done).length
  if (!left) return null
  const hide = () => { setHidden(true); try { api.storage.set(SETUP_HIDDEN, true) } catch {} }
  return jsx(Section, {
    title: 'Set up Operations',
    count: `${left} of ${steps.filter(x => !x.optional).length} left`,
    children: jsxs('div', { className: 'space-y-2 text-xs', children: [
      ...steps.map(x => jsxs('div', { key: x.id, className: 'flex items-start gap-2', children: [
        jsx('span', { style: { marginTop: 3, width: 8, height: 8, flex: 'none', borderRadius: 4, border: `1.5px solid ${x.done ? TONE.good : TONE.warn}`, backgroundColor: x.done ? TONE.good : 'transparent' } }),
        jsxs('div', { className: 'flex-1 space-y-0.5', children: [
          jsx('div', { className: cn('font-medium', x.done && 'text-(--ui-text-quaternary)'), children: x.title }),
          jsx('div', { className: 'text-(--ui-text-quaternary)', style: { userSelect: 'text' }, children: x.text })
        ] }),
        x.action ? jsx(Button, { size: 'xs', variant: 'outline', onClick: x.action.onClick, children: x.action.label }) : null
      ] })),
      jsx('button', { type: 'button', className: 'text-[0.6875rem] text-(--ui-text-quaternary) underline', onClick: hide, children: 'Hide this checklist' })
    ] })
  })
}

// On Overview, one small line pointing at the checklist on the Notifications tab.
function SetupLink() {
  const go = useContext(Nav)
  const { data } = useSetup()
  if (!data || storeGet(SETUP_HIDDEN, false)) return null
  const left = setupSteps(data).filter(x => !x.done).length
  return left ? jsx('button', { type: 'button', className: 'text-[0.6875rem] text-(--ui-text-quaternary) underline', onClick: () => go('notifications'), children: `${plural(left, 'setup step')} left` }) : null
}

function OverviewPage() {
  const [hours, setHours] = useState(24)
  const [drawer, setDrawer] = useState(null)
  const { data, isLoading, isError, refetch, dataUpdatedAt } = useOverview(hours)
  // the day strip marks today's changes whatever window is picked
  const todayChanges = useChanges(24).data?.items || []

  if (isError)
    return jsx(ErrorState, {
      title: 'Operations unavailable',
      description: 'The /api/plugins/operations backend did not respond.',
      children: jsx('button', { type: 'button', className: 'text-xs underline', onClick: () => refetch(), children: 'Retry' })
    })

  if (isLoading || !data)
    return jsxs('div', {
      className: 'space-y-2 p-4',
      children: [jsx(Skeleton, { className: 'h-5 w-1/3' }), jsx(Skeleton, { className: 'h-24 w-full' }), jsx(Skeleton, { className: 'h-32 w-full' })]
    })

  const err = data.errors || {}
  const attention = data.attention || []
  const h = data.hosts || { hosts: [], pool: [] }
  return jsxs('div', {
    className: 'space-y-3 p-4',
    children: [
      jsxs('div', {
        className: 'flex flex-wrap items-center gap-x-3 gap-y-1',
        children: [
          jsx('h2', { className: 'text-sm font-semibold', children: 'Overview' }),
          jsx(RangePicker, { hours, onChange: setHours }),
          jsx(SetupLink, {}),
          jsxs('span', {
            className: 'ml-auto flex items-center gap-1.5 text-[0.6875rem] text-(--ui-text-quaternary)',
            style: { fontFamily: MONO },
            title: 'Refreshes every 30s',
            children: [jsx('span', { style: { width: 7, height: 7, borderRadius: 4, backgroundColor: TONE.info } }), dataUpdatedAt ? `Updated ${fmtClock(dataUpdatedAt / 1000)}` : 'Updating']
          })
        ]
      }),
      err.metrics ? muted(`No metrics collector data (${err.metrics}): requests, errors, hosts and the model pool read as empty.`) : null,
      err.kanban ? muted(`No kanban board (${err.kanban}): task counts and events read as empty.`) : null,
      err.vitals ? muted(`Vitals unavailable: ${err.vitals}`) : data.vitals ? jsx(Vitals, { v: data.vitals, hours, today: data.today || [], hosts: data.hosts }) : null,
      jsx(Section, {
        title: 'Today at a glance',
        count: 'midnight to midnight · hover a mark, click for details',
        error: err.today,
        children: data.today ? jsx(TodayStrip, { hours: data.today, changes: todayChanges, incidents: attention.filter(it => it.source !== 'kanban' && it.ts) }) : null
      }),
      jsx(Section, {
        title: 'Needs attention',
        count: attention.length,
        error: err.attention,
        children: jsx(Attention, { items: attention })
      }),
      jsxs('div', {
        className: 'grid items-start gap-3',
        style: { gridTemplateColumns: 'repeat(auto-fit, minmax(20rem, 1fr))' },
        children: [
          jsx(Section, {
            title: 'Recent activity',
            count: `last ${hours}h`,
            error: err.activity,
            children: jsx(Activity, { items: data.activity || [], onOpen: a => setDrawer({ entry: a }) })
          }),
          jsx(Section, {
            title: 'Changes and news',
            count: `merged MRs and model loads · last ${hours}h`,
            // a busy day lists dozens of merges; scroll inside the card rather than stretch the page
            children: jsx('div', { style: { maxHeight: '28rem', overflowY: 'auto', paddingRight: 4 }, children: jsx(Changes, { hours }) })
          })
        ]
      }),
      jsx(Section, {
        title: 'Hosts',
        count: `${h.hosts.filter(x => !x.stale).length} sending stats${h.hosts.some(x => x.stale) ? ` · ${h.hosts.filter(x => x.stale).length} quiet` : ''} · ${(h.pool_only || []).length} pool only · ${h.pool.length} pool devices`,
        error: err.hosts,
        children: jsx(Hosts, { ...h, onOpen: c => setDrawer({ host: c }) })
      }),
      drawer?.entry
        ? jsx(ActivityDrawer, { a: drawer.entry, hours, onClose: () => setDrawer(null) }, `${drawer.entry.kind}|${drawer.entry.text}|${drawer.entry.where}`)
        : drawer?.host
          ? jsx(HostDrawer, { c: drawer.host, activity: data.activity || [], hours, onOpen: a => setDrawer({ entry: a }), onClose: () => setDrawer(null) }, drawer.host.name)
          : null
    ]
  })
}

// --- merge requests -------------------------------------------------------------------

// Open MRs and those merged in the window, each with its newest pipeline as one square per stage.
const MRSTATE = { open: ['Open', TONE.info], draft: ['Draft', TONE.muted], merged: ['Merged', TONE.good] }
const PIPE = { ok: ['Passed', TONE.good], fail: ['Failed', TONE.bad], run: ['Running', TONE.warn], skip: ['Skipped', TONE.muted] }

// The one thing to know about an MR, most urgent first.
function mrBadge(m) {
  const st = m.pipeline?.stages.map(s => s[1]) || []
  if (m.state === 'merged') return ['merged', TONE.good]
  if (m.conflicts) return ['conflicts', TONE.bad]
  if (st.includes('fail')) return ['failing', TONE.bad]
  if (st.includes('run')) return ['running', TONE.warn]
  if (m.state === 'draft') return ['draft', TONE.muted]
  return m.reviewers.length ? ['review', TONE.warn] : ['open', TONE.info]
}

// Who an open MR waits on: its reviewers, else its author when the pipeline failed or it conflicts.
const mrWait = m => {
  if (m.state !== 'open') return null
  const bad = m.conflicts || m.pipeline?.stages.some(s => s[1] === 'fail')
  return bad ? `${m.author}, to fix !${m.iid}` : m.reviewers.length ? `${m.reviewers.join(', ')}, review of !${m.iid}` : null
}

const useMrs = hours =>
  useQuery({
    queryKey: [ID, 'mrs', hours],
    queryFn: () => api.rest(`/mrs?hours=${hours}`, { timeoutMs: 60_000 }),
    refetchInterval: 60_000
  })

const pips = (stages, size = 9) =>
  jsx('span', {
    className: 'flex gap-0.5',
    title: stages.map(([n, st]) => `${n} ${PIPE[st][0].toLowerCase()}`).join(', '),
    children: stages.map(([n, st], i) => jsx('i', { key: `${n}${i}`, style: { width: size, height: size, borderRadius: 2, backgroundColor: PIPE[st][1] } }))
  })

function MrsPage({ sel: initial }) {
  const [hours, setHours] = useState(24)
  const [selected, setSelected] = useState(initial)
  const { data, isLoading, isError } = useMrs(hours)
  if (isError) return jsx('div', { className: 'p-4', children: muted('Merge requests unavailable: the /api/plugins/operations backend did not respond.') })
  if (isLoading || !data) return jsx('div', { className: 'p-4', children: jsx(Skeleton, { className: 'h-48 w-full' }) })
  const items = data.items || []
  const now = data.generated_at
  const open = items.filter(m => m.state !== 'merged')
  const merged = items.filter(m => m.state === 'merged')
  const sel = items.find(m => m.id === selected)
  const when = t => (t == null ? '-' : now - t > 86400 ? new Date(t * 1000).toLocaleString([], { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' }) : fmtClock(t))
  const head = t => jsx('div', { key: t, className: 'pt-1 text-[0.6875rem] font-medium text-(--ui-text-quaternary)', children: t })
  const kv = (key, left, right) =>
    jsxs('div', {
      key,
      className: 'flex items-center gap-2',
      children: [jsx('span', { className: 'min-w-0 flex-1 truncate', children: left }), jsx('span', { className: 'shrink-0 tabular-nums', style: { fontFamily: MONO, color: 'var(--ui-text-quaternary)' }, children: right })]
    })
  const row = m => {
    const [label, c] = mrBadge(m)
    return jsxs('button', {
      type: 'button',
      key: m.id,
      'aria-pressed': m === sel,
      onClick: () => { haptic('tap'); setSelected(m === sel ? null : m.id) },
      className: cn('flex w-full items-center gap-2 rounded px-2 py-1 text-left text-xs hover:bg-(--chrome-action-hover)', m === sel && 'bg-(--ui-accent)/10'),
      children: [
        jsx('span', { className: 'w-10 shrink-0 tabular-nums text-(--ui-text-quaternary)', style: { fontFamily: MONO }, children: `!${m.iid}` }),
        jsx('i', { className: 'shrink-0', style: { width: 7, height: 7, borderRadius: 9, backgroundColor: MRSTATE[m.state][1] }, title: MRSTATE[m.state][0] }),
        jsxs('span', { className: 'min-w-0 flex-1', children: [
          jsx('span', { className: 'block truncate font-medium', children: m.title }),
          jsx('span', { className: 'block truncate text-[0.6875rem] text-(--ui-text-quaternary)', children: `${m.repo} · ${m.author}` })
        ] }),
        m.pipeline ? pips(m.pipeline.stages) : jsx('span', { className: 'text-[0.6875rem] text-(--ui-text-quaternary)', children: m.pipeline_unavailable ? 'pipeline unavailable' : 'no pipeline' }),
        jsx('span', { className: 'w-16 shrink-0 text-right text-[0.6875rem]', style: { color: c }, children: label })
      ]
    })
  }
  const repos = Object.entries(items.reduce((o, m) => ({ ...o, [m.repo]: (o[m.repo] || 0) + 1 }), {})).sort((a, b) => b[1] - a[1])
  const waiting = open.map(m => [m, mrWait(m)]).filter(([, w]) => w)
  const summary = jsxs('div', {
    className: 'space-y-1 text-xs',
    children: [
      jsx('div', { className: 'text-sm font-semibold', children: `${open.length} open, ${merged.length} merged` }),
      jsx('div', { children: `Merged counts the last ${plural(hours, 'hour')}; open counts every open MR you can see.` }),
      head('Waiting on'),
      ...(waiting.length ? waiting.map(([m, w]) => kv(m.id, w, fmtDur(now - m.created))) : [muted('Nothing open is waiting on review or a fix.')]),
      repos.length ? head('By repo') : null,
      ...repos.map(([r, n]) => kv(r, r, n)),
      jsx('div', { className: 'pt-2', children: muted('Select a merge request for its pipeline and who it waits on.') })
    ]
  })
  const detail = sel && jsxs('div', {
    className: 'space-y-1 text-xs',
    children: [
      jsx('div', { className: 'text-[0.6875rem] font-medium', style: { color: mrBadge(sel)[1] }, children: mrBadge(sel)[0] }),
      jsx('div', { className: 'text-sm font-semibold', children: sel.title }),
      jsx('div', { className: 'text-(--ui-text-quaternary)', children: `${sel.id} by ${sel.author}` }),
      kv('o', 'Opened', when(sel.created)),
      sel.merged ? kv('m', `Merged${sel.merged_by ? ` by ${sel.merged_by}` : ''}`, when(sel.merged)) : kv('u', 'Last updated', when(sel.updated)),
      kv('r', 'Reviewers', sel.reviewers.join(', ') || (sel.merged ? 'none' : 'none yet')),
      sel.conflicts ? jsx('div', { style: { color: TONE.bad }, children: 'Has merge conflicts with its target branch.' }) : null,
      head('Pipeline'),
      sel.pipeline_unavailable
        ? muted('Could not load this pipeline from GitLab; see the note at the top.')
        : sel.pipeline
        ? jsx('div', { className: 'flex flex-wrap gap-1', children: sel.pipeline.stages.map(([n, st], i) => jsx('span', {
            key: `${n}${i}`, className: 'rounded border px-1.5 py-0.5 text-[0.6875rem]',
            style: { borderColor: PIPE[st][1], color: PIPE[st][1] }, children: `${n} ${PIPE[st][0].toLowerCase()}`
          })) })
        : muted('No pipeline has run for this merge request.'),
      jsxs('div', { className: 'flex flex-wrap gap-2 pt-2', children: [
        jsx('a', { href: sel.url, onClick: openOut, className: 'text-(--ui-accent) hover:underline', children: 'Open in GitLab' }),
        sel.pipeline?.url ? jsx('a', { href: sel.pipeline.url, onClick: openOut, className: 'text-(--ui-accent) hover:underline', children: 'Open pipeline' }) : null,
        jsx(Button, { size: 'xs', variant: 'ghost', onClick: () => setSelected(null), children: 'Clear selection' })
      ] })
    ]
  })
  return jsxs('div', {
    className: 'space-y-3 p-4',
    children: [
      jsxs('div', {
        className: 'flex flex-wrap items-center gap-2',
        children: [
          jsx('h2', { className: 'text-sm font-semibold', children: 'Merge requests' }),
          jsx('span', { className: 'text-[0.6875rem] text-(--ui-text-quaternary)', children: 'squares are pipeline stages: passed, failed, running, skipped' }),
          jsx('span', { className: 'ml-auto' }),
          jsx(RangePicker, { hours, onChange: setHours })
        ]
      }),
      ...Object.entries(data.errors || {}).map(([k, v]) =>
        jsx('div', { key: k, className: 'text-xs text-(--ui-text-quaternary)', children: `${{ gitlab: 'GitLab', pipelines: 'Pipelines' }[k] || k} unavailable: ${v}` })
      ),
      !items.length
        ? (data.errors?.gitlab ? null : muted('No open merge requests, and none merged in this window.'))
        : jsxs('div', {
            className: 'flex flex-wrap items-start gap-3',
            children: [
              jsx('div', {
                className: 'min-w-0',
                style: { flex: '999 1 36rem' },
                children: jsx(Section, {
                  title: 'Merge requests',
                  count: plural(items.length, 'merge request'),
                  children: jsxs('div', { className: 'space-y-0.5', children: [
                    open.length ? head('Open') : null,
                    ...open.map(row),
                    merged.length ? head(`Merged in the last ${plural(hours, 'hour')}`) : null,
                    ...merged.map(row),
                    jsx('div', {
                      key: 'legend',
                      className: 'flex flex-wrap gap-x-4 gap-y-1 pt-2',
                      style: { fontFamily: MONO, fontSize: '0.6875rem', color: 'var(--ui-text-quaternary)' },
                      children: Object.entries(PIPE).map(([k, [label, c]]) => jsxs('span', { key: k, className: 'flex items-center gap-1.5', children: [jsx('i', { style: { width: 9, height: 9, borderRadius: 2, backgroundColor: c } }), label] }))
                    })
                  ] })
                })
              }),
              jsx('div', {
                className: 'min-w-0',
                style: { flex: '1 1 18rem' },
                children: jsx(Section, { title: sel ? 'Merge request' : 'Summary', children: sel ? detail : summary })
              })
            ]
          })
    ]
  })
}

// --- notifications --------------------------------------------------------------------

// Hermes Desktop has no notification center: a toast is gone once dismissed. Every alert the poll
// below raises is also kept here (newest ALERT_LOG_MAX, in this Desktop's plugin storage). An
// alert stays unread until marked: one at a time (read on the entry), or all at once (ALERT_READ,
// the time of the last "Mark all read").
const storeGet = (k, d) => { try { return api.storage.get(k, d) } catch { return d } }
const isUnread = (a, readAt) => !a.read && a.at > readAt
const unreadAlerts = () => storeGet(ALERT_LOG, []).filter(a => isUnread(a, storeGet(ALERT_READ, 0))).length

// What a chat or a card is told about one alert. The alert may have cleared since it was raised.
const alertBrief = (a, tail, session) => [
  a.at ? `Hermes Operations raised this alert at ${new Date(a.at).toLocaleString()}:` : 'Hermes Operations lists this under Needs attention:', '', a.text, '',
  `Severity: ${a.sev}. Source: ${a.source}.${a.host ? ` Host: ${a.host}.` : ''}${a.ts ? ` Ongoing since ${new Date(a.ts * 1000).toLocaleString()}.` : ''}`,
  session ? `Session: ${session.session_id}, ${new Date(session.start * 1000).toLocaleString()} to ${new Date(session.end * 1000).toLocaleString()}.` : null,
  a.action ? `Suggested next step: ${a.action}` : null, '',
  'Find out why it happened and fix it if you can. Check first whether it is still happening: it is listed under Needs attention on the Operations Overview while it is.' + (tail ? ` ${tail}` : '')
].filter(x => x !== null).join('\n')

// Two ways to get an alert fixed, on Notifications and Overview: a new chat with the alert written
// into the composer (sent by the user, not here), or a kanban card for a profile, started now.
function AlertActions({ a }) {
  const qc = useQueryClient()
  const [profile, setProfile] = useState('')
  const [busy, setBusy] = useState(false)
  const [note, setNote] = useState(null)
  const { data } = useQuery({ queryKey: [ID, 'assignees'], queryFn: () => api.rest('/assignees'), staleTime: 5 * 60_000 })
  const setup = useSetup().data
  // until the lookup answers, a card would be keyed without the session and duplicate the one keyed with it
  const found = useFindSession(a)
  const session = found.data?.session
  const chat = async () => {
    setBusy(true)
    setNote(null)
    host.newChat(profile || null)
    // the new chat's composer mounts after the route changes; ask until it answers
    try {
      for (let i = 0; i < 20; i++) {
        await new Promise(r => setTimeout(r, 250))
        if (await host.composer.setDraft(null, alertBrief(a, 'If a step needs me, stop and say what.', session)).catch(() => false)) return
      }
      host.notify({ kind: 'error', title: 'Operations', message: 'The new chat did not take the alert. Copy it from the Notifications tab instead.' })
    } finally {
      setBusy(false)
    }
  }
  const card = async () => {
    setBusy(true)
    setNote(null)
    try {
      const out = await api.rest('/attention/fix', { method: 'POST', body: {
        // one rule's findings name different sessions; each gets its own card
        key: session ? `${a.key}:${session.session_id}` : a.key, title: a.text, profile,
        body: alertBrief(a, 'If a step needs a person, block this card and say what.', session) } })
      setNote({ bad: !!out.warning, text: out.existing ? `Card ${out.card} for this alert is already open (${out.status}); it is on the Handoffs tab.` : out.warning || (out.started ? `Card ${out.card} filed; ${profile} started working on it.` : `Card ${out.card} filed for ${profile} (${out.status}).`), card: out.card })
      qc.invalidateQueries({ queryKey: [ID] })
    } catch (e) {
      setNote({ bad: true, text: e?.message || String(e) })
    }
    setBusy(false)
  }
  return jsxs('div', {
    className: 'space-y-2',
    children: [
      jsxs('div', { className: 'flex flex-wrap items-center gap-2', children: [
        jsxs(Select, { value: profile, onValueChange: setProfile, children: [
          jsx(SelectTrigger, { className: 'h-7 w-48 text-xs', children: jsx(SelectValue, { placeholder: 'Pick a profile' }) }),
          jsx(SelectContent, { children: (data?.assignees || []).map(p => jsx(SelectItem, { key: p, value: p, children: p })) })
        ] }),
        host.newChat && host.composer?.setDraft
          ? jsx(Button, { size: 'xs', variant: 'ghost', disabled: busy, onClick: chat, children: profile ? `Open a chat in ${profile} to fix` : 'Open a chat to fix' })
          : null,
        !setup || setup.kanban
          ? jsx(Button, { size: 'xs', variant: 'ghost', disabled: busy || !profile || found.isFetching, onClick: card, children: profile ? `Hand to ${profile} on kanban` : 'Hand to kanban (pick a profile)' })
          : null,
        jsx(Button, { size: 'xs', variant: 'ghost', onClick: async () => setNote(await api.os.writeClipboard(alertBrief(a, '', session)) ? { text: 'Copied.' } : { bad: true, text: 'Could not copy.' }), children: 'Copy' })
      ] }),
      note ? jsxs('div', { className: 'flex flex-wrap items-center gap-2', style: { color: note.bad ? '#f85149' : undefined }, children: [
        note.text, note.card && !note.bad ? jsx(GoLink, { tab: 'handoffs', sel: note.card, children: 'Open the card in Handoffs' }) : null
      ] }) : null
    ]
  })
}

// An alert's details on the Notifications tab: when, whether it is still open, where to see it, and the actions.
function AlertDetail({ a }) {
  const { data: open } = useQuery({ queryKey: [ID, 'attention'], queryFn: () => api.rest('/attention'), refetchInterval: 60_000 })
  const now = open && (open.items || []).find(i => i.key === a.key)
  const line = (label, text) => jsxs('div', { children: [jsx('span', { className: 'text-(--ui-text-quaternary)', children: `${label}: ` }), text] })
  return jsxs('div', {
    className: 'space-y-2 pt-1',
    onClick: e => e.stopPropagation(),
    children: [
      jsxs('div', { className: 'space-y-0.5', style: { userSelect: 'text' }, children: [
        line('Raised', new Date(a.at).toLocaleString()),
        a.ts ? line('Ongoing since', new Date(a.ts * 1000).toLocaleString()) : null,
        a.host ? line('Host', a.host) : null,
        line('Now', !open ? 'checking...' : now ? `still open${now.text !== a.text ? `: ${now.text}` : ''}` : 'cleared, no longer under Needs attention')
      ] }),
      attentionLink(a),
      jsx(AlertActions, { a })
    ]
  })
}

function NotificationsPage({ onRead }) {
  const [log, setLog] = useState(() => storeGet(ALERT_LOG, []))
  const [readAt, setReadAt] = useState(() => storeGet(ALERT_READ, 0))
  const [opened, setOpened] = useState(null)
  // re-read storage before writing: the alert poll may have added entries since this page opened
  const save = next => { try { api.storage.set(ALERT_LOG, next) } catch {} setLog(next); onRead() }
  const markRead = a => save(storeGet(ALERT_LOG, []).map(x => x.key === a.key && x.at === a.at ? { ...x, read: true } : x))
  const markAll = () => { const now = Date.now(); try { api.storage.set(ALERT_READ, now) } catch {} setReadAt(now); save(storeGet(ALERT_LOG, [])) }
  const clear = () => save([])
  // pick up what the alert poll adds while this page is open
  useEffect(() => {
    const t = setInterval(() => {
      const next = storeGet(ALERT_LOG, [])
      if (JSON.stringify(next) !== JSON.stringify(log)) { setLog(next); onRead() }
    }, 5000)
    return () => clearInterval(t)
  }, [log])
  const unread = log.filter(a => isUnread(a, readAt)).length
  return jsxs('div', {
    className: 'space-y-3 p-4',
    children: [
      jsxs('div', { className: 'flex flex-wrap items-center gap-x-3 gap-y-1', children: [
        jsx('h2', { className: 'text-sm font-semibold', children: 'Notifications' }),
        jsx('span', { className: 'text-xs text-(--ui-text-quaternary)', children: 'every alert Operations raised in this Desktop, newest first' }),
        jsx('span', { className: 'ml-auto' }),
        unread ? jsx('button', { type: 'button', className: 'text-[0.6875rem] text-(--ui-text-quaternary) underline', onClick: markAll, children: 'Mark all read' }) : null,
        log.length ? jsx('button', { type: 'button', className: 'text-[0.6875rem] text-(--ui-text-quaternary) underline', onClick: clear, children: 'Clear' }) : null
      ] }),
      jsx(SetupChecklist, {}),
      !log.length
        ? muted('No alerts yet. When something new shows under Needs attention, it is listed here as well as in a toast. Kanban card alerts come from the Kanban plugin.')
        : jsx('div', { className: 'overflow-hidden rounded-md border border-(--ui-stroke-secondary)', children: log.map((a, i) => {
            const [tone, label] = a.open ? ['info', 'already open'] : attentionTag(a)
            const id = `${a.key}:${a.at}`
            return jsxs('div', {
              key: `${id}:${i}`,
              role: 'button',
              tabIndex: 0,
              'aria-expanded': opened === id,
              onClick: () => setOpened(opened === id ? null : id),
              onKeyDown: e => { if (e.target === e.currentTarget && (e.key === 'Enter' || e.key === ' ')) { e.preventDefault(); setOpened(opened === id ? null : id) } },
              className: 'flex cursor-pointer items-start gap-3 border-b border-(--ui-stroke-secondary) px-3 py-2 text-xs last:border-b-0 hover:bg-(--chrome-action-hover)',
              style: isUnread(a, readAt) ? { backgroundColor: 'color-mix(in srgb, var(--ui-accent) 8%, transparent)' } : undefined,
              children: [
                jsx('span', { className: 'w-20 flex-none text-[0.6875rem] text-(--ui-text-quaternary)', style: { fontFamily: MONO }, title: new Date(a.at).toLocaleString(), children: fmtAgo(a.at / 1000) }),
                jsx(Tag, { tone, children: label }),
                jsxs('div', { className: 'min-w-0 flex-1 space-y-0.5', children: [
                  jsx('div', { className: 'font-medium leading-snug', children: a.text }),
                  a.action ? jsx('div', { className: 'leading-snug text-(--ui-text-quaternary)', children: a.action }) : null,
                  opened === id ? jsx(AlertDetail, { a }) : null
                ] }),
                jsx('span', { className: 'flex-none text-[0.6875rem] text-(--ui-text-quaternary)', children: a.source }),
                isUnread(a, readAt) ? jsx('button', { type: 'button', className: 'flex-none text-[0.6875rem] text-(--ui-text-quaternary) underline', onClick: e => { e.stopPropagation(); markRead(a) }, children: 'Mark read' }) : null
              ]
            })
          }) })
    ]
  })
}

const PAGES = { overview: OverviewPage, flow: FlowPage, activity: ActivityPage, handoffs: HandoffsPage, trace: TracePage, mrs: MrsPage, topology: TopologyPage, conformance: ConformancePage, notifications: NotificationsPage }

function OperationsPage() {
  const [nav, setNav] = useState({ tab: 'overview', sel: null, when: {} })
  const { tab, sel, when } = nav
  // the pages this one was reached from, newest last; Back reopens each as its link opened it
  const [trail, setTrail] = useState([])
  // what the page on screen has selected and its time range since it opened, written by the page as it draws
  const here = useRef(null)
  const go = (tab, sel = null, when = {}) => {
    const now = here.current ? { tab: nav.tab, sel: here.current.sel, when: { ...here.current.when, back: true } } : nav
    here.current = null
    setTrail(t => [...t, now].slice(-30))
    setNav({ tab, sel, when })
  }
  // read and pop through a ref, so a second Back before the re-render takes the next entry, not the same one
  const trailRef = useRef(trail)
  trailRef.current = trail
  const back = () => {
    const t = trailRef.current
    if (!t.length) return
    haptic('tap')
    here.current = null
    trailRef.current = t.slice(0, -1)
    setNav(t.at(-1))
    setTrail(trailRef.current)
  }
  const remember = v => { here.current = v }
  const rolesQ = useRoles()
  const roles = rolesQ.data
  // the role viewed as is per viewer; storage can be unavailable, and then it is the first role
  const [roleId, setRoleId] = useState(() => { try { return api.storage.get('role', null) } catch { return null } })
  const [editing, setEditing] = useState(false)
  // bumped when Notifications marks alerts read, so the tab's unread count redraws
  const [, setReads] = useState(0)
  const list = roles?.roles || []
  const role = list.find(r => r.id === roleId) || list[0] || null
  const viewAs = id => { setRoleId(id); try { api.storage.set('role', id) } catch {} }
  const tabs = [
    { id: 'overview', label: 'Overview' },
    { id: 'topology', label: 'Topology' },
    { id: 'flow', label: 'Flow' },
    { id: 'activity', label: 'Activity' },
    { id: 'handoffs', label: 'Handoffs' },
    { id: 'trace', label: 'Trace' },
    { id: 'mrs', label: 'Merge requests' },
    { id: 'conformance', label: 'Conformance' },
    { id: 'notifications', label: unreadAlerts() ? `Notifications (${unreadAlerts()})` : 'Notifications' }
  ]
  // Desktop's backend serves its profile, and refuses a user plugin missing from that profile's
  // plugins.enabled; every tab would then say the backend did not respond, so name the cause once
  if (/not enabled/.test(rolesQ.error?.message || ''))
    return jsx(ErrorState, {
      title: 'Operations is not enabled for this profile',
      description: 'Hermes Desktop runs a backend for the profile it is on, and that profile\'s config.yaml does not list operations under plugins.enabled, so the backend refuses this page. Add "- operations" under plugins: enabled: in ~/.hermes/profiles/<profile>/config.yaml (or ~/.hermes/config.yaml for the default profile), then restart Hermes Desktop.',
      children: jsxs('div', { className: 'flex items-center gap-3', children: [
        jsx('button', { type: 'button', className: 'text-xs underline', onClick: () => host.navigate(`/capabilities?tab=plugins&plugin=${ID}`), children: 'Open Capabilities > Plugins' }),
        jsx('button', { type: 'button', className: 'text-xs underline', onClick: () => rolesQ.refetch(), children: 'Retry' })
      ] })
    })
  return jsx(Nav.Provider, {
    value: go,
    children: jsxs(Role.Provider, {
      value: { role, roles: list },
      // the mouse's back button and Backspace go back here; unhandled, the host walks its own tabs
      children: jsxs('div', {
      onMouseDown: e => { if (e.button === 3 && trail.length) { e.preventDefault(); back() } },
      onKeyDown: e => { if (e.key === 'Backspace' && trail.length && !e.metaKey && !e.ctrlKey && !e.altKey && !typing(e.target)) { e.preventDefault(); back() } },
      children: [
      jsxs('div', {
        className: 'sticky top-0 z-20 flex flex-wrap items-center gap-1 border-b border-(--ui-stroke-secondary) px-4 pt-3 pb-1.5',
        style: { backgroundColor: 'var(--ui-bg, #0d1117)' },
        children: [
          trail.length ? jsx('button', {
            type: 'button', key: 'back', onClick: back, 'aria-label': 'Back',
            className: 'mr-1 flex items-center gap-1.5 rounded border border-(--ui-stroke-secondary) px-2 py-0.5 text-xs hover:bg-(--chrome-action-hover)',
            children: [jsx('i', { key: 'a', style: { width: 0, height: 0, borderTop: '4px solid transparent', borderBottom: '4px solid transparent', borderRight: '5px solid currentColor' } }), 'Back']
          }) : null,
          ...tabs.map(t =>
          jsx('button', {
            type: 'button',
            key: t.id,
            onClick: () => { haptic('tap'); go(t.id) },
            className: cn(
              'rounded border px-2.5 py-0.5 text-xs transition-colors',
              tab === t.id ? 'border-(--ui-accent) bg-(--ui-accent)/10 font-medium text-(--ui-accent)' : 'border-transparent text-(--ui-text-quaternary) hover:bg-(--chrome-action-hover)'
            ),
            children: t.label
          })
        ),
        jsx('span', { key: 'gap', className: 'ml-auto' }),
        role ? jsxs(Select, {
          key: 'role',
          value: role.id,
          onValueChange: viewAs,
          children: [
            jsx(SelectTrigger, { className: 'h-6 w-auto gap-1 text-[0.6875rem]', title: role.summary, 'aria-label': 'View as role', children: jsx(SelectValue, {}) }),
            jsx(SelectContent, { children: list.map(r => jsx(SelectItem, { key: r.id, value: r.id, children: `View as ${r.name}` })) })
          ]
        }) : null,
        roles ? jsx('button', {
          type: 'button', key: 'edit', onClick: () => { haptic('tap'); setEditing(true) },
          className: 'rounded px-2 py-0.5 text-[0.6875rem] text-(--ui-text-quaternary) hover:bg-(--chrome-action-hover)',
          children: 'Edit roles'
        }) : null]
      }),
      editing && roles ? jsx(RolesDrawer, { data: roles, current: role?.id, onClose: () => setEditing(false) }) : null,
      // keyed by the selection, so a link into the tab it is already on opens with the new one
      jsx(PAGES[tab], { sel, when, remember, onRead: () => setReads(n => n + 1) }, `${tab}:${sel}:${when.at}:${when.hours}:${when.start}:${when.note}:${trail.length}`)
      ]
      })
    })
  })
}

// --- alerts ---------------------------------------------------------------------------

// While Desktop runs, alert on each item that newly appears in Needs attention: an in-app toast,
// plus an OS notification while the user is away (Desktop's "Plugin notifications" setting gates
// that). Kanban items are left to the Kanban plugin, which already notifies when a card blocks.
// Seen keys persist, so a restart does not re-alert; the first run only records what is there.
// A key that clears is forgotten, so it alerts again if it comes back.
const ALERT_POLL_MS = 60_000
const ALERT_SEEN = 'alerts.seen'
const ALERT_LOG = 'alerts.log'
const ALERT_READ = 'alerts.read'
const ALERT_LOG_MAX = 200

function startAlerts(ctx) {
  let seen = ctx.storage.get(ALERT_SEEN, null)
  const poll = async () => {
    let out
    try {
      out = await ctx.rest('/attention')
    } catch {
      return
    }
    const items = (out.items || []).filter(i => i.key && i.source !== 'kanban')
    const fresh = seen ? items.filter(i => !seen.includes(i.key)) : []
    // With a source unreadable, its items are missing, not cleared: keep them as seen.
    const keys = items.map(i => i.key)
    seen = Object.keys(out.errors || {}).length ? [...new Set([...(seen || []), ...keys])] : keys
    ctx.storage.set(ALERT_SEEN, seen)
    // The first poll with no log yet (a new install, or an update from before the Notifications
    // tab) lists what is already open there too, as a note rather than an alert.
    const log = ctx.storage.get(ALERT_LOG, null)
    if (fresh.length || !log) {
      const at = Date.now()
      const open = log ? [] : items.filter(i => !fresh.includes(i)).map(i => ({ ...i, at, open: true }))
      ctx.storage.set(ALERT_LOG, [...fresh.map(i => ({ ...i, at })), ...open, ...(log || [])].slice(0, ALERT_LOG_MAX))
    }
    const open = { label: 'Open Operations', onClick: () => host.navigate(PATH) }
    if (fresh.length > 3) {
      host.notify({ kind: fresh.some(i => i.sev === 'crit') ? 'error' : 'warning', title: 'Operations', message: `${fresh.length} new items need attention`, action: open })
      ctx.os.notify({ title: `Operations: ${fresh.length} new items need attention`, body: fresh.map(i => i.text).join('\n'), activate: PATH })
      return
    }
    for (const i of fresh) {
      host.notify({ kind: i.sev === 'crit' ? 'error' : 'warning', title: `Operations: ${i.source}`, message: i.text, detail: i.action || undefined, action: open })
      ctx.os.notify({ title: `Operations: ${i.text}`, body: i.action || '', activate: PATH })
    }
  }
  poll()
  const timer = setInterval(poll, ALERT_POLL_MS)
  ctx.onDispose(() => clearInterval(timer))
}

// --- registration ---------------------------------------------------------------------

export default {
  id: ID, // must match the folder name
  name: 'Operations',
  description: 'Operations overview: vitals, today, what needs attention, hosts and model pool, recent activity, request flow, activity log, handoffs, request traces, topology, conformance, and alerts when something new needs attention.',
  register(ctx) {
    api = ctx
    startAlerts(ctx)
    setupToast(ctx)
    ctx.register({
      id: 'page',
      area: ROUTES_AREA,
      data: { path: PATH },
      // a plain scroller: the SDK ScrollArea's viewport is display: table, which widens to the
      // longest unwrapped line instead of letting rows truncate and text wrap
      render: () => jsx('div', { 'data-ops-pane': '', className: 'h-full overflow-y-auto', children: jsx(OperationsPage, {}) })
    })

    ctx.register({
      id: 'nav',
      area: SIDEBAR_NAV_AREA,
      order: 61,
      data: { codicon: 'pulse', label: 'Operations', path: PATH }
    })

    ctx.register({
      id: 'open',
      area: PALETTE_AREA,
      data: {
        id: `${ID}.open`,
        label: 'Operations: open overview',
        keywords: ['operations', 'ops', 'overview', 'vitals', 'attention', 'hosts', 'pool', 'activity', 'flow', 'requests', 'handoffs', 'conformance'],
        run: () => {
          haptic('tap')
          host.navigate(PATH)
        }
      }
    })
  }
}
