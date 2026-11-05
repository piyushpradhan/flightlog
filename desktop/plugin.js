// Flightlog desktop page: which runs need a look, and what happened in each, at a glance.
// Data comes from dashboard/plugin_api.py through ctx.rest. Styles use theme tokens only.
import {
  host, useQuery, useValue, atom, StatusDot, EmptyState, ErrorState, CopyButton, Codicon, relativeTime,
  ROUTES_AREA, SIDEBAR_NAV_AREA, PALETTE_AREA
} from '@hermes/plugin-sdk'
import { jsx, jsxs, Fragment } from 'react/jsx-runtime'

const ROUTE = '/flightlog'
const BAD = { error: 1, failed: 1, denied: 1, timeout: 1 }
const $selected = atom(null)
let rest = null // ctx.rest, bound in register()

const c = {
  text: 'var(--ui-text-primary)',
  dim: 'var(--ui-text-tertiary)',
  faint: 'var(--ui-text-quaternary)',
  line: 'var(--ui-stroke-secondary)',
  red: 'var(--ui-red)',
  yellow: 'var(--ui-yellow)',
  green: 'var(--ui-green)'
}
const tint = (color, pct) => `color-mix(in oklab, ${color} ${pct}%, transparent)`

const tone = r => (r.headline ? 'warn' : r.outcome === 'ok' ? 'good' : 'bad')
const verdict = r => r.headline || (r.outcome === 'ok' ? 'Looks fine' : `Ended ${r.outcome}`)
const who = r => (r.job_name ? r.job_name : r.job_id ? `Cron job ${r.job_id}` : 'Chat')
const dur = ms => (ms == null ? '' : ms >= 1000 ? `${(ms / 1000).toFixed(1)} s` : `${ms} ms`)
const tok = n => (n >= 1000 ? `${(n / 1000).toFixed(1)}k` : String(n))
const plural = (n, w) => `${n} ${w}${n === 1 ? '' : 's'}`

function stepLabel(e) {
  if (e.kind === 'tool') return [e.name, e.target]
  if (e.kind === 'model') return [BAD[e.status] ? 'Model call failed' : e.status === 'stop' ? 'Model answered' : 'Model call', '']
  if (e.kind === 'subagent') return [`Helper ${e.name}`, e.status]
  if (e.kind === 'approval') return [e.name === 'request' ? 'Asked for approval' : `Approval: ${e.name}`, e.preview || '']
  return null
}

// ---------------------------------------------------------------- run list

function RunRow({ r, active }) {
  return jsxs('button', {
    type: 'button',
    onClick: () => $selected.set(r.run_id),
    style: {
      display: 'grid', gridTemplateColumns: '10px 1fr', gap: '10px', alignItems: 'start', width: '100%',
      textAlign: 'left', padding: '10px 12px', borderRadius: 8, border: 0, cursor: 'pointer',
      background: active ? 'var(--ui-row-active-background)' : 'transparent', color: c.text
    },
    children: [
      jsx(StatusDot, { tone: tone(r), style: { marginTop: 6, width: 8, height: 8 } }),
      jsxs('span', {
        style: { minWidth: 0 },
        children: [
          jsx('span', { style: { display: 'block', fontSize: 13, fontWeight: 500 }, children: verdict(r) }),
          jsx('span', {
            style: { display: 'block', fontSize: 12, color: c.dim, whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' },
            children: `${who(r)} · ${relativeTime(r.ended * 1000)} · ${plural(r.tool_calls, 'tool')}`
          })
        ]
      })
    ]
  })
}

// ---------------------------------------------------------------- run detail

function Verdict({ run, findings }) {
  const lead = findings[0]
  const color = lead ? c.yellow : run.outcome === 'ok' ? c.green : c.red
  return jsxs('div', {
    style: { borderLeft: `3px solid ${color}`, background: tint(color, 8), borderRadius: 8, padding: '14px 16px' },
    children: [
      jsx('div', { style: { fontSize: 17, fontWeight: 600, marginBottom: 4 }, children: lead ? lead.headline : verdict(run) }),
      lead
        ? jsxs(Fragment, {
            children: [
              jsx('p', { style: { margin: '0 0 6px', fontSize: 13 }, children: lead.summary }),
              jsx('p', { style: { margin: 0, fontSize: 13, color: c.dim }, children: lead.report || lead.impact }),
              findings.slice(1).map(f =>
                jsxs('p', { key: f.finding_id, style: { margin: '8px 0 0', fontSize: 13 },
                  children: [jsx('strong', { children: `Also: ${f.headline}. ` }), f.summary] }))
            ]
          })
        : jsx('p', { style: { margin: 0, fontSize: 13, color: c.dim }, children: 'No problems found in this run.' })
    ]
  })
}

function Stat({ label, value }) {
  return jsxs('div', {
    children: [
      jsx('div', { style: { fontSize: 11, color: c.faint, textTransform: 'uppercase', letterSpacing: '.04em' }, children: label }),
      jsx('div', { style: { fontSize: 15, fontVariantNumeric: 'tabular-nums' }, children: value })
    ]
  })
}

function Timeline({ run, events, evidence }) {
  const steps = events.map(e => ({ e, label: stepLabel(e) })).filter(s => s.label)
  const max = Math.max(1, ...steps.map(s => s.e.duration_ms || 0))
  return jsx('ol', {
    style: { listStyle: 'none', margin: 0, padding: 0 },
    children: steps.map(({ e, label: [name, target] }) => {
      const bad = !!BAD[e.status]
      const flagged = evidence.has(e.seq)
      return jsxs('li', {
        key: e.seq,
        style: {
          display: 'grid', gridTemplateColumns: '16px minmax(0,1fr) 120px 56px', gap: '10px', alignItems: 'center',
          padding: '7px 8px', borderRadius: 6, borderBottom: `1px solid ${tint('var(--ui-stroke-secondary)', 50)}`,
          background: bad ? tint(c.red, 9) : flagged ? tint(c.yellow, 8) : 'transparent'
        },
        children: [
          jsx(Codicon, { name: bad ? 'error' : e.kind === 'model' ? 'sparkle' : e.kind === 'subagent' ? 'type-hierarchy-sub' : 'pass',
            style: { color: bad ? c.red : c.faint } }),
          jsxs('div', {
            style: { minWidth: 0 },
            children: [
              jsxs('div', { style: { fontSize: 13, whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' },
                children: [jsx('span', { style: { fontWeight: 500, color: bad ? c.red : c.text }, children: name }),
                  target && jsx('span', { style: { color: c.dim, fontFamily: 'var(--font-mono, ui-monospace)', fontSize: 12 }, children: '  ' + target })] }),
              bad && e.detail && jsx('div', { style: { fontSize: 12, color: c.red, marginTop: 2 }, children: e.detail })
            ]
          }),
          jsx('div', {
            title: `+${(e.ts - run.started).toFixed(1)} s`,
            style: { height: 6, borderRadius: 3, background: tint('var(--ui-stroke-secondary)', 60), overflow: 'hidden' },
            children: e.duration_ms != null && jsx('div', {
              style: { height: '100%', width: `${Math.max(3, (100 * e.duration_ms) / max)}%`, background: bad ? c.red : c.dim, opacity: 0.7 }
            })
          }),
          jsx('div', { style: { fontSize: 12, color: c.faint, textAlign: 'right', fontVariantNumeric: 'tabular-nums' }, children: dur(e.duration_ms) })
        ]
      })
    })
  })
}

function RunDetail({ runId }) {
  const q = useQuery({ queryKey: ['flightlog', 'run', runId], queryFn: () => rest(`/runs/${runId}`), refetchInterval: 15000 })
  if (q.error) return jsx(ErrorState, { title: 'Could not load this run', description: String(q.error) })
  if (!q.data) return null
  const { run, events, findings } = q.data
  const evidence = new Set(findings.flatMap(f => f.evidence))
  const models = events.filter(e => e.kind === 'model').length
  const ack = findings[0] && `hermes flightlog ack ${findings[0].finding_id}`
  return jsxs('div', {
    style: { display: 'flex', flexDirection: 'column', gap: 18, minWidth: 0 },
    children: [
      jsx('div', { style: { fontSize: 12, color: c.dim }, children: `${who(run)} · ${relativeTime(run.ended * 1000)} · run ${run.run_id}` }),
      jsx(Verdict, { run, findings }),
      jsxs('div', {
        style: { display: 'flex', gap: 28, flexWrap: 'wrap' },
        children: [
          jsx(Stat, { label: 'Duration', value: `${(run.ended - run.started).toFixed(1)} s` }),
          jsx(Stat, { label: 'Model calls', value: models }),
          jsx(Stat, { label: 'Tool calls', value: `${run.tool_calls}${run.tool_errors ? ` (${run.tool_errors} failed)` : ''}` }),
          jsx(Stat, { label: 'Tokens', value: tok(run.tokens) }),
          run.cost_usd ? jsx(Stat, { label: 'Cost', value: `$${run.cost_usd.toFixed(3)}` }) : null
        ]
      }),
      jsxs('section', {
        children: [
          jsx('div', { style: { fontSize: 12, fontWeight: 600, color: c.dim, marginBottom: 6 }, children: 'What happened' }),
          jsx(Timeline, { run, events, evidence })
        ]
      }),
      ack && jsxs('div', {
        style: { display: 'flex', alignItems: 'center', gap: 8, fontSize: 12, color: c.dim },
        children: ['False alarm? Silence this check for this job:',
          jsx('code', { style: { fontSize: 12 }, children: ack }), jsx(CopyButton, { text: ack, label: "Copy command" })]
      })
    ]
  })
}

// ---------------------------------------------------------------- page

function FlightlogPage() {
  const q = useQuery({ queryKey: ['flightlog', 'runs'], queryFn: () => rest('/runs?limit=100'), refetchInterval: 5000 })
  const selected = useValue($selected)
  if (q.error) {
    return jsx(ErrorState, {
      title: 'Flightlog backend unavailable',
      description: 'Enable the flightlog agent plugin (hermes plugins enable flightlog), then restart Hermes.'
    })
  }
  if (!q.data) return null
  const { runs, summary } = q.data
  if (!runs.length) {
    return jsx(EmptyState, { title: 'No runs recorded yet', description: 'A run appears here when an agent turn or cron job finishes.' })
  }
  const current = selected || (runs.find(r => r.headline) || runs[0]).run_id
  return jsxs('div', {
    style: { display: 'flex', flexDirection: 'column', height: '100%', color: c.text },
    children: [
      jsxs('header', {
        style: { padding: '18px 24px 14px', borderBottom: `1px solid ${c.line}` },
        children: [
          jsx('div', { style: { fontSize: 18, fontWeight: 600 }, children: 'Flightlog' }),
          jsx('div', {
            style: { fontSize: 13, color: summary.flagged ? c.yellow : c.dim, marginTop: 2 },
            children: `Last 24 hours: ${plural(summary.runs, 'run')}, ` +
              (summary.flagged ? `${summary.flagged} need${summary.flagged === 1 ? 's' : ''} a look` : 'all look fine')
          })
        ]
      }),
      jsxs('div', {
        style: { display: 'grid', gridTemplateColumns: 'minmax(220px, 300px) minmax(0, 1fr)', flex: 1, minHeight: 0 },
        children: [
          jsx('nav', {
            'aria-label': 'Runs',
            style: { overflowY: 'auto', padding: 8, borderRight: `1px solid ${c.line}` },
            children: runs.map(r => jsx(RunRow, { key: r.run_id, r, active: r.run_id === current }))
          }),
          jsx('main', { style: { overflowY: 'auto', padding: '20px 28px' }, children: jsx(RunDetail, { runId: current }) })
        ]
      })
    ]
  })
}

export default {
  id: 'flightlog',
  name: 'Flightlog',
  register(ctx) {
    rest = ctx.rest
    ctx.registerMany([
      { id: 'page', area: ROUTES_AREA, data: { path: ROUTE }, render: () => jsx(FlightlogPage, {}) },
      { id: 'nav', area: SIDEBAR_NAV_AREA, data: { path: ROUTE, label: 'Flightlog', codicon: 'pulse' } },
      {
        id: 'open',
        area: PALETTE_AREA,
        data: { id: 'flightlog.open', label: 'Flightlog: show runs', keywords: ['flightlog', 'runs', 'flight', 'recorder'], run: () => host.navigate(ROUTE) }
      }
    ])
  }
}
