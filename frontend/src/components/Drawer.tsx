import { useEffect, useState } from 'react'
import { api } from '../api'
import { useStore, type Tab } from '../store'
import { emptySelection, type EntityDetail, type Revision, type ReviewIssue } from '../types'
import { shortIri } from './TermPicker'

type DrawerTab = 'issues' | 'inspector' | 'history'

export function Drawer() {
  const model = useStore((s) => s.model)!
  const inspectId = useStore((s) => s.inspectId)
  const [tab, setTab] = useState<DrawerTab>('issues')
  const open = model.issues.filter((i) => i.resolution_state === 'open')
  const problems = open.filter((i) => i.severity !== 'suggestion').length

  useEffect(() => { if (inspectId) setTab('inspector') }, [inspectId])

  return (
    <section className="drawer">
      <nav className="tabs small">
        <button className={tab === 'issues' ? 'active' : ''} onClick={() => setTab('issues')}>
          Issues {problems > 0 && <span className="badge warn">{problems}</span>}
        </button>
        <button className={tab === 'inspector' ? 'active' : ''} onClick={() => setTab('inspector')}>Inspector</button>
        <button className={tab === 'history' ? 'active' : ''} onClick={() => setTab('history')}>History</button>
      </nav>
      <div className="drawer-body">
        {tab === 'issues' && <IssuesList issues={model.issues} />}
        {tab === 'inspector' && <Inspector />}
        {tab === 'history' && <History />}
      </div>
    </section>
  )
}

function IssuesList({ issues }: { issues: ReviewIssue[] }) {
  const rows = useStore((s) => s.rows)
  const setSelection = useStore((s) => s.setSelection)
  const setTab = useStore((s) => s.setTab)
  const inspect = useStore((s) => s.inspect)
  const projectId = useStore((s) => s.projectId)!
  const reload = useStore((s) => s.reload)
  const [showAll, setShowAll] = useState(false)
  const visible = issues.filter((i) => showAll || (i.resolution_state === 'open' && i.severity !== 'suggestion'))
  const hidden = issues.length - visible.length

  const focus = (i: ReviewIssue) => {
    const ids = i.affected_ids.filter((id) => rows.has(id))
    const rel = ids.filter((id) => rows.get(id)!.kind === 'connection')
    setSelection({ ...emptySelection(), entity_ids: ids.filter((id) => !rel.includes(id)), relationship_ids: rel })
    const kind = ids.length ? rows.get(ids[0])!.kind : null
    if (kind) setTab((kind === 'point' ? 'points' : kind === 'connection' ? 'connections' : 'equipment') as Tab)
    if (ids[0]) inspect(ids[0])
  }

  if (!issues.length) return <p className="muted pad">No issues: the model passes validation against the loaded vocabulary.</p>
  return (
    <div>
      <ul className="issues">
        {visible.map((i) => (
          <li key={i.id} className={`issue sev-${i.severity} ${i.resolution_state}`}>
            <span className={`sev ${i.severity}`}>{i.severity}</span>
            <span className="issue-text" onClick={() => focus(i)} title="Select the affected objects">{i.explanation}</span>
            <button className="link" onClick={() => void api.setIssueState(projectId, i.id,
              i.resolution_state === 'dismissed' ? 'open' : 'dismissed').then(reload)}>
              {i.resolution_state === 'dismissed' ? 'reopen' : 'dismiss'}
            </button>
          </li>
        ))}
      </ul>
      {hidden > 0 && <button className="link pad" onClick={() => setShowAll(!showAll)}>
        {showAll ? 'hide suggestions and dismissed' : `show ${hidden} suggestion(s)/dismissed`}</button>}
    </div>
  )
}

function Inspector() {
  const projectId = useStore((s) => s.projectId)!
  const head = useStore((s) => s.model?.head)
  const inspectId = useStore((s) => s.inspectId)
  const [detail, setDetail] = useState<EntityDetail | null>(null)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    if (!inspectId) { setDetail(null); return }
    setError(null)
    api.entity(projectId, inspectId).then(setDetail).catch((e) => { setDetail(null); setError(String(e.message ?? e)) })
  }, [projectId, inspectId, head])

  if (!inspectId) return <p className="muted pad">Select an object to inspect its ontology terms, RDF, evidence, and history.</p>
  if (error) return <p className="error-text pad">{error}</p>
  if (!detail) return <p className="muted pad">Loading…</p>
  return (
    <div className="inspector">
      <div className="inspector-cols">
        <div>
          <h4>{detail.row?.label ?? detail.id}</h4>
          <dl>
            <dt>Id</dt><dd><code>{detail.id}</code></dd>
            <dt>IRI</dt><dd><code>{detail.iri}</code></dd>
            <dt>Types</dt><dd>{detail.types.map((t) => <div key={t.iri} title={t.iri}>{t.label} <code>{shortIri(t.iri)}</code></div>)}</dd>
            <dt>Set or confirmed by a person</dt><dd>{detail.locked.length ? detail.locked.join(', ') : <span className="muted">—</span>}</dd>
          </dl>
          <h5>Evidence</h5>
          {detail.evidence.length ? detail.evidence.map((e) => (
            <div key={e.id} className="evidence-item"><code>{e.id}</code> {JSON.stringify(e.content)}</div>
          )) : <p className="muted">No linked source observations.</p>}
          <h5>Correction history</h5>
          {detail.history.length ? (
            <ul className="history">{detail.history.map((h, i) => (
              <li key={i}><code>{h.revision_id}</code> {h.origin}: {h.field} {fmtJson(h.before)} → {fmtJson(h.after)}</li>
            ))}</ul>
          ) : <p className="muted">No changes recorded.</p>}
        </div>
        <div>
          <h5>RDF (Turtle)</h5>
          <pre className="code turtle">{detail.turtle}</pre>
        </div>
      </div>
    </div>
  )
}

const fmtJson = (s: string) => { try { const v = JSON.parse(s); return v === null ? '—' : String(v) } catch { return s } }

function History() {
  const projectId = useStore((s) => s.projectId)!
  const head = useStore((s) => s.model?.head)
  const [revs, setRevs] = useState<Revision[]>([])
  useEffect(() => { api.revisions(projectId).then(setRevs) }, [projectId, head])
  return (
    <ul className="revisions">
      {revs.map((r) => (
        <li key={r.id} className={r.id === head ? 'current' : ''}>
          <code>{r.id}</code> <span className={`author ${r.author}`}>{r.author}</span> {r.summary}
          <span className="muted"> · {new Date(r.created_at).toLocaleString()}
            {r.validation ? ` · ${r.validation.violations} problem(s)` : ''}</span>
          {r.id === head && <span className="badge">current</span>}
        </li>
      ))}
    </ul>
  )
}
